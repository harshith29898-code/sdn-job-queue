"""Minimal protocol-compliant reference server used to validate the test suite.
P2's real server.py replaces this; tests run against either."""
import socket, threading, time, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import *

class Server:
    def __init__(self, host="127.0.0.1", cport=CLIENT_PORT, wport=WORKER_PORT):
        self.lock = threading.Lock()
        self.workers = {}   # id -> dict(sock, last, up, jobs)
        self.order = []
        self.rr = 0
        self.clients = {}   # job_id -> sock
        self.seen = set()
        self.queue = []
        for port, h in ((cport, self.handle_client), (wport, self.handle_worker)):
            s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port)); s.listen(16)
            threading.Thread(target=self.accept, args=(s, h), daemon=True).start()
        threading.Thread(target=self.monitor, daemon=True).start()

    def accept(self, s, h):
        while True:
            c, _ = s.accept(); threading.Thread(target=h, args=(c,), daemon=True).start()

    def send(self, sock, data):
        try: sock.sendall(data)
        except OSError: pass

    def handle_client(self, c):
        r = LineReader(c)
        while (line := r.readline()) is not None:
            try:
                m = decode(line)
                if m["type"] != "JOB": raise ProtocolError("UNKNOWN_TYPE", "clients send JOB only")
            except ProtocolError as e:
                self.send(c, make_error(e.code, e.message)); continue
            with self.lock:
                if m["job_id"] in self.seen:
                    self.send(c, make_error("DUPLICATE_JOB", "", m["job_id"])); continue
                if not any(w["up"] for w in self.workers.values()):
                    self.send(c, make_error("NO_WORKERS", "", m["job_id"])); continue
                self.seen.add(m["job_id"]); self.clients[m["job_id"]] = c
                self.queue.append(m)
            self.send(c, encode({"type": "ACK", "ref": "JOB", "job_id": m["job_id"]}))
            self.dispatch()

    def dispatch(self):
        with self.lock:
            while self.queue:
                ups = [w for w in self.order if self.workers[w]["up"]]
                if not ups: return
                wid = ups[self.rr % len(ups)]; self.rr += 1
                job = self.queue.pop(0)
                self.workers[wid]["jobs"][job["job_id"]] = job
                self.send(self.workers[wid]["sock"], encode(job))

    def handle_worker(self, c):
        r = LineReader(c); wid = None
        while (line := r.readline()) is not None:
            try: m = decode(line)
            except ProtocolError as e:
                self.send(c, make_error(e.code, e.message)); continue
            if m["type"] == "REGISTER":
                wid = m["worker_id"]
                with self.lock:
                    if wid not in self.order: self.order.append(wid); self.order.sort()
                    self.workers[wid] = {"sock": c, "last": time.time(), "up": True, "jobs": {}}
                self.send(c, encode({"type": "ACK", "ref": "REGISTER", "worker_id": wid}))
            elif m["type"] == "HEARTBEAT" and wid:
                with self.lock: self.workers[wid]["last"] = time.time()
            elif m["type"] == "RESULT" and wid:
                with self.lock:
                    self.workers[wid]["jobs"].pop(m["job_id"], None)
                    cl = self.clients.get(m["job_id"])
                if cl: self.send(cl, encode(m))
        if wid: self.mark_down(wid, c)

    def mark_down(self, wid, sock=None):
        with self.lock:
            w = self.workers.get(wid)
            if not w or not w["up"] or (sock and w["sock"] is not sock): return
            w["up"] = False
            self.queue = list(w["jobs"].values()) + self.queue; w["jobs"] = {}
        self.dispatch()

    def monitor(self):
        while True:
            time.sleep(0.5)
            now = time.time()
            for wid, w in list(self.workers.items()):
                if w["up"] and now - w["last"] > HEARTBEAT_TIMEOUT: self.mark_down(wid)

if __name__ == "__main__":
    Server("0.0.0.0"); time.sleep(10**6)
