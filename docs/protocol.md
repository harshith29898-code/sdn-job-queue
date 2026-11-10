# protocol.md — Distributed Job Queue & Worker Management (Delivery 1, extended for Delivery 2)

Owner: P3. Status: **PROPOSED — agree as a team before coding against it.**
Core idea: the **SERVER** decides *who* does the work; the **CONTROLLER (POX)** decides *how packets travel*.

## 1. Topology and addressing (from architecture)

| Node   | IP           | Role                  |
|--------|--------------|-----------------------|
| c1     | 10.0.0.1/24  | client                |
| c2     | 10.0.0.2/24  | client                |
| c3     | 10.0.0.3/24  | client                |
| server | 10.0.0.10/24 | job queue + scheduler |
| w1     | 10.0.0.21/24 | worker                |
| w2     | 10.0.0.22/24 | worker                |
| w3     | 10.0.0.23/24 | worker                |

D1: 1 switch, POX attached to the switch. D2 adds multiple switches and redundant paths.

## 2. TCP ports

| Port | Channel              | Listener   | Connector      |
|------|----------------------|------------|----------------|
| 5000 | client <-> server    | server     | clients        |
| 5001 | server <-> workers   | server     | workers        |
| 5002 | server <-> controller| POX        | server         |

All connections are long-lived TCP. The server listens on `0.0.0.0` for 5000 and 5001. Concurrent clients are handled with one thread (or task) per connection.

## 3. Framing and encoding (ports 5000, 5001)

- UTF-8 JSON, **one JSON object per line**, terminated by `\n`. No embedded newlines.
- Every message has a string field `"type"`.
- Maximum line length: 4096 bytes. Longer lines are an error.
- Receivers must buffer partial reads and split on `\n` (TCP is a byte stream, not a message stream).
- Unknown extra fields are ignored.

## 4. Message types

`REGISTER, HEARTBEAT, JOB, RESULT, ACK, ERROR`

### 4.1 JOB (client -> server, and server -> worker)
```json
{"type":"JOB","job_id":101,"payload_size":1000,"duration":5}
```
| Field        | Type  | Rules                                              |
|--------------|-------|----------------------------------------------------|
| job_id       | int   | > 0, globally unique (see 5.1)                     |
| payload_size | int   | 0..1000000, size of dummy payload / work units     |
| duration     | int/float | 0 < duration <= 60, seconds of CPU work        |
| priority     | int   | optional, 0..10, default 0 (10 = most urgent). Only the `priority` scheduler uses it (see 5.2) |

When the server forwards a job to a worker it adds one field, `dispatch_timestamp` (Unix time in seconds, a float, taken just before sending). Workers ignore it. The same time is printed in the server log line `Job <id> -> <worker> (dispatched at <time>)`, so waiting time (arrival to dispatch) can be worked out from the log.

Job semantics: the worker performs a CPU-bound busy loop for `duration` seconds, scaled by `payload_size` work (implementation detail owned by P2; the result must not depend on it).

### 4.2 ACK (server -> client, server -> worker)
Confirms receipt/acceptance, **not** completion.
```json
{"type":"ACK","ref":"JOB","job_id":101}
{"type":"ACK","ref":"REGISTER","worker_id":"w1"}
```

### 4.3 RESULT (worker -> server, then server -> client)
```json
{"type":"RESULT","job_id":101,"worker_id":"w1","status":"completed"}
```
`status` is `completed` or `failed`. The server forwards the RESULT unchanged to the client that submitted the job.

### 4.4 REGISTER (worker -> server)
```json
{"type":"REGISTER","worker_id":"w1"}
```
`worker_id` is one of `w1,w2,w3`. Server replies ACK. A repeated REGISTER with the same id replaces the old connection.

### 4.5 HEARTBEAT (worker -> server)
```json
{"type":"HEARTBEAT","worker_id":"w1","load":0}
```
`load` = number of jobs currently running on that worker (int >= 0). **No reply** is sent (avoids chatter).

### 4.6 ERROR (any direction)
```json
{"type":"ERROR","code":"BAD_JSON","message":"could not parse line"}
```
| code            | Meaning                                         |
|-----------------|-------------------------------------------------|
| BAD_JSON        | line is not valid JSON / not an object          |
| UNKNOWN_TYPE    | `type` missing or not in the list               |
| MISSING_FIELD   | required field absent                           |
| BAD_VALUE       | field has wrong type or is out of range         |
| DUPLICATE_JOB   | `job_id` already known                          |
| NO_WORKERS      | reserved; used by `tests/reference_server.py` 
                    only. The real server accepts the job and keeps  
                    it queued until a worker registers              |
| TOO_LONG        | line over 4096 bytes                            |

An ERROR never closes the connection; the sender may continue. Include `"job_id"` in the ERROR when the offending message had one.

## 5. Flows

```
Client --JOB--> Server --ACK--> Client
Server --JOB--> Worker --RESULT--> Server --RESULT--> Client
```
Worker startup: `Worker --REGISTER--> Server --ACK--> Worker`, then HEARTBEAT every 2 s.

### 5.1 job_id allocation
The client generates a unique positive integer `job_id` for each job. The server rejects a repeated id with `DUPLICATE_JOB`.

### 5.2 Scheduling
Each worker runs **one job at a time**. Two decisions are made whenever a worker is idle and a job is waiting:

1. **Which worker:** the next idle, **UP** worker in round-robin order (w1, w2, w3, w1, ...). Down or busy workers are skipped.
2. **Which job:** chosen from the waiting jobs by the algorithm given to the server with `--scheduler`:

| Algorithm | Picks the job with | Notes |
|-----------|--------------------|-------|
| `fcfs` (default) | the earliest arrival | first come, first served |
| `sjf` | the smallest `duration` (ties: earliest arrival) | short jobs go first; long jobs can wait a long time |
| `priority` | the highest `priority + waiting_seconds x aging_rate` (ties: earliest arrival) | `--aging-rate` sets the points gained per second of waiting (default 1.0, must be >= 0) |

**Aging** means a job's score rises the longer it waits, so a low-priority job eventually overtakes newer high-priority jobs and cannot be starved. With `--aging-rate 0` the highest priority always wins. If every job has the same priority, `priority` behaves like `fcfs`.

## 6. Heartbeat and failure rules

- Worker sends HEARTBEAT every **2 s**.
- Server marks a worker **DOWN** after **~6 s** with no heartbeat (or on TCP disconnect).
- On DOWN: server requeues that worker's unfinished job. The job keeps its original arrival order and arrival time, so under `fcfs` it is served before newer jobs (it was already waiting) and under `priority` its aging is not reset.
- A DOWN worker that re-REGISTERs becomes UP again.

## 7. Server <-> Controller status channel (port 5002)

Plain text, one command per line, `\n`-terminated. Server is the TCP client; POX listens.

| Line              | Sent by | Meaning                                    |
|-------------------|---------|--------------------------------------------|
| `WORKER_UP w1`    | server  | worker registered / came back              |
| `WORKER_DOWN w1`  | server  | worker missed heartbeats or disconnected   |
| `LOAD w1 100`     | server  | load of w1, integer 0-100: 0 = idle, 100 = busy (it runs one job at a time) |

POX sends no reply. The server must not wait for one.

The server sends `LOAD` when it hands a job to a worker, when the worker finishes, and for each heartbeat (the worker reports how many jobs it is running, and the server sends that number x 100, capped at 100). Each line is sent on its own short connection to port 5002, by a background sender thread so a slow or unreachable POX never delays scheduling (if more than 500 lines are waiting, new ones are dropped). D1 requirement: POX receives and logs these lines. D2 uses them to drive routing decisions. If POX is unreachable the server logs a warning and keeps working.

## 8. D1 scope vs D2

**D1:** 1 switch, 3 clients, 1 server, 3 workers, POX connected, job queue, round-robin, REGISTER + HEARTBEAT, server->controller status, end-to-end test.
**D2:** multiple switches/redundant paths, load monitoring, dynamic path control, link/worker failure handling, rerouting, requeue/redistribute, performance experiments.

D2 changes to this protocol: optional `priority` in JOB (4.1), `dispatch_timestamp` added by the server (4.1), selectable scheduling algorithms (5.2), and `LOAD` values of 0 or 100 (7).

## 10. Test and measurement tools

| Tool | What it does |
|------|--------------|
| `tests/test_d1.py` | 13 protocol tests (registration, ACK/RESULT, round-robin, bad input, TCP framing, worker failure). Run it against a fresh server with no real workers running. |
| `tests/test_scheduling.py` | 10 tests that start the real server with each algorithm and check the order jobs are picked (fcfs, sjf, priority, aging) and that bad `priority` or `--aging-rate` values are rejected. Needs no Mininet or POX. |
| `app/load_generator.py` | Sends jobs at random intervals (uniform or exponential gaps, optional long idle gap, random priorities, fixed `--seed` for repeatable runs) and writes one CSV row per job: `label, run_id, seq, job_id, planned_gap_s, duration_s, priority, payload_size, submit_time, ack_time, result_time, worker_id, status, error, ack_latency_s, completion_s, extra_delay_s`. Times are Unix seconds, so they line up with POX `t=` stamps and `topology/linkctl.py --log`. |

## 9. Shared definitions
All constants and encode/decode helpers live in `common.py`. Do not hardcode IPs, ports or timeouts elsewhere: `from common import *`.
