#!/usr/bin/env python3
"""GUI bridge (stdlib only). Serves gui/index.html and streams live state to it.
  - worker / job state : tails the server log (no change to server.py needed)
  - network state      : relays the POX statistics stream (TCP 5003)
  - job submission     : acts as a normal client on TCP 5000
Run on the VM:  python3 gui/bridge.py --log /tmp/server.log   then open http://localhost:8080
"""
import argparse, json, os, re, socket, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

lock = threading.Condition()
S = {"algo": "?", "workers": {}, "jobs": {}, "stats": None}
ver = 0
HERE = os.path.dirname(os.path.abspath(__file__))


def touch():
    global ver
    ver += 1
    lock.notify_all()


def job(jid):
    if jid not in S["jobs"]:
        S["jobs"][jid] = {"id": jid, "state": "queued", "worker": None, "t_sub": time.time(),
                          "t_run": None, "t_end": None, "duration": None, "priority": None,
                          "requeues": 0, "src": "other"}
        for old in list(S["jobs"])[:-300]:
            del S["jobs"][old]
    return S["jobs"][jid]


def worker(wid):
    return S["workers"].setdefault(wid, {"id": wid, "state": "idle", "job": None})


def apply(line):
    now = time.time()
    with lock:
        if m := re.search(r"\[SCHEDULER\] Algorithm: (\w+)", line):
            S["algo"] = m[1].lower()
        elif m := re.search(r"\[WORKER\] (\S+) registered", line):
            w = worker(m[1]); w.update(state="idle", job=None)
        elif m := re.search(r"\[WORKER\] (\S+) disconnected|\[WATCHDOG\] (\S+) heartbeat", line):
            worker(m[1] or m[2]).update(state="down", job=None)
        elif m := re.search(r"\[CLIENT\] Queued job (\d+)", line):
            job(int(m[1]))
        elif m := re.search(r"Job (\d+) -> (\S+)", line):
            j = job(int(m[1])); j.update(state="running", worker=m[2], t_run=now)
            worker(m[2]).update(state="busy", job=j["id"])
        elif m := re.search(r"\[SCHEDULER\] Job (\d+) requeued", line):
            j = job(int(m[1])); j.update(state="queued", worker=None, t_run=None, requeues=j["requeues"] + 1)
        elif m := re.search(r"\[RESULT\] Job (\d+) (\w+) by (\S+)", line):
            j = job(int(m[1])); j.update(state="done" if m[2] == "completed" else "failed", worker=m[3], t_end=now)
            worker(m[3]).update(state="idle", job=None)
        else:
            return
        touch()


def tail(path):
    pos = 0
    while True:
        try:
            if os.path.getsize(path) < pos:          # server restarted: start over
                pos = 0
                with lock:
                    S["workers"].clear(); S["jobs"].clear(); touch()
            with open(path, "r", errors="replace") as f:
                f.seek(pos)
                for line in f:
                    if line.endswith("\n"):
                        pos += len(line.encode(errors="replace")); apply(line)
                    else:
                        break
        except OSError:
            pass
        time.sleep(0.1)


def relay_stats(host, port):
    while True:
        try:
            with socket.create_connection((host, port), timeout=5) as s:
                s.settimeout(None)
                for line in s.makefile("r"):
                    with lock:
                        S["stats"] = json.loads(line); touch()
        except (OSError, ValueError):
            pass
        with lock:
            S["stats"] = None; touch()
        time.sleep(3)


class Submitter:
    def __init__(self, host, port):
        self.addr, self.sock, self.mu = (host, port), None, threading.Lock()
        self.next_id = int(time.time() * 1000) % 1_900_000_000 + 1

    def _connect(self):
        self.sock = socket.create_connection(self.addr, timeout=5)
        self.sock.settimeout(None)
        threading.Thread(target=self._recv, args=(self.sock,), daemon=True).start()

    def _recv(self, sock):
        try:
            for line in sock.makefile("r"):
                m = json.loads(line)
                if m.get("type") == "ERROR" and m.get("job_id") in S["jobs"]:
                    with lock:
                        S["jobs"][m["job_id"]].update(state="failed", t_end=time.time(), error=m.get("code")); touch()
        except (OSError, ValueError):
            pass

    def send(self, count, gap, duration, priority, payload):
        for i in range(count):
            with self.mu:
                jid, self.next_id = self.next_id, self.next_id + 1
                with lock:
                    job(jid).update(src="gui", duration=duration, priority=priority); touch()
                msg = {"type": "JOB", "job_id": jid, "payload_size": payload, "duration": duration, "priority": priority}
                for attempt in (1, 2):
                    try:
                        if not self.sock:
                            self._connect()
                        self.sock.sendall((json.dumps(msg) + "\n").encode()); break
                    except OSError:
                        self.sock = None
                        if attempt == 2:
                            with lock:
                                S["jobs"][jid].update(state="failed", error="NO_SERVER", t_end=time.time()); touch()
            if i < count - 1:
                time.sleep(gap)


def snapshot():
    return json.dumps({"now": time.time(), "algo": S["algo"], "stats": S["stats"],
                       "workers": sorted(S["workers"].values(), key=lambda w: w["id"]),
                       "jobs": list(S["jobs"].values())[-200:]})


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        if self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream"); self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            seen = -1
            try:
                while True:
                    with lock:
                        if ver == seen:
                            lock.wait(1.0)
                        seen = ver; data = snapshot()
                    self.wfile.write(f"data: {data}\n\n".encode()); self.wfile.flush()
            except OSError:
                return
        body = open(os.path.join(HERE, "index.html"), "rb").read()
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        try:
            p = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            a = (min(max(int(p.get("count", 1)), 1), 200), min(max(float(p.get("gap", 0.5)), 0), 30),
                 min(max(float(p.get("duration", 2)), 0.1), 60), min(max(int(p.get("priority", 0)), 0), 10),
                 min(max(int(p.get("payload", 1000)), 0), 1_000_000))
            threading.Thread(target=SUB.send, args=a, daemon=True).start()
            out, code = {"ok": True}, 200
        except (ValueError, TypeError):
            out, code = {"ok": False, "error": "bad request"}, 400
        body = json.dumps(out).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default="/tmp/server.log", help="server stdout log (default /tmp/server.log)")
    ap.add_argument("--server", default="10.0.0.10"); ap.add_argument("--client-port", type=int, default=5000)
    ap.add_argument("--stats-host", default="127.0.0.1"); ap.add_argument("--stats-port", type=int, default=5003)
    ap.add_argument("--host", default="127.0.0.1"); ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()
    SUB = Submitter(a.server, a.client_port)
    threading.Thread(target=tail, args=(a.log,), daemon=True).start()
    threading.Thread(target=relay_stats, args=(a.stats_host, a.stats_port), daemon=True).start()
    print(f"GUI on http://{a.host}:{a.port}  (log {a.log}, server {a.server}:{a.client_port})")
    ThreadingHTTPServer((a.host, a.port), H).serve_forever()
