# D1 Integration & Test Plan (P3)

## How to run
1. **Protocol/server conformance (no Mininet):** `RUN_SLOW=1 python3 tests/test_d1.py -v` (starts the reference server). To test P2's real server instead, start `server.py` and run `SERVER_HOST=127.0.0.1 python3 tests/test_d1.py -v`.
2. **Inside Mininet** (run all commands from the repo root so relative paths like `tests/test_d1.py` work):
   1. Start POX: `cd ~/pox` then `PYTHONPATH=~/sdn-job-queue/sdn python3 pox.py forwarding.l2_learning job_status`
   2. Start the topology: `sudo python3 topology/topology.py`
   3. At the Mininet prompt, start the server: `server python3 server.py &` (use the real filename once P2 pushes)
   4. Start the workers: `w1 python3 worker.py &`, `w2 python3 worker.py &`, `w3 python3 worker.py &`
   5. Run the tests from c1: `c1 SERVER_HOST=10.0.0.10 python3 tests/test_d1.py -v`
3. **Full end-to-end demo:** see section "Demo script".

## Test cases
| ID  | Area | Check | Expected | Automated |
|-----|------|-------|----------|-----------|
| T01 | Worker | REGISTER | ACK ref=REGISTER | yes |
| T02 | Worker | HEARTBEAT | no reply | yes |
| T03 | Client | JOB | ACK, later RESULT completed | yes |
| T04 | Scheduling | 9 jobs, 3 workers | 3 each (round-robin) | yes |
| T05 | Concurrency | 3 clients x 3 jobs | no lost results | yes |
| T06 | Errors | duplicate job_id | ERROR DUPLICATE_JOB | yes |
| T07-T10 | Errors | bad JSON / unknown type / missing field / bad value | matching ERROR, connection stays open | yes |
| T11 | Framing | message split over 2 packets | job accepted | yes |
| T12 | Framing | 2 messages in 1 packet | 2 ACKs | yes |
| T13 | Failure | silent worker (no heartbeat ~6 s) | marked DOWN, job requeued, all results arrive | yes (RUN_SLOW=1) |
| N01 | Network | `pingall` in Mininet | 0% dropped | manual |
| N02 | SDN | POX log shows switch connected | "connected" line | manual |
| N03 | Integration | start worker -> POX log | `WORKER_UP w1` | manual |
| N04 | Integration | kill a worker (`xterm w2` and run the worker there, then stop it; or `w2 kill %1`; do not use `pkill -f worker.py`, it kills all workers) | `WORKER_DOWN w2` within ~6 s | manual |
| N05 | E2E | c1,c2,c3 each submit 5 jobs via real client.py | 15 RESULTs, spread over w1-w3 | manual |

## Demo script (D1 viva, ~3 min)
1. `sudo python3 topology/topology.py` -> `pingall` (N01) and show POX log (N02).
2. Start server on `server`, workers on w1-w3: show `WORKER_UP` lines in POX (N03).
3. Run clients on c1-c3 submitting jobs: show round-robin in server log (N05).
4. Kill w2: show `WORKER_DOWN w2` (N04) and that new jobs skip it.
5. `tcpdump` on server-eth0 filtered to `tcp port 5000` to show JSON JOB/ACK/RESULT.

## Results log (fill during testing)
| ID | Date | Pass/Fail | Notes |
|----|------|-----------|-------|

## Integration contract checklist (confirm with P1 / P2)
- [ ] P2 imports ports/IPs/timeouts from `common.py` (no hardcoding)
- [ ] P2's server uses `decode()`/`make_error()` or equivalent behaviour
- [ ] P2 server connects to POX on 5002 and sends `WORKER_UP/DOWN/LOAD` lines
- [ ] P1's POX listens on 5002 and logs received lines
- [ ] POX reachable from `server` at `CONTROLLER_IP` (set in common.py)
- [ ] All three agree on section 4 messages and section 6 timings
