# Integration and Test Plan (P3), Delivery 2

## 1. What is tested, and where

| Layer | What it checks | Tool | Needs Mininet? |
|---|---|---|---|
| Protocol and server | messages, errors, framing, worker failure (13 tests, T01-T13) | `tests/test_d1.py` | No |
| Scheduling | the order each algorithm picks jobs, aging, input checks (10 tests, S01-S10) | `tests/test_scheduling.py` | No |
| Switch logic and tools (POX, no switches needed) | dynamic switch (20 checks), allow-list (26 tests), statistics stream (23 tests), link failure tool (7 tests) | `sdn/test_dynamic_switch.py`, `sdn/test_allowlist.py`, `sdn/test_switch_stats.py`, `topology/test_linkctl.py` | No |
| Network and integration | connectivity, POX status lines, link failures, access control | manual steps in section 4 | Yes |
| Experiments | scheduler comparison, worker failure under load, controller comparison | `experiments/`, `app/load_generator.py` | local ones no, network ones yes |

Run everything from the repo root.

## 2. Running the automated tests (any computer, no Mininet)

```
python3 tests/test_scheduling.py -v                  # about 11 s, expects: Ran 10 tests ... OK
RUN_SLOW=1 python3 tests/test_d1.py -v               # starts a built-in reference server; expects: Ran 13 tests ... OK
```

To run `test_d1.py` against the real server instead, start `python3 app/server.py` in another terminal and run
`SERVER_HOST=127.0.0.1 RUN_SLOW=1 python3 tests/test_d1.py -v`. With no POX running, the server prints
"Status report failed" lines and may run slower; `test_scheduling.py` avoids this by itself.

Rules for `test_d1.py`:
- Restart the server before each run. It remembers every job id it has seen and keeps leftover queued jobs.
- Do not run real workers at the same time. The tests register their own fake workers (w1-w3).
- Do not run it with POX's `--allowlist` on. The fake workers connect from c1, so the allow-list blocks them and the tests fail. That is expected, not a bug.

`test_scheduling.py` needs ports 5000 and 5001 free (stop any running server first).

Unit tests for the network side (no Mininet; POX cloned to `~/pox`, or set `POX_DIR`):

```
python3 sdn/test_dynamic_switch.py -v     # expects 20 of 20 checks; starts POX itself on port 6633 and takes about 90 s, so stop any running POX first
python3 sdn/test_allowlist.py -v          # expects 26 tests OK
python3 sdn/test_switch_stats.py -v       # expects 23 tests OK
python3 topology/test_linkctl.py -v       # expects 7 tests OK
```

## 3. Running inside Mininet

1. POX (first, always):
   `cd ~/pox` then
   `PYTHONPATH=~/sdn-job-queue/sdn python3 pox.py openflow.discovery openflow.spanning_tree dynamic_switch --mode=dynamic job_status`
   (use `--mode=static` for the baseline; the default 3-switch topology needs `openflow.discovery` and `openflow.spanning_tree`).
2. Topology: `cd ~/sdn-job-queue` then `sudo python3 topology/topology.py` (options: `--topo ring|line|mesh|...`, `--switches N`, see `--help`).
3. At the `mininet>` prompt: `pingall` (expect 0% dropped).
4. Start the server and workers, each command on its own line, worker id written `--id=w1` because a bare `w1` is replaced by its IP address:
   ```
   server python3 -u app/server.py --scheduler fcfs > /tmp/server.log 2>&1 &
   w1 python3 -u app/worker.py --id=w1 --server 10.0.0.10 --port 5001 > /tmp/w1.log 2>&1 &
   w2 python3 -u app/worker.py --id=w2 --server 10.0.0.10 --port 5001 > /tmp/w2.log 2>&1 &
   w3 python3 -u app/worker.py --id=w3 --server 10.0.0.10 --port 5001 > /tmp/w3.log 2>&1 &
   ```
   `--scheduler` can be `fcfs`, `sjf` or `priority` (with `--aging-rate N`).
5. One job: `c1 python3 -u app/client.py --server 10.0.0.10 --duration 3`
6. Many jobs at random intervals (from the load generator):
   `c2 python3 -u app/load_generator.py --jobs 40 --seed 1 --label test --interval-min 0.3 --interval-max 0.9 --duration-min 0.5 --duration-max 2`
   Add `--priority-min 0 --priority-max 10` for the priority scheduler, `--idle-after 5 --idle-gap 45` for a long idle gap, or `--dry-run` to only print the schedule. Per-job results go to `results/<label>_<time>.csv`.
7. Protocol tests from c1: stop the real workers (`w1 kill %python3`, same for w2 and w3), restart the server (`server pkill -f app/server.py`, then the server line above), then
   `c1 RUN_SLOW=1 SERVER_HOST=10.0.0.10 python3 tests/test_d1.py -v`

## 4. Test cases

### Automated

| ID | Area | Check | Expected |
|---|---|---|---|
| T01 | Worker | REGISTER | ACK with ref REGISTER |
| T02 | Worker | HEARTBEAT | no reply |
| T03 | Client | JOB | ACK, later RESULT completed |
| T04 | Scheduling | 9 jobs, 3 workers | 3 each (round-robin over workers) |
| T05 | Concurrency | 3 clients x 3 jobs | no lost results |
| T06 | Errors | duplicate job_id | ERROR DUPLICATE_JOB |
| T07-T10 | Errors | bad JSON / unknown type / missing field / bad value | matching ERROR, connection stays open |
| T11 | Framing | message split over 2 packets | job accepted |
| T12 | Framing | 2 messages in 1 packet | 2 ACKs |
| T13 | Failure | silent worker (no heartbeat for ~6 s) | marked DOWN, job requeued, all results arrive (`RUN_SLOW=1`) |
| S01 | Scheduler | fcfs, jobs of different size and priority | served in arrival order |
| S02 | Scheduler | sjf | shortest job first |
| S03 | Scheduler | priority, aging 0 | highest priority first |
| S04 | Scheduler | priority, equal priorities | arrival order (acts like fcfs) |
| S05 | Scheduler | priority, aging 0, old low-priority job and newer high-priority job | high-priority job overtakes |
| S06 | Scheduler | priority, aging 5, same two jobs | the older low-priority job goes first (aging works) |
| S07-S09 | Input | priority 11, -1, "high" | ERROR BAD_VALUE |
| S10 | Input | `--aging-rate -1` | server refuses to start |

| U01 | Switch | `sdn/test_dynamic_switch.py`: dynamic and static behaviour with fake switches | 20 of 20 checks pass |
| U02 | Access control | `sdn/test_allowlist.py`: allow-list rules for client and worker IPs | 26 tests OK |
| U03 | Statistics | `sdn/test_switch_stats.py`: statistics stream lines, links and stale switches | 23 tests OK |
| U04 | Links | `topology/test_linkctl.py`: fail and restore commands (including `--dry-run`) | 7 tests OK |

### Manual (Mininet)

| ID | Area | Check | Expected |
|---|---|---|---|
| N01 | Network | `pingall` | 0% dropped |
| N02 | SDN | POX log after the topology starts | switches connected, links discovered |
| N03 | Integration | start a worker | POX shows `Worker w1 is UP` |
| N04 | Integration | `w2 kill %python3` (do not use `pkill -f worker.py`, it kills all workers) | POX shows `Worker w2 is DOWN` within about 6 s, new jobs skip w2 |
| N05 | End to end | jobs from c1, c2 and c3 | every job gets ACK and RESULT, spread over the workers |
| L01 | Link failure | 3-switch ring, `--mode=dynamic`, load from c2, `flap s1 s2` | jobs keep completing, outage under about 1 s |
| L02 | Link failure | same, `--mode=static` | connectivity returns only when the link returns; the server may mark w1 DOWN and requeue its job |
| L03 | Link failure | 3-switch line, either mode (control) | nothing can reroute, outage lasts as long as the failure |
| A01 | Access control | POX with `--allowlist`: a normal job from c1; a job attempt from a worker host | the c1 job works; the attempt from the worker is dropped and POX logs `BLOCKED`; `pingall` still clean |
| A02 | Access control | allow-list off (default) | everything works as before |

## 5. Network experiments: how to run them

Guidance from the network side (Harshith's measurements):
- **Ring:** run the load from **c2** and fail **s1-s2**. c1 and w3 are on the same switch as the server, so a failure never touches them. s2-s3 is the blocked link, so failing it changes nothing.
- **Line:** the control. Both links can be used; nothing can reroute.
- **Mesh:** skip it in load runs; quote the ping measurements instead, because the link that matters changes between runs.
- **Before each set of runs,** check the link you fail really matters in that run: fail it for 15 s while pinging from c2 to the server and look for a gap. POX can choose a different link to block each time it starts.
- **Static mode varies a lot:** restart POX and Mininet before every static run, and do at least 5 runs per case.

One run (two terminals, started together):

```
# terminal 1, at the mininet> prompt (server and workers already running)
c2 python3 -u app/load_generator.py --jobs 40 --seed 1 --label ring-dynamic-run1 --interval-min 0.3 --interval-max 0.9 --duration-min 0.5 --duration-max 2
# terminal 2, immediately after
sudo python3 topology/linkctl.py flap s1 s2 --after 10 --for 30 --log /tmp/flap-run1.log
```

Keep the CSV from `results/`, the `/tmp/flap-run1.log` file and the POX output for each run. Compare, per run: jobs completed and lost, the longest time with no result arriving, and the slowest job. Repeat with `--mode=static` and with `--topo line`.

## 6. Demo script

Run this live, in order. Each step says what to point out. The GUI steps need `gui/` merged (or the `gui` branch checked out).

1. **Start POX** (terminal 1): `cd ~/pox` then
   `PYTHONPATH=~/sdn-job-queue/sdn python3 pox.py openflow.discovery openflow.spanning_tree dynamic_switch --mode=dynamic job_status switch_stats`
   Point out: `Status channel listening on 0.0.0.0:5002`.
2. **Start the network** (terminal 2): `cd ~/sdn-job-queue` then `sudo python3 topology/topology.py`. At `mininet>` run `pingall`. Point out: 0% dropped.
3. **Start the server and three workers** (the commands in section 3, step 4). Point out: POX logs `Worker w1 is UP`, then w2 and w3.
4. **Start the GUI** (terminal 3, on the VM): `python3 gui/bridge.py --log /tmp/server.log`, then open http://localhost:8080. Point out: three workers shown as available, the switches and links in the Network panel.
5. **Send jobs from the GUI** (for example 10 jobs, 0.5 s apart, 2 s of work). Point out: jobs wait in the queue, bars appear on the worker lanes, round-robin across w1 to w3.
6. **Change the scheduler**: restart the server with `--scheduler sjf` (or `priority --aging-rate 1`), send the same load, and point out the different order of jobs.
7. **Kill a worker during load**: `w1 kill %python3`. Point out: w1 goes offline in the GUI, POX logs `Worker w1 is DOWN`, its unfinished job is requeued and finishes on another worker.
8. **Fail a link during load**: start a load from c2, then `sudo python3 topology/linkctl.py flap s1 s2 --after 5 --for 20`. Point out: the link turns down in the GUI and jobs keep completing in dynamic mode. Repeat with POX in `--mode=static` to show the long outage.
9. **Allow-list (optional)**: restart POX with `--allowlist`, run a job from c1 (works), then the `connect_ex` check from c3 (refused, POX logs `BLOCKED`). See the README section "Allow-list".

## 7. Results log

| ID | Date | Where | Result | Notes |
|---|---|---|---|---|
| T01-T13 | 25 Oct 2026 | Mininet + POX, D1 layout, D1 server | 13 of 13 OK, 7.7 s | `RUN_SLOW=1`, from c1 against 10.0.0.10 |
| T01-T13 | 8 Nov 2026 | one machine, no Mininet, current server | 13 of 13 OK, 6.2 s | regression after the scheduler changes |
| S01-S10 | 8 Nov 2026 | one machine, no Mininet, current server | 10 of 10 OK, about 11 s | also checked that the tests fail against the wrong algorithm |
| E1 scheduler comparison | 8-9 Nov 2026 | one machine, simulated workers | done | see `docs/local_experiments_report.md` |
| E2 worker failure under load | 9 Nov 2026 | one machine, simulated workers | done, no job lost | see `docs/local_experiments_report.md` |
| U01-U04 | [date] | one machine, POX cloned to `~/pox` | 20 of 20 checks, then 26, 23 and 7 tests OK | counts as given in `README.md` |
| N01-N04 | [date] | Mininet + POX, 3-switch ring | `pingall` 0% dropped; POX shows `Worker wN is UP` and `DOWN` | from the quick start in `README.md` |
| T01-T13 on the ring | [date] | Mininet + POX, 3-switch ring and single switch | 13 of 13 OK | as stated in `README.md` |
| L01-L03 (ping measurement) | [date] | Mininet + POX, `ping -D -i 0.2` to the server, link failed for 15 s | ring: dynamic 0.4 s silence, static 15.3 s; line (control): about 15.3 s in both modes | single runs, ping only, not job-level; table in `README.md` |
| A01-A02 | [date] | Mininet + POX, 3-switch ring | allowed client works, other host refused (`connect_ex` returns 11 after about 4 s), POX logs `BLOCKED`; both modes | not tested on the mesh |
| N05 | | Mininet | to do | |
| S01-S10 on the ring | | Mininet | to do | |
| L01-L02 with the load generator (static vs dynamic) | | Mininet | to do | |

## 8. Open items

- [ ] Run N05 and S01-S10 on the 3-switch ring (dynamic mode).
- [ ] Run L01-L02 with the load generator, at least 5 runs per static case (needed for the static against dynamic comparison at job level).
- [ ] Run one load with a long idle gap (`--idle-after 5 --idle-gap 30`) and keep the CSV in `results/`.
- [ ] GUI (`gui/index.html`, `gui/bridge.py`) reads the statistics stream (port 5003) and the server log; test it on Mininet.
- [x] `LOAD` values are 0 or 100: the old 75 values in `README.md` and `sdn/README.md` were fixed.
