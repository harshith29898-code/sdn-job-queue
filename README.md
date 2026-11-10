# sdn-job-queue

Distributed job queue with TCP sockets and an SDN controller.

Clients submit jobs to a central server over TCP. The server queues them and hands them to worker nodes. Everything runs on a Mininet network whose forwarding is managed by a POX SDN controller. The server reports worker status to the controller, and the controller reacts to link failures by recomputing paths.

> The server decides *who* does the work. The controller decides *how packets travel*.

```
clients c1..c7 --TCP:5000--> [ switches s1..s7 ] --TCP:5000/5001--> server --TCP:5001--> workers w1..w7
                                     ^                                |
                         OpenFlow    |                                | TCP:5002 (status lines)
                                     +-------- POX controller <-------+
                                                      |
                                                      +-- TCP:5003 (statistics stream) --> GUI
```

The number of clients, workers and switches (1 to 7 each) and the switch layout are chosen when the topology starts.

## Team

| Role | Part | Who |
|---|---|---|
| P1 | Mininet topology, POX controller (dynamic switch, allow-list, statistics stream, status listener), link-failure experiments | Harshith |
| P2 | Client, server, worker, job queue, scheduling | Nikitha |
| P3 | Protocol, integration, tests, docs, GUI framework, experiments | Nishad |

## Repository layout

```
sdn-job-queue/
├── README.md          this file
├── common.py          shared ports, addresses, timeouts and message helpers
├── app/               client.py, server.py, worker.py
├── sdn/               dynamic_switch.py (POX switch, dynamic or static mode, optional allow-list),
│                      allowlist_policy.py (allow-list rules), switch_stats.py (statistics stream),
│                      job_status.py (POX status listener), and the tests
│                      test_dynamic_switch.py, test_allowlist.py, test_switch_stats.py
├── topology/          topology.py (Mininet topology), linkctl.py (fail and restore links),
│                      test_linkctl.py, analyze_flaps.py (summarise ping silence per failure)
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

Two optional parts can be added to this command: `--allowlist` after `--mode=...` blocks unauthorized hosts (see "Allow-list" below), and `switch_stats` at the end streams switch statistics to the GUI (see "Statistics stream" below).

**Terminal 2: the network (run from the repo root)**

```
cd ~/sdn-job-queue
sudo python3 topology/topology.py
```

Run `python3 topology/topology.py --help` for all options. The topology can be checked without Mininet using `--dry-run`. With no options you get 3 clients, 3 workers and 3 switches in a ring.

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

Jobs go to w1, w2, w3 in turn. POX shows `LOAD wN 100` when a job is assigned and `LOAD wN 0` when it finishes.

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
- **static**: stock behaviour. POX discovery announces "link removed" before it deletes the link, so the spanning tree can leave a switch cut off, and stale flows keep pointing at the dead link until they time out.

Measured with `ping -D -i 0.2` to the server while one link was failed for 15 s with `topology/linkctl.py`. "Silence" is the longest gap between two replies. All are single runs.

| Topology | Dynamic | Static |
|---|---|---|
| 3-switch ring, link in use | 1 ping lost, 0.4 s silence | 15.3 s |
| 3-switch line (no alternate path, the control) | about 15.3 s | 15.3 s and 16.1 s |
| 4-switch mesh, link in use | 0.4 s | 30.6 s and 61.3 s |

- The dynamic times were consistent over six runs (0.4 to 0.7 s). The static times varied a lot (15.3 s, 30.6 s, 61.3 s here, and 37.5 s in an earlier run), so read them as typical, not exact. The 0.2 s ping interval limits the resolution to about 0.2 s.
- The line is the control: with no second path nothing can reroute, so both modes lose connectivity for the whole failure.
- POX chooses which ring and mesh links to block when it starts, and the choice differs between runs. Failing a blocked link changes nothing, so check that the link you fail is in use (see [`sdn/README.md`](sdn/README.md), "Measuring recovery").
- In the default layout the server, c1 and w3 are on s1, so jobs from c1 never cross a switch link. Use c2 or c3 as the client when testing link failures.
- The controller cannot help with packet loss or with a worker's own link going down. With 5% loss on the switch links, 200 pings lost 22.5% (dynamic) and 19% (static). A worker's host link failing for 30 s looked the same in both modes, and the worker got jobs again after the link returned.

The 20 unit tests in `sdn/test_dynamic_switch.py` use fake switches and all pass:

```
cd ~/sdn-job-queue
python3 sdn/test_dynamic_switch.py -v
```

## Failing links on a schedule (linkctl)

While Mininet is running, `topology/linkctl.py` fails and restores switch links from a second terminal. It prints Unix timestamps that match the POX `t=` stamps and `ping -D`.

```
python3 topology/linkctl.py list
sudo python3 topology/linkctl.py flap s1 s3 --after 10 --for 30 --log /tmp/flap.log
```

`list` shows the real links and their interface names. `flap A B` waits `--after` seconds, takes the link down for `--for` seconds, then restores it. `down`, `up` and `--dry-run` are also available (see `--help`). To measure recovery, start `ping -D -i 0.2` from a host to the server, run the flap, and take the longest silence between replies. `topology/analyze_flaps.py` does this for several pings and failures at once. Details are in [`sdn/README.md`](sdn/README.md).

## Allow-list (unauthorized access scenario)

An optional filter in `sdn/dynamic_switch.py`, off by default. Only client IPs may open TCP port 5000 and only worker IPs may open port 5001. POX checks the first packet of each new connection. A refused connection gets a 10 s drop entry and a `BLOCKED` line in the POX log.

```
cd ~/pox
PYTHONPATH=~/sdn-job-queue/sdn python3 pox.py openflow.discovery openflow.spanning_tree dynamic_switch --mode=dynamic --allowlist job_status
```

- `--allowlist` uses the default IPs: clients 10.0.0.1 to 10.0.0.7, workers 10.0.0.21 to 10.0.0.27.
- `--clients=10.0.0.1,10.0.0.2` and `--workers=IP,IP` replace the lists and also turn the filter on.
- It works with both `--mode=dynamic` and `--mode=static`.

To see it work with `--clients=10.0.0.1,10.0.0.2`, run a job from c1 (it completes), then try c3:

```
c3 python3 -c "import socket; sk = socket.socket(); sk.settimeout(4); print(sk.connect_ex(('10.0.0.10', 5000)))"
```

It prints a non-zero number (`11`) after about 4 s, and POX logs `BLOCKED 10.0.0.3 -> 10.0.0.10:5000`.

Tested on a 3-switch ring: allowed and blocked connections, through failures of each ring link, and in both modes. Not tested on the mesh.

Limits: it checks only the source IP, so it does not stop a spoofed address, and it does not look at the destination, so non-workers also cannot use port 5001 between themselves (this is why Mininet's built-in `iperf` hangs; use `iperf -s -p 6001` and `iperf -c <ip> -p 6001`). `tests/test_d1.py` registers its fake workers from c1, so run it without `--allowlist`.

## Statistics stream

`sdn/switch_stats.py` sends one line of JSON about every 2 s to every client connected to TCP port 5003. The GUI reads this stream. Add it to the end of the POX command:

```
cd ~/pox
PYTHONPATH=~/sdn-job-queue/sdn python3 pox.py openflow.discovery openflow.spanning_tree dynamic_switch --mode=dynamic job_status switch_stats
```

Options: `--port=5003`, `--interval=2` (seconds, at least 0.5). Read it from the VM:

```
python3 -c "import socket; f = socket.create_connection(('127.0.0.1', 5003)).makefile(); [print(l.strip()) for l in f]"
```

Each line has `t` (Unix time), `switches` (per switch `s1`, `s2`, ...: `flows`, and per port `rx_pkts`, `tx_pkts`, `rx_bytes`, `tx_bytes`) and `links` (each switch link with `"up": true` or `false`). Link state is the controller's view, so in `--mode=static` it turns false only after discovery times out. A switch that did not answer in a round has `"stale": true`. Keep it off during performance experiments.

## Running the tests

With POX and the topology running, at the `mininet>` prompt. Start POX without `--allowlist`. Restart the server before every run, and do **not** start real workers while the tests run (the tests register their own fake workers w1 to w3).

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

Unit tests that need no Mininet (run from the repo root, with POX cloned to `~/pox`; set `POX_DIR` if it is somewhere else):

```
python3 sdn/test_dynamic_switch.py -v
python3 sdn/test_allowlist.py -v
python3 sdn/test_switch_stats.py -v
python3 topology/test_linkctl.py -v
```

Expect 20 of 20 checks, then 26 tests OK, 23 tests OK and 7 tests OK. `sdn/test_dynamic_switch.py` starts POX itself on port 6633, so stop any running POX first, and it takes about 90 seconds.

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
| 5002 | server -> POX status channel (plain text lines: `WORKER_UP w1`, `WORKER_DOWN w1`, `LOAD w1 100`) |
| 5003 | POX -> GUI statistics stream (one line of JSON about every 2 s) |
| 6633 | OpenFlow, switch <-> POX |

Programs running inside Mininet hosts must use `10.0.0.254` to reach POX, never `127.0.0.1`.

## Documentation

- [`docs/architecture.md`](docs/architecture.md): components, diagram, flows, design decisions
- [`docs/protocol.md`](docs/protocol.md): exact message formats, errors, timings
- [`docs/TEST_PLAN.md`](docs/TEST_PLAN.md): test cases and demo script
- [`sdn/README.md`](sdn/README.md): network side details, link failures, allow-list, statistics stream and the status channel

## Known notes

- `ERROR:packet:(dns) parsing ...` lines in the POX terminal come from POX's built-in DNS parser on newer Python versions. They are harmless.
- `LOAD` is a busy/idle flag (100 or 0), not a measured load.
- `forwarding.l2_multi` dropped every ping on our VM, so the switch logic is `sdn/dynamic_switch.py` instead.
- Tested on real Mininet: ring, line, mesh (4 and 7 switches), `--topo custom`, `--link-delay`, `--link-bw` and `--link-loss`.
- Mininet's built-in `iperf` command hung on our VM, so measure bandwidth with `iperf -s -p 6001` and `iperf -c <ip> -p 6001 -t 5`. Port 5001 is refused for non-worker IPs when the allow-list is on.
- The spanning tree uses one path between any two hosts. The other links are spares that are used only after a failure, so the extra links give resilience, not extra capacity.

## Status

**Delivery 1:** complete. 1 switch, 3 clients, server, 3 workers, POX forwarding, status channel, automated and manual tests.

**Delivery 2, network side, done:** variable numbers of clients, workers and switches in several shapes (line, ring, star, tree, mesh, custom); dynamic and static POX modes; scripted link failure and restore; an optional allow-list; a statistics stream for the GUI; dynamic against static results on a ring, a line and a mesh.

**Delivery 2, application and GUI:** scheduling algorithms (`--scheduler fcfs|sjf|priority` with `--aging-rate`) and dynamic worker registration are merged, and priority with aging is still being tested. The load generator, the GUI and the wider performance experiments are in progress.
