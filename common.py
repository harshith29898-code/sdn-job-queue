"""Shared definitions for the Distributed Job Queue project (see protocol.md)."""
import json

# ---- Network ----
IPS = {
    "c1": "10.0.0.1", "c2": "10.0.0.2", "c3": "10.0.0.3",
    "server": "10.0.0.10",
    "w1": "10.0.0.21", "w2": "10.0.0.22", "w3": "10.0.0.23",
}
SERVER_IP = IPS["server"]
CLIENT_PORT = 5000      # client <-> server
WORKER_PORT = 5001      # server <-> workers
CONTROLLER_PORT = 5002  # server <-> POX
CONTROLLER_IP = "10.0.0.254"

# ---- Timing ----
HEARTBEAT_INTERVAL = 2   # seconds
HEARTBEAT_TIMEOUT = 6    # seconds
CONTROLLER_RETRY = 5     # seconds

# ---- Limits ----
MAX_LINE = 4096
MAX_PAYLOAD = 1_000_000
MAX_DURATION = 60

# ---- Message types / error codes ----
TYPES = {"REGISTER", "HEARTBEAT", "JOB", "RESULT", "ACK", "ERROR"}
REQUIRED = {
    "REGISTER": ["worker_id"],
    "HEARTBEAT": ["worker_id", "load"],
    "JOB": ["job_id", "payload_size", "duration"],
    "RESULT": ["job_id", "worker_id", "status"],
    "ACK": ["ref"],
    "ERROR": ["code"],
}


class ProtocolError(Exception):
    def __init__(self, code, message=""):
        super().__init__(message)
        self.code = code
        self.message = message


def encode(msg: dict) -> bytes:
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")


def make_error(code, message="", job_id=None) -> bytes:
    m = {"type": "ERROR", "code": code, "message": message}
    if job_id is not None:
        m["job_id"] = job_id
    return encode(m)


def decode(line: bytes) -> dict:
    """Parse one line and validate it. Raises ProtocolError with a protocol.md code."""
    if len(line) > MAX_LINE:
        raise ProtocolError("TOO_LONG", "line over %d bytes" % MAX_LINE)
    try:
        msg = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ProtocolError("BAD_JSON", "could not parse line")
    if not isinstance(msg, dict):
        raise ProtocolError("BAD_JSON", "not a JSON object")
    t = msg.get("type")
    if t not in TYPES:
        raise ProtocolError("UNKNOWN_TYPE", "unknown type %r" % (t,))
    for f in REQUIRED[t]:
        if f not in msg:
            raise ProtocolError("MISSING_FIELD", "%s missing %s" % (t, f))
    _check_values(msg)
    return msg


def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def _check_values(msg):
    t = msg["type"]
    if t == "JOB":
        if not _is_int(msg["job_id"]) or msg["job_id"] <= 0:
            raise ProtocolError("BAD_VALUE", "job_id must be int > 0")
        if not _is_int(msg["payload_size"]) or not 0 <= msg["payload_size"] <= MAX_PAYLOAD:
            raise ProtocolError("BAD_VALUE", "payload_size out of range")
        d = msg["duration"]
        if isinstance(d, bool) or not isinstance(d, (int, float)) or not 0 < d <= MAX_DURATION:
            raise ProtocolError("BAD_VALUE", "duration out of range")
    elif t == "HEARTBEAT":
        if not _is_int(msg["load"]) or msg["load"] < 0:
            raise ProtocolError("BAD_VALUE", "load must be int >= 0")
    elif t == "RESULT":
        if msg["status"] not in ("completed", "failed"):
            raise ProtocolError("BAD_VALUE", "status must be completed|failed")
    if t in ("REGISTER", "HEARTBEAT", "RESULT") and not isinstance(msg["worker_id"], str):
        raise ProtocolError("BAD_VALUE", "worker_id must be string")


class LineReader:
    """Buffers a TCP byte stream and yields complete lines (handles partial reads)."""
    def __init__(self, sock):
        self.sock, self.buf = sock, b""

    def readline(self):
        while b"\n" not in self.buf:
            if len(self.buf) > MAX_LINE:
                line, self.buf = self.buf, b""
                return line
            chunk = self.sock.recv(4096)
            if not chunk:
                return None
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line
