#!/usr/bin/env python3
"""Turn the results of experiments/local_experiments.py into tables and graphs.

    python3 experiments/analyze_local.py

Reads results/scheduler/ and results/crash/ (whichever exist) and writes
results/*.md, results/*.csv and results/figures/*.png.

Waiting time here is measured exactly: the time the job was handed to a worker
(the "dispatched at" time in the server log) minus the time the load generator
sent it. Completion time is send time to result time.
"""

import csv
import glob
import json
import os
import re
import statistics
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "results")
FIGURES = os.path.join(RESULTS, "figures")

DISPATCH_RE = re.compile(r"Job (\d+) -> (w\d+) \(dispatched at ([0-9.]+)\)")

SCHEDULER_ORDER = ["fcfs", "sjf", "priority-aging0", "priority-aging1"]
SCHEDULER_LABELS = {
    "fcfs": "FCFS\n(first come,\nfirst served)",
    "sjf": "SJF\n(shortest job\nfirst)",
    "priority-aging0": "Priority\n(no aging)",
    "priority-aging1": "Priority\n(aging 1)",
}
COLORS = {"fcfs": "#4c72b0", "sjf": "#dd8452", "priority-aging0": "#c44e52", "priority-aging1": "#55a868"}

SIZE_CLASSES = [("short jobs\n(0.5 to 1.25 s)", 0.0, 1.25),
                ("medium jobs\n(1.25 to 2.25 s)", 1.25, 2.25),
                ("long jobs\n(2.25 to 3 s)", 2.25, 99)]
PRIORITY_CLASSES = [("low priority\n(0 to 3)", 0, 3),
                    ("medium priority\n(4 to 7)", 4, 7),
                    ("high priority\n(8 to 10)", 8, 10)]


def percentile(values, pct):
    ordered = sorted(values)
    rank = max(1, int(round(pct / 100.0 * len(ordered) + 0.4999)))
    return ordered[min(rank, len(ordered)) - 1]


def mean_sd(values):
    values = [v for v in values if v is not None]
    if not values:
        return None, None, 0
    if len(values) == 1:
        return values[0], 0.0, 1
    return statistics.mean(values), statistics.stdev(values), len(values)


def fmt(values, digits=2):
    m, s, n = mean_sd(values)
    if m is None:
        return "n/a"
    return f"{m:.{digits}f} ± {s:.{digits}f}" if n > 1 else f"{m:.{digits}f}"


def load_run(base):
    """base = path without extension. Returns dict with rows (list) and dispatches (job -> [(worker, time)])."""
    with open(base + ".csv", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for key in ("duration_s", "submit_time", "result_time", "completion_s"):
            r[key] = float(r[key]) if r.get(key) not in (None, "") else None
        r["priority"] = int(r["priority"])
        r["job_id"] = int(r["job_id"])
    dispatches = {}
    log_path = base + ".server.log"
    if os.path.exists(log_path):
        with open(log_path) as f:
            for line in f:
                m = DISPATCH_RE.search(line)
                if m:
                    dispatches.setdefault(int(m.group(1)), []).append((m.group(2), float(m.group(3))))
    meta = {}
    meta_path = base + ".meta.json"
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            meta = json.load(f)
    return {"rows": rows, "dispatches": dispatches, "meta": meta}


def waits(run):
    """List of (row, wait_seconds) for completed jobs that have a dispatch time."""
    out = []
    for r in run["rows"]:
        d = run["dispatches"].get(r["job_id"])
        if r["status"] == "completed" and d and r["submit_time"] is not None:
            out.append((r, d[0][1] - r["submit_time"]))
    return out


def group_mean(pairs, key, lo, hi):
    group = [w for r, w in pairs if lo <= r[key] <= hi]
    return statistics.mean(group) if group else None


# ---------------------------------------------------------------- scheduler experiment
def analyze_scheduler():
    folder = os.path.join(RESULTS, "scheduler")
    if not os.path.isdir(folder):
        return None
    per_config = {}
    for name in SCHEDULER_ORDER:
        runs = []
        for path in sorted(glob.glob(os.path.join(folder, f"{name}_seed*.csv"))):
            runs.append(load_run(path[:-4]))
        if not runs:
            continue
        stats = {"runs": len(runs), "mean_wait": [], "p95_wait": [], "max_wait": [], "mean_completion": [],
                 "makespan": [], "lost": [], "size": {c[0]: [] for c in SIZE_CLASSES},
                 "prio": {c[0]: [] for c in PRIORITY_CLASSES}}
        for run in runs:
            pairs = waits(run)
            w = [x for _, x in pairs]
            comp = [r["completion_s"] for r, _ in pairs]
            stats["mean_wait"].append(statistics.mean(w))
            stats["p95_wait"].append(percentile(w, 95))
            stats["max_wait"].append(max(w))
            stats["mean_completion"].append(statistics.mean(comp))
            submits = [r["submit_time"] for r in run["rows"] if r["submit_time"] is not None]
            results = [r["result_time"] for r in run["rows"] if r["result_time"] is not None]
            stats["makespan"].append(max(results) - min(submits))
            stats["lost"].append(sum(1 for r in run["rows"] if r["status"] != "completed"))
            for label, lo, hi in SIZE_CLASSES:
                stats["size"][label].append(group_mean(pairs, "duration_s", lo, hi))
            for label, lo, hi in PRIORITY_CLASSES:
                stats["prio"][label].append(group_mean(pairs, "priority", lo, hi))
        per_config[name] = stats
    return per_config


def scheduler_tables(per_config):
    lines = []
    lines.append("### Scheduler comparison: overall (seconds, mean ± standard deviation over runs)\n")
    lines.append("| Scheduler | Runs | Mean wait | 95th percentile wait | Longest wait | Mean time to result | Whole batch finished in | Jobs lost |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for name, s in per_config.items():
        lines.append(f"| {name} | {s['runs']} | {fmt(s['mean_wait'])} | {fmt(s['p95_wait'])} | {fmt(s['max_wait'])} | "
                     f"{fmt(s['mean_completion'])} | {fmt(s['makespan'], 1)} | {sum(s['lost'])} |")
    lines.append("\n### Mean wait by job size (seconds)\n")
    lines.append("| Scheduler | " + " | ".join(c[0].replace("\n", " ") for c in SIZE_CLASSES) + " |")
    lines.append("|---|" + "---|" * len(SIZE_CLASSES))
    for name, s in per_config.items():
        lines.append(f"| {name} | " + " | ".join(fmt(s["size"][c[0]]) for c in SIZE_CLASSES) + " |")
    lines.append("\n### Mean wait by job priority (seconds)\n")
    lines.append("| Scheduler | " + " | ".join(c[0].replace("\n", " ") for c in PRIORITY_CLASSES) + " |")
    lines.append("|---|" + "---|" * len(PRIORITY_CLASSES))
    for name, s in per_config.items():
        lines.append(f"| {name} | " + " | ".join(fmt(s["prio"][c[0]]) for c in PRIORITY_CLASSES) + " |")
    return "\n".join(lines)


def bar_panel(ax, per_config, getter, title, ylabel):
    names = list(per_config)
    means, sds = [], []
    for n in names:
        m, s, _ = mean_sd(getter(per_config[n]))
        means.append(m or 0)
        sds.append(s or 0)
    ax.bar(range(len(names)), means, yerr=sds, capsize=4, color=[COLORS[n] for n in names])
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels([SCHEDULER_LABELS[n] for n in names], fontsize=8)
    ax.set_title(title, fontsize=10)
    ax.set_ylabel(ylabel)
    ax.grid(axis="y", alpha=0.3)
    for i, m in enumerate(means):
        ax.text(i, m + sds[i], f"{m:.1f}", ha="center", va="bottom", fontsize=8)


def grouped_panel(ax, per_config, classes, field, title, ylabel):
    names = list(per_config)
    width = 0.8 / len(names)
    for i, n in enumerate(names):
        means, sds = [], []
        for label, _, _ in classes:
            m, s, _ = mean_sd(per_config[n][field][label])
            means.append(m or 0)
            sds.append(s or 0)
        xs = [j + i * width for j in range(len(classes))]
        ax.bar(xs, means, width, yerr=sds, capsize=2, color=COLORS[n],
               label=SCHEDULER_LABELS[n].replace("\n", " "))
    ax.set_xticks([j + width * (len(names) - 1) / 2 for j in range(len(classes))])
    ax.set_xticklabels([c[0] for c in classes], fontsize=8)
    ax.set_title(title, fontsize=10)
    ax.set_ylabel(ylabel)
    ax.grid(axis="y", alpha=0.3)


def scheduler_figures(per_config):
    os.makedirs(FIGURES, exist_ok=True)
    fig, axes = plt.subplots(1, 3, figsize=(14, 4.4))
    bar_panel(axes[0], per_config, lambda s: s["mean_wait"], "Average time a job waits in the queue",
              "seconds (error bars: one standard deviation)")
    bar_panel(axes[1], per_config, lambda s: s["max_wait"], "Longest wait of any job in a run",
              "seconds")
    bar_panel(axes[2], per_config, lambda s: s["makespan"], "Time to finish the whole batch",
              "seconds")
    fig.suptitle("Scheduler comparison: same 50-job workload, 3 workers, overloaded queue", fontsize=11)
    fig.tight_layout()
    fig.savefig(os.path.join(FIGURES, "scheduler_overall.png"), dpi=150)
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(13, 4.6))
    grouped_panel(axes[0], per_config, SIZE_CLASSES, "size", "Waiting time by job size",
                  "average wait (seconds)")
    grouped_panel(axes[1], per_config, PRIORITY_CLASSES, "prio", "Waiting time by job priority",
                  "average wait (seconds)")
    axes[1].legend(fontsize=8, loc="upper right")
    fig.suptitle("Who waits longest under each scheduler?", fontsize=11)
    fig.tight_layout()
    fig.savefig(os.path.join(FIGURES, "scheduler_by_group.png"), dpi=150)
    plt.close(fig)


# ---------------------------------------------------------------- crash experiment
def analyze_crash():
    folder = os.path.join(RESULTS, "crash")
    if not os.path.isdir(folder):
        return None
    per_config = {}
    for name in ("baseline", "crash", "hang"):
        runs = [load_run(p[:-4]) for p in sorted(glob.glob(os.path.join(folder, f"{name}_seed*.csv")))]
        if not runs:
            continue
        s = {"runs": len(runs), "completed": [], "lost": [], "mean_completion": [], "max_completion": [],
             "requeued": [], "handover": [], "affected_completion": []}
        for run in runs:
            rows = run["rows"]
            done = [r for r in rows if r["status"] == "completed"]
            s["completed"].append(len(done))
            s["lost"].append(len(rows) - len(done))
            comp = [r["completion_s"] for r in done]
            s["mean_completion"].append(statistics.mean(comp))
            s["max_completion"].append(max(comp))
            fail_time = run["meta"].get("fail_time")
            requeued = [jid for jid, d in run["dispatches"].items() if len(d) > 1]
            s["requeued"].append(len(requeued))
            for jid in requeued:
                d = run["dispatches"][jid]
                if fail_time:
                    s["handover"].append(d[1][1] - fail_time)
                row = next((r for r in rows if r["job_id"] == jid), None)
                if row and row["completion_s"] is not None:
                    s["affected_completion"].append(row["completion_s"])
        per_config[name] = s
    return per_config


CRASH_LABELS = {"baseline": "No failure", "crash": "Worker w2 killed\n(connection closes)",
                "hang": "Worker w2 frozen\n(heartbeats stop)"}
CRASH_COLORS = {"baseline": "#8c8c8c", "crash": "#dd8452", "hang": "#c44e52"}


def crash_tables(per_config):
    lines = []
    lines.append("### Worker failure during load (50 jobs, FCFS, 3 workers; w2 fails 8 s after the load starts)\n")
    lines.append("| Situation | Runs | Jobs completed (of 50) | Jobs lost | Jobs requeued | "
                 "Seconds from failure until the job reached another worker | Mean time to result | Slowest job |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for name, s in per_config.items():
        handover = fmt(s["handover"]) if s["handover"] else "no failure"
        lines.append(f"| {CRASH_LABELS[name].replace(chr(10), ' ')} | {s['runs']} | "
                     f"{fmt(s['completed'], 1)} | {sum(s['lost'])} | {sum(s['requeued'])} | {handover} | "
                     f"{fmt(s['mean_completion'])} | {fmt(s['max_completion'])} |")
    return "\n".join(lines)


def crash_figures(per_config):
    os.makedirs(FIGURES, exist_ok=True)
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.4))
    names = list(per_config)
    ax = axes[0]
    for i, n in enumerate(names):
        m, s, _ = mean_sd(per_config[n]["max_completion"])
        ax.bar(i, m, yerr=s, capsize=4, color=CRASH_COLORS[n])
        ax.text(i, m + s, f"{m:.1f}", ha="center", va="bottom", fontsize=8)
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels([CRASH_LABELS[n] for n in names], fontsize=8)
    ax.set_title("Slowest job in the run", fontsize=10)
    ax.set_ylabel("seconds")
    ax.grid(axis="y", alpha=0.3)

    ax = axes[1]
    shown = [n for n in names if per_config[n]["handover"]]
    for i, n in enumerate(shown):
        vals = per_config[n]["handover"]
        m, s, _ = mean_sd(vals)
        ax.bar(i, m, yerr=s, capsize=4, color=CRASH_COLORS[n])
        ax.text(i, m + s, f"{m:.1f}", ha="center", va="bottom", fontsize=8)
    ax.set_xticks(range(len(shown)))
    ax.set_xticklabels([CRASH_LABELS[n] for n in shown], fontsize=8)
    ax.set_title("Time from failure until the interrupted job\nwas handed to another worker", fontsize=10)
    ax.set_ylabel("seconds")
    ax.grid(axis="y", alpha=0.3)
    fig.suptitle("Worker failure under load: no job is lost, detection time differs", fontsize=11)
    fig.tight_layout()
    fig.savefig(os.path.join(FIGURES, "worker_failure.png"), dpi=150)
    plt.close(fig)


def main():
    out = ["# Local experiment results (generated by experiments/analyze_local.py)\n"]
    sched = analyze_scheduler()
    if sched:
        scheduler_figures(sched)
        out.append(scheduler_tables(sched))
    crash = analyze_crash()
    if crash:
        crash_figures(crash)
        out.append("\n" + crash_tables(crash))
    if not sched and not crash:
        print("No results found. Run experiments/local_experiments.py first.")
        return 1
    text = "\n".join(out) + "\n"
    with open(os.path.join(RESULTS, "local_experiments.md"), "w") as f:
        f.write(text)
    print(text)
    print("Figures written to", FIGURES)
    return 0


if __name__ == "__main__":
    sys.exit(main())
