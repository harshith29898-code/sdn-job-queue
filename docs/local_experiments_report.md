# Application-level experiments: scheduling and worker failure

These experiments measure the job-processing application on its own. They run on
one machine over localhost, with the real `app/server.py` and `app/load_generator.py`,
and no Mininet or POX. The workers are simulated (`experiments/sim_worker.py`), not the real `app/worker.py`. The network experiments (static against dynamic controller,
link failures) are reported separately.

## 1. How the experiments were run

- **Workload.** 50 jobs, sent 0.2 to 0.6 s apart (about 2.5 jobs per second). Each job needs
  0.5 to 3 s of work and has a random priority from 0 to 10.
- **Overload on purpose.** Three workers can finish about 1.7 jobs per second, so a queue builds
  up. Only when jobs wait does the scheduler's choice of order matter.
- **Same jobs for every scheduler.** Each seed produces exactly the same jobs, gaps and priorities,
  so differences come from the scheduler alone. Each setting was run with 5 seeds.
- **Simulated workers.** The machine used has one CPU core, so three real workers would compete for
  it and stretch every job. The simulated workers (`experiments/sim_worker.py`) follow the same
  protocol as `app/worker.py` (register, heartbeat every 2 s, one job at a time) but wait for the job's
  duration instead of computing. Queueing behaviour does not depend on what the work is.
- **Waiting time** is measured exactly: the time the server handed the job to a worker (its
  `dispatched at` log line) minus the time the load generator sent it.

## 2. Scheduler comparison

Settings compared: **FCFS** (first come, first served), **SJF** (shortest job first), **priority with
no aging**, and **priority with aging 1** (a job gains 1 priority point for every second it waits).

Seconds, mean ± standard deviation over 5 runs:

| Scheduler | 95th percentile wait | Mean wait | Longest wait | Whole batch finished in |
|---|---|---|---|---|
| FCFS | 8.02 ± 2.35 | 4.01 ± 1.07 | 8.66 ± 2.26 | 30.0 ± 2.3 |
| SJF | 16.10 ± 5.64 | 2.71 ± 0.83 | 19.78 ± 3.39 | 30.5 ± 2.2 |
| Priority, no aging | 16.65 ± 3.06 | 4.04 ± 1.37 | 20.72 ± 4.21 | 30.2 ± 2.4 |
| Priority, aging 1 | 10.32 ± 2.30 | 4.09 ± 1.24 | 11.93 ± 3.06 | 30.3 ± 2.2 |

The 95th percentile wait is the main measure of how badly the slowest jobs are treated. It is less noisy than the single longest wait, which depends on one job.

Mean wait by job size and by priority:

| Scheduler | short jobs | medium jobs | long jobs | low priority (0-3) | medium (4-7) | high (8-10) |
|---|---|---|---|---|---|---|
| FCFS | 4.06 | 4.27 | 3.43 | 3.89 | 4.10 | 4.06 |
| SJF | 0.39 | 1.03 | 7.58 | 2.20 | 2.37 | 3.76 |
| Priority, no aging | 4.29 | 4.26 | 3.49 | 8.93 | 1.96 | 0.61 |
| Priority, aging 1 | 4.38 | 4.52 | 3.09 | 7.06 | 3.38 | 1.02 |

Figures: `results/figures/scheduler_overall.png` and `results/figures/scheduler_by_group.png`.

### What the results show

1. **All four finish the batch in the same time (about 30 s).** None of them does more work; they only
   choose who waits. This is expected, because workers are never idle while jobs are queued.
2. **FCFS is the fairest.** Everyone waits about the same, and its 95th percentile wait (8.0 s) is the lowest.
3. **SJF gives the best average (2.7 s against 4.0 s for FCFS)** because short jobs jump the queue.
   The price is paid by long jobs, who wait 7.6 s on average against 0.4 s for short jobs. Its 95th
   percentile wait is 16.1 s, twice FCFS (8.0 s). Because every scheduler used seeds 1 to 5, the runs can be
   compared one by one: SJF had the lower mean wait than FCFS in every seed (5 of 5) and the higher 95th
   percentile wait in every seed (5 of 5). That is stronger than the overlapping standard deviations alone.
4. **Priority without aging serves urgent jobs fast** (high-priority jobs wait 0.6 s) **but starves
   the low-priority ones** (8.9 s on average, and the 95th percentile wait is 16.7 s).
5. **Aging fixes the worst case at a small cost.** Compared with no aging, on the same seeds, aging 1
   lowered the 95th percentile wait in 5 of 5 runs (by 6.3 s on average, from 16.7 s down to 10.3 s) and the
   longest wait in 5 of 5 runs (by 8.8 s on average, from 20.7 s down to 11.9 s). High-priority jobs waited 0.4 s longer on average (0.6 s to 1.0 s). So aging
   keeps urgent jobs quick while stopping low-priority jobs from waiting indefinitely.

**Which should be used?** It depends on the goal: SJF for the lowest average wait, FCFS for fairness,
priority with aging when some jobs are more urgent but nothing may starve.

## 3. Worker failure under load

FCFS, 3 workers, the same 50-job workload. At 8 s after the load starts, worker w2 fails in one of two ways:

- **killed:** the process dies and its connection closes, so the server notices at once;
- **frozen:** the process stops but the connection stays open, so heartbeats just stop and the server
  has to wait for the 6 s heartbeat timeout.

Only 3 runs (seeds 1 to 3) of each, so the averages below are approximate. Seconds, mean ± standard deviation:

| Situation | Jobs completed (of 50) | Jobs lost | Jobs requeued | From failure until the interrupted job reached another worker | Interrupted job's own time to result | Mean time to result (all jobs) | Slowest job |
|---|---|---|---|---|---|---|---|
| No failure | 50 | 0 | 0 | n/a | n/a | 6.48 ± 0.94 | 12.08 ± 1.64 |
| w2 killed | 50 | 0 | 3 (one per run) | 1.24 ± 0.95 | 6.25 ± 1.61 | 11.05 ± 1.55 | 23.20 ± 2.35 |
| w2 frozen | 50 | 0 | 3 (one per run) | 7.65 ± 1.03 | 12.10 ± 2.59 | 11.01 ± 1.43 | 23.08 ± 2.36 |

Figure: `results/figures/worker_failure.png` (3 seeds per case).

### What the results show

1. **No job was lost in any run.** The job w2 was running was put back in the queue and finished on another
   worker.
2. **How the server learns of the failure matters for the interrupted job.** A closed connection is noticed
   at once (about 1.2 s until the job was running elsewhere, which includes waiting for a free worker). A frozen
   worker costs the full heartbeat timeout (about 7.7 s, the 6 s timeout plus the server's one-second check
   interval and the wait for a free worker).
3. **Overall slowdown is the same either way.** The slowest job took about 23 s in both failure cases
   against 12 s with no failure. That is because the system lost one of its three workers for the rest of
   the run, so the queue drained more slowly. Detection speed only changes the delay of the one interrupted job.
   The mean time to result over all jobs is almost the same for killed and frozen (11.05 s and 11.01 s) because the
   extra 6 s of detection delay falls on only 1 job in 50, which is about 0.1 s on the mean. The interrupted job
   itself shows the difference directly: its own time to result averaged 6.3 s when w2 was killed and 12.1 s when
   it was frozen, a difference of about 6 s.

## 4. Limits of these experiments

- Simulated workers on one machine over localhost. There is no network delay or loss, and workers do no
  real computation. The scheduler results should carry over; absolute times will differ on a real network.
- 5 runs per scheduler and 3 per failure case. The differences described above held in every run, but
  with so few runs the exact averages are approximate and the standard deviations are large.
- The load is deliberately heavy. With a light load there is no queue and all schedulers behave the same.
- Only one aging rate (1 point per second) and one failure time were tried.
- The worker failure test fails one worker. Server failure and network failures are not covered here.

## 5. How to reproduce

From the repo root, with ports 5000 and 5001 free:

```
python3 experiments/local_experiments.py scheduler --seeds 5     # about 11 minutes
python3 experiments/local_experiments.py crash --seeds 3         # about 5 minutes
python3 experiments/analyze_local.py                              # tables and graphs
```

Raw per-job data is in `results/scheduler/` and `results/crash/` (one CSV, one server log and one small
metadata file per run). Add `--real-workers` to use `app/worker.py` on a machine with at least three CPU cores.
