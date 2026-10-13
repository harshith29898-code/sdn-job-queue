
#!/usr/bin/env python3
"""P2: Submit a job and wait for its result."""

import argparse
import json
import socket
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="10.0.0.10")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--payload-size", type=int, default=1000)
    parser.add_argument("--duration", type=float, default=2)
    args = parser.parse_args()

    job_id = int(time.time() * 1000) % 2_000_000_000
    job = {
        "type": "JOB",
        "job_id": job_id,
        "payload_size": args.payload_size,
        "duration": args.duration,
    }

    with socket.create_connection((args.server, args.port), timeout=10) as sock:
        sock.settimeout(None)
        reader = sock.makefile("r", encoding="utf-8")

        sock.sendall((json.dumps(job) + "\n").encode())

        while True:
            line = reader.readline()
            if not line:
                print("Server closed the connection before returning a result.")
                break

            message = json.loads(line)
            print("Server:", json.dumps(message, indent=2))

            if message.get("type") == "RESULT":
                break
            if message.get("type") == "ERROR":
                break

        reader.close()


if __name__ == "__main__":
    main()
