#!/usr/bin/env python3
"""P2: TCP job server with selectable scheduling and POX status."""

import os
import sys
import argparse
import math
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

# Scheduling configuration
scheduler_algorithm = "fcfs"
AGING_RATE = 1.0

# Shared state
pending_jobs = []
lock = threading.RLock()
condition = threading.Condition(lock)

workers = {}
pending_clients = {}
worker_order = []
next_worker = 0
seen_job_ids = set()
job_sequence = 0


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
    """Send a plain-text status update to the POX controller."""
    try:
        with socket.create_connection((POX_HOST, POX_PORT), timeout=2) as conn:
            conn.sendall((message + "\n").encode())
    except OSError as exc:
        print(f"[POX] Status report failed: {exc}")


def load_percentage(worker_load):
    """Convert the worker's binary load (0 or 1) to a POX percentage."""
    return min(worker_load * 100, 100)


def requeue_in_flight(state):
    """Requeue an unfinished job after worker failure."""
    job = state.get("job")

    if job is not None:
        state["job"] = None
        state["busy"] = False
        state["load"] = 0
        pending_jobs.append(job)
        print(f"[SCHEDULER] Job {job['job_id']} requeued")
        condition.notify_all()


def register_worker(worker_id, sock, send_lock):
    with condition:
        old = workers.get(worker_id)

        if old:
            requeue_in_flight(old)

            try:
                old["socket"].close()
            except OSError:
                pass

        if worker_id not in worker_order:
            worker_order.append(worker_id)

        workers[worker_id] = {
            "socket": sock,
            "send_lock": send_lock,
            "last_seen": time.monotonic(),
            "busy": False,
            "load": 0,
            "job": None,
        }

        condition.notify_all()

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
                    send_error(
                        sock, exc.code, exc.message,
                        send_lock=send_lock
                    )
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
                        sock, "BAD_VALUE",
                        "Worker is not registered or ID mismatched",
                        send_lock=send_lock
                    )
                    continue

                worker_load = message["load"]
                should_report = False

                with condition:
                    state = workers.get(worker_id)

                    if state and state["socket"] is sock:
                        state["last_seen"] = time.monotonic()
                        state["load"] = worker_load
                        should_report = True

                # Keep scheduler-owned busy state separate from heartbeat load.
                if should_report:
                    report_status(
                        f"LOAD {worker_id} {load_percentage(worker_load)}"
                    )

            elif kind == "RESULT":
                if worker_id is None or message["worker_id"] != worker_id:
                    send_error(
                        sock, "BAD_VALUE",
                        "Worker is not registered or ID mismatched",
                        send_lock=send_lock
                    )
                    continue

                job_id = message["job_id"]
                client = None
                accepted_result = False

                with condition:
                    state = workers.get(worker_id)

                    if state and state["socket"] is sock:
                        assigned_job = state.get("job")

                        if assigned_job and assigned_job["job_id"] == job_id:
                            state["busy"] = False
                            state["load"] = 0
                            state["job"] = None
                            client = pending_clients.pop(job_id, None)
                            accepted_result = True
                            condition.notify_all()

                if not accepted_result:
                    send_error(
                        sock, "BAD_VALUE",
                        f"Job {job_id} was not assigned to this worker",
                        job_id=job_id,
                        send_lock=send_lock
                    )
                    continue

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
                    f"Unexpected message type: {kind}",
                    send_lock=send_lock
                )

    except OSError as exc:
        print(f"[WORKER] {address}: {exc}")

    finally:
        report_down = False

        with condition:
            state = workers.get(worker_id)

            if state and state["socket"] is sock:
                workers.pop(worker_id, None)
                requeue_in_flight(state)
                condition.notify_all()
                report_down = True

        if report_down:
            report_status(f"WORKER_DOWN {worker_id}")
            print(f"[WORKER] {worker_id} disconnected")

        try:
            sock.close()
        except OSError:
            pass


def handle_client(sock, address):
    global job_sequence

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
                "priority": message.get("priority", 0),
            }

            with condition:
                if job_id in seen_job_ids:
                    duplicate = True
                else:
                    duplicate = False
                    seen_job_ids.add(job_id)
                    pending_clients[job_id] = sock

                    job_sequence += 1
                    job["_sequence"] = job_sequence
                    job["_arrival_time"] = time.monotonic()

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
                with condition:
                    pending_clients.pop(job_id, None)
                    seen_job_ids.discard(job_id)
                break

            with condition:
                pending_jobs.append(job)
                condition.notify_all()

            print(f"[CLIENT] Queued job {job_id} from {address}")

    except OSError as exc:
        print(f"[CLIENT] {address}: {exc}")


def choose_worker():
    """Round-robin selection among registered, idle workers."""
    global next_worker

    with lock:
        count = len(worker_order)
        now = time.monotonic()

        if count == 0:
            return None

        for offset in range(count):
            index = (next_worker + offset) % count
            worker_id = worker_order[index]
            state = workers.get(worker_id)

            if (
                state
                and not state["busy"]
                and now - state["last_seen"] <= HEARTBEAT_TIMEOUT
            ):
                next_worker = (index + 1) % count
                state["busy"] = True
                return worker_id, state

        return None


def choose_job():
    """Select a queued job according to the configured algorithm."""
    if not pending_jobs:
        return None

    now = time.monotonic()

    if scheduler_algorithm == "fcfs":
        index = min(
            range(len(pending_jobs)),
            key=lambda i: pending_jobs[i]["_sequence"]
        )

    elif scheduler_algorithm == "sjf":
        index = min(
            range(len(pending_jobs)),
            key=lambda i: (
                pending_jobs[i]["duration"],
                pending_jobs[i]["_sequence"]
            )
        )

    elif scheduler_algorithm == "priority":
        index = max(
            range(len(pending_jobs)),
            key=lambda i: (
                pending_jobs[i]["priority"]
                + (now - pending_jobs[i]["_arrival_time"]) * AGING_RATE,
                -pending_jobs[i]["_sequence"]
            )
        )

    else:
        raise ValueError(
            f"Unknown scheduling algorithm: {scheduler_algorithm}"
        )

    return pending_jobs.pop(index)


def scheduler():
    while True:
        with condition:
            while not pending_jobs or not any(
                not state["busy"]
                and time.monotonic() - state["last_seen"] <= HEARTBEAT_TIMEOUT
                for state in workers.values()
            ):
                condition.wait(timeout=0.5)

            job = choose_job()
            selected = choose_worker()

            if job is None or selected is None:
                if job is not None:
                    pending_jobs.append(job)
                continue

            worker_id, state = selected
            state["job"] = job
            state["load"] = 1

            # Record the dispatch time immediately before sending the job.
            dispatch_timestamp = time.time()
            job["_dispatch_timestamp"] = dispatch_timestamp

        # Do not send internal queue metadata to the worker.
        worker_job = {
            key: value for key, value in job.items()
            if not key.startswith("_")
        }

        # Include the dispatch timestamp in the worker's job message.
        worker_job["dispatch_timestamp"] = dispatch_timestamp

        try:
            send_json(state["socket"], worker_job, state["send_lock"])

            report_status(
                f"LOAD {worker_id} {load_percentage(state['load'])}"
            )

            print(
                f"[SCHEDULER] {scheduler_algorithm.upper()}: "
                f"Job {job['job_id']} -> {worker_id} "
                f"(dispatched at {dispatch_timestamp:.6f})"
            )

        except OSError:
            report_down = False

            with condition:
                still_ours = state["job"] is job

                state["job"] = None
                state["busy"] = False
                state["load"] = 0

                current = workers.get(worker_id)

                if current and current["socket"] is state["socket"]:
                    workers.pop(worker_id, None)
                    report_down = True

                if still_ours:
                    pending_jobs.append(job)

                condition.notify_all()

            if report_down:
                report_status(f"WORKER_DOWN {worker_id}")


def watchdog():
    while True:
        time.sleep(1)
        now = time.monotonic()
        timed_out = []

        with condition:
            stale = [
                (worker_id, state["socket"])
                for worker_id, state in workers.items()
                if now - state["last_seen"] > HEARTBEAT_TIMEOUT
            ]

            for worker_id, sock in stale:
                state = workers.pop(worker_id, None)

                if state:
                    requeue_in_flight(state)
                    timed_out.append((worker_id, sock))

            condition.notify_all()

        # Network operations happen after releasing the shared lock.
        for worker_id, sock in timed_out:
            print(f"[WATCHDOG] {worker_id} heartbeat timed out")
            report_status(f"WORKER_DOWN {worker_id}")

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
            target=handler,
            args=(conn, address),
            daemon=True
        ).start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SDN Job Queue Server")

    parser.add_argument(
        "--scheduler",
        "--algorithm",
        dest="algorithm",
        choices=["fcfs", "sjf", "priority"],
        default="fcfs",
        help="Job scheduling algorithm (also accepts --algorithm)"
    )

    parser.add_argument(
        "--aging-rate",
        type=float,
        default=AGING_RATE,
        help="Priority points gained per second while waiting"
    )

    args = parser.parse_args()

    if not math.isfinite(args.aging_rate) or args.aging_rate < 0:
        parser.error("--aging-rate must be a finite, non-negative number")

    scheduler_algorithm = args.algorithm
    AGING_RATE = args.aging_rate

    print(f"[SCHEDULER] Algorithm: {scheduler_algorithm.upper()}")
    print(f"[SCHEDULER] Aging rate: {AGING_RATE} priority points/second")

    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()

    threading.Thread(
        target=listen,
        args=(CLIENT_PORT, handle_client, "Clients"),
        daemon=True
    ).start()

    listen(WORKER_PORT, handle_worker, "Workers")
