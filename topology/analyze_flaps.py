#!/usr/bin/env python3
"""
analyze_flaps.py - how did each ping cope with each link failure?

Usage:
    python3 analyze_flaps.py [--tail SECONDS] FLAPS_FILE PING_LOG [PING_LOG ...]

FLAPS_FILE  the text printed by `linkctl.py flap` (lines like
            "t=1794093468.385 link s1-s3 DOWN (interfaces: ...)")
PING_LOG    output of `ping -D ...` (lines like
            "[1794093468.5] 64 bytes from 10.0.0.10: icmp_seq=12 ttl=64 time=0.4 ms")
--tail      seconds after the link comes back that still count as part of the
            failure (default 5; use 45 for --mode=static, which recovers slowly)

For every failure and every ping it prints how many replies were lost and the
longest silence between two replies in that window:
    no effect  : no replies lost (the failure did not touch that path)
    rerouted   : replies lost, silence of 3 s or less
    slow       : silence of more than 3 s but shorter than the failure
    no reroute : silence about as long as the failure or longer
It also says so when a ping log does not cover a failure (the ping started too
late or ended too early), because then its result means nothing.
"""
import os
import re
import sys

FLAP_RE = re.compile(r"t=(\d+\.\d+)\s+link\s+(\S+)\s+(DOWN|UP)")
REPLY_RE = re.compile(r"\[(\d+\.\d+)\]\s+\d+ bytes from .*icmp_seq=(\d+)")


def parse_flaps(text):
    """[(link, down_time, up_time)] in order. A DOWN with no UP is ignored."""
    flaps, open_downs = [], {}
    for line in text.splitlines():
        m = FLAP_RE.search(line)
        if not m:
            continue
        t, link, kind = float(m.group(1)), m.group(2), m.group(3)
        if kind == "DOWN":
            open_downs[link] = t
        elif link in open_downs:
            flaps.append((link, open_downs.pop(link), t))
    return flaps


def parse_ping(text):
    """[(time, seq)] of the replies, in the order they arrived."""
    return [(float(m.group(1)), int(m.group(2)))
            for m in (REPLY_RE.search(line) for line in text.splitlines()) if m]


def judge(replies, down, up, tail):
    """Returns (status, lost, longest_gap). status is one of
    'no effect', 'rerouted', 'slow', 'no reroute', 'not covered'."""
    if not replies or replies[0][0] > down - 1 or replies[-1][0] < up + 1:
        return "not covered", 0, 0.0
    end = up + tail
    lost, gap = 0, 0.0
    for (t0, s0), (t1, s1) in zip(replies, replies[1:]):
        if t1 >= down - 1 and t0 <= end:
            lost += max(s1 - s0 - 1, 0)
            gap = max(gap, t1 - t0)
    if lost == 0:
        return "no effect", 0, gap
    if gap <= 3.0:
        return "rerouted", lost, gap
    if gap >= 0.8 * (up - down):
        return "no reroute", lost, gap
    return "slow", lost, gap


def label(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    return stem.rsplit("_", 1)[-1]


def main(argv):
    tail = 5.0
    if argv and argv[0] == "--tail":
        tail = float(argv[1])
        argv = argv[2:]
    if len(argv) < 2:
        print(__doc__)
        return 2
    flaps = parse_flaps(open(argv[0]).read())
    if not flaps:
        print("No complete DOWN/UP pairs found in %s" % argv[0])
        return 1
    pings = [(label(p), parse_ping(open(p).read())) for p in argv[1:]]
    for link, down, up in flaps:
        print("%s failed at t=%.3f for %.1f s" % (link, down, up - down))
        results = [(name,) + judge(replies, down, up, tail) for name, replies in pings]
        for name, status, lost, gap in results:
            if status in ("rerouted", "slow", "no reroute"):
                print("  %-10s %-12s lost %d, longest silence %.1f s" % (status.upper(), name, lost, gap))
        quiet = [n for n, s, _, _ in results if s == "no effect"]
        if quiet:
            print("  no effect: " + " ".join(quiet))
        bad = [n for n, s, _, _ in results if s == "not covered"]
        if bad:
            print("  CHECK (ping log does not cover this failure, ignore these): " + " ".join(bad))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
