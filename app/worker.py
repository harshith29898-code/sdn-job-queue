#!/usr/bin/env python3
"""P2: Worker registration, heartbeats, job execution and results."""

import argparse
import hashlib
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import (  # noqa: E402
    SERVER_IP, WORKER_PORT, HEARTBEAT_INTERVAL, MAX_PAYLOAD, MAX_DURATION,
)


def send_message(sock, lock, message):
    with lock:
        sock.sendall((json.dumps(message) + "\n").encode())


def execute_job(job):
    payload_size = max(1, min(int(job.get("payload_size", 1000)), MAX_PAYLOAD))
    duration = max(0.1, min(float(job.get("duration", 2)), MAX_DURATION))
    payload = b"x" * payload_size
    digest = hashlib.sha256(payload).digest()
    deadline = time.monotonic() + duration
    iterations = 0

    while time.monotonic() < deadline:
        digest = hashlib.sha256(digest + payload).digest()
        iterations += 1

    return {
        "status": "completed",
        "iterations": iterations,
        "result_digest": digest.hex(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True, help="Worker ID, e.g. w1")
    parser.add_argument("--server", default=SERVER_IP)
    parser.add_argument("--port", type=int, default=WORKER_PORT)
    args = parser.parse_args()

    while True:
        try:
            with socket.create_connection(
                (args.server, args.port), timeout=10
            ) as sock:
                sock.settimeout(None)
                reader = sock.makefile("r", encoding="utf-8")
                send_lock = threading.Lock()
                stop = threading.Event()
                busy = threading.Event()

                send_message(sock, send_lock, {
                    "type": "REGISTER",
                    "worker_id": args.id,
                })
                # Wait for the server to acknowledge registration first.
                line = reader.readline()
                if not line:
                    raise ConnectionError("Server closed before registration ACK")

                response = json.loads(line)
                if (
                    response.get("type") == "ERROR"
                    or response.get("type") != "ACK"
                    or response.get("ref") != "REGISTER"
                    or response.get("worker_id") != args.id
                ):
                    raise ConnectionError(
                        f"Registration was not acknowledged: {response}"
                    )

                print(f"[{args.id}] Registration acknowledged by server")

                def heartbeat():
                    while not stop.wait(HEARTBEAT_INTERVAL):
                        try:
                            send_message(sock, send_lock, {
                                "type": "HEARTBEAT",
                                "worker_id": args.id,
                                "load": 1 if busy.is_set() else 0,
                            })
                        except OSError:
                            return

                threading.Thread(target=heartbeat, daemon=True).start()

                for line in reader:
                    job = json.loads(line)
                    if job.get("type") != "JOB":
                        continue

                    job_id = job.get("job_id")
                    print(f"[{args.id}] Executing job {job_id}")

                    busy.set()
                    try:
                        result = execute_job(job)
                    except Exception as exc:
                        result = {"status": "failed", "error": str(exc)}
                    finally:
                        busy.clear()

                    send_message(sock, send_lock, {
                        "type": "RESULT",
                        "job_id": job_id,
                        "worker_id": args.id,
                        **result,
                    })

                    print(f"[{args.id}] Sent result for job {job_id}")

                stop.set()
                reader.close()

        except (OSError, ValueError) as exc:
            print(f"[{args.id}] Connection error: {exc}; retrying in 2 seconds")
            time.sleep(2)


if __name__ == "__main__":
    main()
