#!/usr/bin/env python3
"""
topology.py - Mininet topology for the Distributed Job Queue project (Delivery 2).

The number of clients, workers and switches is configurable (1 to 7 each), and the
switches can be wired in several shapes so the network has multiple routes.

IP plan (unchanged from Delivery 1, all on 10.0.0.0/24):
      c1..c7   10.0.0.1  - 10.0.0.7
      server   10.0.0.10
      w1..w7   10.0.0.21 - 10.0.0.27
      the VM   10.0.0.254   (attached to s1, so hosts can reach POX at 10.0.0.254)

Switch shapes (--topo):
      line    s1-s2-s3-...-sN                  no redundant path
      ring    line plus a link sN-s1 (N >= 3)  two paths between any pair
      (default "auto": one switch alone, two in a line, three or more in a ring)
      star    s1 in the middle, others on it   no redundant path
      tree    binary tree rooted at s1         no redundant path
      mesh    every switch linked to every other
      custom  your own links, e.g. --links "1-2,2-3,3-1,3-4"

Host placement (--placement):
      spread  server on s1; clients and workers spread round-robin over the switches
      single  every host on s1 (useful for debugging)

Usage (needs root for the real network, uses the system python3):
    python3 topology/topology.py --dry-run                  # validate and print the plan, no root needed
    sudo python3 topology/topology.py                       # 3 switches (a ring), 3 clients, 3 workers
    sudo python3 topology/topology.py --clients 7 --workers 7 --switches 7 --topo mesh
    sudo python3 topology/topology.py --switches 4 --topo custom --links "1-2,2-3,3-4,4-1,1-3"
    sudo python3 topology/topology.py --test                # wait, run pingall (retrying), exit 0 or 1
    sudo python3 topology/topology.py --switches 1          # the Delivery 1 layout

Start POX first, in another terminal (see sdn/README.md):
    cd ~/pox
    PYTHONPATH=~/sdn-job-queue/sdn python3 pox.py openflow.discovery openflow.spanning_tree \
        forwarding.l2_learning job_status
"""
import argparse
import itertools
import os
import sys
import time

SERVER_IP = "10.0.0.10"
CONTROLLER_HOST_IP = "10.0.0.254"  # the VM's own address on the Mininet network
CLIENT_IP_BASE = 0    # c1 -> 10.0.0.1, c2 -> 10.0.0.2, ...
WORKER_IP_BASE = 20   # w1 -> 10.0.0.21, w2 -> 10.0.0.22, ...
PREFIX_LEN = 24

MAX_NODES = 7         # maximum clients, workers and switches
SHAPES = ("line", "ring", "star", "tree", "mesh", "custom")
DEFAULT_SHAPE = "auto"   # 1 switch: none, 2 switches: line, 3 or more: ring
PLACEMENTS = ("spread", "single")


class TopologyError(Exception):
    """A bad option combination (reported to the user without a traceback)."""


# ---------------------------------------------------------------------------
# Pure logic (no Mininet needed): shapes, validation, host placement
# ---------------------------------------------------------------------------

def client_ip(i):
    return "10.0.0.%d" % (CLIENT_IP_BASE + i)


def worker_ip(i):
    return "10.0.0.%d" % (WORKER_IP_BASE + i)


def check_counts(clients, workers, switches):
    for name, value in (("clients", clients), ("workers", workers), ("switches", switches)):
        if not 1 <= value <= MAX_NODES:
            raise TopologyError("--%s must be between 1 and %d (got %d)" % (name, MAX_NODES, value))


def parse_links(text, n):
    """Parse '1-2,2-3' or 's1-s2,s2-s3' into sorted (a, b) pairs, checked against n switches."""
    if not text:
        raise TopologyError("--topo custom needs --links, e.g. --links \"1-2,2-3,3-1\"")
    links = []
    for token in text.replace(" ", "").lower().split(","):
        if not token:
            continue
        parts = token.replace("s", "").split("-")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise TopologyError("bad link %r in --links (use the form 1-2 or s1-s2)" % token)
        a, b = int(parts[0]), int(parts[1])
        if a == b:
            raise TopologyError("link %r joins a switch to itself" % token)
        if not (1 <= a <= n and 1 <= b <= n):
            raise TopologyError("link %r uses a switch outside s1..s%d" % (token, n))
        link = (min(a, b), max(a, b))
        if link in links:
            raise TopologyError("link %r is listed twice" % token)
        links.append(link)
    return links


def resolve_shape(shape, n):
    """Turn the default 'auto' into a real shape for n switches."""
    if shape != DEFAULT_SHAPE:
        return shape
    if n == 1:
        return "single"
    return "line" if n == 2 else "ring"


def shape_links(shape, n, custom=None):
    """Return the list of switch-to-switch links (1-based, a < b) for a shape."""
    if shape == "single":
        return []
    if shape == "custom":
        return parse_links(custom, n)
    if custom:
        raise TopologyError("--links only works with --topo custom")
    if shape == "line":
        return [(i, i + 1) for i in range(1, n)]
    if shape == "ring":
        if n < 3:
            raise TopologyError("a ring needs at least 3 switches (got %d)" % n)
        return [(i, i + 1) for i in range(1, n)] + [(1, n)]
    if shape == "star":
        return [(1, i) for i in range(2, n + 1)]
    if shape == "tree":
        return [(i // 2, i) for i in range(2, n + 1)]
    if shape == "mesh":
        return list(itertools.combinations(range(1, n + 1), 2))
    raise TopologyError("unknown topology shape %r" % shape)


def _connected(n, links):
    if n <= 1:
        return True
    adj = {i: set() for i in range(1, n + 1)}
    for a, b in links:
        adj[a].add(b)
        adj[b].add(a)
    seen, stack = {1}, [1]
    while stack:
        for nxt in adj[stack.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return len(seen) == n


def analyze(n, links):
    """Check the switch graph. Raises if it is split in two; otherwise reports
    which links are critical (their failure would cut the network in two)."""
    if not _connected(n, links):
        raise TopologyError("the switches are not all connected to each other; "
                            "add links so every switch can reach every other")
    critical = [l for l in links if not _connected(n, [x for x in links if x != l])]
    return {"links": links, "critical": critical, "redundant": n > 1 and len(critical) < len(links)}


def place_hosts(clients, workers, n, mode):
    """Return {host name: switch number}. See the module docstring for the rules."""
    if mode not in PLACEMENTS:
        raise TopologyError("unknown placement %r" % mode)
    plan = {"server": 1}
    for i in range(1, clients + 1):
        plan["c%d" % i] = 1 if mode == "single" else ((i - 1) % n) + 1
    for i in range(1, workers + 1):
        plan["w%d" % i] = 1 if mode == "single" else (i % n) + 1
    return plan


def make_plan(clients, workers, switches, shape, links_text=None, placement="spread"):
    check_counts(clients, workers, switches)
    shape = resolve_shape(shape, switches)
    links = shape_links(shape, switches, links_text)
    info = analyze(switches, links)
    return {
        "clients": clients, "workers": workers, "switches": switches, "shape": shape,
        "placement": placement, "links": info["links"], "critical": info["critical"],
        "redundant": info["redundant"], "hosts": place_hosts(clients, workers, switches, placement),
    }


def describe(plan):
    """Return a printable summary of the plan."""
    fmt = lambda ls: ", ".join("s%d-s%d" % l for l in ls) or "none"
    lines = [
        "Topology plan: %d switch(es), %d client(s), %d worker(s), shape=%s, placement=%s" % (
            plan["switches"], plan["clients"], plan["workers"], plan["shape"], plan["placement"]),
        "  switch links : " + fmt(plan["links"]),
    ]
    if plan["switches"] > 1:
        if plan["redundant"]:
            lines.append("  redundant    : yes, a link failure can be rerouted unless it is critical")
        else:
            lines.append("  redundant    : NO, any link failure cuts the network in two")
        lines.append("  critical     : " + fmt(plan["critical"]))
    by_switch = {}
    for host, sw in plan["hosts"].items():
        by_switch.setdefault(sw, []).append(host)
    for sw in sorted(by_switch):
        lines.append("  s%-2d hosts    : %s" % (sw, " ".join(by_switch[sw])))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Mininet part (imports Mininet only when it is really needed)
# ---------------------------------------------------------------------------

def build_network(num_clients, num_workers, controller_ip, controller_port, attach_root=True,
                  num_switches=1, shape=DEFAULT_SHAPE, links_text=None, placement="spread",
                  link_params=None):
    """Create (but do not start) the Mininet network.

    The first four arguments and attach_root are the same as in Delivery 1; with the
    default num_switches=1 the result is the Delivery 1 layout.
    link_params: optional dict for switch-to-switch links, e.g. {"bw": 10, "delay": "5ms", "loss": 1}.
    """
    from mininet.log import info
    from mininet.net import Mininet
    from mininet.node import Node, OVSSwitch, RemoteController

    plan = make_plan(num_clients, num_workers, num_switches, shape, links_text, placement)
    link_params = link_params or {}

    kwargs = {}
    if link_params:
        from mininet.link import TCLink
        kwargs["link"] = TCLink
    net = Mininet(controller=None, switch=OVSSwitch, autoSetMacs=True, **kwargs)

    info("*** Adding remote controller (POX)\n")
    net.addController("c0", controller=RemoteController, ip=controller_ip, port=controller_port)

    info("*** Adding %d switch(es)\n" % plan["switches"])
    switches = {}
    for i in range(1, plan["switches"] + 1):
        # POX (gar) speaks OpenFlow 1.0
        switches[i] = net.addSwitch("s%d" % i, protocols="OpenFlow10")

    info("*** Linking switches (%s)\n" % plan["shape"])
    for a, b in plan["links"]:
        net.addLink(switches[a], switches[b], **link_params)

    info("*** Adding server\n")
    server = net.addHost("server", ip="%s/%d" % (SERVER_IP, PREFIX_LEN))
    net.addLink(server, switches[plan["hosts"]["server"]])

    info("*** Adding clients\n")
    for i in range(1, num_clients + 1):
        host = net.addHost("c%d" % i, ip="%s/%d" % (client_ip(i), PREFIX_LEN))
        net.addLink(host, switches[plan["hosts"]["c%d" % i]])

    info("*** Adding workers\n")
    for i in range(1, num_workers + 1):
        host = net.addHost("w%d" % i, ip="%s/%d" % (worker_ip(i), PREFIX_LEN))
        net.addLink(host, switches[plan["hosts"]["w%d" % i]])

    if attach_root:
        # Hosts live in private network namespaces, so they cannot reach programs
        # running on the VM itself (like POX). Attach the VM to s1 with its own IP
        # so the server can open the status channel to POX at CONTROLLER_HOST_IP.
        info("*** Attaching the VM (POX host) to s1 as %s\n" % CONTROLLER_HOST_IP)
        root = Node("root", inNamespace=False)
        link = net.addLink(root, switches[1])
        root.setIP(CONTROLLER_HOST_IP, prefixLen=PREFIX_LEN, intf=link.intf1)

    return net


def set_link_state(net, a, b, up):
    """Bring the link between switches a and b (numbers) up or down.
    Same as typing 'link s1 s2 down' at the mininet> prompt; for failure scripts."""
    net.configLinkStatus("s%d" % a, "s%d" % b, "up" if up else "down")


def run_ping_test(net, settle, attempts=3):
    """Wait for the controller to learn the topology, then pingall until 0% is dropped."""
    from mininet.log import info
    info("*** Waiting %d s for the controller to discover the topology\n" % settle)
    time.sleep(settle)
    dropped = 100.0
    for n in range(1, attempts + 1):
        info("*** pingall, attempt %d of %d\n" % (n, attempts))
        dropped = net.pingAll()
        if dropped == 0:
            return True
    return False


def parse_args():
    p = argparse.ArgumentParser(
        description="Mininet topology for the job queue project",
        epilog="Shapes: %s. Placement: %s." % (", ".join(SHAPES), ", ".join(PLACEMENTS)))
    p.add_argument("--clients", type=int, default=3, help="number of clients, 1-%d (default 3)" % MAX_NODES)
    p.add_argument("--workers", type=int, default=3, help="number of workers, 1-%d (default 3)" % MAX_NODES)
    p.add_argument("--switches", type=int, default=3, help="number of switches, 1-%d (default 3)" % MAX_NODES)
    p.add_argument("--topo", choices=(DEFAULT_SHAPE,) + SHAPES, default=DEFAULT_SHAPE,
                   help="switch shape (default auto: ring for 3+ switches, line for 2)")
    p.add_argument("--links", help="for --topo custom: switch links such as \"1-2,2-3,3-1\"")
    p.add_argument("--placement", choices=PLACEMENTS, default="spread", help="host placement (default spread)")
    p.add_argument("--link-bw", type=float, help="bandwidth limit in Mbit/s on switch-to-switch links")
    p.add_argument("--link-delay", help="delay on switch-to-switch links, e.g. 5ms")
    p.add_argument("--link-loss", type=float, help="packet loss percent on switch-to-switch links")
    p.add_argument("--controller-ip", default="127.0.0.1", help="POX controller IP")
    p.add_argument("--controller-port", type=int, default=6633, help="POX OpenFlow port")
    p.add_argument("--no-root", action="store_true",
                   help="do not attach the VM to s1 (no server-to-POX status channel)")
    p.add_argument("--dry-run", action="store_true", help="validate and print the plan, then exit (no root needed)")
    p.add_argument("--test", action="store_true", help="run pingall (retrying) and exit with 0 on success, 1 on failure")
    p.add_argument("--settle", type=int, default=8,
                   help="seconds to wait before --test so the controller can discover links (default 8)")
    return p.parse_args()


def main():
    args = parse_args()

    try:
        plan = make_plan(args.clients, args.workers, args.switches, args.topo, args.links, args.placement)
    except TopologyError as e:
        sys.exit("Topology error: %s" % e)

    print(describe(plan))
    if args.dry_run:
        return

    if os.geteuid() != 0:
        sys.exit("Mininet needs root. Run: sudo python3 topology/topology.py")

    from mininet.cli import CLI
    from mininet.log import info, setLogLevel

    link_params = {}
    if args.link_bw is not None:
        link_params["bw"] = args.link_bw
    if args.link_delay:
        link_params["delay"] = args.link_delay
    if args.link_loss is not None:
        link_params["loss"] = args.link_loss

    setLogLevel("info")
    net = build_network(args.clients, args.workers, args.controller_ip, args.controller_port,
                        attach_root=not args.no_root, num_switches=args.switches, shape=args.topo,
                        links_text=args.links, placement=args.placement, link_params=link_params)

    net.start()
    if not net.waitConnected(timeout=10):
        info("*** WARNING: not every switch connected to POX. "
             "Is POX running on %s:%d?\n" % (args.controller_ip, args.controller_port))

    ok = True
    try:
        if args.test:
            ok = run_ping_test(net, args.settle)
        else:
            CLI(net)
    finally:
        net.stop()
    if args.test:
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
