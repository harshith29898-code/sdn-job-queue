#!/usr/bin/env python3
"""Simulated worker for application-level experiments.

It speaks exactly the same protocol as app/worker.py (REGISTER, HEARTBEAT every
2 s, JOB in, RESULT out, one job at a time) but "does the work" by waiting for
the job's duration instead of burning CPU. That makes timings repeatable on a
machine with few CPU cores, where three real workers would fight for one core
and stretch every job.

    python3 experiments/sim_worker.py --id=w1 --server 127.0.0.1 --port 5001
"""
import argparse
import json
import socket
import threading
import time


def main():
    p = argparse.ArgumentParser(description="Simulated job worker (sleeps instead of computing).")
    p.add_argument("--id", required=True, help="worker id, for example w1")
    p.add_argument("--server", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5001)
    args = p.parse_args()

    sock = socket.create_connection((args.server, args.port), timeout=10)
    sock.settimeout(None)
    send_lock = threading.Lock()
    busy = threading.Event()

    def send(message):
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        with send_lock:
            sock.sendall(data)

    send({"type": "REGISTER", "worker_id": args.id})

    def heartbeat():
        while True:
            try:
                send({"type": "HEARTBEAT", "worker_id": args.id,
                      "load": 1 if busy.is_set() else 0})
            except OSError:
                return
            time.sleep(2)

    threading.Thread(target=heartbeat, daemon=True).start()

    for raw in sock.makefile("rb"):
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        if message.get("type") != "JOB":
            continue                      # the REGISTER ACK and anything else
        busy.set()
        time.sleep(float(message.get("duration", 1)))
        busy.clear()
        send({"type": "RESULT", "job_id": message["job_id"],
              "worker_id": args.id, "status": "completed"})


if __name__ == "__main__":
    main()
