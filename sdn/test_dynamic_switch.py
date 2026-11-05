#!/usr/bin/env python3
"""
test_dynamic_switch.py - tests for sdn/dynamic_switch.py without Mininet.

It starts POX itself and connects fake OpenFlow 1.0 switches to it, so it checks
the controller logic only (learning, flows, and the reaction to link changes). It
does not prove anything about Mininet or Open vSwitch; use topology.py for that.

Needs: POX in ~/pox (or set POX_DIR), port 6633 free (stop any running POX first).
Run:   python3 sdn/test_dynamic_switch.py        (takes about 90 seconds)
Exit code 0 means every check passed.
"""
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time

POX_DIR = os.environ.get("POX_DIR", os.path.expanduser("~/pox"))
SDN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, POX_DIR)

import pox.openflow.libopenflow_01 as of                       # noqa: E402
import pox.lib.packet as pkt                                   # noqa: E402
from pox.lib.addresses import EthAddr                          # noqa: E402
from pox.openflow.util import make_type_to_unpacker_table      # noqa: E402

UNPACKERS = make_type_to_unpacker_table()
RESULTS = []


def check(name, ok):
    RESULTS.append(bool(ok))
    print(("PASS  " if ok else "FAIL  ") + name)


class FakeSwitch(object):
    """Just enough of an OpenFlow 1.0 switch to talk to POX."""

    def __init__(self, dpid, nports=3, link_state=None):
        self.dpid, self.nports = dpid, nports
        self.buf, self.log, self.peer = b"", [], None
        self.link_state = link_state if link_state is not None else {"up": True}
        self.s = socket.create_connection(("127.0.0.1", 6633))
        self.s.sendall(of.ofp_hello().pack())

    def pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.s.settimeout(max(0.01, end - time.time()))
            try:
                chunk = self.s.recv(65535)
            except socket.timeout:
                break
            if not chunk:
                break
            self.buf += chunk
            while len(self.buf) >= 8:
                length = struct.unpack("!H", self.buf[2:4])[0]
                if len(self.buf) < length:
                    break
                _, msg = UNPACKERS[self.buf[1]](self.buf, 0)
                self.buf = self.buf[length:]
                self.log.append(msg)
                self._react(msg)

    def _ports(self, down=()):
        return [of.ofp_phy_port(port_no=i, name="s%d-eth%d" % (self.dpid, i),
                                hw_addr=EthAddr("aa:00:00:00:%02x:%02x" % (self.dpid, i)),
                                state=(of.OFPPS_LINK_DOWN if i in down else 0))
                for i in range(1, self.nports + 1)]

    def _react(self, msg):
        reply = None
        if isinstance(msg, of.ofp_features_request):
            reply = of.ofp_features_reply(datapath_id=self.dpid, n_buffers=0, n_tables=1,
                                          capabilities=0, actions=0, ports=self._ports())
        elif isinstance(msg, of.ofp_stats_request):
            reply = of.ofp_stats_reply(type=msg.type)
            if msg.type == of.OFPST_DESC:
                reply.body = of.ofp_desc_stats(mfr_desc="fake", hw_desc="fake", sw_desc="fake",
                                               serial_num="1", dp_desc="fake")
        elif isinstance(msg, of.ofp_barrier_request):
            reply = of.ofp_barrier_reply()
        elif isinstance(msg, of.ofp_echo_request):
            reply = of.ofp_echo_reply()
        elif isinstance(msg, of.ofp_packet_out) and self.peer and self.link_state["up"]:
            # whatever POX sends out port 1 (LLDP) crosses the link to the other switch
            for a in msg.actions:
                if isinstance(a, of.ofp_action_output) and a.port == 1:
                    self.peer.inject(msg.data, 1)
        if reply is not None:
            reply.xid = msg.xid
            self.s.sendall(reply.pack())

    def inject(self, data, port):
        pi = of.ofp_packet_in(in_port=port, reason=of.OFPR_NO_MATCH, data=data)
        pi.buffer_id = None
        self.s.sendall(pi.pack())

    def packet_in(self, src, dst, port):
        eth = pkt.ethernet(src=EthAddr(src), dst=EthAddr(dst), type=0x88b5)
        eth.payload = b"x" * 40
        self.inject(eth.pack(), port)

    def port_status(self, port, down):
        ps = of.ofp_port_status(reason=of.OFPPR_MODIFY)
        ps.desc = self._ports(down=(port,) if down else ())[port - 1]
        self.s.sendall(ps.pack())

    def take(self):
        out, self.log = self.log, []
        return out


def flow_mods(msgs):
    return [m for m in msgs if isinstance(m, of.ofp_flow_mod)]


def deletes(msgs):
    return [m for m in flow_mods(msgs) if m.command == of.OFPFC_DELETE]


def floods(msgs):
    return [m for m in msgs if isinstance(m, of.ofp_packet_out) and
            any(isinstance(a, of.ofp_action_output) and a.port == of.OFPP_FLOOD for a in m.actions)]


def flows_to_port(msgs, port):
    return [m for m in flow_mods(msgs)
            if m.command == of.OFPFC_ADD and m.match.dl_type != pkt.ethernet.LLDP_TYPE and
            any(isinstance(a, of.ofp_action_output) and a.port == port for a in m.actions)]


class Pox(object):
    """Runs POX with discovery, spanning tree and dynamic_switch in the given mode."""

    def __init__(self, mode):
        env = dict(os.environ, PYTHONPATH=SDN_DIR)
        self.out = tempfile.NamedTemporaryFile(prefix="pox-test-", suffix=".log", delete=False)
        self.proc = subprocess.Popen(
            [sys.executable, "pox.py", "openflow.discovery", "openflow.spanning_tree",
             "dynamic_switch", "--mode=" + mode],
            cwd=POX_DIR, env=env, stdout=self.out, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", 6633), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        self.stop()
        sys.exit("POX did not start; is port 6633 free? Log: " + self.out.name)

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        time.sleep(0.5)


A, B = "00:00:00:00:00:0a", "00:00:00:00:00:0b"


def single_switch(mode):
    print("\n--- one switch, mode=%s" % mode)
    pox = Pox(mode)
    try:
        sw = FakeSwitch(1)
        sw.pump(2.0)
        sw.take()

        sw.packet_in(A, B, 1); sw.pump(0.5); m = sw.take()
        check("unknown destination is flooded", len(floods(m)) == 1)

        sw.packet_in(B, A, 2); sw.pump(0.5); m = sw.take()
        fm = flows_to_port(m, 1)
        check("known destination gets a flow to the learned port", len(fm) == 1)
        check("flow timeouts match l2_learning (idle 10, hard 30)",
              len(fm) == 1 and fm[0].idle_timeout == 10 and fm[0].hard_timeout == 30)

        sw.port_status(3, down=False); sw.pump(2.5); m = sw.take()
        check("a port config change with the link still up does not flush", not deletes(m))

        sw.port_status(2, down=True); sw.pump(0.8); m = sw.take()
        if mode == "dynamic":
            check("link down: flows are deleted on the switch", deletes(m))
            sw.pump(2.0); m = sw.take()
            check("a second flush pass follows", deletes(m))
            sw.packet_in(A, B, 1); sw.pump(0.5); m = sw.take()
            check("after the flush a known destination is flooded again (MAC table cleared)",
                  len(floods(m)) == 1)
        else:
            sw.pump(2.0); m += sw.take()
            check("static mode: link down does NOT flush", not deletes(m))
            sw.packet_in(A, B, 1); sw.pump(0.5); m = sw.take()
            check("static mode: the stale table is kept (flow still goes to the old port)",
                  len(flows_to_port(m, 2)) == 1)
    finally:
        pox.stop()


def two_switches():
    print("\n--- two switches joined by one link, mode=dynamic")
    pox = Pox("dynamic")
    try:
        state = {"up": True}
        s1, s2 = FakeSwitch(1, 2, state), FakeSwitch(2, 2, state)
        s1.peer, s2.peer = s2, s1

        def pump_both(seconds):
            end = time.time() + seconds
            while time.time() < end:
                s1.pump(0.05); s2.pump(0.05)

        pump_both(14)       # handshake and at least one LLDP discovery cycle
        check("link discovered: both switches flushed after the link was added",
              deletes(s1.log) and deletes(s2.log))
        pump_both(3); s1.take(); s2.take()

        t0 = time.time()
        state["up"] = False
        s1.port_status(1, down=True); s2.port_status(1, down=True)
        seen = {}
        while time.time() - t0 < 6 and len(seen) < 2:
            pump_both(0.05)
            for name, sw in (("s1", s1), ("s2", s2)):
                if name not in seen and deletes(sw.log):
                    seen[name] = time.time() - t0
        print("      flush after link failure:", dict((k, round(v, 2)) for k, v in seen.items()), "seconds")
        check("both switches flushed within 2 s of the failure (stock discovery needs 10-15 s)",
              len(seen) == 2 and max(seen.values()) < 2.0)
    finally:
        pox.stop()


class RingSwitch(FakeSwitch):
    """A fake switch that also remembers NO_FLOOD port settings (as a real switch would)
    and passes LLDP over the wires of a small network."""

    def __init__(self, dpid, net):
        FakeSwitch.__init__(self, dpid, nports=3)
        self.net, self.noflood = net, {}

    def _react(self, msg):
        if isinstance(msg, of.ofp_port_mod):
            if msg.mask & of.OFPPC_NO_FLOOD:
                self.noflood[msg.port_no] = bool(msg.config & of.OFPPC_NO_FLOOD)
        elif isinstance(msg, of.ofp_packet_out):
            for a in msg.actions:
                if isinstance(a, of.ofp_action_output):
                    end = self.net.peer((self.dpid, a.port))
                    if end is not None and self.net.up(self.dpid, a.port):
                        self.net.switches[end[0]].inject(msg.data, end[1])
        else:
            FakeSwitch._react(self, msg)


class RingNet(object):
    """Three switches in a ring: s1.1-s2.1, s2.2-s3.1, s1.2-s3.2 (same wiring as topology.py)."""
    WIRES = [((1, 1), (2, 1)), ((2, 2), (3, 1)), ((1, 2), (3, 2))]

    def __init__(self):
        self.state = dict((i, True) for i in range(len(self.WIRES)))
        self.switches = dict((d, RingSwitch(d, self)) for d in (1, 2, 3))

    def _wire(self, dpid, port):
        for i, (a, b) in enumerate(self.WIRES):
            if (dpid, port) in (a, b):
                return i, (b if (dpid, port) == a else a)
        return None, None

    def peer(self, end):
        return self._wire(*end)[1]

    def up(self, dpid, port):
        return self.state[self._wire(dpid, port)[0]]

    def pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            for sw in self.switches.values():
                sw.pump(0.05)

    def blocked(self, i):
        a, b = self.WIRES[i]
        return self.switches[a[0]].noflood.get(a[1]) or self.switches[b[0]].noflood.get(b[1])

    def connected(self):
        """Are the 3 switches joined by links that are up and not blocked by NO_FLOOD?"""
        adj = dict((d, set()) for d in (1, 2, 3))
        for i, (a, b) in enumerate(self.WIRES):
            if self.state[i] and not self.blocked(i):
                adj[a[0]].add(b[0]); adj[b[0]].add(a[0])
        seen, todo = set([1]), [1]
        while todo:
            for n in adj[todo.pop()]:
                if n not in seen:
                    seen.add(n); todo.append(n)
        return len(seen) == 3

    def set_link(self, i, up):
        a, b = self.WIRES[i]
        self.state[i] = up
        self.switches[a[0]].port_status(a[1], down=not up)
        time.sleep(0.003)
        self.switches[b[0]].port_status(b[1], down=not up)


def ring_failover():
    """After an in-use link fails, no switch may be left cut off (stock POX can do that)."""
    print("\n--- three switches in a ring, mode=dynamic")
    pox = Pox("dynamic")
    try:
        net = RingNet()
        net.pump(18)                                    # discovery and spanning tree settle
        check("ring: all switches connected before any failure", net.connected())
        in_use = [i for i in range(3) if not net.blocked(i)]
        for i in in_use:
            a, b = net.WIRES[i]
            name = "s%d-s%d" % (a[0], b[0])
            net.set_link(i, False)
            net.pump(3)
            check("ring: still connected 3 s after %s fails (not cut off)" % name, net.connected())
            net.set_link(i, True)
            net.pump(8)
            check("ring: connected after %s is restored" % name, net.connected())
    finally:
        pox.stop()


if __name__ == "__main__":
    if not os.path.exists(os.path.join(POX_DIR, "pox.py")):
        sys.exit("POX not found in %s (set POX_DIR)" % POX_DIR)
    single_switch("dynamic")
    single_switch("static")
    two_switches()
    ring_failover()
    print("\n%d of %d checks passed" % (sum(RESULTS), len(RESULTS)))
    sys.exit(0 if all(RESULTS) else 1)
