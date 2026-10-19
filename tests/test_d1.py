"""D1 black-box conformance + integration tests (protocol.md).
Talks raw TCP to a running server, so it works against P2's server.py or the reference one.

  SERVER_HOST=10.0.0.10 python3 tests/test_d1.py -v      # inside Mininet (e.g. from c1)
  python3 tests/test_d1.py -v                             # localhost, starts reference server
"""
import os, sys, socket, threading, time, unittest, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import *

HOST = os.environ.get("SERVER_HOST")
SLOW = os.environ.get("RUN_SLOW") == "1"   # heartbeat-timeout test takes ~8 s


def connect(port):
    s = socket.create_connection((HOST, port), timeout=10)
    return s, LineReader(s)

def recv_msg(r):
    line = r.readline()
    return None if line is None else json.loads(line)

class FakeWorker:
    """Speaks the worker side of the protocol; records jobs; auto-replies with RESULT."""
    def __init__(self, wid, run_time=0.2, heartbeat=True):
        self.wid, self.jobs, self.run_time, self.hb = wid, [], run_time, heartbeat
        self.sock, self.r = connect(WORKER_PORT)
        self.stop = False
        self.sock.sendall(encode({"type": "REGISTER", "worker_id": wid}))
        self.ack = recv_msg(self.r)
        threading.Thread(target=self.loop, daemon=True).start()
        if heartbeat: threading.Thread(target=self.beat, daemon=True).start()

    def beat(self):
        while not self.stop:
            try: self.sock.sendall(encode({"type": "HEARTBEAT", "worker_id": self.wid, "load": 0}))
            except OSError: return
            time.sleep(HEARTBEAT_INTERVAL)

    def loop(self):
        while not self.stop:
            try: m = recv_msg(self.r)
            except (OSError, ValueError): return
            if m is None: return
            if m["type"] == "JOB":
                self.jobs.append(m["job_id"])
                time.sleep(self.run_time)
                try: self.sock.sendall(encode({"type": "RESULT", "job_id": m["job_id"],
                                               "worker_id": self.wid, "status": "completed"}))
                except OSError: return

    def close(self):
        self.stop = True
        try: self.sock.close()
        except OSError: pass


def submit(r_sock, job_id, duration=1):
    s, r = r_sock
    s.sendall(encode({"type": "JOB", "job_id": job_id, "payload_size": 1000, "duration": duration}))

_next = [int(time.time()) % 100000 * 10]
def fresh_id():
    _next[0] += 1
    return _next[0]


class TestWorkerChannel(unittest.TestCase):
    def test_T01_register_gets_ack(self):
        w = FakeWorker("w1"); self.addCleanup(w.close)
        self.assertEqual(w.ack, {"type": "ACK", "ref": "REGISTER", "worker_id": "w1"})

    def test_T02_heartbeat_has_no_reply(self):
        s, r = connect(WORKER_PORT); self.addCleanup(s.close)
        s.sendall(encode({"type": "REGISTER", "worker_id": "w2"})); recv_msg(r)
        s.sendall(encode({"type": "HEARTBEAT", "worker_id": "w2", "load": 0}))
        s.settimeout(1.0)
        with self.assertRaises(socket.timeout):
            r.readline()


class TestClientChannel(unittest.TestCase):
    def setUp(self):
        self.workers = [FakeWorker(w) for w in ("w1", "w2", "w3")]
        for w in self.workers: self.addCleanup(w.close)
        time.sleep(0.3)

    def test_T03_job_ack_then_result(self):
        c = connect(CLIENT_PORT); self.addCleanup(c[0].close)
        jid = fresh_id(); submit(c, jid)
        ack = recv_msg(c[1])
        self.assertEqual(ack, {"type": "ACK", "ref": "JOB", "job_id": jid})
        res = recv_msg(c[1])
        self.assertEqual((res["type"], res["job_id"], res["status"]), ("RESULT", jid, "completed"))
        self.assertIn(res["worker_id"], ("w1", "w2", "w3"))

    def test_T04_round_robin_distribution(self):
        # 3 sequential batches so rr order is observable: 9 jobs -> each worker gets 3
        c = connect(CLIENT_PORT); self.addCleanup(c[0].close)
        before = [len(w.jobs) for w in self.workers]
        ids = [fresh_id() for _ in range(9)]
        for j in ids: submit(c, j)
        got = 0
        while got < 18:                      # 9 ACK + 9 RESULT
            if recv_msg(c[1]) is None: break
            got += 1
        after = [len(w.jobs) for w in self.workers]
        self.assertEqual([a - b for a, b in zip(after, before)], [3, 3, 3])

    def test_T05_three_concurrent_clients(self):
        results = {}
        def run(n):
            c = connect(CLIENT_PORT)
            ids = [fresh_id() for _ in range(3)]
            for j in ids: submit(c, j)
            done = set()
            while len(done) < 3:
                m = recv_msg(c[1])
                if m is None: break
                if m["type"] == "RESULT": done.add(m["job_id"])
            results[n] = (set(ids), done); c[0].close()
        ts = [threading.Thread(target=run, args=(n,)) for n in range(3)]
        [t.start() for t in ts]; [t.join(30) for t in ts]
        for n in range(3):
            self.assertEqual(results[n][0], results[n][1], "client %d lost results" % n)

    def test_T06_duplicate_job_id(self):
        c = connect(CLIENT_PORT); self.addCleanup(c[0].close)
        jid = fresh_id(); submit(c, jid); recv_msg(c[1])  # ACK
        submit(c, jid)
        seen = []
        while True:
            m = recv_msg(c[1]); seen.append(m)
            if m["type"] == "ERROR": break
        self.assertEqual(m["code"], "DUPLICATE_JOB")

    def _send_raw_expect_error(self, raw, code):
        c = connect(CLIENT_PORT); self.addCleanup(c[0].close)
        c[0].sendall(raw)
        m = recv_msg(c[1])
        self.assertEqual((m["type"], m["code"]), ("ERROR", code))
        # connection must stay usable after an ERROR
        jid = fresh_id(); submit(c, jid)
        self.assertEqual(recv_msg(c[1])["type"], "ACK")

    def test_T07_bad_json(self):        self._send_raw_expect_error(b"{not json\n", "BAD_JSON")
    def test_T08_unknown_type(self):    self._send_raw_expect_error(b'{"type":"PING"}\n', "UNKNOWN_TYPE")
    def test_T09_missing_field(self):   self._send_raw_expect_error(b'{"type":"JOB","job_id":5}\n', "MISSING_FIELD")
    def test_T10_bad_value(self):
        self._send_raw_expect_error(b'{"type":"JOB","job_id":-1,"payload_size":10,"duration":1}\n', "BAD_VALUE")

    def test_T11_message_split_across_packets(self):
        c = connect(CLIENT_PORT); self.addCleanup(c[0].close)
        jid = fresh_id()
        raw = encode({"type": "JOB", "job_id": jid, "payload_size": 10, "duration": 1})
        c[0].sendall(raw[:10]); time.sleep(0.3); c[0].sendall(raw[10:])
        self.assertEqual(recv_msg(c[1])["job_id"], jid)

    def test_T12_two_messages_in_one_packet(self):
        c = connect(CLIENT_PORT); self.addCleanup(c[0].close)
        a, b = fresh_id(), fresh_id()
        c[0].sendall(encode({"type": "JOB", "job_id": a, "payload_size": 10, "duration": 1}) +
                     encode({"type": "JOB", "job_id": b, "payload_size": 10, "duration": 1}))
        acks = set()
        while len(acks) < 2:
            m = recv_msg(c[1])
            if m["type"] == "ACK": acks.add(m["job_id"])
        self.assertEqual(acks, {a, b})


@unittest.skipUnless(SLOW, "set RUN_SLOW=1 (takes ~10 s)")
class TestFailure(unittest.TestCase):
    def test_T13_dead_worker_skipped_and_job_requeued(self):
        ws = [FakeWorker("w1"), FakeWorker("w2", heartbeat=False, run_time=999), FakeWorker("w3")]
        for w in ws: self.addCleanup(w.close)
        time.sleep(0.3)
        c = connect(CLIENT_PORT); self.addCleanup(c[0].close)
        j1, j2 = fresh_id(), fresh_id()
        submit(c, j1); submit(c, j2)         # j2 lands on w2 (silent worker) -> must be requeued
        done = set()
        c[0].settimeout(HEARTBEAT_TIMEOUT + 10)
        while done != {j1, j2}:
            m = recv_msg(c[1])
            if m and m["type"] == "RESULT": done.add(m["job_id"])
        self.assertEqual(done, {j1, j2})


if __name__ == "__main__":
    if HOST is None:                        # local mode: start the reference server
        HOST = "127.0.0.1"
        from reference_server import Server
        Server(HOST); time.sleep(0.3)
    unittest.main(argv=[sys.argv[0]] + [a for a in sys.argv[1:]])
