#!/bin/bash
# View ravenSDR service logs (systemd/journald)
#
# Usage:
#   ./scripts/logs.sh            # follow live
#   ./scripts/logs.sh errors     # errors + warnings + tracebacks, this boot
#   ./scripts/logs.sh sat        # satellite/APT activity only
#   ./scripts/logs.sh today      # everything since midnight

case "$1" in
    errors)
        journalctl -u ravensdr -b --no-pager \
            | grep -iE "ERROR|CRITICAL|WARNING|Traceback|Exception|failed|could not"
        ;;
    sat)
        journalctl -u ravensdr --since "24 hours ago" --no-pager \
            | grep -iE "apt|noaa|satellite|pass|tle|aptdec|decode"
        ;;
    today)
        journalctl -u ravensdr --since today --no-pager
        ;;
    *)
        journalctl -u ravensdr -f -n 50
        ;;
esac
