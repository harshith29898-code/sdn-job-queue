# Architecture: Distributed Job Queue and Worker Management (Deliveries 1 and 2)

A distributed job-processing system built from raw TCP sockets, running on a Mininet network whose forwarding is managed by a POX SDN controller. Delivery 2 adds redundant switch topologies, a controller that reacts to link failures, an optional allow-list, and a statistics stream for the GUI.

Related documents: [`protocol.md`](protocol.md) (exact message formats), [`TEST_PLAN.md`](TEST_PLAN.md) (tests and demo script), [`../sdn/README.md`](../sdn/README.md) (network side run instructions).

---

## 1. Objectives

1. **Clients** submit jobs to a central server over TCP and receive an acknowledgment, then the result.
2. The **server** queues jobs, schedules them across **workers**, tracks worker health, and returns results.
3. **Workers** register with the server, execute jobs, send heartbeats, and return results.
4. All traffic runs through a **Mininet** network of 1 to 7 switches. A **POX** controller installs the flow rules that let the hosts communicate, keeps loops from forming, reacts to link failures by recomputing paths, and receives status reports from the server so the network side knows about worker state.
5. When a worker fails, the server marks it down, skips it, and requeues the job it was running, so the client still gets a result.
6. When a link fails and the topology has a spare path, traffic moves to it within a fraction of a second, and an optional allow-list stops unauthorized hosts from reaching the server ports.

**One-line division of labour:** the server decides *who* does the work; the controller decides *how packets travel*.

---

## 2. Components

| Component | File | Responsibility |
|---|---|---|
| Client | `app/client.py` | Creates a job, sends it to the server, prints the ACK and the RESULT. Several clients can run at once. |
| Server | `app/server.py` | Accepts clients (port 5000) and workers (port 5001) concurrently. Keeps the job queue, assigns jobs (the job-ordering algorithm is chosen with `--scheduler`, see section 7), tracks worker heartbeats, requeues the job of a failed worker, forwards results to the right client, and reports worker status to POX. |
| Worker | `app/worker.py` | Registers with the server, runs jobs, sends heartbeats, returns results. |
| Shared definitions | `common.py` | Ports, addresses, timeouts, and the encode/decode/validation helpers used by all programs and tests. |
| Mininet topology | `topology/topology.py` | Builds the virtual network: 1 to 7 switches in a chosen shape (line, ring, star, tree, mesh or custom), 1 to 7 clients, 1 server, 1 to 7 workers, optional bandwidth, delay and loss on the switch links, plus the VM attached as the controller host. |
| SDN controller | `sdn/dynamic_switch.py` (with POX `openflow.discovery` and `openflow.spanning_tree`) | A learning switch that installs flow rules so every host can reach every other host. In `--mode=dynamic` it reacts to link failures; in `--mode=static` it behaves like POX's stock `l2_learning` and is the baseline. Optionally enforces the allow-list. |
| Allow-list rules | `sdn/allowlist_policy.py` | Pure rule logic (which source IPs may open ports 5000 and 5001), used by `dynamic_switch.py`. |
| Status listener | `sdn/job_status.py` | POX component that receives `WORKER_UP`, `WORKER_DOWN` and `LOAD` lines from the server on port 5002, logs them, and keeps the current worker table. |
| Statistics stream | `sdn/switch_stats.py` | POX component that polls every switch for port counters and flow counts and sends one line of JSON about every 2 s to clients on port 5003 (the GUI). |
| Link failure tool | `topology/linkctl.py` | Fails and restores links of the running network from a second terminal, with timestamps that match the POX log. |
| Result analysis | `topology/analyze_flaps.py` | Reads `linkctl.py` output and `ping -D` logs and reports, for each failure, how many pings were lost and the longest silence. |

---

## 3. System diagram

```
   clients c1..c7                                    workers w1..w7
         |                                                  |
         |  TCP :5000                                TCP :5001
         |  JOB / ACK / RESULT                       REGISTER / HEARTBEAT / JOB / RESULT
         v                                                  v
      +-------------------------------------------------------+
      |   Mininet switches s1..sN (OpenFlow 1.0, N = 1 to 7)  |
      |   linked as a line, ring, star, tree, mesh or custom  |
      +-------------------------------------------------------+
         ^                        |                        ^
         | OpenFlow               | TCP :5000/:5001        |
         | (flow rules,           v                        |
         |  statistics)     +-----------+                  |
         |                  |  server   |------------------+
         |                  | 10.0.0.10 |
         |                  +-----------+
         |                        |
         |                        | TCP :5002  (plain text status lines)
         |                        v
         |      +------------------------------------------+
         +------|  POX controller, reachable at 10.0.0.254 |
                |  openflow.discovery                      |
                |  openflow.spanning_tree                  |
                |  dynamic_switch  (dynamic | static,       |
                |                   optional allow-list)   |
                |  job_status                              |
                |  switch_stats  --- TCP :5003 ---> GUI    |
                +------------------------------------------+
```

Three kinds of traffic exist:

- **Data path:** client, server and worker TCP traffic, forwarded by the switches according to the controller's flow rules.
- **Control path:** OpenFlow between the switches and POX, plus the server-to-controller status channel on port 5002.
- **Monitoring path:** the statistics stream from POX to the GUI on port 5003.

---

## 4. Topology and addressing

### 4.1 Addresses

All hosts are on `10.0.0.0/24`. The addresses do not depend on the topology.

| Host | IP | Role |
|---|---|---|
| c1 to c7 | 10.0.0.1 to 10.0.0.7 | clients |
| server | 10.0.0.10 | job queue and scheduler |
| w1 to w7 | 10.0.0.21 to 10.0.0.27 | workers |
| VM (POX host) | 10.0.0.254 | controller side, attached to `s1` |

**Why the VM is attached to the switch.** Mininet hosts run in private network namespaces and cannot reach `127.0.0.1` on the VM, which is where POX runs. The topology script therefore links the VM to `s1` with the address `10.0.0.254`. The server sends its status lines to `10.0.0.254:5002`, and the GUI can read the statistics stream at `10.0.0.254:5003` or `127.0.0.1:5003`. The VM is not counted as a host, so with the default 3 clients, 3 workers and the server, `pingall` covers 7 hosts (42 pings).

### 4.2 Shapes and options

`topology.py` takes `--clients`, `--workers` and `--switches` (1 to 7 each, default 3) and a switch shape with `--topo`:

| Shape | Wiring | Redundant paths? |
|---|---|---|
| `line` | s1-s2-s3-...-sN | no |
| `ring` | a line plus a link from the last switch back to s1 (3 or more switches) | yes |
| `star` | s1 in the middle, every other switch linked to s1 | no |
| `tree` | binary tree rooted at s1 | no |
| `mesh` | every switch linked to every other switch | yes |
| `custom` | exactly the links given with `--links` | depends |

The script rejects a network whose switches are not all connected. `--switches 1` gives the Delivery 1 layout. Host placement is `spread` by default (the server on `s1`; with 3 switches c1 on s1, c2 on s2, c3 on s3, w1 on s2, w2 on s3, w3 on s1) or `single` (everything on `s1`). `--link-bw`, `--link-delay` and `--link-loss` limit the switch-to-switch links. `--dry-run` checks the plan without Mininet, and `--test` runs `pingall`. The full option list is in [`../sdn/README.md`](../sdn/README.md).

While it runs, `topology.py` writes the real links and interface names to `/tmp/sdn-topology-state.json`, which `linkctl.py` reads.

### 4.3 Loops and the spanning tree

Redundant links create loops, and a learning switch floods broadcasts and unknown destinations, so a flooded frame would circle forever. POX's `openflow.spanning_tree` picks a loop-free set of links that connects every switch and blocks flooding on the rest. The blocked links stay up as spares and carry traffic only after a tree link fails and the tree is recomputed. POX builds the tree from the order in which it discovered the links, so which link is blocked can differ between runs. The consequence is that the topology gives resilience, not extra capacity: there is one active path between any two hosts.

---

## 5. Communication

### 5.1 Ports

| Port | Channel | Listener | Connector |
|---|---|---|---|
| 5000 | client <-> server | server | clients |
| 5001 | server <-> workers | server | workers |
| 5002 | server -> controller | POX | server |
| 5003 | controller -> GUI (statistics) | POX | GUI |
| 6633 | OpenFlow, switch <-> controller | POX | the switches |

### 5.2 Application protocol (ports 5000 and 5001)

UTF-8 JSON, one object per line, terminated by `\n`. Message types: `REGISTER`, `HEARTBEAT`, `JOB`, `RESULT`, `ACK`, `ERROR`. Bad input gets an `ERROR` with a code (`BAD_JSON`, `UNKNOWN_TYPE`, `MISSING_FIELD`, `BAD_VALUE`, `DUPLICATE_JOB`, and others) and the connection stays open. Full field definitions are in [`protocol.md`](protocol.md).

### 5.3 Status channel (port 5002)

Plain text, one command per line, sent by the server and received by POX. POX sends no reply and the server never waits for one.

| Line | Sent when |
|---|---|
| `WORKER_UP w1` | a worker registers |
| `WORKER_DOWN w1` | a worker disconnects, times out, or a send to it fails |
| `LOAD w1 100` | a job is assigned to w1 (busy), and when a heartbeat reports w1 as busy |
| `LOAD w1 0` | w1 returns a result (idle), and when a heartbeat reports w1 as idle |

Each worker runs one job at a time, so the load is only ever 0 or 100: it is a busy/idle flag, not a measured load. If POX is not reachable, the server logs a warning and keeps working.

### 5.4 Statistics stream (port 5003)

Every `--interval` seconds (default 2) `switch_stats.py` asks each connected switch for its port counters and its flow count over OpenFlow, then sends one line of JSON to every connected client. A client that connects gets the latest line straight away, and a slow client only ever gets the newest line, so it cannot slow POX down.

```
{"t": 1794071755.243,
 "switches": {"s1": {"dpid": 1, "flows": 7,
                     "ports": {"1": {"rx_pkts": 10, "tx_pkts": 12,
                                     "rx_bytes": 840, "tx_bytes": 1008}}}},
 "links": [{"a": "s1", "a_port": 2, "b": "s2", "b_port": 1, "up": true}]}
```

(Shown on several lines here; on the wire it is one line.) `t` is Unix time, the same clock as the `t=` stamps in the POX log. `links` lists every switch-to-switch link discovery has seen. `up` is true only while discovery sees the link in both directions, so it is the controller's view: in static mode it turns false only after discovery's own timeout. A switch that did not answer in a round carries `"stale": true`.

---

## 6. Communication flows

### 6.1 Startup

1. POX starts and listens for OpenFlow connections (port 6633), for status lines (port 5002) and, with `switch_stats`, for statistics clients (port 5003).
2. The topology script builds the network. The switches connect to POX, which discovers the links, computes the spanning tree and installs forwarding rules as traffic appears, so all hosts become reachable.
3. The server starts listening on 5000 and 5001.
4. Each worker connects to the server on 5001 and sends `REGISTER`. The server replies with an `ACK`, then marks the worker schedulable and sends `WORKER_UP` to POX.
5. Workers send a `HEARTBEAT` every 2 seconds.

### 6.2 One job

1. A client connects to the server on 5000 and sends a `JOB`. The first packet of this connection reaches a switch with no matching rule, so the switch passes it to POX, which installs a flow (and, with the allow-list on, checks the source IP first). The rest of the connection runs through the switch alone.
2. The server rejects duplicates and bad input with an `ERROR`; otherwise it replies `ACK` and queues the job.
3. The scheduler picks the next idle, live worker, sends it the `JOB`, and reports `LOAD <worker> 100`.
4. The worker runs the job and sends a `RESULT`.
5. The server reports `LOAD <worker> 0` and forwards the `RESULT` to the client that submitted the job.

### 6.3 Worker failure

1. A worker stops (crashed process, closed connection) or stops sending heartbeats.
2. The server detects it: immediately on disconnect, or when the watchdog sees no heartbeat for 6 seconds.
3. The server removes the worker, sends `WORKER_DOWN` to POX, and puts the worker's unfinished job back on the queue (at the back).
4. The scheduler assigns the job to another worker, and the result still reaches the original client.

### 6.4 Link failure

1. A link between switches goes down. Each switch reports its port going down to POX in an OpenFlow port-status message.
2. In `--mode=dynamic`, `dynamic_switch.py` reacts at once: it tells discovery to drop the links on that port (stock discovery would wait 10 to 15 s for its own timeout), asks the spanning tree to recompute from the updated link table, and clears every switch's flow table and learned MAC table. The flush runs 0.2 s after the first event, so the events of one change share one flush, and again 1.5 s later, after the spanning tree has moved its ports.
3. Traffic is relearned over the new tree: the first packets of each connection go through POX again, and the rules are reinstalled. TCP segments lost during the gap are retransmitted, so connections normally survive.
4. In `--mode=static`, none of this happens. POX learns about the failure only when discovery times out, and the old flow entries (up to 30 s) keep sending traffic to the dead link.
5. When the link returns, discovery detects it, the tree is recomputed, and the flush runs again.

On the 3-switch ring and the 4-switch mesh, dynamic mode recovered in 0.4 s (one lost ping at a 0.2 s ping interval), while static took 15.3 s or longer. On a line there is no second path, so both modes lose connectivity until the link returns. The numbers are in section 11.

### 6.5 Unauthorized connection (allow-list)

1. With `--allowlist`, only client IPs may open port 5000 and only worker IPs may open port 5001.
2. The first packet of a new connection reaches POX as a packet-in. If its source IP is not allowed for that port, POX installs a 10 s drop entry and logs `BLOCKED <src> -> <dst>:<port>`.
3. The refused SYN never reaches the server, so the TCP handshake never completes and the client's `connect()` times out.
4. Because the check runs on each packet-in rather than at startup, it is still enforced after a link-failure flush.

---

## 7. Scheduling and failure handling

- **Queue:** jobs wait in a FIFO queue.
- **Round-robin:** `next_worker` is a position in the fixed worker order (`w1, w2, w3`). The server starts after the worker chosen last time and takes the first one that is registered, idle and has a fresh heartbeat. A failed worker is skipped and the rotation carries on from where it was.
- **One job per worker at a time:** a worker is marked busy until it returns its result.
- **Heartbeat:** every 2 seconds from each worker. A worker with no heartbeat for 6 seconds is marked down.
- **Requeue:** each worker's state records the job it is running. When a worker fails, that job goes back on the queue exactly once, even if two failure paths (watchdog and disconnect) fire together.
- **Delivery 2:** the job-ordering algorithm is chosen with `--scheduler fcfs|sjf|priority` (default `fcfs`), and `--aging-rate` sets the priority points a waiting job gains per second. The server records the time it sends each job to a worker (`dispatch_timestamp`), so waiting time can be separated from network time.

---

## 8. Expected network behaviour

- At startup every host can reach every other host (`pingall`: 0% dropped, 42/42 received with the default 7 hosts).
- Traffic between clients, server and workers is forwarded by flow rules installed by POX.
- Status lines from the server reach POX at `10.0.0.254:5002` and appear in the POX log.
- If a link on an active path fails and the topology has a spare path, traffic moves to it with a brief interruption: about 0.4 s in dynamic mode, 15 s or more in static mode.
- With the allow-list on, a host that is not on the list cannot complete a connection to port 5000 or 5001, and ping and other ports are unaffected.

---

## 9. Socket-SDN integration

The application and the network share information in two directions:

- **Application to network:** the server reports worker registration, failure and busy/idle state to POX. POX keeps the latest state of each worker in `core.JobStatus.workers`, for example `{"w1": {"state": "UP", "load": 100}}`. POX only logs and records this information. It does not install flow rules from it: the load is only 0 or 100, and the spanning tree gives one active path between two hosts, so there is no routing choice to make from it.
- **Network to application:** when the network fails, the controller reroutes (section 6.4) and the server's heartbeat watchdog detects workers that become unreachable. The statistics stream (section 5.4) gives the GUI a live view of the switches, ports and links.

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
| Own switch (`dynamic_switch.py`) instead of `l2_learning` or `l2_multi` | The stock modules do not react to a link failure until discovery times out, and `l2_multi` dropped every ping on our VM. The own switch keeps `l2_learning`'s forwarding and adds the reaction. |
| Dynamic and static modes in one file | `--mode=static` is the same forwarding without the reaction, so the comparison changes one thing only. |
| Clear all flows and MAC tables on every link change | Simple and safe. The cost is a short relearning period during which traffic goes through POX again. |
| Ask the spanning tree to recompute after discovery's table is up to date | Stock discovery raises "link removed" before it deletes the link, so the tree is recomputed with the dead link still in and can leave a switch cut off. |
| Allow-list checked on each packet-in, not installed at startup | A link-failure flush deletes every flow, so rules installed at startup would disappear. |
| Statistics over a TCP stream of JSON lines, newest line only per slow client | Simple for the GUI to read, and a slow reader cannot slow the controller. |
| Link failures with `ip link set` through `linkctl.py` | The same operation as Mininet's `link s1 s2 down`, but it can run from a script, with timestamps that match the POX log and `ping -D`. |

---

## 11. Verification

**Delivery 1:**

- **Automated:** `tests/test_d1.py` has 13 tests covering registration, heartbeats, job ACK and result, round-robin distribution, three concurrent clients, duplicate ids, bad input (JSON, type, field, value), message framing across packets, and a silent worker being skipped with its job requeued. All pass inside the Mininet topology, including the slow failure test, on a 3-switch ring and on a single switch.
- **Manual:** `pingall`, three jobs landing on w1, w2, w3, a worker killed, the next job going to the right worker, and POX showing `UP`, `LOAD` and `DOWN` lines. See [`TEST_PLAN.md`](TEST_PLAN.md).

**Delivery 2, network side:**

- **Unit tests (no Mininet):** `sdn/test_dynamic_switch.py` (20 checks, fake switches against a real POX), `sdn/test_allowlist.py` (26 tests), `sdn/test_switch_stats.py` (23 tests) and `topology/test_linkctl.py` (7 tests). They test the logic, not Mininet or Open vSwitch.
- **On Mininet:** the topologies ring, line, mesh (4 and 7 switches) and custom, and the link options `--link-delay`, `--link-bw` and `--link-loss`. A 20 ms delay gave about 40 ms round trip, a 10 Mbit/s limit gave 9.32 Mbit/s, and 10% loss on one link gave 18% packet loss.
- **Link failure, measured with `ping -D -i 0.2` to the server and a 15 s failure from `linkctl.py`, single runs:**

| Topology | Dynamic | Static |
|---|---|---|
| 3-switch ring, link in use | 1 ping lost, 0.4 s silence | 15.3 s |
| 3-switch line (control) | about 15.3 s | 15.3 s and 16.1 s |
| 4-switch mesh, link in use | 0.4 s | 30.6 s and 61.3 s |

- **Controls:** with 5% loss on the switch links, 200 pings lost 22.5% (dynamic) and 19% (static), and a worker's own link down for 30 s looked the same in both modes, with the worker getting jobs again afterwards. The controller cannot help in either case.
- **Allow-list:** on the 3-switch ring, allowed clients complete jobs and blocked hosts time out, through failures of each ring link and in both modes. Not tested on the mesh.

---

## 12. Scope

**Delivery 1 (done):** 1 switch, 3 clients, server, 3 workers; POX forwarding; job queue with round-robin; registration and heartbeats; requeue on worker failure; server-to-controller status channel; automated and manual tests.

**Delivery 2, network side (done):** 1 to 7 clients, workers and switches in several shapes; dynamic and static POX modes; scripted link failure and restore; optional allow-list; statistics stream for the GUI; dynamic against static comparison on a ring, a line and a mesh.

**Delivery 2, application, GUI and experiments (in progress):** scheduling algorithms and the load generator, the GUI, and the wider performance experiments. See the notes of their authors in the repository.

## 13. Known limitations

- `LOAD` is a busy/idle flag (100 or 0), not a measured load.
- POX records and logs the status messages but does not change flow rules because of them.
- The spanning tree uses one path between two hosts, so extra links give resilience, not extra capacity. Which link is blocked can differ between runs, so a failure test must use a link that is in use.
- Dynamic mode clears all flows on every link change, so traffic briefly goes through POX again after a failure.
- Static results vary a lot between runs (15 s to 61 s), and all results are single runs measured at a 0.2 s ping interval, so they are indicative, not exact.
- The allow-list checks only the source IP (a spoofed address is not stopped), does not look at the destination, and covers only ports 5000 and 5001.
- In the statistics stream the link state is the controller's view, so in static mode it turns false late.
- Status reports are fire-and-forget: if POX is down, the server logs a warning and continues without retrying.
- POX prints a Python-version warning and harmless `ERROR:packet:(dns)` lines on newer Python versions. Forwarding and the status channel are unaffected.
