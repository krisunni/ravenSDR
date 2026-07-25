#!/bin/bash
# Start ravenSDR.
#
# ravenSDR runs 24x7 as a systemd unit (ravensdr.service), so this script now
# delegates to systemctl. It used to launch a second copy with nohup + a PID
# file, which fought the service for the RTL-SDR dongle (usb_claim_interface
# error -6) and got restarted from under you by Restart=always.
#
# Usage:
#   ./scripts/start.sh          # start the service
#   ./scripts/stop.sh           # stop it
#   ./scripts/logs.sh           # follow logs
#
# For a foreground dev run (stop the service first): python3 -m ravensdr.app

set -e

if ! systemctl cat ravensdr.service >/dev/null 2>&1; then
    echo "ravensdr.service is not installed on this host." >&2
    echo "Run it in the foreground instead: python3 -m ravensdr.app" >&2
    exit 1
fi

if systemctl is-active --quiet ravensdr; then
    echo "ravenSDR is already running."
else
    sudo systemctl start ravensdr
    echo "ravenSDR started."
fi

echo
echo "  Status: systemctl status ravensdr"
echo "  Logs:   ./scripts/logs.sh"
echo "  Stop:   ./scripts/stop.sh"
