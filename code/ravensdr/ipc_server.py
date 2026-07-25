# Radio-side IPC server.
#
# Listens on a Unix socket, dispatches `req` messages through a CommandRegistry,
# and broadcasts `ev` messages to every connected UI.
#
# Threading/socket model: this module imports `socket` and `threading` plainly,
# so it adapts to whichever implementation the host process is running.
#
#   - In the standalone radio daemon (phase 18 endpoint) those are the real
#     stdlib modules: real threads, real sockets, no eventlet anywhere.
#   - Today the radio half still lives inside the monkey-patched app, so both
#     are eventlet's green versions.
#
# Both are self-consistent and safe — green threads blocking in recv() yield to
# the hub rather than stalling it. What must NEVER happen is mixing the two (a
# real OS thread touching green primitives), which is the failure documented in
# emit_bridge.py. The one behavioural difference worth knowing: a green socket
# raises EOFError where a plain socket returns b"" — both are handled below.

import logging
import os
import socket
import threading

from ravensdr.ipc import (
    CommandRegistry, FrameBuffer, ProtocolError, encode, make_event, REQ,
)

log = logging.getLogger(__name__)


def _shutdown_close(sock):
    """Force a socket down, waking any thread blocked reading it.

    close() alone is NOT enough: while another thread sits in recv() on the same
    fd, the kernel keeps the underlying socket alive, so no FIN is sent and the
    peer goes on believing the link is healthy. shutdown() tears the connection
    down immediately and releases the blocked reader. Without this, a radio
    process that exits leaves every UI reporting LINK UP forever.
    """
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass    # already dead, or a listener that doesn't support it
    try:
        sock.close()
    except OSError:
        pass


class IpcServer:
    """Accepts UI connections, answers commands, fans out events."""

    def __init__(self, socket_path, registry=None, socket_mode=0o660):
        self.socket_path = socket_path
        self.registry = registry or CommandRegistry()
        self._socket_mode = socket_mode
        self._listener = None
        self._clients = set()          # connected client sockets
        self._clients_lock = threading.Lock()
        self._running = False
        self._accept_thread = None

    # ── Lifecycle ──

    def start(self):
        if self._running:
            return
        self._prepare_socket_path()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(self.socket_path)
        listener.listen(8)
        try:
            os.chmod(self.socket_path, self._socket_mode)
        except OSError as e:
            log.warning("could not chmod %s: %s", self.socket_path, e)
        self._listener = listener
        self._running = True
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="ipc-accept", daemon=True)
        self._accept_thread.start()
        log.info("IPC server listening on %s (commands: %s)",
                 self.socket_path, ", ".join(self.registry.names) or "none")

    def _prepare_socket_path(self):
        directory = os.path.dirname(self.socket_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # A leftover socket file from an unclean exit would make bind() fail with
        # EADDRINUSE even though nothing is listening.
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError as e:
                log.warning("could not remove stale socket %s: %s",
                            self.socket_path, e)

    def stop(self):
        self._running = False
        listener, self._listener = self._listener, None
        _shutdown_close(listener)
        with self._clients_lock:
            clients, self._clients = set(self._clients), set()
        for sock in clients:
            _shutdown_close(sock)
        try:
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
        except OSError:
            pass
        log.info("IPC server stopped")

    @property
    def client_count(self):
        with self._clients_lock:
            return len(self._clients)

    # ── Accept / serve ──

    def _accept_loop(self):
        while self._running:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                if self._running:
                    log.debug("accept failed; listener closed")
                return
            with self._clients_lock:
                self._clients.add(conn)
            log.info("UI connected (%d client(s))", self.client_count)
            threading.Thread(target=self._serve_client, args=(conn,),
                             name="ipc-client", daemon=True).start()

    def _serve_client(self, conn):
        frames = FrameBuffer()
        try:
            while self._running:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                for msg in frames.feed(chunk):
                    if msg.get("t") != REQ:
                        continue    # radio only accepts requests
                    reply = self.registry.dispatch(msg)
                    conn.sendall(encode(reply))
        except ProtocolError as e:
            log.warning("dropping UI connection: %s", e)
        except EOFError:
            # eventlet's green socket raises EOFError on a closed peer where a
            # plain socket returns b"". The radio currently runs inside the
            # monkey-patched app, so `socket` here is green and a normal UI
            # disconnect took this path — logging a traceback for an entirely
            # routine event. Both shapes mean the same thing: peer went away.
            pass
        except OSError:
            pass
        finally:
            self._drop_client(conn)
            log.info("UI disconnected (%d client(s))", self.client_count)

    def _drop_client(self, conn):
        with self._clients_lock:
            self._clients.discard(conn)
        _shutdown_close(conn)

    # ── Events ──

    def broadcast(self, name, data=None):
        """Push an event to every connected UI. Never raises.

        A dead or wedged UI must not be able to break the radio, so send failures
        just drop that client.
        """
        payload = encode(make_event(name, data))
        with self._clients_lock:
            targets = list(self._clients)
        for sock in targets:
            try:
                sock.sendall(payload)
            except OSError:
                self._drop_client(sock)
