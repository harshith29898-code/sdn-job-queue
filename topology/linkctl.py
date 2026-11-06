#!/usr/bin/env python3
"""
linkctl.py - fail and restore links of the running Mininet network, from a script.

topology.py writes the real links and interface names of the running network to
a state file; this tool reads it and brings the interfaces up or down with
`ip link set`, the same thing the Mininet command `link s1 s3 down` does. It lets
an experiment script (or another terminal) cause failures at exact moments
without typing at the mininet> prompt.

Every action prints a line with a Unix timestamp, matching the "t=..." stamps in
the POX log and the times printed by `ping -D`, so recovery time can be computed:
    t=1730000000.123 link s1-s3 DOWN (interfaces: s1-eth2, s3-eth2)

Usage (needs root, except for "list"; run it in a second terminal while
topology.py is running):
    sudo python3 topology/linkctl.py list
    sudo python3 topology/linkctl.py down s1 s3          # also written as: down s1-s3
    sudo python3 topology/linkctl.py up s1 s3
    sudo python3 topology/linkctl.py flap s1 s3 --after 10 --for 30
        # wait 10 s, fail the link, wait 30 s, restore it
    sudo python3 topology/linkctl.py down c1 s1          # cut a host off (works for any link)
Options for every command:
    --log FILE    also append the timestamped lines to FILE
    --dry-run     print the commands without running them (no root needed)
"""
import argparse
import json
import os
import subprocess
import sys
import time

STATE_FILE = os.environ.get("SDN_TOPO_STATE", "/tmp/sdn-topology-state.json")


class LinkCtlError(Exception):
    """A problem to report to the user without a traceback."""


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        raise LinkCtlError("no running topology found (%s is missing). "
                           "Start topology/topology.py first." % path)
    except ValueError:
        raise LinkCtlError("%s is not readable; restart topology.py" % path)


def label(link):
    return "%s-%s" % (link["a"], link["b"])


def find_link(state, a, b):
    for link in state["links"]:
        if set((link["a"], link["b"])) == set((a, b)):
            return link
    raise LinkCtlError("there is no link between %s and %s. Links: %s" % (
        a, b, ", ".join(label(l) for l in state["links"])))


def parse_pair(tokens):
    """Accept 's1 s3' or 's1-s3'."""
    if len(tokens) == 1 and "-" in tokens[0]:
        tokens = tokens[0].split("-")
    if len(tokens) != 2 or not all(tokens):
        raise LinkCtlError("name the two ends of a link, for example: s1 s3 (or s1-s3)")
    return tokens[0], tokens[1]


def set_link(link, up, dry_run=False, log=None):
    """Bring every root-namespace interface of the link up or down. Returns the timestamp."""
    stamp = time.time()
    for iface in link["toggle"]:
        cmd = ["ip", "link", "set", "dev", iface, "up" if up else "down"]
        if dry_run:
            print("DRY RUN: " + " ".join(cmd))
            continue
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True)
        if result.returncode != 0:
            raise LinkCtlError("`%s` failed: %s (is the topology still running?)" % (
                " ".join(cmd), result.stderr.strip()))
    line = "t=%.3f link %s %s (interfaces: %s)" % (
        stamp, label(link), "UP" if up else "DOWN", ", ".join(link["toggle"]))
    print(line, flush=True)
    if log:
        with open(log, "a") as f:
            f.write(line + "\n")
    return stamp


def cmd_list(state, args):
    for link in state["links"]:
        print("%-8s %s <-> %s   toggles: %s" % (
            label(link), link["a_if"], link["b_if"], ", ".join(link["toggle"])))


def cmd_set(state, args, up):
    a, b = parse_pair(args.link)
    set_link(find_link(state, a, b), up, args.dry_run, args.log)


def cmd_flap(state, args):
    a, b = parse_pair(args.link)
    link = find_link(state, a, b)
    print("waiting %.1f s, then failing %s for %.1f s" % (args.after, label(link), args.duration),
          flush=True)
    time.sleep(args.after)
    set_link(link, False, args.dry_run, args.log)
    try:
        time.sleep(args.duration)
    finally:
        # restore the link even if the wait is interrupted (Ctrl+C)
        set_link(link, True, args.dry_run, args.log)


def build_parser():
    p = argparse.ArgumentParser(description="Fail and restore links of the running Mininet network")
    p.add_argument("--state", default=STATE_FILE, help="state file written by topology.py")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--log", help="also append the timestamped lines to this file")
        sp.add_argument("--dry-run", action="store_true", help="print the commands without running them")

    sub.add_parser("list", help="show the links of the running network")
    for name, text in (("down", "fail a link"), ("up", "restore a link")):
        sp = sub.add_parser(name, help=text)
        sp.add_argument("link", nargs="+", help="the two ends, e.g. s1 s3 or s1-s3")
        common(sp)
    sp = sub.add_parser("flap", help="fail a link after a delay, then restore it")
    sp.add_argument("link", nargs="+", help="the two ends, e.g. s1 s3 or s1-s3")
    sp.add_argument("--after", type=float, default=0, help="seconds to wait before failing it (default 0)")
    sp.add_argument("--for", dest="duration", type=float, required=True,
                    help="seconds to keep it down before restoring it")
    common(sp)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command != "list" and not args.dry_run and os.geteuid() != 0:
            raise LinkCtlError("changing links needs root. Run it with sudo.")
        state = load_state(args.state)
        if args.command == "list":
            cmd_list(state, args)
        elif args.command == "flap":
            if args.after < 0 or args.duration <= 0:
                raise LinkCtlError("--after must be 0 or more and --for must be above 0")
            cmd_flap(state, args)
        else:
            cmd_set(state, args, up=(args.command == "up"))
    except LinkCtlError as e:
        sys.exit("linkctl: %s" % e)


if __name__ == "__main__":
    main()
