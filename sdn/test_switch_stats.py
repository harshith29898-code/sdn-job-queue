"""
test_switch_stats.py - tests for switch_stats.py (no Mininet needed).

Uses real POX stats classes, fake switch connections and real localhost sockets.
Run from the repo root:
    python3 sdn/test_switch_stats.py -v
POX is looked for in $POX_DIR, then ~/pox.
"""
import collections
import json
import os
import socket
import sys
import time
import unittest
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
for candidate in (os.environ.get("POX_DIR"), os.path.expanduser("~/pox")):
    if candidate and os.path.isdir(os.path.join(candidate, "pox")):
        sys.path.insert(0, candidate)
        break

import pox.core
if pox.core.core is None:
    pox.core.initialize()
core = pox.core.core

import pox.openflow.libopenflow_01 as of
import switch_stats

Link = collections.namedtuple("Link", "dpid1 port1 dpid2 port2")


class FakeConn(object):
    def __init__(self, dpid):
        self.dpid = dpid
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


class FakeOpenflow(object):
    def __init__(self):
        self.connections = {}

    def addListeners(self, obj):
        pass


class FakeDiscovery(object):
    def __init__(self):
        self.adjacency = set()
        self.listeners = []

    def addListenerByName(self, name, fn):
        self.listeners.append((name, fn))


FAKE_OF = FakeOpenflow()
FAKE_DISC = FakeDiscovery()
core.register("openflow", FAKE_OF)
core.register("openflow_discovery", FAKE_DISC)


def port_stat(port_no, rx_p=0, tx_p=0, rx_b=0, tx_b=0):
    return of.ofp_port_stats(port_no=port_no, rx_packets=rx_p, tx_packets=tx_p,
                             rx_bytes=rx_b, tx_bytes=tx_b)


def ports_event(dpid, *stats):
    return SimpleNamespace(dpid=dpid, stats=list(stats))


def flows_event(dpid, count):
    return SimpleNamespace(dpid=dpid, stats=of.ofp_aggregate_stats_reply(flow_count=count))


def read_line(sock):
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
    return buf.decode()


class Base(unittest.TestCase):
    def setUp(self):
        FAKE_OF.connections = {}
        FAKE_DISC.adjacency = set()
        self.stats = switch_stats.SwitchStats(port=0, interval=2, start_timer=False)
        self.socks = []

    def tearDown(self):
        self.stats.stream.close()
        for s in self.socks:
            s.close()

    def connect(self):
        s = socket.create_connection(("127.0.0.1", self.stats.stream.port), timeout=3)
        self.socks.append(s)
        return s


class BuildLineTests(unittest.TestCase):
    def test_one_line_of_json(self):
        line = switch_stats.build_line(
            1794071755.2434,
            {1: {"ports": {1: {"rx_pkts": 1, "tx_pkts": 2, "rx_bytes": 3, "tx_bytes": 4}},
                 "flows": 7, "stale": False}},
            [(switch_stats.link_key(1, 2, 2, 1), True)])
        self.assertTrue(line.endswith("\n"))
        self.assertEqual(line.count("\n"), 1)
        sample = json.loads(line)
        self.assertEqual(sample["t"], 1794071755.243)
        s1 = sample["switches"]["s1"]
        self.assertEqual(s1["dpid"], 1)
        self.assertEqual(s1["flows"], 7)
        self.assertEqual(s1["ports"]["1"], {"rx_pkts": 1, "tx_pkts": 2, "rx_bytes": 3, "tx_bytes": 4})
        self.assertNotIn("stale", s1)
        self.assertEqual(sample["links"], [{"a": "s1", "a_port": 2, "b": "s2", "b_port": 1, "up": True}])

    def test_stale_flag_only_when_true(self):
        line = switch_stats.build_line(1.0, {3: {"ports": {}, "flows": 0, "stale": True}}, [])
        self.assertTrue(json.loads(line)["switches"]["s3"]["stale"])

    def test_link_key_same_for_both_directions(self):
        self.assertEqual(switch_stats.link_key(1, 2, 3, 4), switch_stats.link_key(3, 4, 1, 2))


class PollAndReplyTests(Base):
    def test_poll_asks_every_switch_for_ports_and_flows(self):
        c1, c2 = FakeConn(1), FakeConn(2)
        FAKE_OF.connections = {1: c1, 2: c2}
        calls = []
        core.call_delayed = lambda *a, **k: calls.append(a)
        try:
            self.stats._poll()
        finally:
            del core.call_delayed
        for conn in (c1, c2):
            self.assertEqual(len(conn.sent), 2)
            self.assertIsInstance(conn.sent[0], of.ofp_stats_request)
            self.assertIsInstance(conn.sent[0].body, of.ofp_port_stats_request)
            self.assertIsInstance(conn.sent[1].body, of.ofp_aggregate_stats_request)
        self.assertEqual(calls[0][0], switch_stats.REPLY_WAIT)

    def test_replies_become_the_line(self):
        FAKE_OF.connections = {1: FakeConn(1)}
        self.stats.round_replies = {}
        self.stats._handle_PortStatsReceived(ports_event(
            1, port_stat(1, 10, 12, 840, 1008), port_stat(2, 5, 6, 400, 480),
            port_stat(of.OFPP_LOCAL, 99, 99, 99, 99)))
        self.stats._handle_AggregateFlowStatsReceived(flows_event(1, 7))
        s1 = json.loads(self.stats.build_line())["switches"]["s1"]
        self.assertEqual(s1["flows"], 7)
        self.assertEqual(sorted(s1["ports"]), ["1", "2"])        # local port left out
        self.assertEqual(s1["ports"]["1"], {"rx_pkts": 10, "tx_pkts": 12, "rx_bytes": 840, "tx_bytes": 1008})
        self.assertNotIn("stale", s1)

    def test_missing_reply_is_marked_stale(self):
        FAKE_OF.connections = {1: FakeConn(1)}
        self.stats._handle_PortStatsReceived(ports_event(1, port_stat(1, 1, 1, 1, 1)))
        self.assertTrue(json.loads(self.stats.build_line())["switches"]["s1"]["stale"])

    def test_a_new_round_makes_old_numbers_stale_until_answered(self):
        FAKE_OF.connections = {1: FakeConn(1)}
        self.stats._handle_PortStatsReceived(ports_event(1, port_stat(1, 1, 1, 1, 1)))
        self.stats._handle_AggregateFlowStatsReceived(flows_event(1, 3))
        core.call_delayed = lambda *a, **k: None
        try:
            self.stats._poll()
        finally:
            del core.call_delayed
        s1 = json.loads(self.stats.build_line())["switches"]["s1"]
        self.assertTrue(s1["stale"])
        self.assertEqual(s1["flows"], 3)       # previous numbers kept

    def test_only_connected_switches_are_listed(self):
        self.stats._handle_PortStatsReceived(ports_event(5, port_stat(1)))
        self.assertEqual(json.loads(self.stats.build_line())["switches"], {})

    def test_connection_down_forgets_the_switch(self):
        FAKE_OF.connections = {1: FakeConn(1)}
        self.stats._handle_PortStatsReceived(ports_event(1, port_stat(1, 4, 4, 4, 4)))
        self.stats._handle_AggregateFlowStatsReceived(flows_event(1, 2))
        self.stats._handle_ConnectionDown(SimpleNamespace(dpid=1))
        FAKE_OF.connections = {1: FakeConn(1)}      # it reconnects
        s1 = json.loads(self.stats.build_line())["switches"]["s1"]
        self.assertEqual(s1["ports"], {})
        self.assertEqual(s1["flows"], 0)

    def test_emit_broadcasts_a_line(self):
        FAKE_OF.connections = {1: FakeConn(1)}
        client = self.connect()
        self.stats._emit()
        sample = json.loads(read_line(client))
        self.assertIn("s1", sample["switches"])
        self.assertEqual(self.stats.lines_sent, 1)


class LinkTests(Base):
    def test_link_up_needs_both_directions(self):
        FAKE_DISC.adjacency = {Link(1, 2, 2, 1), Link(2, 1, 1, 2)}
        self.assertEqual(self.stats.build_line() and json.loads(self.stats.build_line())["links"],
                         [{"a": "s1", "a_port": 2, "b": "s2", "b_port": 1, "up": True}])
        FAKE_DISC.adjacency = {Link(1, 2, 2, 1)}
        self.assertFalse(json.loads(self.stats.build_line())["links"][0]["up"])

    def test_removed_link_stays_listed_as_down(self):
        FAKE_DISC.adjacency = {Link(1, 2, 2, 1), Link(2, 1, 1, 2)}
        self.stats.build_line()
        FAKE_DISC.adjacency = set()
        links = json.loads(self.stats.build_line())["links"]
        self.assertEqual(len(links), 1)
        self.assertFalse(links[0]["up"])

    def test_link_back_is_up_again(self):
        FAKE_DISC.adjacency = {Link(1, 2, 2, 1), Link(2, 1, 1, 2)}
        self.stats.build_line()
        FAKE_DISC.adjacency = set()
        self.stats.build_line()
        FAKE_DISC.adjacency = {Link(1, 2, 2, 1), Link(2, 1, 1, 2)}
        self.assertTrue(json.loads(self.stats.build_line())["links"][0]["up"])

    def test_link_event_registers_a_link(self):
        self.stats._handle_LinkEvent(SimpleNamespace(added=True, link=Link(2, 2, 3, 1)))
        links = json.loads(self.stats.build_line())["links"]
        self.assertEqual(links, [{"a": "s2", "a_port": 2, "b": "s3", "b_port": 1, "up": False}])

    def test_links_found_before_start_are_known(self):
        FAKE_DISC.adjacency = {Link(1, 1, 3, 2), Link(3, 2, 1, 1)}
        late = switch_stats.SwitchStats(port=0, interval=2, start_timer=False)
        try:
            self.assertEqual(len(late.known_links), 1)
        finally:
            late.stream.close()

    def test_listens_for_link_events(self):
        self.assertIn("LinkEvent", [name for name, _ in FAKE_DISC.listeners])


class StreamTests(Base):
    def test_new_client_gets_the_latest_line_at_once(self):
        self.stats.stream.broadcast('{"n":1}\n')
        client = self.connect()
        self.assertEqual(read_line(client), '{"n":1}\n')

    def test_all_clients_get_each_line(self):
        a, b = self.connect(), self.connect()
        time.sleep(0.3)
        self.stats.stream.broadcast('{"n":2}\n')
        self.assertEqual(read_line(a), '{"n":2}\n')
        self.assertEqual(read_line(b), '{"n":2}\n')

    def test_dead_client_is_dropped_and_others_keep_working(self):
        a, b = self.connect(), self.connect()
        time.sleep(0.3)
        a.close()
        for i in range(30):
            self.stats.stream.broadcast(json.dumps({"n": i, "pad": "x" * 2000}) + "\n")
            time.sleep(0.02)
        deadline = time.time() + 3
        while len(self.stats.stream.clients) != 1 and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(self.stats.stream.clients), 1)
        self.stats.stream.broadcast('{"n":"last"}\n')
        got = ""
        while '"last"' not in got:
            got += read_line(b)
        self.assertIn('"last"', got)

    def test_slow_client_does_not_slow_broadcast(self):
        self.connect()          # connects but never reads
        time.sleep(0.3)
        big = json.dumps({"pad": "x" * 10000}) + "\n"
        start = time.time()
        for _ in range(500):
            self.stats.stream.broadcast(big)
        self.assertLess(time.time() - start, 1.0)


class LaunchTests(unittest.TestCase):
    def run_launch(self, **kwargs):
        captured = []
        core.registerNew = lambda cls, *a: captured.append(a)
        try:
            switch_stats.launch(**kwargs)
        finally:
            del core.registerNew
        return captured[0]

    def test_defaults(self):
        self.assertEqual(self.run_launch(), (5003, 2.0))

    def test_options_arrive_as_strings(self):
        self.assertEqual(self.run_launch(port="6000", interval="1.5"), (6000, 1.5))

    def test_bad_values_are_errors(self):
        for kwargs in ({"port": "abc"}, {"port": "0"}, {"port": "70000"},
                       {"interval": "0.1"}, {"interval": "fast"}):
            with self.assertRaises(RuntimeError):
                self.run_launch(**kwargs)


if __name__ == "__main__":
    unittest.main()
