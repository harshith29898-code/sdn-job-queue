#!/usr/bin/env python3
"""Application-level experiments: scheduler comparison and worker failure.

These run on ONE machine over localhost (no Mininet, no POX), with the REAL
app/server.py and app/load_generator.py. Workers are simulated (see
experiments/sim_worker.py) unless you pass --real-workers, which uses app/worker.py
and is only meaningful on a machine with at least as many CPU cores as workers.

    python3 experiments/local_experiments.py scheduler --seeds 5
    python3 experiments/local_experiments.py crash --seeds 3
    python3 experiments/analyze_local.py            # tables and graphs

Run from the repo root. Ports 5000 and 5001 must be free. Finished runs are kept
in results/scheduler/ and results/crash/ and skipped if you run again, so an
interrupted experiment can simply be restarted.

What each experiment does
-------------------------
scheduler: the SAME random workload (same seed) is sent to the server under four
    settings: fcfs, sjf, priority with no aging, priority with aging 1. The load is
    heavier than the three workers can handle, so a queue builds and the order in
    which the scheduler picks jobs actually matters.
crash: the same workload with fcfs, in three situations: no failure, worker w2
    killed (its connection closes at once), and worker w2 frozen (its connection
    stays open but heartbeats stop, so the server must wait for the timeout).
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from test_scheduling import ServerProcess  # noqa: E402  (starts the real server, POX stubbed)

LOAD_GENERATOR = os.path.join(ROOT, "app", "load_generator.py")
SIM_WORKER = os.path.join(ROOT, "experiments", "sim_worker.py")
REAL_WORKER = os.path.join(ROOT, "app", "worker.py")

# the workload: 50 jobs, 0.2 to 0.6 s apart, 0.5 to 3 s of work each, random priority 0 to 10.
# Arrival rate (about 2.5 jobs/s) is higher than what 3 workers can finish (about 1.7 jobs/s).
WORKLOAD = ["--jobs", "50", "--interval-min", "0.2", "--interval-max", "0.6",
            "--duration-min", "0.5", "--duration-max", "3.0",
            "--priority-min", "0", "--priority-max", "10"]

SCHEDULER_CONFIGS = [
    ("fcfs", ["--scheduler", "fcfs"]),
    ("sjf", ["--scheduler", "sjf"]),
    ("priority-aging0", ["--scheduler", "priority", "--aging-rate", "0"]),
    ("priority-aging1", ["--scheduler", "priority", "--aging-rate", "1"]),
]

CRASH_CONFIGS = ["baseline", "crash", "hang"]
FAIL_AT_SECONDS = 8.0   # after the load starts


def start_cluster(server_args, real_workers):
    server = ServerProcess(*server_args)
    if not server.wait_ready():
        raise RuntimeError("server did not start (are ports 5000/5001 free?)\n" + server.output())
    workers = {}
    for wid in ("w1", "w2", "w3"):
        if real_workers:
            cmd = [sys.executable, REAL_WORKER, "--id=" + wid, "--server", "127.0.0.1", "--port", "5001"]
        else:
            cmd = [sys.executable, SIM_WORKER, "--id=" + wid, "--server", "127.0.0.1", "--port", "5001"]
        workers[wid] = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 10
    while time.time() < deadline:
        out = server.output()
        if all(f"{w} registered" in out for w in workers):
            return server, workers
        time.sleep(0.1)
    stop_cluster(server, workers)
    raise RuntimeError("workers did not register")


def stop_cluster(server, workers):
    for proc in workers.values():
        try:
            proc.send_signal(signal.SIGCONT)      # in case it was frozen
        except OSError:
            pass
        proc.kill()
        proc.wait()
    server.stop()


def generator_command(seed, label, csv_path):
    return [sys.executable, LOAD_GENERATOR, "--server", "127.0.0.1", *WORKLOAD,
            "--seed", str(seed), "--label", label, "--csv", csv_path,
            "--quiet", "--timeout", "120"]


def run_one(out_dir, name, seed, server_args, real_workers, fail_mode=None):
    csv_path = os.path.join(out_dir, f"{name}_seed{seed}.csv")
    if os.path.exists(csv_path):
        print(f"  skip {name} seed {seed} (already done)", flush=True)
        return
    server, workers = start_cluster(server_args, real_workers)
    meta = {"name": name, "seed": seed, "fail_mode": fail_mode, "fail_time": None}
    try:
        started = time.time()
        gen = subprocess.Popen(generator_command(seed, name, csv_path),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if fail_mode in ("crash", "hang"):
            time.sleep(max(0.0, started + FAIL_AT_SECONDS - time.time()))
            meta["fail_time"] = time.time()
            if fail_mode == "crash":
                workers["w2"].kill()
            else:
                workers["w2"].send_signal(signal.SIGSTOP)
        gen.wait(timeout=300)
        with open(os.path.join(out_dir, f"{name}_seed{seed}.server.log"), "w") as f:
            f.write(server.output())
        with open(os.path.join(out_dir, f"{name}_seed{seed}.meta.json"), "w") as f:
            json.dump(meta, f)
    finally:
        stop_cluster(server, workers)
    print(f"  done {name} seed {seed} in {time.time() - started:.0f} s", flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("experiment", choices=("scheduler", "crash"))
    p.add_argument("--seeds", type=int, default=5, help="how many repeats (seeds 1..N)")
    p.add_argument("--real-workers", action="store_true",
                   help="use app/worker.py (CPU-bound) instead of simulated workers")
    args = p.parse_args()

    out_dir = os.path.join(ROOT, "results", args.experiment)
    os.makedirs(out_dir, exist_ok=True)
    os.chdir(ROOT)
    print(f"Experiment '{args.experiment}', {args.seeds} seeds, "
          f"{'real' if args.real_workers else 'simulated'} workers", flush=True)

    for seed in range(1, args.seeds + 1):
        # configurations are interleaved per seed so slow drift affects them all equally
        if args.experiment == "scheduler":
            for name, server_args in SCHEDULER_CONFIGS:
                run_one(out_dir, name, seed, server_args, args.real_workers)
        else:
            for name in CRASH_CONFIGS:
                run_one(out_dir, name, seed, ["--scheduler", "fcfs"], args.real_workers,
                        fail_mode=None if name == "baseline" else name)
    print("All runs finished.", flush=True)


if __name__ == "__main__":
    main()
