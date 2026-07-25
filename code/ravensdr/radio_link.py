# UI-side client for the radio process.
#
# This is the whole point of the two-process split: the UI owns no hardware and
# never blocks on it. If the radio daemon is starting, restarting, wedged, or
# outright dead, the console still loads and renders — it just reports LINK DOWN
# and refuses commands with a clear reason instead of hanging or 500-ing.
#
# Link state is first-class C2 telemetry, not an implementation detail: the
# console shows LINK UP/DOWN alongside the SDR's commanded/actual state so an
# operator can tell "the radio says nothing is tuned" apart from "I can't reach
# the radio at all".

import logging
import socket
import time

from ravensdr.ipc import FrameBuffer, ProtocolError, make_request, EV, RES

log = logging.getLogger(__name__)

LINK_UP = "UP"
LINK_DOWN = "DOWN"

DEFAULT_TIMEOUT = 10.0        # seconds to wait for a command response
RECONNECT_MIN = 0.5           # initial reconnect backoff
RECONNECT_MAX = 10.0          # backoff ceiling — stay responsive after an outage


class RadioLinkError(Exception):
    """A command could not be completed (link down, timeout, or radio error)."""


class RadioLink:
    """Connection to the radio daemon, with auto-reconnect.

    Injectables (spawn_fn / event_factory / sleep_fn) keep this testable and let
    the UI drive it with eventlet greenthreads without importing eventlet here.
    """

    def __init__(self, socket_path, spawn_fn, event_factory, sleep_fn=None,
                 on_event=None, on_link_change=None, timeout=DEFAULT_TIMEOUT):
        self._path = socket_path
        self._spawn = spawn_fn
        self._new_event = event_factory     # () -> object with .wait()/.send()
        self._sleep = sleep_fn or time.sleep
        self._on_event = on_event or (lambda name, data: None)
        self._on_link_change = on_link_change or (lambda snapshot: None)
        self._timeout = timeout

        self._sock = None
        self._link = LINK_DOWN
        self._since = time.time()
        self._reconnects = 0
        self._last_error = None
        self._running = False

        self._next_id = 1
        self._waiters = {}      # req_id -> event
        self._replies = {}      # req_id -> res message

    # ── Observable link state ──

    @property
    def link(self):
        return self._link

    @property
    def is_up(self):
        return self._link == LINK_UP

    def snapshot(self):
        return {
            "link": self._link,
            "socket": self._path,
            "reconnects": self._reconnects,
            "last_error": self._last_error,
            "for": round(max(0.0, time.time() - self._since), 1),
        }

    def _set_link(self, state, error=None):
        if state == self._link and error == self._last_error:
            return
        self._link = state
        self._last_error = error
        self._since = time.time()
        log.info("Radio link %s%s", state, f" ({error})" if error else "")
        self._on_link_change(self.snapshot())

    # ── Lifecycle ──

    def start(self):
        """Begin connecting; returns immediately (never blocks UI startup)."""
        if self._running:
            return
        self._running = True
        self._spawn(self._connect_loop)

    def stop(self):
        self._running = False
        self._close()

    def _close(self):
        sock, self._sock = self._sock, None
        if sock is not None:
            # shutdown() before close() so the reader thread blocked in recv()
            # actually wakes and the radio sees us go away — see
            # ipc_server._shutdown_close for why close() alone is insufficient.
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        # Wake anyone waiting — their command died with the connection.
        for req_id, ev in list(self._waiters.items()):
            self._replies.setdefault(req_id, None)
            try:
                ev.send(None)
            except Exception:
                pass
        self._waiters.clear()

    def _connect_loop(self):
        """Connect, read until the link drops, then back off and retry forever."""
        backoff = RECONNECT_MIN
        while self._running:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(self._path)
            except OSError as e:
                self._set_link(LINK_DOWN, f"connect failed: {e.strerror or e}")
                self._sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)
                continue

            self._sock = sock
            self._reconnects += 1
            self._set_link(LINK_UP)
            backoff = RECONNECT_MIN
            try:
                self._read_until_closed(sock)
                reason = "radio closed the connection"
            except (OSError, ProtocolError) as e:
                reason = str(e)
            self._close()
            if self._running:
                self._set_link(LINK_DOWN, reason)
                self._sleep(backoff)

    def _read_until_closed(self, sock):
        frames = FrameBuffer()
        while self._running:
            chunk = sock.recv(65536)
            if not chunk:
                return
            for msg in frames.feed(chunk):
                self._handle(msg)

    def _handle(self, msg):
        kind = msg.get("t")
        if kind == EV:
            try:
                self._on_event(msg.get("name"), msg.get("data") or {})
            except Exception:
                log.exception("radio event handler failed: %s", msg.get("name"))
            return
        if kind == RES:
            req_id = msg.get("id")
            waiter = self._waiters.pop(req_id, None)
            self._replies[req_id] = msg
            if waiter is not None:
                try:
                    waiter.send(None)
                except Exception:
                    pass

    # ── Commands ──

    def request(self, cmd, args=None, timeout=None):
        """Send a command and wait for its response.

        Raises RadioLinkError if the link is down, the radio reports an error, or
        the response does not arrive in time. Callers in HTTP handlers should let
        that surface as a clear message — never as a hang.
        """
        sock = self._sock
        if sock is None or self._link != LINK_UP:
            raise RadioLinkError(
                f"radio link is {self._link} — the radio service may be stopped "
                f"or restarting")

        req_id = self._next_id
        self._next_id += 1
        ev = self._new_event()
        self._waiters[req_id] = ev

        try:
            sock.sendall(_encode_request(req_id, cmd, args))
        except OSError as e:
            self._waiters.pop(req_id, None)
            raise RadioLinkError(f"failed to send {cmd}: {e}") from e

        if not _wait_with_timeout(ev, timeout or self._timeout, self._sleep,
                                  lambda: req_id in self._replies):
            self._waiters.pop(req_id, None)
            self._replies.pop(req_id, None)
            raise RadioLinkError(
                f"{cmd} timed out after {timeout or self._timeout:.0f}s")

        reply = self._replies.pop(req_id, None)
        if reply is None:
            raise RadioLinkError(f"{cmd} failed — link dropped mid-command")
        if not reply.get("ok"):
            raise RadioLinkError(reply.get("error") or f"{cmd} failed")
        return reply.get("data") or {}


def _encode_request(req_id, cmd, args):
    from ravensdr.ipc import encode
    return encode(make_request(req_id, cmd, args))


def _wait_with_timeout(ev, timeout, sleep_fn, is_done):
    """Wait for `ev`, bounded by `timeout`. True if the reply arrived."""
    deadline = time.time() + timeout
    # Poll rather than rely on a timeout-capable primitive: keeps this agnostic
    # to whether the caller handed us an eventlet Event or a test double.
    while time.time() < deadline:
        if is_done():
            return True
        sleep_fn(0.01)
    return is_done()
