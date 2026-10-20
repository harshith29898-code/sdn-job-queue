#!/usr/bin/env python3
"""P2: TCP job server, job queue, round-robin scheduling and POX status."""

import os
import sys
import queue
import socket
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import decode, make_error, ProtocolError, LineReader

CLIENT_PORT = 5000
WORKER_PORT = 5001
POX_HOST = "10.0.0.254"
POX_PORT = 5002
HEARTBEAT_TIMEOUT = 6

jobs = queue.Queue()
lock = threading.RLock()
workers = {}
pending_clients = {}
worker_order = []
next_worker = 0
seen_job_ids = set()


def send_json(sock, message, send_lock=None):
    import json
    data = (json.dumps(message) + "\n").encode()
    if send_lock:
        with send_lock:
            sock.sendall(data)
    else:
        sock.sendall(data)


def send_error(sock, code, message="", job_id=None, send_lock=None):
    data = make_error(code, message, job_id)
    if send_lock:
        with send_lock:
            sock.sendall(data)
    else:
        sock.sendall(data)


def report_status(message):
    """POX expects plain-text lines, NOT JSON."""
    try:
        with socket.create_connection((POX_HOST, POX_PORT), timeout=2) as conn:
            conn.sendall((message + "\n").encode())
    except OSError as exc:
        print(f"[POX] Status report failed: {exc}")


def register_worker(worker_id, sock, send_lock):
    with lock:
        old = workers.get(worker_id)
        if old:
            try:
                old["socket"].close()
            except OSError:
                pass
        else:
            worker_order.append(worker_id)

        workers[worker_id] = {
            "socket": sock,
            "send_lock": send_lock,
            "last_seen": time.monotonic(),
            "busy": False,
        }

    print(f"[WORKER] {worker_id} registered")
    report_status(f"WORKER_UP {worker_id}")


def handle_worker(sock, address):
    worker_id = None
    send_lock = threading.Lock()
    reader = LineReader(sock)

    try:
        while True:
            line = reader.readline()
            if line is None:
                break

            try:
                message = decode(line)
            except ProtocolError as exc:
                try:
                    send_error(sock, exc.code, exc.message, send_lock=send_lock)
                except OSError:
                    break
                continue

            kind = message["type"]

            if kind == "REGISTER":
                if worker_id is not None:
                    send_error(
                        sock, "BAD_VALUE", "Worker already registered",
                        send_lock=send_lock
                    )
                    continue

                worker_id = message["worker_id"]

                # Send the ACK BEFORE making this worker schedulable.
                try:
                    send_json(sock, {
                        "type": "ACK",
                        "ref": "REGISTER",
                        "worker_id": worker_id,
                    }, send_lock)
                except OSError:
                    break

                register_worker(worker_id, sock, send_lock)

            elif kind == "HEARTBEAT":
                if worker_id is None or message["worker_id"] != worker_id:
                    send_error(
                        sock, "BAD_VALUE", "Worker is not registered or ID mismatched",
                        send_lock=send_lock
                    )
                    continue

                with lock:
                    if workers.get(worker_id, {}).get("socket") is sock:
                        workers[worker_id]["last_seen"] = time.monotonic()

            elif kind == "RESULT":
                if worker_id is None or message["worker_id"] != worker_id:
                    send_error(
                        sock, "BAD_VALUE", "Worker is not registered or ID mismatched",
                        send_lock=send_lock
                    )
                    continue

                job_id = message["job_id"]
                client = None

                with lock:
                    state = workers.get(worker_id)
                    if state and state["socket"] is sock:
                        state["busy"] = False
                        client = pending_clients.pop(job_id, None)

                report_status(f"LOAD {worker_id} 0")

                if client:
                    try:
                        send_json(client, message)
                    except OSError:
                        pass

                print(f"[RESULT] Job {job_id} completed by {worker_id}")

            else:
                send_error(
                    sock, "BAD_VALUE",
                    f"Unexpected message type for worker connection: {kind}",
                    send_lock=send_lock
                )

    except OSError as exc:
        print(f"[WORKER] {address}: {exc}")

    finally:
        with lock:
            state = workers.get(worker_id)
            if state and state["socket"] is sock:
                workers.pop(worker_id, None)
                report_status(f"WORKER_DOWN {worker_id}")
                print(f"[WORKER] {worker_id} disconnected")

        sock.close()


def handle_client(sock, address):
    reader = LineReader(sock)

    try:
        while True:
            line = reader.readline()
            if line is None:
                break

            try:
                message = decode(line)
            except ProtocolError as exc:
                send_error(sock, exc.code, exc.message)
                continue

            if message["type"] != "JOB":
                send_error(
                    sock, "BAD_VALUE",
                    f"Expected JOB message, got {message['type']}"
                )
                continue

            job_id = message["job_id"]
            job = {
                "type": "JOB",
                "job_id": job_id,
                "payload_size": message["payload_size"],
                "duration": message["duration"],
            }

            # Reserve the ID atomically so simultaneous duplicates are rejected.
            with lock:
                if job_id in seen_job_ids:
                    duplicate = True
                else:
                    duplicate = False
                    seen_job_ids.add(job_id)
                    pending_clients[job_id] = sock

            if duplicate:
                send_error(
                    sock, "DUPLICATE_JOB",
                    f"Job ID {job_id} has already been submitted",
                    job_id=job_id
                )
                continue

            try:
                send_json(sock, {
                    "type": "ACK",
                    "ref": "JOB",
                    "job_id": job_id,
                })
            except OSError:
                with lock:
                    pending_clients.pop(job_id, None)
                    seen_job_ids.discard(job_id)
                break

            jobs.put(job)
            print(f"[CLIENT] Queued job {job_id} from {address}")

    except OSError as exc:
        print(f"[CLIENT] {address}: {exc}")
    finally:
        # The socket stays open so the result handler can deliver the result.
        pass


def choose_worker():
    global next_worker

    with lock:
        available = [
            wid for wid in worker_order
            if wid in workers
            and not workers[wid]["busy"]
            and time.monotonic() - workers[wid]["last_seen"]
                <= HEARTBEAT_TIMEOUT
        ]

        if not available:
            return None

        worker_id = available[next_worker % len(available)]
        next_worker += 1
        workers[worker_id]["busy"] = True
        return worker_id, workers[worker_id]


def scheduler():
    while True:
        job = jobs.get()

        while True:
            selected = choose_worker()

            if selected is None:
                time.sleep(0.5)
                continue

            worker_id, state = selected

            try:
                send_json(state["socket"], job, state["send_lock"])
                report_status(f"LOAD {worker_id} 75")
                print(f"[SCHEDULER] Job {job['job_id']} -> {worker_id}")
                break

            except OSError:
                with lock:
                    if workers.get(worker_id, {}).get("socket") is state["socket"]:
                        workers.pop(worker_id, None)
                    state["busy"] = False

                report_status(f"WORKER_DOWN {worker_id}")
                jobs.put(job)
                break

        jobs.task_done()


def watchdog():
    while True:
        time.sleep(1)
        now = time.monotonic()

        with lock:
            stale = [
                (wid, state["socket"])
                for wid, state in workers.items()
                if now - state["last_seen"] > HEARTBEAT_TIMEOUT
            ]

            for wid, sock in stale:
                workers.pop(wid, None)
                print(f"[WATCHDOG] {wid} heartbeat timed out")
                report_status(f"WORKER_DOWN {wid}")
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


def listen(port, handler, name):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen()
    print(f"[SERVER] {name} listening on port {port}")

    while True:
        conn, address = server.accept()
        threading.Thread(
            target=handler, args=(conn, address), daemon=True
        ).start()


if __name__ == "__main__":
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(
        target=listen, args=(CLIENT_PORT, handle_client, "Clients"),
        daemon=True
    ).start()
    listen(WORKER_PORT, handle_worker, "Workers")
