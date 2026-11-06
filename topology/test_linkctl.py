#!/usr/bin/env python3
"""Tests for linkctl.py and topology.write_state, using a fake network (no Mininet, no root).
Run:  python3 topology/test_linkctl.py"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import linkctl      # noqa: E402
import topology     # noqa: E402


class FakeNode(object):
    def __init__(self, name, in_namespace):
        self.name, self.inNamespace = name, in_namespace


class FakeIntf(object):
    def __init__(self, node, name):
        self.node, self.name = node, name


class FakeLink(object):
    def __init__(self, n1, i1, n2, i2):
        self.intf1, self.intf2 = FakeIntf(n1, i1), FakeIntf(n2, i2)


class FakeNet(object):
    """s1-s2, s1-c1 (a host link) and root-s1, like the real network."""
    def __init__(self):
        s1, s2 = FakeNode("s1", False), FakeNode("s2", False)
        c1, root = FakeNode("c1", True), FakeNode("root", False)
        self.links = [FakeLink(s1, "s1-eth1", s2, "s2-eth1"),
                      FakeLink(c1, "c1-eth0", s1, "s1-eth2"),
                      FakeLink(root, "root-eth0", s1, "s1-eth3")]


def run(argv):
    out = io.StringIO()
    with redirect_stdout(out):
        linkctl.main(argv)
    return out.getvalue()


class LinkCtlTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.state = os.path.join(self.dir, "state.json")
        topology.write_state(FakeNet(), self.state)
        self.base = ["--state", self.state]

    def test_state_lists_only_root_namespace_interfaces_to_toggle(self):
        with open(self.state) as f:
            links = json.load(f)["links"]
        by_name = dict((l["a"] + "-" + l["b"], l["toggle"]) for l in links)
        self.assertEqual(by_name["s1-s2"], ["s1-eth1", "s2-eth1"])
        self.assertEqual(by_name["c1-s1"], ["s1-eth2"])          # host side is not touched
        self.assertEqual(by_name["root-s1"], ["root-eth0", "s1-eth3"])

    def test_down_dry_run_prints_both_ip_commands_and_a_timestamp(self):
        out = run(self.base + ["down", "s1", "s2", "--dry-run"])
        self.assertIn("ip link set dev s1-eth1 down", out)
        self.assertIn("ip link set dev s2-eth1 down", out)
        self.assertRegex(out, r"t=\d+\.\d{3} link s1-s2 DOWN")

    def test_pair_can_be_written_with_a_dash_and_in_either_order(self):
        out = run(self.base + ["up", "s2-s1", "--dry-run"])
        self.assertIn("ip link set dev s1-eth1 up", out)
        self.assertRegex(out, r"link s1-s2 UP")

    def test_host_link_only_toggles_the_switch_side(self):
        out = run(self.base + ["down", "c1", "s1", "--dry-run"])
        self.assertIn("ip link set dev s1-eth2 down", out)
        self.assertNotIn("c1-eth0", out)

    def test_unknown_link_and_missing_state_give_clear_errors(self):
        with self.assertRaises(SystemExit) as ctx:
            run(self.base + ["down", "s1", "s9", "--dry-run"])
        self.assertIn("no link between s1 and s9", str(ctx.exception))
        with self.assertRaises(SystemExit) as ctx:
            run(["--state", os.path.join(self.dir, "missing.json"), "list"])
        self.assertIn("Start topology/topology.py first", str(ctx.exception))

    def test_flap_fails_then_restores_and_writes_the_log(self):
        log = os.path.join(self.dir, "events.log")
        out = run(self.base + ["flap", "s1", "s2", "--after", "0.1", "--for", "0.2", "--dry-run", "--log", log])
        self.assertLess(out.index("DOWN"), out.index(" UP "))
        with open(log) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 2)
        down_t = float(lines[0].split()[0][2:]); up_t = float(lines[1].split()[0][2:])
        self.assertGreaterEqual(up_t - down_t, 0.19)

    def test_list_shows_every_link(self):
        out = run(self.base + ["list"])
        for name in ("s1-s2", "c1-s1", "root-s1"):
            self.assertIn(name, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
