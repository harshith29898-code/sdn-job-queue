"""D2 scheduling tests: does the server pick jobs in the order each algorithm promises?

These tests start the REAL app/server.py themselves (one fresh server per test,
with the algorithm under test) and talk to it over localhost. They need no
Mininet and no POX, so they run on any computer:

    python3 tests/test_scheduling.py -v

How each test works
-------------------
1. Start the server with the algorithm under test.
2. Connect ONE fake worker. Give it a first "blocker" job and make it hold on to
   that job, so the worker is busy.
3. Submit several more jobs while the worker is busy. They pile up in the queue.
4. Let the worker finish the blocker. It then receives the queued jobs one at a
   time, in the order the scheduler chose. We record that order and compare it
   with what the algorithm should do.

Port 5000 (clients) and 5001 (workers) must be free, so stop any running server
first.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from common import LineReader, encode  # noqa: E402

SERVER_PATH = os.path.join(ROOT, "app", "server.py")
HOST = "127.0.0.1"
CLIENT_PORT = 5000
WORKER_PORT = 5001

# The server reports status to POX on port 5002. There is no POX here, so make
# that connection fail instantly (as it does when POX is down) instead of waiting
# 2 seconds each time. The server code itself is run unchanged.
LAUNCHER = r"""
import runpy, socket, sys
_real = socket.create_connection
def _no_pox(address, *a, **k):
    if address[1] == 5002:
        raise ConnectionRefusedError("no POX in scheduling tests")
    return _real(address, *a, **k)
socket.create_connection = _no_pox
server_path = sys.argv[1]
sys.argv = [server_path] + sys.argv[2:]
runpy.run_path(server_path, run_name="__main__")
"""

BLOCKER = 1  # job id of the job that keeps the worker busy


class ServerProcess:
    def __init__(self, *args):
        self.log = tempfile.NamedTemporaryFile(mode="w+", suffix=".log", delete=False)
        self.proc = subprocess.Popen(
            [sys.executable, "-c", LAUNCHER, SERVER_PATH, *args],
            stdout=self.log, stderr=subprocess.STDOUT, cwd=ROOT)

    def wait_ready(self, timeout=8):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return False
            try:
                socket.create_connection((HOST, WORKER_PORT), timeout=0.3).close()
                return True
            except OSError:
                time.sleep(0.1)
        return False

    def output(self):
        self.log.flush()
        with open(self.log.name) as f:
            return f.read()

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log.close()
        try:
            os.unlink(self.log.name)
        except OSError:
            pass


class HoldingWorker:
    """Fake worker. Holds its FIRST job until released, then answers every job at once."""

    def __init__(self, worker_id="w1"):
        self.worker_id = worker_id
        self.order = []                 # job ids in the order they were given to us
        self.release_first = threading.Event()
        self.stopped = False
        self.sock = socket.create_connection((HOST, WORKER_PORT), timeout=10)
        self.sock.settimeout(None)
        self.reader = LineReader(self.sock)
        self.sock.sendall(encode({"type": "REGISTER", "worker_id": worker_id}))
        ack = json.loads(self.reader.readline())
        assert ack.get("type") == "ACK", ack
        threading.Thread(target=self._loop, daemon=True).start()
        threading.Thread(target=self._heartbeat, daemon=True).start()

    def _heartbeat(self):
        while not self.stopped:
            try:
                self.sock.sendall(encode({"type": "HEARTBEAT",
                                          "worker_id": self.worker_id, "load": 1}))
            except OSError:
                return
            time.sleep(1)

    def _loop(self):
        while not self.stopped:
            try:
                line = self.reader.readline()
            except OSError:
                return
            if line is None:
                return
            msg = json.loads(line)
            if msg.get("type") != "JOB":
                continue
            self.order.append(msg["job_id"])
            if len(self.order) == 1:
                self.release_first.wait(30)
            try:
                self.sock.sendall(encode({"type": "RESULT", "job_id": msg["job_id"],
                                          "worker_id": self.worker_id,
                                          "status": "completed"}))
            except OSError:
                return

    def close(self):
        self.stopped = True
        try:
            self.sock.close()
        except OSError:
            pass


class Client:
    def __init__(self):
        self.sock = socket.create_connection((HOST, CLIENT_PORT), timeout=10)
        self.reader = LineReader(self.sock)

    def submit(self, job_id, duration=1, priority=None):
        msg = {"type": "JOB", "job_id": job_id, "payload_size": 100, "duration": duration}
        if priority is not None:
            msg["priority"] = priority
        self.sock.sendall(encode(msg))

    def next_message(self):
        line = self.reader.readline()
        return None if line is None else json.loads(line)

    def wait_for_ack(self, job_id):
        while True:
            msg = self.next_message()
            if msg is None:
                raise AssertionError("server closed the connection")
            if msg.get("type") == "ACK" and msg.get("job_id") == job_id:
                return
            if msg.get("type") == "ERROR":
                raise AssertionError("server rejected job %s: %s" % (job_id, msg))

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class SchedulingBase(unittest.TestCase):
    def start_server(self, *args):
        server = ServerProcess(*args)
        self.addCleanup(server.stop)
        if not server.wait_ready():
            self.fail("server did not start. Is port 5000/5001 already in use?\n"
                      + server.output())
        self.server = server
        return server

    def run_scenario(self, server_args, jobs):
        """jobs: list of dicts with id, duration, priority, delay (seconds to wait
        BEFORE submitting that job). Returns the order the worker received them,
        not counting the blocker job."""
        self.start_server(*server_args)
        worker = HoldingWorker()
        self.addCleanup(worker.close)
        client = Client()
        self.addCleanup(client.close)

        client.submit(BLOCKER)                      # keeps the worker busy
        client.wait_for_ack(BLOCKER)
        deadline = time.time() + 5
        while not worker.order and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(worker.order, [BLOCKER], "worker should be holding the blocker")

        for job in jobs:
            time.sleep(job.get("delay", 0.3))
            client.submit(job["id"], job.get("duration", 1), job.get("priority"))
            client.wait_for_ack(job["id"])

        time.sleep(0.2)
        worker.release_first.set()
        deadline = time.time() + 10
        while len(worker.order) < len(jobs) + 1 and time.time() < deadline:
            time.sleep(0.05)
        return worker.order[1:]


class TestAlgorithms(SchedulingBase):
    def test_S01_fcfs_serves_in_arrival_order(self):
        jobs = [dict(id=11, duration=3, priority=0),
                dict(id=12, duration=1, priority=9),
                dict(id=13, duration=2, priority=5)]
        order = self.run_scenario(["--scheduler", "fcfs"], jobs)
        self.assertEqual(order, [11, 12, 13])

    def test_S02_sjf_serves_shortest_job_first(self):
        jobs = [dict(id=11, duration=3),
                dict(id=12, duration=1),
                dict(id=13, duration=2)]
        order = self.run_scenario(["--scheduler", "sjf"], jobs)
        self.assertEqual(order, [12, 13, 11])

    def test_S03_priority_without_aging_serves_highest_priority_first(self):
        jobs = [dict(id=11, priority=2),
                dict(id=12, priority=9),
                dict(id=13, priority=5)]
        order = self.run_scenario(["--scheduler", "priority", "--aging-rate", "0"], jobs)
        self.assertEqual(order, [12, 13, 11])

    def test_S04_priority_equal_priorities_behave_like_fcfs(self):
        jobs = [dict(id=11, priority=3),
                dict(id=12, priority=3),
                dict(id=13, priority=3)]
        order = self.run_scenario(["--scheduler", "priority", "--aging-rate", "1"], jobs)
        self.assertEqual(order, [11, 12, 13])

    def test_S05_without_aging_low_priority_job_is_overtaken(self):
        # control for S06: with no aging the newer, higher-priority job goes first
        jobs = [dict(id=11, priority=0),
                dict(id=12, priority=5, delay=2.0)]
        order = self.run_scenario(["--scheduler", "priority", "--aging-rate", "0"], jobs)
        self.assertEqual(order, [12, 11])

    def test_S06_aging_lets_an_old_low_priority_job_overtake(self):
        # job 11 (priority 0) waits 2 s longer than job 12 (priority 5).
        # With aging 5 points/second, that extra 10 points beats the 5-point gap.
        jobs = [dict(id=11, priority=0),
                dict(id=12, priority=5, delay=2.0)]
        order = self.run_scenario(["--scheduler", "priority", "--aging-rate", "5"], jobs)
        self.assertEqual(order, [11, 12])


class TestInputChecks(SchedulingBase):
    def _expect_error(self, priority, code="BAD_VALUE"):
        self.start_server("--scheduler", "priority")
        client = Client()
        self.addCleanup(client.close)
        client.sock.sendall(encode({"type": "JOB", "job_id": 77, "payload_size": 10,
                                    "duration": 1, "priority": priority}))
        msg = client.next_message()
        self.assertEqual((msg["type"], msg["code"]), ("ERROR", code))

    def test_S07_priority_above_range_rejected(self):
        self._expect_error(11)

    def test_S08_negative_priority_rejected(self):
        self._expect_error(-1)

    def test_S09_non_numeric_priority_rejected(self):
        self._expect_error("high")

    def test_S10_negative_aging_rate_rejected_at_startup(self):
        result = subprocess.run(
            [sys.executable, SERVER_PATH, "--scheduler", "priority", "--aging-rate", "-1"],
            capture_output=True, text=True, cwd=ROOT, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("aging-rate", result.stderr)


if __name__ == "__main__":
    unittest.main()
