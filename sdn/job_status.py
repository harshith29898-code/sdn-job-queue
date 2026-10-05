"""
job_status.py - POX component: server -> controller status channel (Delivery 1).

Listens on a TCP port (default 5002) for plain-text, one-message-per-line status
updates from the job server, as in the Project Common Reference:

    WORKER_UP w1        worker w1 registered / is alive
    WORKER_DOWN w1      worker w1 was marked down (missed heartbeats)
    LOAD w1 75          worker w1 reports a load figure (0-100)

The component logs every message and keeps the current worker table in
core.JobStatus.workers, so other POX components (for example the D2 rerouting
logic) can read it.

The server may keep one connection open and send many lines, or open a new
connection per message. Both work. No reply is sent.

Run (see sdn/README.md):
    cd ~/pox
    PYTHONPATH=~/sdn-job-queue/sdn python3 pox.py forwarding.l2_learning job_status
"""
import socket
import threading

from pox.core import core

log = core.getLogger()


class JobStatus(object):
    def __init__(self, host, port):
        self.host = host
        self.port = port
        # worker_id -> {"state": "UP" or "DOWN", "load": int or None}
        self.workers = {}
        self._lock = threading.Lock()

        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((host, port))
        self._server.listen(5)

        thread = threading.Thread(target=self._accept_loop, daemon=True)
        thread.start()
        log.info("Status channel listening on %s:%d", host, port)

    # ---- networking (runs in background threads) ----

    def _accept_loop(self):
        while True:
            try:
                conn, addr = self._server.accept()
            except OSError:
                return  # socket closed, POX is shutting down
            thread = threading.Thread(target=self._client_loop, args=(conn, addr), daemon=True)
            thread.start()

    def _client_loop(self, conn, addr):
        peer = "%s:%d" % addr
        log.info("Status connection from %s", peer)
        try:
            with conn, conn.makefile("r") as lines:
                for line in lines:
                    self.handle_message(line, peer)
        except OSError as e:
            log.warning("Status connection from %s dropped: %s", peer, e)
        log.info("Status connection from %s closed", peer)

    # ---- message handling ----

    def handle_message(self, line, peer="?"):
        parts = line.strip().split()
        if not parts:
            return
        cmd = parts[0].upper()

        if cmd == "WORKER_UP" and len(parts) == 2:
            self._set(parts[1], state="UP")
            log.info("Worker %s is UP", parts[1])
        elif cmd == "WORKER_DOWN" and len(parts) == 2:
            self._set(parts[1], state="DOWN")
            log.warning("Worker %s is DOWN", parts[1])
        elif cmd == "LOAD" and len(parts) == 3 and parts[2].isdigit():
            self._set(parts[1], load=int(parts[2]))
            log.info("Worker %s load = %s", parts[1], parts[2])
        else:
            log.warning("Unrecognized status message from %s: %r", peer, line.strip())

    def _set(self, worker_id, state=None, load=None):
        with self._lock:
            entry = self.workers.setdefault(worker_id, {"state": "UP", "load": None})
            if state is not None:
                entry["state"] = state
            if load is not None:
                entry["load"] = load


def launch(host="0.0.0.0", port=5002):
    """POX entry point. Options: --host=0.0.0.0 --port=5002"""
    core.registerNew(JobStatus, host, int(port))
