"""End-to-end IPC tests: a real IpcServer and RadioLink over a real Unix socket.

Proves the two-process boundary actually works — request/response correlation,
event fan-out, and the behaviour that justifies the split: the UI side stays
usable (and honest about it) when the radio is absent.
"""

import threading
import time

import pytest

from ravensdr.ipc import CommandRegistry
from ravensdr.ipc_server import IpcServer
from ravensdr.radio_link import LINK_DOWN, LINK_UP, RadioLink, RadioLinkError


class _Ev:
    """Minimal event double — RadioLink only needs send()."""

    def __init__(self):
        self._flag = threading.Event()

    def send(self, _=None):
        self._flag.set()

    def wait(self, timeout=None):
        return self._flag.wait(timeout)


def _link(path, **kw):
    return RadioLink(
        socket_path=path,
        spawn_fn=lambda fn: threading.Thread(target=fn, daemon=True).start(),
        event_factory=_Ev,
        timeout=kw.pop("timeout", 5.0),
        **kw,
    )


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def sock_path(tmp_path):
    return str(tmp_path / "radio.sock")


@pytest.fixture
def server(sock_path):
    reg = CommandRegistry()
    reg.register("ping", lambda args: {"pong": args.get("n", 0)})
    reg.register("boom", lambda args: (_ for _ in ()).throw(RuntimeError("device busy")))
    srv = IpcServer(sock_path, registry=reg)
    srv.start()
    yield srv
    srv.stop()


class TestRequestResponse:
    def test_command_round_trip(self, server, sock_path):
        link = _link(sock_path)
        link.start()
        assert _wait_for(lambda: link.is_up), "link never came up"
        assert link.request("ping", {"n": 42}) == {"pong": 42}
        link.stop()

    def test_many_sequential_commands_stay_correlated(self, server, sock_path):
        link = _link(sock_path)
        link.start()
        assert _wait_for(lambda: link.is_up)
        for n in range(25):
            assert link.request("ping", {"n": n})["pong"] == n
        link.stop()

    def test_radio_side_error_surfaces_as_exception(self, server, sock_path):
        link = _link(sock_path)
        link.start()
        assert _wait_for(lambda: link.is_up)
        with pytest.raises(RadioLinkError, match="device busy"):
            link.request("boom")
        # The connection survives a failed command.
        assert link.request("ping", {"n": 1}) == {"pong": 1}
        link.stop()

    def test_unknown_command_surfaces_as_exception(self, server, sock_path):
        link = _link(sock_path)
        link.start()
        assert _wait_for(lambda: link.is_up)
        with pytest.raises(RadioLinkError, match="unknown command"):
            link.request("no_such_command")
        link.stop()


class TestEvents:
    def test_broadcast_reaches_the_ui(self, server, sock_path):
        seen = []
        link = _link(sock_path, on_event=lambda n, d: seen.append((n, d)))
        link.start()
        assert _wait_for(lambda: server.client_count == 1)
        server.broadcast("status", {"state": "LOCKED"})
        assert _wait_for(lambda: seen)
        assert seen[0] == ("status", {"state": "LOCKED"})
        link.stop()

    def test_broadcast_with_no_clients_is_harmless(self, server):
        server.broadcast("status", {"state": "LOCKED"})   # must not raise

    def test_events_reach_every_connected_ui(self, server, sock_path):
        a, b = [], []
        l1 = _link(sock_path, on_event=lambda n, d: a.append(n))
        l2 = _link(sock_path, on_event=lambda n, d: b.append(n))
        l1.start()
        l2.start()
        assert _wait_for(lambda: server.client_count == 2)
        server.broadcast("status", {})
        assert _wait_for(lambda: a and b)
        l1.stop()
        l2.stop()


class TestLinkResilience:
    def test_ui_starts_and_reports_down_when_radio_absent(self, sock_path):
        """The console must load with no radio at all — the whole point."""
        link = _link(sock_path)
        link.start()                      # nothing is listening
        time.sleep(0.2)
        assert link.link == LINK_DOWN
        snap = link.snapshot()
        assert snap["link"] == LINK_DOWN
        assert "connect failed" in (snap["last_error"] or "")
        link.stop()

    def test_command_while_down_fails_fast_with_reason(self, sock_path):
        link = _link(sock_path)
        link.start()
        time.sleep(0.2)
        started = time.time()
        with pytest.raises(RadioLinkError, match="link is DOWN"):
            link.request("ping")
        # Fails fast rather than hanging the HTTP handler.
        assert time.time() - started < 1.0
        link.stop()

    def test_link_recovers_when_radio_starts_later(self, sock_path):
        """UI comes up first, radio second — must connect on its own."""
        link = _link(sock_path)
        link.start()
        time.sleep(0.2)
        assert link.link == LINK_DOWN

        reg = CommandRegistry()
        reg.register("ping", lambda args: {"pong": 1})
        srv = IpcServer(sock_path, registry=reg)
        srv.start()
        try:
            assert _wait_for(lambda: link.link == LINK_UP, timeout=8.0)
            assert link.request("ping") == {"pong": 1}
        finally:
            link.stop()
            srv.stop()

    def test_link_drops_then_reconnects_after_radio_restart(self, sock_path):
        reg = CommandRegistry()
        reg.register("ping", lambda args: {"pong": 1})
        srv = IpcServer(sock_path, registry=reg)
        srv.start()
        link = _link(sock_path)
        link.start()
        assert _wait_for(lambda: link.is_up)

        srv.stop()                                   # radio restarts
        assert _wait_for(lambda: link.link == LINK_DOWN, timeout=8.0)

        srv2 = IpcServer(sock_path, registry=reg)
        srv2.start()
        try:
            assert _wait_for(lambda: link.is_up, timeout=10.0)
            assert link.request("ping") == {"pong": 1}
        finally:
            link.stop()
            srv2.stop()

    def test_stale_socket_file_does_not_block_startup(self, sock_path):
        """An unclean exit leaves a socket file; bind() must still succeed."""
        with open(sock_path, "w") as fh:
            fh.write("")
        reg = CommandRegistry()
        reg.register("ping", lambda args: {"pong": 1})
        srv = IpcServer(sock_path, registry=reg)
        srv.start()                                  # must not raise EADDRINUSE
        try:
            link = _link(sock_path)
            link.start()
            assert _wait_for(lambda: link.is_up)
            link.stop()
        finally:
            srv.stop()


class TestDisconnectIsQuiet:
    def test_client_disconnect_logs_no_traceback(self, server, sock_path, caplog):
        """A UI going away is routine — green sockets raise EOFError where plain
        ones return b"", and that must not surface as an error."""
        import logging
        link = _link(sock_path)
        link.start()
        assert _wait_for(lambda: server.client_count == 1)

        with caplog.at_level(logging.ERROR, logger="ravensdr.ipc_server"):
            link.stop()
            assert _wait_for(lambda: server.client_count == 0)

        assert not [r for r in caplog.records if r.exc_info], \
            "disconnect produced a traceback"
