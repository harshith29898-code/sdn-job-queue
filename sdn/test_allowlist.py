"""
test_allowlist.py - tests for the allow-list in dynamic_switch.py.

Uses real POX packet classes and a fake switch connection (no Mininet).
Run from the repo root:
    python3 sdn/test_allowlist.py -v
POX is looked for in $POX_DIR, then ~/pox.
"""
import os
import sys
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

import pox.openflow.libopenflow_01 as of
from pox.lib.addresses import EthAddr, IPAddr
from pox.lib.packet import ethernet, ipv4, tcp, icmp

import allowlist_policy
import dynamic_switch
from allowlist_policy import AllowList, parse_ip_list

SERVER_MAC, SERVER_IP = "00:00:00:00:00:10", "10.0.0.10"


class FakeConnection(object):
    def __init__(self):
        self.sent = []
        self.ports = {}

    def addListeners(self, obj):
        pass

    def send(self, msg):
        self.sent.append(msg)


def mac_for(ip):
    return "00:00:00:00:%02x:%02x" % (int(ip.split(".")[2]), int(ip.split(".")[3]))


def tcp_packet(src_ip, dst_ip, dst_port, src_port=40000):
    t = tcp()
    t.srcport, t.dstport = src_port, dst_port
    t.SYN = True
    t.off = 5
    t.payload = b""
    ip = ipv4(srcip=IPAddr(src_ip), dstip=IPAddr(dst_ip), protocol=ipv4.TCP_PROTOCOL)
    ip.payload = t
    eth = ethernet(type=ethernet.IP_TYPE, src=EthAddr(mac_for(src_ip)),
                   dst=EthAddr(mac_for(dst_ip)))
    eth.payload = ip
    return ethernet(eth.pack())      # parse it again, like POX does for real packets


def icmp_packet(src_ip, dst_ip):
    ic = icmp()
    ic.type = 8
    ic.payload = b"ping"
    ip = ipv4(srcip=IPAddr(src_ip), dstip=IPAddr(dst_ip), protocol=ipv4.ICMP_PROTOCOL)
    ip.payload = ic
    eth = ethernet(type=ethernet.IP_TYPE, src=EthAddr(mac_for(src_ip)),
                   dst=EthAddr(mac_for(dst_ip)))
    eth.payload = ip
    return ethernet(eth.pack())


def packet_in(packet, port=1):
    ofp = of.ofp_packet_in(buffer_id=5, in_port=port, data=packet.pack())
    return SimpleNamespace(parsed=packet, ofp=ofp, port=port, dpid=1)


class Base(unittest.TestCase):
    def make_brain(self, policy):
        self.conn = FakeConnection()
        brain = dynamic_switch.SwitchBrain(self.conn)
        brain.allowlist = policy
        return brain

    def learn_server(self, brain):
        """Let the switch learn the server's MAC on port 2 (like an ARP reply)."""
        brain._handle_PacketIn(packet_in(icmp_packet(SERVER_IP, "10.0.0.1"), port=2))
        self.conn.sent.clear()

    def last(self):
        self.assertEqual(len(self.conn.sent), 1, "expected exactly one message")
        return self.conn.sent[0]

    def assert_dropped(self):
        msg = self.last()
        self.assertIsInstance(msg, of.ofp_flow_mod)
        self.assertEqual(msg.actions, [])
        self.assertEqual(msg.buffer_id, 5)
        self.assertEqual(msg.idle_timeout, dynamic_switch.ALLOWLIST_DROP_SECONDS)

    def assert_forwarded(self):
        msg = self.last()
        self.assertIsInstance(msg, of.ofp_flow_mod)
        self.assertEqual([a.port for a in msg.actions], [2])


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.p = AllowList()

    def test_client_allowed_on_5000(self):
        self.assertIsNone(self.p.check("10.0.0.1", 5000))
        self.assertIsNone(self.p.check("10.0.0.7", 5000))

    def test_worker_refused_on_5000(self):
        self.assertIsNotNone(self.p.check("10.0.0.21", 5000))

    def test_worker_allowed_on_5001(self):
        self.assertIsNone(self.p.check("10.0.0.21", 5001))
        self.assertIsNone(self.p.check("10.0.0.27", 5001))

    def test_client_refused_on_5001(self):
        self.assertIsNotNone(self.p.check("10.0.0.1", 5001))

    def test_unknown_ip_refused_on_both(self):
        self.assertIsNotNone(self.p.check("10.0.0.99", 5000))
        self.assertIsNotNone(self.p.check("10.0.0.99", 5001))

    def test_other_ports_untouched(self):
        for port in (22, 5002, 6633, 40000):
            self.assertIsNone(self.p.check("10.0.0.99", port))

    def test_blocked_counter(self):
        self.p.check("10.0.0.99", 5000)
        self.p.check("10.0.0.99", 5001)
        self.p.check("10.0.0.1", 5000)
        self.assertEqual(self.p.blocked, 2)

    def test_custom_lists(self):
        p = AllowList(clients=("10.0.0.1",), workers=("10.0.0.21",))
        self.assertIsNone(p.check("10.0.0.1", 5000))
        self.assertIsNotNone(p.check("10.0.0.2", 5000))
        self.assertIsNotNone(p.check("10.0.0.22", 5001))

    def test_default_lists_match_topology(self):
        self.assertEqual(len(allowlist_policy.DEFAULT_CLIENTS), 7)
        self.assertEqual(allowlist_policy.DEFAULT_WORKERS[0], "10.0.0.21")
        self.assertEqual(allowlist_policy.DEFAULT_WORKERS[-1], "10.0.0.27")

    def test_parse_ip_list(self):
        self.assertEqual(parse_ip_list("10.0.0.1, 10.0.0.2", "x"), ("10.0.0.1", "10.0.0.2"))
        for bad in ("10.0.0.300", "hello", "", ","):
            with self.assertRaises(ValueError):
                parse_ip_list(bad, "x")


class SwitchTests(Base):
    def test_allowed_client_is_forwarded(self):
        brain = self.make_brain(AllowList())
        self.learn_server(brain)
        brain._handle_PacketIn(packet_in(tcp_packet("10.0.0.1", SERVER_IP, 5000)))
        self.assert_forwarded()

    def test_unauthorized_client_is_dropped(self):
        brain = self.make_brain(AllowList(clients=("10.0.0.1",)))
        self.learn_server(brain)
        brain._handle_PacketIn(packet_in(tcp_packet("10.0.0.3", SERVER_IP, 5000)))
        self.assert_dropped()
        self.assertEqual(brain.allowlist.blocked, 1)

    def test_client_cannot_use_worker_port(self):
        brain = self.make_brain(AllowList())
        self.learn_server(brain)
        brain._handle_PacketIn(packet_in(tcp_packet("10.0.0.1", SERVER_IP, 5001)))
        self.assert_dropped()

    def test_worker_allowed_on_5001_refused_on_5000(self):
        brain = self.make_brain(AllowList())
        self.learn_server(brain)
        brain._handle_PacketIn(packet_in(tcp_packet("10.0.0.21", SERVER_IP, 5001)))
        self.assert_forwarded()
        self.conn.sent.clear()
        brain._handle_PacketIn(packet_in(tcp_packet("10.0.0.21", SERVER_IP, 5000)))
        self.assert_dropped()

    def test_refused_packet_is_not_flooded_when_server_unknown(self):
        brain = self.make_brain(AllowList())     # server MAC not learned
        brain._handle_PacketIn(packet_in(tcp_packet("10.0.0.99", SERVER_IP, 5000)))
        self.assert_dropped()

    def test_ping_from_unlisted_ip_still_works(self):
        brain = self.make_brain(AllowList())
        self.learn_server(brain)
        brain._handle_PacketIn(packet_in(icmp_packet("10.0.0.99", SERVER_IP)))
        self.assert_forwarded()

    def test_status_port_not_filtered(self):
        brain = self.make_brain(AllowList())
        self.learn_server(brain)
        brain._handle_PacketIn(packet_in(tcp_packet("10.0.0.99", SERVER_IP, 5002)))
        self.assert_forwarded()

    def test_return_traffic_not_filtered(self):
        brain = self.make_brain(AllowList())
        self.learn_server(brain)
        brain._handle_PacketIn(packet_in(
            tcp_packet(SERVER_IP, "10.0.0.1", 40000, src_port=5000), port=2))
        # destination (c1) not learned -> flooded, but not dropped by the allow-list
        msg = self.last()
        self.assertIsInstance(msg, of.ofp_packet_out)
        self.assertEqual(brain.allowlist.blocked, 0)

    def test_off_means_everything_allowed(self):
        brain = self.make_brain(None)
        self.learn_server(brain)
        brain._handle_PacketIn(packet_in(tcp_packet("10.0.0.99", SERVER_IP, 5000)))
        self.assert_forwarded()

    def test_still_blocked_after_a_flush(self):
        brain = self.make_brain(AllowList(clients=("10.0.0.1",)))
        self.learn_server(brain)
        brain.flush()
        self.conn.sent.clear()
        self.learn_server(brain)
        brain._handle_PacketIn(packet_in(tcp_packet("10.0.0.3", SERVER_IP, 5000)))
        self.assert_dropped()


class WiringTests(Base):
    def test_connection_up_gives_brain_the_allowlist(self):
        policy = AllowList()
        ds = dynamic_switch.DynamicSwitch.__new__(dynamic_switch.DynamicSwitch)
        ds.allowlist, ds.brains, ds.link_up = policy, {}, {}
        ds._handle_ConnectionUp(SimpleNamespace(dpid=1, connection=FakeConnection()))
        self.assertIs(ds.brains[1].allowlist, policy)

    def run_launch(self, **kwargs):
        captured = []
        core = dynamic_switch.core
        core.registerNew = lambda cls, *a: captured.append(a)
        try:
            dynamic_switch.launch(**kwargs)
        finally:
            del core.registerNew
        return captured[0]       # (mode, policy)

    def test_launch_default_has_no_allowlist(self):
        mode, policy = self.run_launch()
        self.assertEqual(mode, "dynamic")
        self.assertIsNone(policy)

    def test_launch_flag_turns_it_on(self):
        _, policy = self.run_launch(allowlist=True)
        self.assertIsNotNone(policy)
        self.assertEqual(set(policy.clients), set(allowlist_policy.DEFAULT_CLIENTS))

    def test_launch_false_string_is_off(self):
        _, policy = self.run_launch(allowlist="False")
        self.assertIsNone(policy)

    def test_launch_clients_option_implies_allowlist(self):
        _, policy = self.run_launch(mode="static", clients="10.0.0.1,10.0.0.2")
        self.assertEqual(set(policy.clients), {"10.0.0.1", "10.0.0.2"})
        self.assertEqual(set(policy.workers), set(allowlist_policy.DEFAULT_WORKERS))

    def test_launch_bad_ip_is_an_error(self):
        with self.assertRaises(RuntimeError):
            self.run_launch(clients="10.0.0.300")


if __name__ == "__main__":
    unittest.main()
