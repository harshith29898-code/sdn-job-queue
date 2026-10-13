
#!/usr/bin/env python3
"""P2: Worker registration, heartbeats, job execution and results."""

import argparse
import hashlib
import json
import socket
import threading
import time


def send_message(sock, lock, message):
    with lock:
        sock.sendall((json.dumps(message) + "\n").encode())


def execute_job(job):
    payload_size = max(1, min(int(job.get("payload_size", 1000)), 1_000_000))
    duration = max(0.1, min(float(job.get("duration", 2)), 30))
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
    parser.add_argument("--server", default="10.0.0.10")
    parser.add_argument("--port", type=int, default=5001)
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

                send_message(sock, send_lock, {
                    "type": "REGISTER",
                    "worker_id": args.id,
                })
                print(f"[{args.id}] Registered with server")

                def heartbeat():
                    while not stop.wait(2):
                        try:
                            send_message(sock, send_lock, {
                                "type": "HEARTBEAT",
                                "worker_id": args.id,
                                "timestamp": time.time(),
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

                    try:
                        result = execute_job(job)
                    except Exception as exc:
                        result = {"status": "failed", "error": str(exc)}

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
