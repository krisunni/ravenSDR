# ravensdr-ui — the console process. Owns NO hardware.
#
# Phase 18: this is the half of the split that a browser talks to. It serves the
# templates and static assets, keeps browser Socket.IO sessions, and forwards
# operator intent to the radio process over the Unix socket in radio_link.py.
#
# The defining property: it starts, serves, and renders whether or not the radio
# is alive. If the radio is stopped, restarting, or wedged, the console loads and
# reports LINK DOWN instead of hanging or refusing to start. No code path here
# opens an SDR, spawns rtl_fm, or runs inference — so nothing the hardware does
# can take the operator interface down.
#
# During the transition the radio process still serves its own Flask app. Routes
# not yet migrated to IPC commands are proxied to it over HTTP (see _proxy_get),
# so the console is fully functional at every step of the migration rather than
# only at the end.

import eventlet
eventlet.monkey_patch()

import logging
import os
import urllib.error
import urllib.request

from flask import Flask, Response, jsonify, make_response, render_template, request
from flask_socketio import SocketIO

from ravensdr.ipc import resolve_socket_path
from ravensdr.radio_link import RadioLink, RadioLinkError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("ravensdr.ui")

VERSION = "1.2.1"

UI_PORT = int(os.environ.get("RAVENSDR_UI_PORT", "5001"))
# Base URL of the radio process's legacy HTTP API, used only for routes that
# have not been migrated to IPC commands yet.
RADIO_HTTP = os.environ.get("RAVENSDR_RADIO_HTTP", "http://127.0.0.1:5000")

app = Flask(
    __name__,
    template_folder="../templates",
    static_folder="../static",
)
app.config["SECRET_KEY"] = "ravensdr-ui"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")


class _GreenEvent:
    """Minimal waiter for RadioLink; it only needs send()."""

    def __init__(self):
        self._fired = False

    def send(self, _=None):
        self._fired = True


def _on_radio_event(name, data):
    """Relay a radio event straight through to every connected browser."""
    socketio.emit(name, data)


def _on_link_change(snapshot):
    """Tell browsers the link changed, and resync once it is back."""
    log.info("Radio link %s", snapshot.get("link"))
    socketio.emit("radio_link", snapshot)
    if snapshot.get("link") == "UP":
        # A radio restart can leave the console showing stale state, so pull a
        # fresh snapshot rather than waiting for the next periodic broadcast.
        socketio.start_background_task(_resync)


def _resync():
    try:
        socketio.emit("status", radio.request("status"))
        socketio.emit("sdr_state", radio.request("sdr_state"))
    except RadioLinkError as e:
        log.warning("resync failed: %s", e)


radio = RadioLink(
    socket_path=resolve_socket_path(),
    spawn_fn=socketio.start_background_task,
    event_factory=_GreenEvent,
    sleep_fn=eventlet.sleep,
    on_event=_on_radio_event,
    on_link_change=_on_link_change,
)


def _command(cmd, args=None):
    """Run an IPC command, converting link failures into a clean HTTP error.

    Never hangs: RadioLink fails fast when the link is down and times out
    otherwise, so a dead radio yields 503 rather than a stuck request.
    """
    try:
        return jsonify(radio.request(cmd, args)), 200
    except RadioLinkError as e:
        return jsonify({"error": str(e), "link": radio.snapshot()}), 503


# ── Pages ──


def _register_asset_helper(flask_app, fallback_version):
    """Expose asset() to templates: /static/x.js?v=<mtime>.

    Keyed on the file's modification time rather than the app version, so ANY
    edit to a JS/CSS file invalidates the browser copy automatically. Relying on
    a manual version bump is what let a stale ravensdr.js keep running after a
    fix shipped — and a stale one is not cosmetic here: an old build force-tuned
    the radio on every page load.
    """
    import os as _os

    @flask_app.template_global("asset")
    def _asset(filename):
        path = _os.path.join(flask_app.static_folder, filename)
        try:
            stamp = str(int(_os.path.getmtime(path)))
        except OSError:
            stamp = fallback_version
        return f"/static/{filename}?v={stamp}"


_register_asset_helper(app, VERSION)

@app.route("/")
def index():
    """The console. Must render even with the radio down."""
    # The page must never be cached: it carries the asset URLs, so a cached
    # copy would keep pointing at old JS/CSS no matter how well those are
    # versioned. The assets themselves stay cacheable — their URLs change.
    resp = make_response(render_template("index.html", version=VERSION))
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


@app.route("/learn")
def learn():
    """Static explainer: how RF becomes a spectrogram becomes a classification.

    Served from the node itself on the same port as the console — D3 is vendored
    under static/vendor rather than pulled from a CDN, because the node is meant
    to work air-gapped and a field kit with no uplink should still render this.
    """
    resp = make_response(render_template("learn.html", version=VERSION))
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


@app.route("/api/link")
def api_link():
    return jsonify(radio.snapshot())


# ── Commands (IPC) ──

@app.route("/api/tune", methods=["POST"])
def api_tune():
    data = request.get_json(force=True)
    return _command("tune", {"preset_id": data.get("preset_id")})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    return _command("stop")


@app.route("/api/squelch", methods=["POST"])
def api_squelch():
    return _command("squelch", {"level": request.get_json(force=True).get("level", 0)})


@app.route("/api/gain", methods=["POST"])
def api_gain():
    return _command("gain", {"value": request.get_json(force=True).get("value", "auto")})


@app.route("/api/status")
def api_status():
    return _command("status")


@app.route("/api/sdr/state")
def api_sdr_state():
    return _command("sdr_state")


@app.route("/api/presets")
def api_presets():
    return _command("presets")


# ── Not-yet-migrated routes ──

@app.route("/audio-stream")
def audio_stream():
    """Relay the radio's audio stream.

    Audio is the one path that stays HTTP for now: it is continuous and
    latency-sensitive, so it gets migrated to a dedicated PCM socket only once
    the command plane is proven (phase 18, remaining work).
    """
    return _proxy_stream("/audio-stream")


@app.route("/api/<path:subpath>")
def api_proxy(subpath):
    """Forward any API route not yet migrated to an IPC command."""
    return _proxy_get("/api/" + subpath, request.query_string.decode())


def _proxy_get(path, query=""):
    url = RADIO_HTTP + path + (("?" + query) if query else "")
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return Response(resp.read(), status=resp.status,
                            content_type=resp.headers.get("Content-Type", "application/json"))
    except urllib.error.HTTPError as e:
        return Response(e.read(), status=e.code,
                        content_type=e.headers.get("Content-Type", "application/json"))
    except OSError as e:
        return jsonify({"error": f"radio unreachable: {e}", "link": radio.snapshot()}), 503


def _proxy_stream(path):
    url = RADIO_HTTP + path
    try:
        resp = urllib.request.urlopen(url, timeout=10)
    except OSError as e:
        return jsonify({"error": f"radio unreachable: {e}"}), 503

    def generate():
        try:
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                yield chunk
        finally:
            resp.close()

    return Response(generate(),
                    content_type=resp.headers.get("Content-Type", "audio/wav"))


# ── Socket.IO ──

@socketio.on("connect")
def on_connect():
    log.info("Console connected")
    socketio.emit("radio_link", radio.snapshot())
    if radio.is_up:
        socketio.start_background_task(_resync)


def main():
    log.info("Starting ravenSDR UI v%s on port %d (radio socket: %s)",
             VERSION, UI_PORT, radio._path)
    radio.start()   # non-blocking; the console serves whether or not this connects
    socketio.run(app, host="0.0.0.0", port=UI_PORT,
                 allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
