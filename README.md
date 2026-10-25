# sdn-job-queue

Distributed job queue with TCP sockets and an SDN controller.

Clients submit jobs to a central server over TCP. The server queues them and hands them to worker nodes in round-robin order. Everything runs on a Mininet network whose forwarding is managed by a POX SDN controller, and the server reports worker status to the controller.

> The server decides *who* does the work. The controller decides *how packets travel*.

```
clients c1..c3 --TCP:5000--> [ switch s1 ] --TCP:5000/5001--> server --TCP:5001--> workers w1..w3
                                   ^                             |
                       OpenFlow    |                             | TCP:5002 (status lines)
                                   +------ POX controller <------+
```

## Team

| Role | Part | Who |
|---|---|---|
| P1 | Mininet topology, POX controller, status listener | Harshith |
| P2 | Client, server, worker, job queue, scheduling | Nikitha |
| P3 | Protocol, integration, tests | Nishad |

## Repository layout

```
sdn-job-queue/
├── README.md          this file
├── common.py          shared ports, addresses, timeouts and message helpers
├── app/               client.py, server.py, worker.py
├── sdn/               job_status.py (POX status listener) and its README
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

**Terminal 1: POX with the forwarding and status components**

```
cd ~/pox
PYTHONPATH=~/sdn-job-queue/sdn python3 pox.py forwarding.l2_learning job_status
```

Expect `Status channel listening on 0.0.0.0:5002` and `POX 0.7.0 (gar) is up.`

**Terminal 2: the network (run from the repo root)**

```
cd ~/sdn-job-queue
sudo python3 topology/topology.py
```

At the `mininet>` prompt, check connectivity:

```
pingall
```

Expect `0% dropped (42/42 received)`.

**Start the server, then the three workers** (paste one line at a time):

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

> At the `mininet>` prompt, a bare host name such as `w1` is replaced by its IP address. That is why the worker id is written `--id=w1`.

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

Expect all 13 `ok` and `OK`. The full test list and the demo script are in [`docs/TEST_PLAN.md`](docs/TEST_PLAN.md).

## Addresses and ports

| Host | IP |
|---|---|
| c1, c2, c3 | 10.0.0.1, 10.0.0.2, 10.0.0.3 |
| server | 10.0.0.10 |
| w1, w2, w3 | 10.0.0.21, 10.0.0.22, 10.0.0.23 |
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

## Status

**Delivery 1:** complete. 1 switch, 3 clients, server, 3 workers, POX forwarding, status channel, automated and manual tests.

**Delivery 2 (planned):** redundant switches and links, load monitoring, dynamic path control and rerouting, link-failure scenario, performance comparison against a static baseline.
