#!/usr/bin/env bash
# ravenSDR — Start the application
#
# On a deployed node ravenSDR runs 24x7 as ravensdr.service; use
# code/scripts/start.sh (systemctl) there. This script is the foreground
# dev runner — it will refuse to start while the service holds the dongle.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/.venv"

if systemctl is-active --quiet ravensdr 2>/dev/null; then
    echo "ravensdr.service is running — it already holds the SDR dongle." >&2
    echo "Stop it first:  ./code/scripts/stop.sh" >&2
    exit 1
fi

if [ ! -d "$VENV" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install -r "$SCRIPT_DIR/code/requirements.txt"
    "$VENV/bin/pip" install -e "$SCRIPT_DIR/code"
fi

cd "$SCRIPT_DIR/code"
exec "$VENV/bin/python" -m ravensdr.app
