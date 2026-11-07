# sdn-job-queue

Distributed job queue with TCP sockets and an SDN controller.

Clients submit jobs to a central server over TCP. The server queues them and hands them to worker nodes. Everything runs on a Mininet network whose forwarding is managed by a POX SDN controller. The server reports worker status to the controller, and the controller reacts to link failures by recomputing paths.

> The server decides *who* does the work. The controller decides *how packets travel*.

```
clients c1..c7 --TCP:5000--> [ switches s1..s7 ] --TCP:5000/5001--> server --TCP:5001--> workers w1..w7
                                     ^                                |
                         OpenFlow    |                                | TCP:5002 (status lines)
                                     +-------- POX controller <-------+
```

The number of clients, workers and switches (1 to 7 each) and the switch layout are chosen when the topology starts.

## Team

| Role | Part | Who |
|---|---|---|
| P1 | Mininet topology, POX controller (dynamic switch, status listener), SDN experiments | Harshith |
| P2 | Client, server, worker, job queue, scheduling | Nikitha |
| P3 | Protocol, integration, tests, docs, GUI framework, experiments | Nishad |

## Repository layout

```
sdn-job-queue/
├── README.md          this file
├── common.py          shared ports, addresses, timeouts and message helpers
├── app/               client.py, server.py, worker.py
├── sdn/               dynamic_switch.py (POX switch, dynamic or static mode),
│                      job_status.py (POX status listener), test_dynamic_switch.py
├── topology/          topology.py (Mininet topology)
├── tests/             test_d1.py, reference_server.py
└── docs/              architecture.md, protocol.md, TEST_PLAN.md
```

## Requirements

- Ubuntu (tested on 26.04 with Python 3.14) in a VM
- Mininet: `sudo apt install mininet`
- POX cloned next to the repo: `git clone https://github.com/noxrepo/pox ~/pox`
- This repo cloned to `~/sdn-job-queue`
- The system `python3` (no virtual environment needed; there are no extra packages)

POX prints a warning that it officially supports Python 3.6 to 3.9. It runs fine on newer versions in our tests.

## Quick start

Use two terminals. Start POX first.

**Terminal 1: POX with discovery, spanning tree, the switch and the status listener**

```
cd ~/pox
PYTHONPATH=~/sdn-job-queue/sdn python3 pox.py openflow.discovery openflow.spanning_tree dynamic_switch --mode=dynamic job_status
```

Expect `Status channel listening on 0.0.0.0:5002` and `POX 0.7.0 (gar) is up.`

`--mode=dynamic` reacts to link failures. `--mode=static` is the stock learning-switch behaviour and is used as the baseline in the performance comparison (see below).

**Terminal 2: the network (run from the repo root)**

```
cd ~/sdn-job-queue
sudo python3 topology/topology.py
```

Run `python3 topology/topology.py --help` for all options. The topology can be checked without Mininet using `--dry-run`.

At the `mininet>` prompt, check connectivity:

```
pingall
```

Expect `0% dropped`.

**Start the server, then the workers** (paste one line at a time):

```
server python3 -u app/server.py > /tmp/server.log 2>&1 &
w1 python3 -u app/worker.py --id=w1 --server 10.0.0.10 --port 5001 > /tmp/w1.log 2>&1 &
w2 python3 -u app/worker.py --id=w2 --server 10.0.0.10 --port 5001 > /tmp/w2.log 2>&1 &
w3 python3 -u app/worker.py --id=w3 --server 10.0.0.10 --port 5001 > /tmp/w3.log 2>&1 &
```

POX should show `Worker w1 is UP`, then w2 and w3.

**Send a job** (repeat to see round-robin):

```
c1 python3 -u app/client.py --server 10.0.0.10 --port 5000 --payload-size 1000 --duration 3
server cat /tmp/server.log
```

Jobs go to w1, w2, w3 in turn. POX shows `LOAD wN 75` when a job is assigned and `LOAD wN 0` when it finishes.

**Kill a worker and send another job:**

```
w1 kill %python3
c1 python3 -u app/client.py --server 10.0.0.10 --port 5000 --payload-size 1000 --duration 3
```

POX shows `Worker w1 is DOWN`, and the next job goes to a live worker (w2 here).

**Shut down:** type `exit` at the `mininet>` prompt, press Ctrl+C in the POX terminal, and run `sudo mn -c` if Mininet is left in a bad state.

> At the `mininet>` prompt, a bare host name such as `w1` is replaced by its IP address. That is why the worker id is written `--id=w1`. Also put separate commands on separate lines: do not chain them with `&`.

## Link failures and the static baseline

On a topology with redundant links (for example a ring of 3 switches), the two POX modes behave differently when a link goes down:

- **dynamic**: drops discovery links at once, recomputes the spanning tree, and flushes flows and MAC tables.
- **static**: stock behaviour. POX discovery announces "link removed" before it deletes the link, so the spanning tree can leave a switch cut off.

Measured on a 3-switch ring, with `ping -D` every 0.2 s and one link failed:

| Mode | Result |
|---|---|
| static | connectivity lost for 37.5 s, or until the run ended |
| dynamic | 1 of 300 packets lost |

This is a small sample (one topology, a few runs), so treat it as an indication, not a benchmark. The 20 unit tests in `sdn/test_dynamic_switch.py` use fake switches and all pass:

```
cd ~/sdn-job-queue
python3 sdn/test_dynamic_switch.py -v
```

## Running the tests

With POX and the topology running, at the `mininet>` prompt. Restart the server before every run, and do **not** start real workers while the tests run (the tests register their own fake workers w1 to w3).

```
server pkill -f app/server.py
server python3 -u app/server.py > /tmp/server.log 2>&1 &
c1 SERVER_HOST=10.0.0.10 python3 tests/test_d1.py -v
```

Expect T01 to T12 `ok`, T13 skipped, and `OK (skipped=1)`. For the slow failure test as well (about 15 seconds), restart the server again and run:

```
c1 RUN_SLOW=1 SERVER_HOST=10.0.0.10 python3 tests/test_d1.py -v
```

Expect all 13 `ok` and `OK`. These pass on a 3-switch ring and on a single switch. The full test list and the demo script are in [`docs/TEST_PLAN.md`](docs/TEST_PLAN.md).

## Addresses and ports

| Host | IP |
|---|---|
| c1 to c7 | 10.0.0.1 to 10.0.0.7 |
| server | 10.0.0.10 |
| w1 to w7 | 10.0.0.21 to 10.0.0.27 |
| VM (where POX runs) | 10.0.0.254 |

| Port | Use |
|---|---|
| 5000 | client <-> server |
| 5001 | server <-> workers |
| 5002 | server -> POX status channel (plain text lines: `WORKER_UP w1`, `WORKER_DOWN w1`, `LOAD w1 75`) |
| 6633 | OpenFlow, switch <-> POX |

Programs running inside Mininet hosts must use `10.0.0.254` to reach POX, never `127.0.0.1`.

## Documentation

- [`docs/architecture.md`](docs/architecture.md): components, diagram, flows, design decisions
- [`docs/protocol.md`](docs/protocol.md): exact message formats, errors, timings
- [`docs/TEST_PLAN.md`](docs/TEST_PLAN.md): test cases and demo script
- [`sdn/README.md`](sdn/README.md): network side details and the status channel

## Known notes

- `ERROR:packet:(dns) parsing ...` lines in the POX terminal come from POX's built-in DNS parser on newer Python versions. They are harmless.
- `LOAD` is a busy/idle flag (75 or 0), not a measured load.
- `forwarding.l2_multi` dropped every ping on our VM, so the switch logic is `sdn/dynamic_switch.py` instead.
- Tested on real Mininet: link failure on a 7-switch mesh, `--topo custom`, `--link-delay`, `--link-bw` and `--link-loss`. Mininet's built-in `iperf` command hung on our VM, so measure bandwidth with `iperf -s -p 6001` and `iperf -c <ip> -p 6001 -t 5`. Port 5001 is refused for non-worker IPs when the allow-list is on.

## Status

**Delivery 1:** complete. 1 switch, 3 clients, server, 3 workers, POX forwarding, status channel, automated and manual tests.

**Delivery 2, done:** variable numbers of clients, workers and switches; topology shapes (line, ring, star, tree, mesh, custom); dynamic and static POX modes; link-failure comparison on a 3-switch ring.

**Delivery 2, in progress:** scripted link failure and restore, scheduling algorithms and dynamic worker registration, flow and port statistics, allow-list filtering, GUI, wider performance evaluation.
