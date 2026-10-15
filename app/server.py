
#!/usr/bin/env python3
"""P2: TCP job server, job queue, round-robin scheduling and POX status."""

import json
import queue
import socket
import threading
import time

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


def send_json(sock, message, send_lock=None):
    data = (json.dumps(message) + "\n").encode()
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
    reader = sock.makefile("r", encoding="utf-8")

    try:
        for line in reader:
            message = json.loads(line)
            kind = message.get("type")

            if kind == "REGISTER":
                worker_id = str(message["worker_id"])
                register_worker(worker_id, sock, send_lock)

            elif kind == "HEARTBEAT" and worker_id:
                with lock:
                    if workers.get(worker_id, {}).get("socket") is sock:
                        workers[worker_id]["last_seen"] = time.monotonic()

            elif kind == "RESULT" and worker_id:
                job_id = message.get("job_id")

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

    except (OSError, ValueError, KeyError) as exc:
        print(f"[WORKER] {address}: {exc}")

    finally:
        with lock:
            state = workers.get(worker_id)
            if state and state["socket"] is sock:
                workers.pop(worker_id, None)
                report_status(f"WORKER_DOWN {worker_id}")
                print(f"[WORKER] {worker_id} disconnected")

        reader.close()
        sock.close()


def handle_client(sock, address):
    reader = sock.makefile("r", encoding="utf-8")

    try:
        for line in reader:
            try:
                message = json.loads(line)
                if message.get("type") != "JOB":
                    raise ValueError("Expected a JOB message")

                job_id = message.get("job_id", int(time.time() * 1000))
                job = {
                    "type": "JOB",
                    "job_id": job_id,
                    "payload_size": max(
                        1, min(int(message.get("payload_size", 1000)), 1_000_000)
                    ),
                    "duration": max(
                        0.1, min(float(message.get("duration", 2)), 30)
                    ),
                }

                with lock:
                    pending_clients[job_id] = sock

                jobs.put(job)
                send_json(sock, {
                    "type": "ACK",
                    "job_id": job_id,
                    "status": "queued",
                })
                print(f"[CLIENT] Queued job {job_id} from {address}")

            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                send_json(sock, {"type": "ERROR", "message": str(exc)})

    except OSError as exc:
        print(f"[CLIENT] {address}: {exc}")
    finally:
        reader.close()
        # Keep the socket open while its job is pending.
        # The result handler closes it after delivery, or on disconnect.


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
