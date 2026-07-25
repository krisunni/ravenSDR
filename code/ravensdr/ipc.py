# IPC between the UI process and the radio process.
#
# ravenSDR runs as two services so the console is never taken down by the
# hardware (see architecture/design.md §3.9 and phase-18):
#
#   ravensdr-ui.service     Flask + Socket.IO + static assets. Owns no hardware.
#   ravensdr-radio.service  RTL-SDR, Hailo, decoders, schedulers.
#
# They speak newline-delimited JSON over a Unix domain socket. NDJSON is
# deliberate: it is trivially framed, greppable in a log, and needs no schema
# compiler on a Pi.
#
# Three message kinds:
#   req  UI  -> radio   {"t":"req","id":7,"cmd":"tune","args":{...}}
#   res  radio -> UI    {"t":"res","id":7,"ok":true,"data":{...}}
#   ev   radio -> UI    {"t":"ev","name":"status","data":{...}}   (unsolicited)
#
# `ev` is what keeps the UI live: the radio pushes status/transcript/detection
# events and the UI relays them to browsers over Socket.IO.
#
# The framing/codec below is pure (no sockets) so it can be unit-tested and so
# either side can run on green or real sockets.

import json
import logging

log = logging.getLogger(__name__)

# Message-kind tags
REQ = "req"
RES = "res"
EV = "ev"

# A single message may not exceed this; guards against a desynced stream
# consuming unbounded memory while waiting for a newline that never comes.
MAX_FRAME_BYTES = 4 * 1024 * 1024

DEFAULT_SOCKET_PATH = "/run/ravensdr/radio.sock"
DEFAULT_AUDIO_SOCKET_PATH = "/run/ravensdr/audio.sock"

# Env override, so the UI and radio can be pointed at the same socket explicitly.
SOCKET_ENV_VAR = "RAVENSDR_RADIO_SOCKET"


def resolve_socket_path(filename="radio.sock", env_var=SOCKET_ENV_VAR):
    """Pick a socket path both processes can agree on and actually write to.

    /run/ravensdr is the right home, but the services run as an unprivileged user
    and cannot create it without `RuntimeDirectory=` in the unit. Rather than fail
    at bind time, fall back to /tmp/ravensdr.

    The candidate list is deliberately NOT environment-dependent beyond the
    explicit override. An earlier version consulted XDG_RUNTIME_DIR, and because
    systemd does not set it for the service, the daemon resolved
    /tmp/ravensdr/radio.sock while an interactive shell resolved
    /run/user/1000/ravensdr/radio.sock — the client then sat at LINK DOWN against
    a perfectly healthy radio. A rendezvous path must be reached by identical
    reasoning in both processes.
    """
    import os

    explicit = os.environ.get(env_var)
    if explicit:
        return explicit

    candidates = [
        os.path.join(os.path.dirname(DEFAULT_SOCKET_PATH), filename),
        os.path.join("/tmp/ravensdr", filename),
    ]

    for path in candidates:
        directory = os.path.dirname(path)
        try:
            os.makedirs(directory, exist_ok=True)
            if os.access(directory, os.W_OK):
                return path
        except OSError:
            continue
    return candidates[-1]


class ProtocolError(Exception):
    """Raised when a peer sends something unparseable or oversized."""


# ── Codec ──

def encode(msg):
    """Serialize one message to a single NDJSON frame (bytes, newline-ended)."""
    return (json.dumps(msg, separators=(",", ":"), default=str) + "\n").encode("utf-8")


def make_request(req_id, cmd, args=None):
    return {"t": REQ, "id": req_id, "cmd": cmd, "args": args or {}}


def make_response(req_id, ok, data=None, error=None):
    msg = {"t": RES, "id": req_id, "ok": bool(ok)}
    if ok:
        msg["data"] = data if data is not None else {}
    else:
        msg["error"] = error or "unknown error"
    return msg


def make_event(name, data=None):
    return {"t": EV, "name": name, "data": data if data is not None else {}}


class FrameBuffer:
    """Accumulates bytes and yields complete NDJSON messages.

    Stream sockets split writes anywhere, so a reader must buffer until it sees a
    newline. feed() returns the messages that became complete on this chunk.
    """

    def __init__(self, max_frame_bytes=MAX_FRAME_BYTES):
        self._buf = bytearray()
        self._max = max_frame_bytes

    def feed(self, chunk):
        """Add received bytes; return a list of decoded messages."""
        if not chunk:
            return []
        self._buf.extend(chunk)
        if len(self._buf) > self._max:
            # Drop the buffer rather than grow forever on a desynced stream.
            self._buf.clear()
            raise ProtocolError(
                f"frame exceeded {self._max} bytes without a newline — stream desynced")

        messages = []
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(self._buf[:nl])
            del self._buf[:nl + 1]
            if not line.strip():
                continue
            messages.append(self._decode_line(line))
        return messages

    @staticmethod
    def _decode_line(line):
        try:
            msg = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise ProtocolError(f"bad JSON frame: {e}") from e
        if not isinstance(msg, dict):
            raise ProtocolError(f"frame is not an object: {type(msg).__name__}")
        if msg.get("t") not in (REQ, RES, EV):
            raise ProtocolError(f"unknown message kind: {msg.get('t')!r}")
        return msg

    @property
    def pending_bytes(self):
        return len(self._buf)


# ── Command dispatch (radio side) ──

class CommandRegistry:
    """Maps command names to handlers on the radio side.

    A handler takes the request's `args` dict and returns the response payload.
    Raising is fine — dispatch converts it into an error response so one bad
    command never drops the connection.
    """

    def __init__(self):
        self._handlers = {}

    def register(self, name, handler):
        if name in self._handlers:
            raise ValueError(f"command already registered: {name}")
        self._handlers[name] = handler

    def command(self, name):
        """Decorator form: @registry.command("tune")"""
        def deco(fn):
            self.register(name, fn)
            return fn
        return deco

    @property
    def names(self):
        return sorted(self._handlers)

    def dispatch(self, msg):
        """Handle one `req` message; return the `res` message to send back."""
        req_id = msg.get("id")
        cmd = msg.get("cmd")
        handler = self._handlers.get(cmd)
        if handler is None:
            return make_response(req_id, False, error=f"unknown command: {cmd}")
        try:
            data = handler(msg.get("args") or {})
        except ValueError as e:
            # Bad input from the UI (unknown preset, invalid mode) is an expected
            # client error, not a defect — report it without a stack trace so the
            # journal isn't noisy with tracebacks for ordinary validation.
            log.warning("IPC command %s rejected: %s", cmd, e)
            return make_response(req_id, False, error=f"{type(e).__name__}: {e}")
        except Exception as e:
            log.exception("IPC command %s failed", cmd)
            return make_response(req_id, False, error=f"{type(e).__name__}: {e}")
        return make_response(req_id, True, data=data)
