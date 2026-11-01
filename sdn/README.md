# SDN / network side (Mininet + POX)

This folder and `topology/` hold the network side of the project: the Mininet topology, the POX controller setup, and the status channel from the job server to the controller.

| File | What it is |
|---|---|
| `topology/topology.py` | Mininet topology: 1 to 7 switches in several shapes, 1 to 7 clients, 1 server, 1 to 7 workers |
| `sdn/job_status.py` | POX component that receives worker-status messages from the server on port 5002 |

## Network layout

Up to 7 switches (`s1` to `s7`) connected to a remote POX controller. All hosts are on `10.0.0.0/24`. The IP plan does not depend on the topology.

| Host | IP |
|---|---|
| c1 to c7 | 10.0.0.1 to 10.0.0.7 |
| server | 10.0.0.10 |
| w1 to w7 | 10.0.0.21 to 10.0.0.27 |
| VM itself (where POX runs) | 10.0.0.254 |

Mininet hosts live in private network namespaces, so they cannot reach `127.0.0.1` on the VM. The topology script therefore attaches the VM to `s1` as `10.0.0.254`. **Programs running inside Mininet hosts must use `10.0.0.254` to reach POX, never `127.0.0.1`.**

## Topology options

| Option | Meaning |
|---|---|
| `--clients N` | 1 to 7 clients (default 3) |
| `--workers N` | 1 to 7 workers (default 3) |
| `--switches N` | 1 to 7 switches (default 3) |
| `--topo SHAPE` | `auto` (default), `line`, `ring`, `star`, `tree`, `mesh`, `custom` |
| `--links "1-2,2-3"` | the links for `--topo custom` (switch numbers, or `s1-s2` style) |
| `--placement MODE` | `spread` (default) or `single` |
| `--link-bw`, `--link-delay`, `--link-loss` | optional limits on switch-to-switch links, e.g. `--link-bw 10 --link-delay 5ms --link-loss 1` |
| `--dry-run` | check the options and print the plan; no root, no POX needed |
| `--test` | wait `--settle` seconds (default 8), run `pingall` up to 3 times, exit 0 if nothing was dropped, 1 otherwise |

### Shapes

| Shape | Wiring | Redundant paths? |
|---|---|---|
| `auto` | 1 switch alone, 2 in a line, 3 or more in a ring | depends |
| `line` | s1-s2-s3-...-sN | no |
| `ring` | a line plus a link from the last switch back to s1 (needs 3 or more) | yes |
| `star` | s1 in the middle, every other switch linked to s1 | no |
| `tree` | binary tree rooted at s1 (s2 and s3 under s1, s4 and s5 under s2, ...) | no |
| `mesh` | every switch linked to every other switch | yes |
| `custom` | exactly the links you give with `--links` | depends |

The script rejects a network whose switches are not all connected, and prints which links are *critical* (their failure would cut the network in two). Use ring, mesh or a custom shape with cycles for rerouting experiments.

### Host placement

- `spread` (default): the server is on `s1`; client `ci` is on switch `((i-1) mod N) + 1`; worker `wi` is on switch `(i mod N) + 1`. With 3 switches this gives c1 on s1, c2 on s2, c3 on s3, w1 on s2, w2 on s3, w3 on s1.
- `single`: every host on `s1`. Handy for debugging, but a failure of a link between switches then changes nothing.

## Ports

| Port | Used for |
|---|---|
| 5000 | client <-> server |
| 5001 | server <-> workers |
| 5002 | server -> POX status channel |
| 6633 | OpenFlow (switch <-> POX, POX default) |

## Requirements

- Ubuntu with Mininet installed (`sudo apt install mininet`)
- POX cloned to `~/pox`: `git clone https://github.com/noxrepo/pox ~/pox`
- This repo cloned to `~/sdn-job-queue`
- System `python3` (the scripts are run with the system Python, not a virtual environment)

POX prints a warning that it officially supports Python 3.6 to 3.9 when run on newer versions. It has worked on Python 3.14 in our tests: one switch with `l2_learning`, and a 3-switch ring with `discovery` + `spanning_tree` + `l2_learning` (0% dropped by `pingall`).

## How to run

Use two terminals.

**Terminal 1: start POX**

With more than one switch, the controller has to stop broadcasts from circling around loops. Start POX with link discovery, spanning tree and the learning switch:

```
cd ~/pox
PYTHONPATH=~/sdn-job-queue/sdn python3 pox.py openflow.discovery openflow.spanning_tree forwarding.l2_learning job_status
```

Expect `Status channel listening on 0.0.0.0:5002`, `POX 0.7.0 (gar) is up.`, and `link detected` lines from discovery once the switches connect.

For a single switch (the Delivery 1 setup) the old command still works:

```
PYTHONPATH=~/sdn-job-queue/sdn python3 pox.py forwarding.l2_learning job_status
```

Always include `openflow.discovery` and `openflow.spanning_tree` when the topology has more than one switch and any loop (ring, mesh, custom with cycles). Without them, `l2_learning` lets broadcasts circulate forever.

`forwarding.l2_multi` is not used: in our test on Python 3.14 it dropped 100% of pings, even on a 3-switch line with no loops.

**Terminal 2: start the topology**

```
cd ~/sdn-job-queue
sudo python3 topology/topology.py
```

This opens the `mininet>` prompt. Examples:

```
python3 topology/topology.py --dry-run --switches 7 --topo mesh        # only check the plan
sudo python3 topology/topology.py --test                                # pingall check, exit code 0 or 1
sudo python3 topology/topology.py --clients 7 --workers 7 --switches 7 --topo mesh
sudo python3 topology/topology.py --switches 4 --topo custom --links "1-2,2-3,3-4,4-1,1-3"
sudo python3 topology/topology.py --switches 1                          # the Delivery 1 layout
```

Start POX first. If a switch cannot reach POX, the topology script prints a warning and nothing will forward. After starting, give the controller a few seconds to discover the links before running traffic.

**Shutting down:** type `exit` at the `mininet>` prompt, then press Ctrl+C in the POX terminal. If Mininet is left in a bad state, run `sudo mn -c`.

## Quick checks

At the `mininet>` prompt:

```
pingall                  # expect: 0% dropped
net                      # shows each switch and what is attached to it
links                    # shows every link and its status
server ping -c 2 w1      # server can reach worker w1
link s1 s2 down          # fail a link (use 'up' to restore it), then run pingall again
```

To test the status channel by hand (this plays the part of the server):

```
server bash -c 'echo "WORKER_UP w1" > /dev/tcp/10.0.0.254/5002'
```

POX should log `Worker w1 is UP`.

Note: when typing at the `mininet>` prompt, Mininet replaces a bare host name like `w1` with its IP address. Keep names next to a quote mark, as above, when testing by hand. A real program sending the message is not affected.

## Status channel (server -> controller)

The server opens a TCP connection to `10.0.0.254:5002` and sends plain-text lines. Each line is one message and ends with a newline (`\n`). The server may keep one connection open and send many lines, or connect per message. The controller sends no reply.

| Message | Meaning |
|---|---|
| `WORKER_UP <worker_id>` | Worker registered or is alive, e.g. `WORKER_UP w1` |
| `WORKER_DOWN <worker_id>` | Worker was marked down (missed heartbeats), e.g. `WORKER_DOWN w1` |
| `LOAD <worker_id> <0-100>` | Worker load figure, e.g. `LOAD w1 75` |

Anything else is logged as `Unrecognized status message` and ignored; it does not crash the controller.

Python example for the server side:

```python
import socket

def send_status(line, host="10.0.0.254", port=5002):
    with socket.create_connection((host, port), timeout=2) as s:
        s.sendall((line + "\n").encode())

send_status("WORKER_UP w1")
```

The controller keeps the latest state of each worker in `core.JobStatus.workers`, a dictionary such as `{"w1": {"state": "UP", "load": 75}}`. The Delivery 2 rerouting logic will read this table.

## Known quirks

- `ERROR:packet:(dns) parsing questions: ord() expected string of length 1, but int found` lines in the POX terminal come from POX's built-in DNS packet parser on newer Python versions. They are harmless and do not affect forwarding or the status channel.

- With `l2_learning`, flow rules learned before a link failure keep pointing at the dead link until they time out. The Delivery 2 rerouting logic has to flush them.

## Not done yet (Delivery 2)

- Rerouting when a link fails, using POX port-status and discovery events
- Using worker load and status to change flow rules
- Flow and port statistics monitoring
- A static-rules baseline mode for the performance comparison
