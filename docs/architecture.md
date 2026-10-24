# Architecture: Distributed Job Queue and Worker Management (Delivery 1)

A distributed job-processing system built from raw TCP sockets, running on a Mininet network whose forwarding is managed by a POX SDN controller.

Related documents: [`protocol.md`](protocol.md) (exact message formats), [`TEST_PLAN.md`](TEST_PLAN.md) (tests and demo script), [`../sdn/README.md`](../sdn/README.md) (network side run instructions).

---

## 1. Objectives

1. **Clients** submit jobs to a central server over TCP and receive an acknowledgment, then the result.
2. The **server** queues jobs, schedules them across **workers** in round-robin order, tracks worker health, and returns results.
3. **Workers** register with the server, execute jobs, send heartbeats, and return results.
4. All traffic runs through a **Mininet** network. A **POX** controller installs the flow rules that let the hosts communicate, and receives status reports from the server so the network side knows about worker state.
5. When a worker fails, the server marks it down, skips it, and requeues the job it was running, so the client still gets a result.

**One-line division of labour:** the server decides *who* does the work; the controller decides *how packets travel*.

---

## 2. Components

| Component | File | Responsibility |
|---|---|---|
| Client | `app/client.py` | Creates a job, sends it to the server, prints the ACK and the RESULT. Several clients can run at once. |
| Server | `app/server.py` | Accepts clients (port 5000) and workers (port 5001) concurrently. Keeps the job queue, assigns jobs round-robin, tracks worker heartbeats, requeues the job of a failed worker, forwards results to the right client, and reports worker status to POX. |
| Worker | `app/worker.py` | Registers with the server, runs jobs, sends heartbeats, returns results. |
| Shared definitions | `common.py` | Ports, addresses, timeouts, and the encode/decode/validation helpers used by all programs and tests. |
| Mininet topology | `topology/topology.py` | Builds the virtual network: 1 switch, 3 clients, 1 server, 3 workers, plus the VM attached as the controller host. |
| SDN controller | POX (`forwarding.l2_learning`) | Learns MAC addresses and installs flow rules so every host can reach every other host. |
| Status listener | `sdn/job_status.py` | POX component that receives `WORKER_UP`, `WORKER_DOWN` and `LOAD` lines from the server on port 5002, logs them, and keeps the current worker table. |

---

## 3. System diagram

```
   clients c1..c3                                   workers w1..w3
         |                                                |
         |  TCP :5000                              TCP :5001
         |  JOB / ACK / RESULT                     REGISTER / HEARTBEAT / JOB / RESULT
         v                                                v
      +------------------------------------------------------+
      |             Mininet switch s1 (OpenFlow 1.0)         |
      +------------------------------------------------------+
         ^                       |                       ^
         | OpenFlow              | TCP :5000/:5001       |
         | (flow rules)          v                       |
         |                 +-----------+                 |
         |                 |  server   |-----------------+
         |                 | 10.0.0.10 |
         |                 +-----------+
         |                       |
         |                       | TCP :5002  (plain text status lines)
         |                       v
         |            +---------------------------+
         +------------|  POX controller           |
                      |  reachable at 10.0.0.254  |
                      |  forwarding.l2_learning   |
                      |  + job_status             |
                      +---------------------------+
```

Two kinds of traffic exist:

- **Data path:** client, server and worker TCP traffic, forwarded by the switch according to the controller's flow rules.
- **Control path:** OpenFlow between the switch and POX, plus the server-to-controller status channel on port 5002.

---

## 4. Topology and addressing

**Delivery 1 (as built):** one switch `s1`, all hosts on `10.0.0.0/24`.

| Host | IP | Role |
|---|---|---|
| c1, c2, c3 | 10.0.0.1, 10.0.0.2, 10.0.0.3 | clients |
| server | 10.0.0.10 | job queue and scheduler |
| w1, w2, w3 | 10.0.0.21, 10.0.0.22, 10.0.0.23 | workers |
| VM (POX host) | 10.0.0.254 | controller side, attached to `s1` |

**Why the VM is attached to the switch.** Mininet hosts run in private network namespaces and cannot reach `127.0.0.1` on the VM, which is where POX runs. The topology script therefore links the VM to `s1` with the address `10.0.0.254`. The server sends its status lines to `10.0.0.254:5002`. The VM is not counted as a host, so `pingall` covers the 7 hosts (42 pings).

**Delivery 2 (planned):** three switches with redundant paths between them, so the controller can reroute traffic when a link fails.

---

## 5. Communication

### 5.1 Ports

| Port | Channel | Listener | Connector |
|---|---|---|---|
| 5000 | client <-> server | server | clients |
| 5001 | server <-> workers | server | workers |
| 5002 | server -> controller | POX | server |
| 6633 | OpenFlow, switch <-> controller | POX | s1 |

### 5.2 Application protocol (ports 5000 and 5001)

UTF-8 JSON, one object per line, terminated by `\n`. Message types: `REGISTER`, `HEARTBEAT`, `JOB`, `RESULT`, `ACK`, `ERROR`. Bad input gets an `ERROR` with a code (`BAD_JSON`, `UNKNOWN_TYPE`, `MISSING_FIELD`, `BAD_VALUE`, `DUPLICATE_JOB`, and others) and the connection stays open. Full field definitions are in [`protocol.md`](protocol.md).

### 5.3 Status channel (port 5002)

Plain text, one command per line, sent by the server and received by POX. POX sends no reply and the server never waits for one.

| Line | Sent when |
|---|---|
| `WORKER_UP w1` | a worker registers |
| `WORKER_DOWN w1` | a worker disconnects, times out, or a send to it fails |
| `LOAD w1 75` | a job is assigned to w1 (busy) |
| `LOAD w1 0` | w1 returns a result (idle) |

If POX is not reachable, the server logs a warning and keeps working.

---

## 6. Communication flows

### 6.1 Startup

1. POX starts and listens for OpenFlow connections (port 6633) and for status lines (port 5002).
2. The topology script builds the network. `s1` connects to POX, which installs forwarding rules as traffic appears, so all hosts become reachable.
3. The server starts listening on 5000 and 5001.
4. Each worker connects to the server on 5001 and sends `REGISTER`. The server replies with an `ACK`, then marks the worker schedulable and sends `WORKER_UP` to POX.
5. Workers send a `HEARTBEAT` every 2 seconds.

### 6.2 One job

1. A client connects to the server on 5000 and sends a `JOB`.
2. The server rejects duplicates and bad input with an `ERROR`; otherwise it replies `ACK` and queues the job.
3. The scheduler picks the next idle, live worker in round-robin order, sends it the `JOB`, and reports `LOAD <worker> 75`.
4. The worker runs the job and sends a `RESULT`.
5. The server reports `LOAD <worker> 0` and forwards the `RESULT` to the client that submitted the job.

### 6.3 Worker failure

1. A worker stops (crashed process, closed connection) or stops sending heartbeats.
2. The server detects it: immediately on disconnect, or when the watchdog sees no heartbeat for 6 seconds.
3. The server removes the worker, sends `WORKER_DOWN` to POX, and puts the worker's unfinished job back on the queue (at the back).
4. The scheduler assigns the job to another worker, and the result still reaches the original client.

### 6.4 Link failure (Delivery 2)

1. A link between switches goes down.
2. The switch reports the port change to POX.
3. POX removes the flow rules that used the link and installs rules along a backup path.
4. TCP connections stall briefly and recover once packets flow again.

---

## 7. Scheduling and failure handling

- **Queue:** jobs wait in a FIFO queue.
- **Round-robin:** `next_worker` is a position in the fixed worker order (`w1, w2, w3`). The server starts after the worker chosen last time and takes the first one that is registered, idle and has a fresh heartbeat. A failed worker is skipped and the rotation carries on from where it was.
- **One job per worker at a time:** a worker is marked busy until it returns its result.
- **Heartbeat:** every 2 seconds from each worker. A worker with no heartbeat for 6 seconds is marked down.
- **Requeue:** each worker's state records the job it is running. When a worker fails, that job goes back on the queue exactly once, even if two failure paths (watchdog and disconnect) fire together.

---

## 8. Expected network behaviour

- At startup every host can reach every other host (`pingall`: 0% dropped, 42/42 received).
- Traffic between clients, server and workers is forwarded by flow rules installed by POX.
- Status lines from the server reach POX at `10.0.0.254:5002` and appear in the POX log.
- (Delivery 2) If a link on an active path fails, traffic moves to a backup path with a brief interruption.

---

## 9. Socket-SDN integration

The application and the network share information in two directions:

- **Application to network:** the server reports worker registration, failure and busy/idle state to POX. POX keeps the latest state of each worker in `core.JobStatus.workers`, for example `{"w1": {"state": "UP", "load": 75}}`.
- **Network to application:** if the network fails, the controller reroutes (Delivery 2), and the server's heartbeat watchdog detects workers that become unreachable.

---

## 10. Design decisions

| Decision | Reason |
|---|---|
| POX as the controller | Pure Python, nothing to compile, and it runs on the team's Ubuntu setup. Ryu needed an older Python than the one installed. |
| OpenFlow 1.0 | The version POX supports. Enough for forwarding and rerouting. |
| JSON, one object per line | Easy to read and debug, and a newline gives a clear message boundary on a TCP byte stream. |
| Separate plain-text status channel | Keeps controller traffic simple to parse and separate from application messages. |
| VM attached to the switch as 10.0.0.254 | The only way for isolated Mininet hosts to reach POX on the VM. |
| Heartbeat 2 s, timeout 6 s | Detects a dead worker within a few seconds without false alarms on a single missed beat. |
| Server-side scheduling, controller-side forwarding | Each part has one job and can be tested separately. |

---

## 11. Verification

- **Automated:** `tests/test_d1.py` has 13 tests covering registration, heartbeats, job ACK and result, round-robin distribution, three concurrent clients, duplicate ids, bad input (JSON, type, field, value), message framing across packets, and a silent worker being skipped with its job requeued. All pass inside the Mininet topology, including the slow failure test.
- **Manual:** `pingall`, three jobs landing on w1, w2, w3, a worker killed, the next job going to the right worker, and POX showing `UP`, `LOAD` and `DOWN` lines. See [`TEST_PLAN.md`](TEST_PLAN.md).

---

## 12. Scope

**Delivery 1 (done):** 1 switch, 3 clients, server, 3 workers; POX forwarding; job queue with round-robin; registration and heartbeats; requeue on worker failure; server-to-controller status channel; automated and manual tests.

**Delivery 2 (planned):** redundant switches and links; load monitoring; dynamic path control and rerouting; link-failure scenario; performance comparison against a static baseline.

## 13. Known limitations

- `LOAD` is a busy/idle flag (75 or 0), not a measured load.
- In Delivery 1 POX records and logs the status messages but does not yet change flow rules because of them.
- With one switch there is no path redundancy.
- Status reports are fire-and-forget: if POX is down, the server logs a warning and continues without retrying.
- POX prints a Python-version warning and harmless `ERROR:packet:(dns)` lines on newer Python versions. Forwarding and the status channel are unaffected.
