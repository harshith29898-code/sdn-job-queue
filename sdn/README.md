# SDN / network side (Mininet + POX)

This folder and `topology/` hold the network side of the project: the Mininet topology, the POX controller setup, the status channel from the job server to the controller, link-failure tools, the allow-list and the statistics stream for the GUI.

| File | What it is |
|---|---|
| `topology/topology.py` | Mininet topology: 1 to 7 switches in several shapes, 1 to 7 clients, 1 server, 1 to 7 workers |
| `sdn/job_status.py` | POX component that receives worker-status messages from the server on port 5002 |
| `sdn/dynamic_switch.py` | POX learning switch that reacts to link failures (`--mode=dynamic`) or behaves like `l2_learning` (`--mode=static`, the baseline), with an optional allow-list |
| `sdn/allowlist_policy.py` | The allow-list rules (no POX imports), used by `dynamic_switch.py` |
| `sdn/switch_stats.py` | POX component that streams switch statistics to the GUI on port 5003 |
| `topology/linkctl.py` | Fails and restores links of the running network from a script, with timestamps (see below) |
| `topology/analyze_flaps.py` | Summarises ping silence for each link failure (see "Measuring recovery") |
| `topology/test_linkctl.py` | Tests for `linkctl.py`, no Mininet or root needed |
| `sdn/test_dynamic_switch.py` | Tests for `dynamic_switch.py` using fake switches, no Mininet needed |
| `sdn/test_allowlist.py` | Tests for the allow-list, no Mininet needed |
| `sdn/test_switch_stats.py` | Tests for the statistics stream, no Mininet needed |

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

Because the server is on `s1`, jobs from c1 (and work on w3) never cross a link between switches in the default layout. Use c2 or c3 as the client when you test link failures.

## Ports

| Port | Used for |
|---|---|
| 5000 | client <-> server |
| 5001 | server <-> workers |
| 5002 | server -> POX status channel |
| 5003 | POX -> GUI statistics stream |
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

With more than one switch, the controller has to stop broadcasts from circling around loops and has to cope with link failures. Start POX with link discovery, spanning tree, the dynamic switch and the status channel:

```
cd ~/pox
PYTHONPATH=~/sdn-job-queue/sdn python3 pox.py openflow.discovery openflow.spanning_tree dynamic_switch --mode=dynamic job_status
```

Expect `Dynamic switch started in dynamic mode`, `Status channel listening on 0.0.0.0:5002`, `POX 0.7.0 (gar) is up.`, and `link detected` lines from discovery once the switches connect.

`--mode` chooses how the controller behaves when a link changes:

| Mode | Behaviour |
|---|---|
| `dynamic` | When a switch port goes down or up, discovery is told straight away to drop the links on that port (instead of waiting for its 10-15 s link timeout), spanning tree is told to recompute from the updated link table, and every switch's flow table and learned MAC table are cleared, once right away and once about 1.5 s later. |
| `static` | Never reacts. Same forwarding as `forwarding.l2_learning`. This is the baseline for the performance comparison. |

Every reaction is logged with a Unix timestamp (`t=1730000000.123`), so recovery time can be computed from the POX log.

**Why the spanning tree needs a nudge.** In this POX version, discovery raises its "link removed" event *before* it deletes the link from its own table. Spanning tree recomputes at that moment with the dead link still in, so it can keep the old tree, or even leave a switch cut off with the only link to it blocked (`NO_FLOOD`), until some later link event. `dynamic_switch --mode=dynamic` calls spanning tree's update again once the table is correct. `--mode=static` does not, so it shows stock POX behaviour.

Other ways to start POX that also work:

```
# stock learning switch, multi-switch (what we used before dynamic_switch; no reaction to failures)
PYTHONPATH=~/sdn-job-queue/sdn python3 pox.py openflow.discovery openflow.spanning_tree forwarding.l2_learning job_status

# single switch, the Delivery 1 setup
PYTHONPATH=~/sdn-job-queue/sdn python3 pox.py forwarding.l2_learning job_status
```

Always include `openflow.discovery` and `openflow.spanning_tree` when the topology has more than one switch and any loop (ring, mesh, custom with cycles). Without them, a learning switch lets broadcasts circulate forever.

`forwarding.l2_multi` is not used: in our test on Python 3.14 it dropped 100% of pings, even on a 3-switch line with no loops.

**Optional parts of the POX command**

- `--allowlist` (after `--mode=...`) turns on the allow-list, see "Allow-list" below.
- `switch_stats` (at the end) turns on the statistics stream, see "Statistics stream" below.

```
PYTHONPATH=~/sdn-job-queue/sdn python3 pox.py openflow.discovery openflow.spanning_tree dynamic_switch --mode=dynamic --allowlist job_status switch_stats
```

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

## Measuring recovery after a link failure

**Which link to fail.** Spanning tree blocks some links, and failing a blocked link changes nothing. Which link is blocked depends on the order in which POX discovered the links, so it can differ between runs. Find a link that carries traffic. A port with `NO_FLOOD` is blocked by spanning tree, and the link between two blocked ends is the unused one:

```
mininet> sh for s in s1 s2 s3; do echo == $s; ovs-ofctl -O OpenFlow10 show $s | grep -E "\(s[0-9]-eth|config"; done
```

**By hand.** Ping across the link while failing it (here s1-s3, with c1 on s1 and w2 on s3). `ping -D` stamps each reply with the Unix time, which matches the `t=` stamps in the POX log. Do not press Ctrl+C while it runs.

```
mininet> c1 ping -D -i 0.2 -c 300 w2 > /tmp/run.txt &
mininet> sh sleep 6
mininet> link s1 s3 down
mininet> sh sleep 62
mininet> sh tail -4 /tmp/run.txt
mininet> sh awk '/bytes from/ {gsub(/[][]/,"",$1); t=$1; if (prev && t-prev > 1) printf "gap %.1f s from %.3f to %.3f\n", t-prev, prev, t; prev=t}' /tmp/run.txt
```

The `gap` line is the outage. Repeat with `--mode=static` and `--mode=dynamic` for the comparison. After a run, bring the link back with `link s1 s3 up` and wait about 20 s.

**With `linkctl.py` and `analyze_flaps.py` (several links at once).** Start one ping from each host to the server at the `mininet>` prompt (they run for 10 minutes):

```
c1 ping -D -i 0.2 -c 3000 server > /tmp/run_c1.log 2>&1 &
c2 ping -D -i 0.2 -c 3000 server > /tmp/run_c2.log 2>&1 &
c3 ping -D -i 0.2 -c 3000 server > /tmp/run_c3.log 2>&1 &
```

In a second terminal, fail the links one after another and keep the output:

```
rm -f /tmp/run_flaps.txt
sudo -v
for link in "s1 s2" "s1 s3" "s2 s3"; do
  sudo python3 topology/linkctl.py flap $link --after 5 --for 15 2>&1 | tee -a /tmp/run_flaps.txt
  sleep 20
done
```

Then summarise:

```
python3 topology/analyze_flaps.py /tmp/run_flaps.txt /tmp/run_c*.log
```

For each failure and each ping it prints how many replies were lost and the longest silence between two replies: `no effect` (nothing lost, the failure did not touch that path), `rerouted` (silence of 3 s or less), `slow`, or `no reroute` (silence about as long as the failure or longer). It also says when a ping log does not cover a failure. For `--mode=static` use `sleep 60` instead of `sleep 20` and add `--tail 45` to `analyze_flaps.py`, because static recovers slowly. With a 0.2 s ping interval the resolution is about 0.2 s. Restart POX and Mininet before every static run, so that one run does not carry over into the next.

**Results we measured** (a 15 s failure, single runs, silence = longest gap between replies):

| Topology | Dynamic | Static |
|---|---|---|
| 3-switch ring, link in use | 1 ping lost, 0.4 s | 15.3 s |
| 3-switch line (no alternate path, the control) | about 15.3 s | 15.3 s and 16.1 s |
| 4-switch mesh, link in use | 0.4 s | 30.6 s and 61.3 s |

Dynamic was consistent over six runs (0.4 to 0.7 s). Static varied a lot. With 5% loss on the switch links, 200 pings lost 22.5% (dynamic) and 19% (static), and a worker's own link going down for 30 s looked the same in both modes: the controller cannot help with either.

## Failing links from a script (`linkctl.py`)

`topology.py` writes the real links and interface names of the running network to `/tmp/sdn-topology-state.json` (set `SDN_TOPO_STATE` to change the path) and removes it when it exits. `topology/linkctl.py` reads that file and brings the interfaces up or down with `ip link set`, which is the same thing the Mininet command `link s1 s3 down` does. Run it in a second terminal while `topology.py` is running:

```
sudo python3 topology/linkctl.py list                      # the links of the running network
sudo python3 topology/linkctl.py down s1 s3                # fail a link (also: down s1-s3)
sudo python3 topology/linkctl.py up s1 s3                  # restore it
sudo python3 topology/linkctl.py flap s1 s3 --after 10 --for 30
sudo python3 topology/linkctl.py down c1 s1                # cut a host off (any link works)
sudo python3 topology/linkctl.py down root s1              # cut the VM off from s1 (POX status channel)
```

`flap` waits `--after` seconds, fails the link, waits `--for` seconds, then restores it (it also restores it if you press Ctrl+C during the wait). Add `--log FILE` to append the output to a file, or `--dry-run` to print the commands without running them. `--state FILE` points at a different state file.

Every action prints a Unix timestamp that matches the `t=` stamps in the POX log and the times from `ping -D`:

```
t=1730000000.123 link s1-s3 DOWN (interfaces: s1-eth2, s3-eth2)
```

An experiment script can start a `ping -D` or a job run, call `linkctl.py flap ...`, and compute the outage from the timestamps. Unlike the Mininet CLI, `linkctl.py` can be started from a script or another terminal.

To check the tool itself without Mininet or root: `python3 topology/test_linkctl.py`.

## Allow-list

An optional filter in `dynamic_switch.py` for the "unauthorized access" scenario, off by default. Only client IPs may open TCP port 5000 and only worker IPs may open port 5001. Ping, ARP, port 5002 and the return traffic of allowed connections are not touched.

| Option (after `dynamic_switch`) | Meaning |
|---|---|
| `--allowlist` | turn it on with the default IPs: clients 10.0.0.1 to 10.0.0.7, workers 10.0.0.21 to 10.0.0.27 |
| `--clients=IP,IP,...` | replace the allowed client IPs (turns it on) |
| `--workers=IP,IP,...` | replace the allowed worker IPs (turns it on) |

It works with both `--mode=dynamic` and `--mode=static`.

How it works: POX checks the first packet of each new TCP connection, before anything is flooded or any flow is installed. A refused connection gets a 10 s drop entry in the switch and a `BLOCKED` line in the POX log. The refused SYN never reaches the server, so the handshake never completes and the client's `connect()` times out. Because the check runs on each packet-in rather than at startup, a link-failure flush does not remove it.

To see it work, start POX with `--clients=10.0.0.1,10.0.0.2`, start the server and workers, then at the `mininet>` prompt:

```
c1 python3 -u app/client.py --server 10.0.0.10 --port 5000 --payload-size 1000 --duration 3
c3 python3 -c "import socket; sk = socket.socket(); sk.settimeout(4); print(sk.connect_ex(('10.0.0.10', 5000)))"
```

The first completes a job. The second waits about 4 seconds and prints a non-zero number (`11`), and POX logs `BLOCKED 10.0.0.3 -> 10.0.0.10:5000 ...`. A client trying port 5001 (`c1 ... 5001`) is also blocked.

Limits:

- It checks only the source IP, so a spoofed address is not stopped.
- It does not look at the destination, so non-workers also cannot use port 5001 between themselves. This is why Mininet's built-in `iperf` command hangs while the allow-list is on. Use another port, for example `iperf -s -p 6001` and `iperf -c <ip> -p 6001`.
- `tests/test_d1.py` registers its fake workers from c1, so run it without `--allowlist`.
- Tested on a 3-switch ring (blocked and allowed connections, through failures of each ring link, in both modes). Not tested on the mesh.

## Statistics stream

`switch_stats.py` sends one line of JSON about every 2 s to every client connected to TCP port 5003. The GUI reads this stream. Add `switch_stats` to the end of the POX command (after `openflow.discovery`, which it needs for the link list):

```
PYTHONPATH=~/sdn-job-queue/sdn python3 pox.py openflow.discovery openflow.spanning_tree dynamic_switch --mode=dynamic job_status switch_stats
```

| Option (after `switch_stats`) | Meaning |
|---|---|
| `--port=5003` | the TCP port to listen on |
| `--interval=2` | seconds between lines, at least 0.5 |

Read it from the VM:

```
python3 -c "import socket; f = socket.create_connection(('127.0.0.1', 5003)).makefile(); [print(l.strip()) for l in f]"
```

One line looks like this (shown on several lines here; on the wire it is one line):

```
{"t": 1794071755.243,
 "switches": {"s1": {"dpid": 1, "flows": 7,
                     "ports": {"1": {"rx_pkts": 10, "tx_pkts": 12,
                                     "rx_bytes": 840, "tx_bytes": 1008}}}},
 "links": [{"a": "s1", "a_port": 2, "b": "s2", "b_port": 1, "up": true}]}
```

- `t` is Unix time, the same clock as the `t=` stamps in the POX log and `ping -D`.
- `switches` has one entry per connected switch (`s<N>` is the switch's dpid). The counters are the switch's own, per port; the switch's local port is left out. `flows` is the number of flow entries now, and includes discovery's own LLDP flow.
- `links` lists every switch-to-switch link that discovery has seen. `up` is true only while discovery sees the link in both directions, so it is the controller's view: in `--mode=static` it turns false only after discovery's timeout, in `--mode=dynamic` almost at once. Hosts are not listed.
- `"stale": true` appears on a switch that did not answer this round; its numbers are the previous ones.
- A client that connects gets the latest line straight away. A slow client only ever gets the newest line, so it cannot slow POX down.

Keep it off during performance experiments, so the polling does not affect the results.

## Controller tests (no Mininet)

```
cd ~/sdn-job-queue
python3 sdn/test_dynamic_switch.py
python3 sdn/test_allowlist.py -v
python3 sdn/test_switch_stats.py -v
python3 topology/test_linkctl.py -v
```

`sdn/test_dynamic_switch.py` starts POX itself on port 6633 (stop any running POX first) and connects fake switches to it. It takes about 90 seconds. It checks learning, flow timeouts, that static mode never reacts, that dynamic mode clears flows and learned tables within about a second of a link going down, and that in a 3-switch ring no switch is left cut off after an in-use link fails and comes back. The other three tests use real POX packet classes with fake switch connections (and real localhost sockets for the stream). Expect 20 of 20 checks, then 26, 23 and 7 tests OK. Set `POX_DIR` if POX is not in `~/pox`. They test the logic only, not Mininet or Open vSwitch.

## Status channel (server -> controller)

The server opens a TCP connection to `10.0.0.254:5002` and sends plain-text lines. Each line is one message and ends with a newline (`\n`). The server may keep one connection open and send many lines, or connect per message. The controller sends no reply.

| Message | Meaning |
|---|---|
| `WORKER_UP <worker_id>` | Worker registered or is alive, e.g. `WORKER_UP w1` |
| `WORKER_DOWN <worker_id>` | Worker was marked down (missed heartbeats), e.g. `WORKER_DOWN w1` |
| `LOAD <worker_id> <number>` | Worker load, e.g. `LOAD w1 100`. Each worker runs one job at a time, so the number is 100 when busy and 0 when idle |

Anything else is logged as `Unrecognized status message` and ignored; it does not crash the controller.

Python example for the server side:

```python
import socket

def send_status(line, host="10.0.0.254", port=5002):
    with socket.create_connection((host, port), timeout=2) as s:
        s.sendall((line + "\n").encode())

send_status("WORKER_UP w1")
```

The controller keeps the latest state of each worker in `core.JobStatus.workers`, a dictionary such as `{"w1": {"state": "UP", "load": 100}}`. It only logs and records this: no flow rules are installed from it, because the load is only 0 or 100 and the spanning tree gives one active path between two hosts, so there is no routing choice to make from it.

## Known quirks

- `ERROR:packet:(dns) parsing questions: ord() expected string of length 1, but int found` lines in the POX terminal come from POX's built-in DNS packet parser on newer Python versions. They are harmless and do not affect forwarding or the status channel.

- With the stock `l2_learning`, flow rules and learned MAC entries from before a link failure keep pointing at the dead link (flows only time out after 30 s, and the MAC table is never cleared). `dynamic_switch --mode=dynamic` clears both.

- Which ring or mesh link is blocked by the spanning tree can differ between runs, and in static mode the tree can change after a link failure. Check that the link you fail is in use, and restart POX and Mininet between static runs.

- Mininet's built-in `iperf` command hung on our VM. Measure bandwidth with `iperf -s -p 6001` on one host and `iperf -c <ip> -p 6001 -t 5` on another.

## Not done

- Rules installed from worker load or status (`WORKER_DOWN`, `LOAD`). The load is only 0 or 100 and the network has one active path between two hosts, so there was no routing choice to base on it.
- Load balancing across several paths: the spanning tree uses one path at a time, and the other links are spares used only after a failure.
- Showing the blocked ports of the spanning tree in the statistics stream.
