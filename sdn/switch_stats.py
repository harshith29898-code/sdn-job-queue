"""
switch_stats.py - streams switch statistics to a TCP client (Delivery 2).

Every --interval seconds (default 2) it asks each connected switch for its
port counters and its flow count, then sends ONE line of JSON to every client
connected to --port (default 5003). The GUI reads this stream.

Run it together with the other components (discovery must come before it):
    cd ~/pox
    PYTHONPATH=~/sdn-job-queue/sdn python3 pox.py openflow.discovery \\
        openflow.spanning_tree dynamic_switch --mode=dynamic job_status \\
        switch_stats --port=5003 --interval=2

Look at the stream from the VM:
    python3 -c "import socket; f = socket.create_connection(('127.0.0.1', 5003)).makefile(); [print(l.strip()) for l in f]"

One line looks like this (pretty-printed here; on the wire it is one line):
    {"t": 1794071755.243,
     "switches": {"s1": {"dpid": 1, "flows": 7,
                         "ports": {"1": {"rx_pkts": 10, "tx_pkts": 12,
                                         "rx_bytes": 840, "tx_bytes": 1008}}}},
     "links": [{"a": "s1", "a_port": 2, "b": "s2", "b_port": 1, "up": true}]}

- t          Unix time (seconds) when the line was built, same clock as the
             "t=" stamps in the POX log and ping -D.
- switches   one entry per connected switch. "s<N>" is the switch's dpid; the
             counters are the switch's own (rx/tx packets and bytes per port;
             the switch's local port is left out). "flows" is the number of
             flow entries now (it includes discovery's own LLDP flow).
             "stale": true appears if the switch did not answer this round, in
             which case its numbers are the previous ones.
- links      every switch-to-switch link discovery has seen. "up" is true only
             while discovery sees the link in both directions, so this is the
             CONTROLLER'S view: in --mode=static it turns false only after
             discovery's own timeout, in --mode=dynamic almost at once.
             (Hosts are not listed; the VM's own link is not a switch link.)

A client that connects gets the latest line straight away. A client that reads
slowly only ever gets the newest line; it never slows POX down.
"""
import json
import socket
import threading
import time

from pox.core import core
import pox.openflow.libopenflow_01 as of
from pox.lib.recoco import Timer

log = core.getLogger()

DEFAULT_PORT = 5003
DEFAULT_INTERVAL = 2.0
MIN_INTERVAL = 0.5
REPLY_WAIT = 0.4        # seconds to wait for the switches' replies before sending a line
SEND_TIMEOUT = 5.0      # a client that cannot take a line in this long is dropped


def switch_name(dpid):
    return "s%d" % dpid


def link_key(dpid1, port1, dpid2, port2):
    """The same key for both directions of a link."""
    return tuple(sorted([(dpid1, port1), (dpid2, port2)]))


def build_line(t, switches, links):
    """The text sent to clients: one line of JSON, ending in a newline.

    switches: {dpid: {"ports": {port_no: {rx_pkts, tx_pkts, rx_bytes, tx_bytes}},
                      "flows": int, "stale": bool}}
    links:    [(key, up)] where key comes from link_key()"""
    out_switches = {}
    for dpid, info in switches.items():
        entry = {"dpid": dpid,
                 "flows": info["flows"],
                 "ports": dict((str(p), c) for p, c in info["ports"].items())}
        if info.get("stale"):
            entry["stale"] = True
        out_switches[switch_name(dpid)] = entry
    out_links = []
    for (end1, end2), up in links:
        out_links.append({"a": switch_name(end1[0]), "a_port": end1[1],
                          "b": switch_name(end2[0]), "b_port": end2[1],
                          "up": bool(up)})
    sample = {"t": round(t, 3), "switches": out_switches, "links": out_links}
    return json.dumps(sample, sort_keys=True, separators=(",", ":")) + "\n"


class _Client(object):
    """One connected reader, with its own writer thread. Keeps only the newest line."""

    def __init__(self, conn, on_dead):
        self.conn = conn
        self.on_dead = on_dead
        self.pending = None
        self.dead = False
        self.cond = threading.Condition()
        conn.settimeout(SEND_TIMEOUT)
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def offer(self, data):
        with self.cond:
            self.pending = data
            self.cond.notify()

    def close(self):
        with self.cond:
            self.dead = True
            self.cond.notify()
        try:
            self.conn.close()
        except OSError:
            pass

    def _run(self):
        while True:
            with self.cond:
                while self.pending is None and not self.dead:
                    self.cond.wait()
                if self.dead:
                    return
                data, self.pending = self.pending, None
            try:
                self.conn.sendall(data)
            except OSError:
                self.close()
                self.on_dead(self)
                return


class StreamServer(object):
    """Accepts TCP clients and broadcasts lines to them."""

    def __init__(self, port, host="0.0.0.0"):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.clients = []
        self.last = None
        self.lock = threading.Lock()
        self.closed = False
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        while not self.closed:
            try:
                conn, addr = self.sock.accept()
            except OSError:
                return
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            client = _Client(conn, self._remove)
            with self.lock:
                self.clients.append(client)
                last = self.last
            if last is not None:
                client.offer(last)
            log.info("Statistics client connected from %s:%d", addr[0], addr[1])

    def _remove(self, client):
        with self.lock:
            if client in self.clients:
                self.clients.remove(client)
        log.info("Statistics client disconnected")

    def broadcast(self, text):
        data = text.encode()
        with self.lock:
            self.last = data
            clients = list(self.clients)
        for client in clients:
            client.offer(data)

    def close(self):
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass
        with self.lock:
            clients, self.clients = self.clients, []
        for client in clients:
            client.close()


class SwitchStats(object):
    def __init__(self, port=DEFAULT_PORT, interval=DEFAULT_INTERVAL, start_timer=True):
        self.interval = interval
        self.stream = StreamServer(port)
        self.ports = {}             # dpid -> {port_no: counters}
        self.flows = {}             # dpid -> flow count
        self.round_replies = {}     # dpid -> set of replies seen this round
        self.known_links = set()    # link keys discovery has ever reported
        self.lines_sent = 0

        core.openflow.addListeners(self)
        discovery = getattr(core, "openflow_discovery", None)
        if discovery is None:
            log.warning("switch_stats: openflow.discovery is not loaded, so no links will be listed")
        else:
            discovery.addListenerByName("LinkEvent", self._handle_LinkEvent)
            for link in list(discovery.adjacency):
                self.known_links.add(link_key(link.dpid1, link.port1, link.dpid2, link.port2))
        if start_timer:
            self.timer = Timer(interval, self._poll, recurring=True)
        log.info("Statistics stream on port %d, one line every %g s", self.stream.port, interval)

    # ---- asking the switches ----

    def _poll(self):
        self.round_replies = {}
        for connection in list(core.openflow.connections.values()):
            connection.send(of.ofp_stats_request(body=of.ofp_port_stats_request()))
            connection.send(of.ofp_stats_request(body=of.ofp_aggregate_stats_request()))
        core.call_delayed(REPLY_WAIT, self._emit)

    def _handle_PortStatsReceived(self, event):
        ports = {}
        for stat in event.stats:
            if stat.port_no >= of.OFPP_MAX:
                continue        # the switch's local port
            ports[stat.port_no] = {"rx_pkts": stat.rx_packets, "tx_pkts": stat.tx_packets,
                                   "rx_bytes": stat.rx_bytes, "tx_bytes": stat.tx_bytes}
        self.ports[event.dpid] = ports
        self.round_replies.setdefault(event.dpid, set()).add("ports")

    def _handle_AggregateFlowStatsReceived(self, event):
        self.flows[event.dpid] = event.stats.flow_count
        self.round_replies.setdefault(event.dpid, set()).add("flows")

    def _handle_ConnectionDown(self, event):
        self.ports.pop(event.dpid, None)
        self.flows.pop(event.dpid, None)
        self.round_replies.pop(event.dpid, None)

    def _handle_LinkEvent(self, event):
        if event.added:
            link = event.link
            self.known_links.add(link_key(link.dpid1, link.port1, link.dpid2, link.port2))

    # ---- building and sending a line ----

    def _links(self):
        discovery = getattr(core, "openflow_discovery", None)
        directed = set()
        if discovery is not None:
            for link in list(discovery.adjacency):
                directed.add((link.dpid1, link.port1, link.dpid2, link.port2))
                self.known_links.add(link_key(link.dpid1, link.port1, link.dpid2, link.port2))
        result = []
        for key in sorted(self.known_links):
            (d1, p1), (d2, p2) = key
            up = (d1, p1, d2, p2) in directed and (d2, p2, d1, p1) in directed
            result.append((key, up))
        return result

    def build_line(self):
        switches = {}
        for dpid in sorted(core.openflow.connections.keys()):
            switches[dpid] = {
                "ports": self.ports.get(dpid, {}),
                "flows": self.flows.get(dpid, 0),
                "stale": self.round_replies.get(dpid, set()) != {"ports", "flows"},
            }
        return build_line(time.time(), switches, self._links())

    def _emit(self):
        self.stream.broadcast(self.build_line())
        self.lines_sent += 1


def launch(port=DEFAULT_PORT, interval=DEFAULT_INTERVAL):
    """POX entry point. Options: --port=5003  --interval=2 (seconds, at least 0.5)"""
    try:
        port = int(port)
        interval = float(interval)
    except (TypeError, ValueError):
        raise RuntimeError("switch_stats: --port must be a number and --interval a number of seconds")
    if not 0 < port < 65536:
        raise RuntimeError("switch_stats: --port must be between 1 and 65535")
    if interval < MIN_INTERVAL:
        raise RuntimeError("switch_stats: --interval must be at least %g seconds" % MIN_INTERVAL)
    core.registerNew(SwitchStats, port, interval)
