#!/usr/bin/env python3
"""
topology.py - Mininet topology for the Distributed Job Queue project (Delivery 1).

Layout (matches the Project Common Reference):
    1 switch (s1), 3 clients, 1 server, 3 workers, all on 10.0.0.0/24
      c1..c3   10.0.0.1  - 10.0.0.3
      server   10.0.0.10
      w1..w3   10.0.0.21 - 10.0.0.23
    s1 connects to a remote POX controller (default 127.0.0.1:6633).

Usage (needs root, uses the system python3):
    sudo python3 topology.py                 # start the network, open the Mininet CLI
    sudo python3 topology.py --test          # run pingall once, then exit
    sudo python3 topology.py --clients 2 --workers 4

Start POX first, in another terminal:
    cd ~/pox && python3 pox.py forwarding.l2_learning
"""
import argparse
import os
import sys

from mininet.cli import CLI
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController

SERVER_IP = "10.0.0.10"
CLIENT_IP_BASE = 0    # c1 -> 10.0.0.1, c2 -> 10.0.0.2, ...
WORKER_IP_BASE = 20   # w1 -> 10.0.0.21, w2 -> 10.0.0.22, ...
PREFIX_LEN = 24


def build_network(num_clients, num_workers, controller_ip, controller_port):
    """Create (but do not start) the Mininet network."""
    net = Mininet(controller=None, switch=OVSSwitch, autoSetMacs=True)

    info("*** Adding remote controller (POX)\n")
    net.addController(
        "c0",
        controller=RemoteController,
        ip=controller_ip,
        port=controller_port,
    )

    info("*** Adding switch\n")
    # POX (gar) speaks OpenFlow 1.0
    s1 = net.addSwitch("s1", protocols="OpenFlow10")

    info("*** Adding server\n")
    server = net.addHost("server", ip="%s/%d" % (SERVER_IP, PREFIX_LEN))
    net.addLink(server, s1)

    info("*** Adding clients\n")
    for i in range(1, num_clients + 1):
        ip = "10.0.0.%d/%d" % (CLIENT_IP_BASE + i, PREFIX_LEN)
        client = net.addHost("c%d" % i, ip=ip)
        net.addLink(client, s1)

    info("*** Adding workers\n")
    for i in range(1, num_workers + 1):
        ip = "10.0.0.%d/%d" % (WORKER_IP_BASE + i, PREFIX_LEN)
        worker = net.addHost("w%d" % i, ip=ip)
        net.addLink(worker, s1)

    return net


def parse_args():
    parser = argparse.ArgumentParser(description="Mininet topology for the job queue project")
    parser.add_argument("--clients", type=int, default=3, help="number of clients (default 3)")
    parser.add_argument("--workers", type=int, default=3, help="number of workers (default 3)")
    parser.add_argument("--controller-ip", default="127.0.0.1", help="POX controller IP")
    parser.add_argument("--controller-port", type=int, default=6633, help="POX OpenFlow port")
    parser.add_argument("--test", action="store_true", help="run pingall and exit")
    return parser.parse_args()


def main():
    args = parse_args()

    if os.geteuid() != 0:
        sys.exit("Mininet needs root. Run: sudo python3 topology.py")

    setLogLevel("info")
    net = build_network(args.clients, args.workers, args.controller_ip, args.controller_port)

    net.start()
    if not net.waitConnected(timeout=10):
        info("*** WARNING: the switch did not connect to POX. "
             "Is POX running on %s:%d?\n" % (args.controller_ip, args.controller_port))

    try:
        if args.test:
            net.pingAll()
        else:
            CLI(net)
    finally:
        net.stop()


if __name__ == "__main__":
    main()
