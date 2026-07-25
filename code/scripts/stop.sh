#!/bin/bash
# Stop ravenSDR.
#
# Delegates to systemctl — see start.sh for why. Stopping the service is the
# only way to actually stop it: killing the process directly just trips
# Restart=always. The unit stops the whole control group, so rtl_fm / dump1090
# / ffmpeg children go down with it.
#
# Note this stops a 24x7 collector — scheduled satellite passes and WEFAX
# broadcasts will be missed until it is started again.

set -e

if ! systemctl cat ravensdr.service >/dev/null 2>&1; then
    echo "ravensdr.service is not installed on this host." >&2
    PID=$(pgrep -f "python3? -m ravensdr.app" | head -1)
    if [ -n "$PID" ]; then
        echo "Stopping foreground process (PID $PID)..."
        kill "$PID"
    else
        echo "ravenSDR is not running."
    fi
    exit 0
fi

if ! systemctl is-active --quiet ravensdr; then
    echo "ravenSDR is not running."
    exit 0
fi

sudo systemctl stop ravensdr
echo "ravenSDR stopped."
echo
echo "  Start again:  ./scripts/start.sh"
echo "  Disable boot: sudo systemctl disable ravensdr"
