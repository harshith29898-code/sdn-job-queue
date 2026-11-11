#!/usr/bin/env python3
"""Tests for gui/bridge.py's log reader (no Mininet or browser):  python3 gui/test_bridge.py -v"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge  # noqa: E402


def feed(*lines, live=True):
    bridge.S.update(algo="?", workers={}, jobs={}, stats=None)
    bridge.CLOCK.update(live=live, ts=1000.0)
    for ln in lines:
        bridge.apply(ln)
    return bridge.S


class ParserTests(unittest.TestCase):
    def test_normal_flow(self):
        S = feed("[WORKER] w1 registered", "[CLIENT] Queued job 5 from ('10.0.0.1', 1)",
                 "[SCHEDULER] SJF: Job 5 -> w1 (dispatched at 2000.5)", "[RESULT] Job 5 completed by w1")
        self.assertEqual(S["jobs"][5]["state"], "done")
        self.assertEqual(S["workers"]["w1"]["state"], "idle")

    def test_glued_result_and_dispatch(self):
        # the bug: the RESULT of job 5 and the dispatch of job 6 share one line, so job 5 stayed "running"
        S = feed("[WORKER] w1 registered",
                 "[SCHEDULER] SJF: Job 5 -> w1 (dispatched at 2000.5)",
                 "[RESULT] Job 5 completed by w1[SCHEDULER] SJF: Job 6 -> w1 (dispatched at 2001.0)",
                 "[RESULT] Job 6 completed by w1[POX] Status report failed: [Errno 111] Connection refused")
        self.assertEqual({j["state"] for j in S["jobs"].values()}, {"done"})
        self.assertEqual(S["workers"]["w1"]["state"], "idle")

    def test_lost_result_line_does_not_leave_job_running(self):
        S = feed("[WORKER] w1 registered", "[SCHEDULER] FCFS: Job 1 -> w1 (dispatched at 5.0)",
                 "[SCHEDULER] FCFS: Job 2 -> w1 (dispatched at 6.0)")     # RESULT of job 1 never seen
        self.assertEqual(S["jobs"][1]["state"], "done")
        self.assertEqual(S["jobs"][2]["state"], "running")

    def test_failed_status_and_worker_id_not_swallowing_glue(self):
        S = feed("[WORKER] w1 registered", "[SCHEDULER] FCFS: Job 3 -> w1 (dispatched at 5.0)",
                 "[RESULT] Job 3 failed by w1[WORKER] w1 disconnected")
        self.assertEqual(S["jobs"][3]["state"], "failed")
        self.assertEqual(S["workers"]["w1"]["state"], "down")
        self.assertEqual(set(S["workers"]), {"w1"})

    def test_requeue_frees_worker(self):
        S = feed("[WORKER] w2 registered", "[SCHEDULER] FCFS: Job 4 -> w2 (dispatched at 5.0)",
                 "[SCHEDULER] Job 4 requeued", "[WATCHDOG] w2 heartbeat timed out")
        self.assertEqual((S["jobs"][4]["state"], S["jobs"][4]["requeues"], S["workers"]["w2"]["state"]),
                         ("queued", 1, "down"))

    def test_replayed_history_has_no_fake_waits_or_open_ended_bars(self):
        S = feed("[CLIENT] Queued job 8 from ('10.0.0.1', 1)", "[SCHEDULER] FCFS: Job 8 -> w1 (dispatched at 50.0)",
                 "[RESULT] Job 8 completed by w1", live=False)
        j = S["jobs"][8]
        self.assertTrue(j["hist"])
        self.assertEqual(j["state"], "done")
        self.assertGreaterEqual(j["t_end"], j["t_run"])


if __name__ == "__main__":
    unittest.main()
