"""
allowlist_policy.py - who may talk to the job-queue ports (Delivery 2).

The rule is the "unauthorized access" scenario:
    TCP port 5000 (client -> server)  only from the client IPs
    TCP port 5001 (worker -> server)  only from the worker IPs
Everything else (ping, ARP, the status channel on 5002, return traffic of an
allowed connection) is not touched.

This file has no POX imports, so it can be tested on its own.
dynamic_switch.py applies it to the first packet of each new TCP connection.
"""
import ipaddress

CLIENT_PORT = 5000
WORKER_PORT = 5001

# topology.py addresses: clients 10.0.0.1-7, workers 10.0.0.21-27
DEFAULT_CLIENTS = tuple("10.0.0.%d" % n for n in range(1, 8))
DEFAULT_WORKERS = tuple("10.0.0.%d" % n for n in range(21, 28))


def parse_ip_list(text, what):
    """'10.0.0.1,10.0.0.2' -> ('10.0.0.1', '10.0.0.2'). Raises ValueError."""
    ips = []
    for item in str(text).split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ips.append(str(ipaddress.IPv4Address(item)))
        except ValueError:
            raise ValueError("%s: %r is not an IPv4 address" % (what, item))
    if not ips:
        raise ValueError("%s: the list is empty" % what)
    return tuple(ips)


class AllowList(object):
    def __init__(self, clients=DEFAULT_CLIENTS, workers=DEFAULT_WORKERS):
        self.clients = frozenset(clients)
        self.workers = frozenset(workers)
        self.blocked = 0        # how many connection attempts were refused

    def check(self, src_ip, dst_port):
        """Return None if allowed, else a short reason string.

        src_ip is a dotted string such as '10.0.0.3'; dst_port is an int."""
        reason = None
        if dst_port == CLIENT_PORT and src_ip not in self.clients:
            reason = "port %d is for clients only" % CLIENT_PORT
        elif dst_port == WORKER_PORT and src_ip not in self.workers:
            reason = "port %d is for workers only" % WORKER_PORT
        if reason is not None:
            self.blocked += 1
        return reason
