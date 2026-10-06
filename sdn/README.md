# SDN / network side (Mininet + POX)

This folder and `topology/` hold the network side of the project: the Mininet topology, the POX controller setup, and the status channel from the job server to the controller.

| File | What it is |
|---|---|
| `topology/topology.py` | Mininet topology: 1 switch, 3 clients, 1 server, 3 workers |
| `sdn/job_status.py` | POX component that receives worker-status messages from the server on port 5002 |

## Network layout

One switch (`s1`) connected to a remote POX controller. All hosts are on `10.0.0.0/24`.

| Host | IP |
|---|---|
| c1, c2, c3 | 10.0.0.1, 10.0.0.2, 10.0.0.3 |
| server | 10.0.0.10 |
| w1, w2, w3 | 10.0.0.21, 10.0.0.22, 10.0.0.23 |
| VM itself (where POX runs) | 10.0.0.254 |

Mininet hosts live in private network namespaces, so they cannot reach `127.0.0.1` on the VM. The topology script therefore attaches the VM to `s1` as `10.0.0.254`. **Programs running inside Mininet hosts must use `10.0.0.254` to reach POX, never `127.0.0.1`.**

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

POX prints a warning that it officially supports Python 3.6 to 3.9 when run on newer versions. It has worked on Python 3.14 in our tests.

## How to run

Use two terminals.

**Terminal 1: start POX with the forwarding and status components**

```
cd ~/pox
PYTHONPATH=~/sdn-job-queue/sdn python3 pox.py forwarding.l2_learning job_status
```

Expect a line like `Status channel listening on 0.0.0.0:5002` and `POX 0.7.0 (gar) is up.`

**Terminal 2: start the topology**

```
cd ~/sdn-job-queue
sudo python3 topology/topology.py
```

This opens the `mininet>` prompt. Useful options:

```
sudo python3 topology/topology.py --test                   # run pingall once and exit
sudo python3 topology/topology.py --clients 2 --workers 4  # change host counts
```

Start POX first. If the switch cannot reach POX, the topology script prints a warning and nothing will forward.

**Shutting down:** type `exit` at the `mininet>` prompt, then press Ctrl+C in the POX terminal. If Mininet is left in a bad state, run `sudo mn -c`.

## Quick checks

At the `mininet>` prompt:

```
pingall                  # expect: 0% dropped (42/42 received)
server ping -c 2 w1      # server can reach worker w1
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

## Not done yet (Delivery 2)

- Redundant switches and links, and rerouting when a link fails
- Using worker load and status to change flow rules
