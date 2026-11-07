#!/usr/bin/env python3
"""P3: load generator. Sends many jobs at random intervals and records timings.

It replaces "run client.py by hand over and over". One run opens one TCP
connection to the server, sends jobs on a random schedule (it does NOT wait for
one job to finish before sending the next), and writes one CSV row per job.

Examples (run from the repo root, inside Mininet on a client host):

    # 20 jobs, 0.5 to 3 s apart, each job 1 to 3 s of work
    c1 python3 -u app/load_generator.py --jobs 20 --seed 1

    # Poisson-style arrivals (exponential gaps, mean 1 s)
    c1 python3 -u app/load_generator.py --jobs 30 --distribution exponential --mean 1.0

    # A long idle gap: send 5 jobs, wait 45 s, send the rest
    c1 python3 -u app/load_generator.py --jobs 10 --idle-after 5 --idle-gap 45

    # Show the schedule without connecting (works anywhere, no server needed)
    python3 app/load_generator.py --jobs 8 --seed 1 --dry-run

The same --seed always gives the same gaps and job sizes, so two experiments
(for example round-robin versus another scheduler) can use identical load.

Timestamps are Unix wall-clock times, so they line up with the t= stamps in the
POX log and with the lines written by topology/linkctl.py --log.
"""

import argparse
import csv
import json
import os
import random
import socket
import statistics
import sys
import threading
import time

CSV_COLUMNS = [
    "label", "run_id", "seq", "job_id", "planned_gap_s", "duration_s",
    "payload_size", "submit_time", "ack_time", "result_time", "worker_id",
    "status", "error", "ack_latency_s", "completion_s", "extra_delay_s",
]


def parse_args():
    p = argparse.ArgumentParser(
        description="Send jobs at random intervals and record how long each takes.")
    p.add_argument("--server", default="10.0.0.10", help="job server address (default 10.0.0.10)")
    p.add_argument("--port", type=int, default=5000, help="client port (default 5000)")
    p.add_argument("--jobs", type=int, default=20, help="number of jobs to send (default 20)")
    p.add_argument("--distribution", choices=("uniform", "exponential"), default="uniform",
                   help="how the gaps between jobs are drawn (default uniform)")
    p.add_argument("--interval-min", type=float, default=0.5,
                   help="uniform: shortest gap in seconds (default 0.5)")
    p.add_argument("--interval-max", type=float, default=3.0,
                   help="uniform: longest gap in seconds (default 3.0)")
    p.add_argument("--mean", type=float, default=1.5,
                   help="exponential: mean gap in seconds (default 1.5)")
    p.add_argument("--duration-min", type=float, default=1.0,
                   help="shortest job, seconds of work (default 1.0)")
    p.add_argument("--duration-max", type=float, default=3.0,
                   help="longest job, seconds of work (default 3.0)")
    p.add_argument("--payload-size", type=int, default=1000, help="payload size (default 1000)")
    p.add_argument("--idle-after", type=int, default=None, metavar="K",
                   help="after K jobs have been sent, wait --idle-gap seconds before the next one")
    p.add_argument("--idle-gap", type=float, default=30.0,
                   help="length of the long idle gap in seconds (default 30)")
    p.add_argument("--seed", type=int, default=None,
                   help="random seed; the same seed repeats the same schedule")
    p.add_argument("--timeout", type=float, default=120.0,
                   help="seconds to keep waiting for results after the last job is sent (default 120)")
    p.add_argument("--label", default="run", help="name of this run, stored in the CSV (default run)")
    p.add_argument("--csv", default=None,
                   help="CSV file to write (default results/<label>_<time>.csv)")
    p.add_argument("--id-base", type=int, default=None,
                   help="first job id (default: derived from the clock, so runs do not collide)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the schedule and exit; does not connect to anything")
    p.add_argument("--quiet", action="store_true", help="do not print a line per job")
    args = p.parse_args()

    problems = []
    if args.jobs < 1:
        problems.append("--jobs must be at least 1")
    if not 0 <= args.interval_min <= args.interval_max:
        problems.append("need 0 <= --interval-min <= --interval-max")
    if args.mean <= 0:
        problems.append("--mean must be greater than 0")
    if not 0.1 <= args.duration_min <= args.duration_max <= 30:
        problems.append("need 0.1 <= --duration-min <= --duration-max <= 30")
    if not 0 <= args.payload_size <= 1_000_000:
        problems.append("--payload-size must be 0 to 1000000")
    if args.idle_after is not None and not 0 <= args.idle_after < args.jobs:
        problems.append("--idle-after must be 0 to (jobs - 1)")
    if args.idle_gap < 0:
        problems.append("--idle-gap must not be negative")
    if problems:
        p.error("; ".join(problems))
    return args


def build_schedule(args, rng):
    """List of (planned_gap_s, duration_s). The gap is the wait BEFORE sending that job."""
    schedule = []
    for seq in range(args.jobs):
        if seq == 0:
            gap = 0.0
        elif args.distribution == "uniform":
            gap = rng.uniform(args.interval_min, args.interval_max)
        else:
            gap = rng.expovariate(1.0 / args.mean)
        if args.idle_after is not None and seq == args.idle_after and seq > 0:
            gap = args.idle_gap
        duration = rng.uniform(args.duration_min, args.duration_max)
        schedule.append((round(gap, 3), round(duration, 2)))
    return schedule


def print_schedule(schedule):
    print(f"{'job':>4} {'wait before sending (s)':>24} {'work (s)':>9} {'sent at (s)':>12}")
    t = 0.0
    for seq, (gap, duration) in enumerate(schedule):
        t += gap
        print(f"{seq:>4} {gap:>24.3f} {duration:>9.2f} {t:>12.3f}")
    print(f"\nLast job is sent about {t:.1f} s after the start.")


def percentile(values, pct):
    """Nearest-rank percentile of a non-empty list."""
    ordered = sorted(values)
    rank = max(1, int(round(pct / 100.0 * len(ordered) + 0.4999)))
    return ordered[min(rank, len(ordered)) - 1]


class Run:
    """Everything recorded about one run, shared between the sender and receiver."""

    def __init__(self, records):
        self.records = records          # job_id -> dict
        self.lock = threading.Lock()
        self.connection_closed = threading.Event()
        self.quiet = False

    def outstanding(self):
        with self.lock:
            return [r for r in self.records.values()
                    if r["submit_time"] is not None and r["status"] == "pending"]

    def note(self, text):
        if not self.quiet:
            print(text, flush=True)


def receiver(sock, run):
    """Read ACK / RESULT / ERROR lines and stamp the matching job record."""
    reader = sock.makefile("rb")
    try:
        for raw in reader:
            now = time.time()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            kind = msg.get("type")
            job_id = msg.get("job_id")
            with run.lock:
                rec = run.records.get(job_id)
                if rec is None:
                    continue
                if kind == "ACK" and msg.get("ref") == "JOB":
                    rec["ack_time"] = now
                elif kind == "RESULT":
                    rec["result_time"] = now
                    rec["worker_id"] = msg.get("worker_id", "")
                    rec["status"] = msg.get("status", "completed")
                elif kind == "ERROR":
                    rec["error"] = msg.get("code", "ERROR")
                    rec["status"] = "error"
            if kind == "RESULT":
                took = now - rec["submit_time"]
                run.note(f"  job {job_id} done by {rec['worker_id']} in {took:.2f} s")
            elif kind == "ERROR":
                run.note(f"  job {job_id} rejected: {rec['error']}")
    except OSError:
        pass
    finally:
        run.connection_closed.set()


def derive_times(rec):
    """Fill the calculated columns. All are blank if the information is missing."""
    rec["ack_latency_s"] = rec["completion_s"] = rec["extra_delay_s"] = ""
    if rec["submit_time"] is not None and rec["ack_time"] is not None:
        rec["ack_latency_s"] = round(rec["ack_time"] - rec["submit_time"], 4)
    if rec["submit_time"] is not None and rec["result_time"] is not None:
        completion = rec["result_time"] - rec["submit_time"]
        rec["completion_s"] = round(completion, 4)
        # time spent waiting (queue + network) = total time minus the job's own work
        rec["extra_delay_s"] = round(completion - rec["duration_s"], 4)


def write_csv(path, records):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for rec in sorted(records.values(), key=lambda r: r["seq"]):
            row = dict(rec)
            for col in ("submit_time", "ack_time", "result_time"):
                if row[col] is not None:
                    row[col] = f"{row[col]:.4f}"
                else:
                    row[col] = ""
            writer.writerow({c: row.get(c, "") for c in CSV_COLUMNS})


def summarize(records, started, args):
    recs = list(records.values())
    sent = [r for r in recs if r["submit_time"] is not None]
    done = [r for r in recs if r["status"] == "completed"]
    failed = [r for r in recs if r["status"] == "failed"]
    errors = [r for r in recs if r["status"] == "error"]
    lost = [r for r in recs if r["status"] == "pending"]
    unsent = [r for r in recs if r["submit_time"] is None]

    print("\n=== Summary ===")
    print(f"Jobs planned: {len(recs)}   sent: {len(sent)}   completed: {len(done)}")
    if failed:
        print(f"Reported as failed by a worker: {len(failed)}")
    if errors:
        codes = sorted({r["error"] for r in errors})
        print(f"Rejected by the server: {len(errors)} ({', '.join(codes)})")
    if lost:
        print(f"LOST (no result before the timeout): {len(lost)}")
    if unsent:
        print(f"Never sent (connection broke first): {len(unsent)}")

    if done:
        comp = [r["completion_s"] for r in done]
        extra = [r["extra_delay_s"] for r in done]
        print("\nTime from sending a job to getting its result:")
        print(f"  average {statistics.mean(comp):.2f} s, median {statistics.median(comp):.2f} s, "
              f"95th percentile {percentile(comp, 95):.2f} s, slowest {max(comp):.2f} s")
        print("Extra delay = that time minus the job's own work time (waiting in the queue + network):")
        print(f"  average {statistics.mean(extra):.2f} s, 95th percentile {percentile(extra, 95):.2f} s, "
              f"worst {max(extra):.2f} s")
        first = min(r["submit_time"] for r in sent)
        last = max(r["result_time"] for r in done)
        span = max(last - first, 1e-9)
        print(f"Throughput: {len(done) / span:.2f} jobs per second over {span:.1f} s")
        per_worker = {}
        for r in done:
            per_worker[r["worker_id"]] = per_worker.get(r["worker_id"], 0) + 1
        shares = ", ".join(f"{w}: {n}" for w, n in sorted(per_worker.items()))
        print(f"Jobs per worker: {shares}")
    else:
        print("No job completed, so there are no timings to report.")


def main():
    args = parse_args()

    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(1, 10**6)
    rng = random.Random(seed)
    schedule = build_schedule(args, rng)

    if args.dry_run:
        print(f"Dry run. Seed {seed}. Nothing is sent.\n")
        print_schedule(schedule)
        return 0

    stamp = time.strftime("%Y%m%d_%H%M%S")
    csv_path = args.csv or os.path.join("results", f"{args.label}_{stamp}.csv")
    run_id = f"{args.label}-{stamp}-seed{seed}"
    base = args.id_base if args.id_base is not None else (int(time.time() * 1000) % 1_900_000_000) + 1

    records = {}
    for seq, (gap, duration) in enumerate(schedule):
        job_id = base + seq
        records[job_id] = {
            "label": args.label, "run_id": run_id, "seq": seq, "job_id": job_id,
            "planned_gap_s": gap, "duration_s": duration, "payload_size": args.payload_size,
            "submit_time": None, "ack_time": None, "result_time": None,
            "worker_id": "", "status": "pending", "error": "",
        }

    run = Run(records)
    run.quiet = args.quiet

    print(f"Connecting to {args.server}:{args.port}  (seed {seed}, {args.jobs} jobs, label '{args.label}')")
    try:
        sock = socket.create_connection((args.server, args.port), timeout=10)
    except OSError as exc:
        print(f"Could not connect: {exc}")
        return 2
    sock.settimeout(None)

    threading.Thread(target=receiver, args=(sock, run), daemon=True).start()

    started = time.time()
    next_send = started
    for seq, (gap, duration) in enumerate(schedule):
        next_send += gap
        delay = next_send - time.time()
        if delay > 0:
            if args.idle_after is not None and seq == args.idle_after and gap >= 5:
                run.note(f"  ...idle for {gap:.0f} s before job {seq}")
            time.sleep(delay)
        if run.connection_closed.is_set():
            break
        job_id = base + seq
        message = {"type": "JOB", "job_id": job_id,
                   "payload_size": args.payload_size, "duration": duration}
        line = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        try:
            with run.lock:
                records[job_id]["submit_time"] = time.time()
            sock.sendall(line)
        except OSError:
            with run.lock:
                records[job_id]["submit_time"] = None
            break
        run.note(f"sent job {job_id} (work {duration:.2f} s)")

    # wait for the remaining results
    deadline = time.time() + args.timeout
    while time.time() < deadline and run.outstanding() and not run.connection_closed.is_set():
        time.sleep(0.1)

    try:
        sock.close()
    except OSError:
        pass

    for rec in records.values():
        derive_times(rec)
    write_csv(csv_path, records)
    summarize(records, started, args)
    print(f"\nPer-job results written to {csv_path}")

    everything_ok = all(r["status"] == "completed" for r in records.values())
    return 0 if everything_ok else 1


if __name__ == "__main__":
    sys.exit(main())
