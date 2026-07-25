# WEFAX broadcast schedule parser and job scheduler

import datetime
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

# Feature flag — WEFAX needs an HF antenna. Set WEFAX_ENABLED=false to keep the
# scheduler from claiming the SDR every :00/:30 UTC slot when no HF antenna is
# connected. Mirrors ADSB_ENABLED / METEOR_ENABLED.
WEFAX_ENABLED = os.environ.get("WEFAX_ENABLED", "true").lower() == "true"

# ── Hardcoded NMC Point Reyes schedule (UTC times, daily) ──
# Source: https://www.weather.gov/marine/radiofax
# Frequencies in kHz: 4346.0, 8682.0, 12786.0, 17151.2
NMC_SCHEDULE = [
    {"utc_time": "00:30", "chart_type": "surface_analysis", "description": "North Pacific Surface Analysis", "duration_minutes": 10},
    {"utc_time": "01:00", "chart_type": "24hr_forecast", "description": "24-Hour Surface Forecast", "duration_minutes": 10},
    {"utc_time": "01:30", "chart_type": "wave_chart", "description": "North Pacific Wave Analysis", "duration_minutes": 10},
    {"utc_time": "05:00", "chart_type": "48hr_forecast", "description": "48-Hour Surface Forecast", "duration_minutes": 10},
    {"utc_time": "05:30", "chart_type": "96hr_forecast", "description": "96-Hour Surface Forecast", "duration_minutes": 10},
    {"utc_time": "06:00", "chart_type": "surface_analysis", "description": "North Pacific Surface Analysis", "duration_minutes": 10},
    {"utc_time": "06:30", "chart_type": "wave_chart", "description": "Wind/Wave Forecast", "duration_minutes": 10},
    {"utc_time": "12:30", "chart_type": "surface_analysis", "description": "North Pacific Surface Analysis", "duration_minutes": 10},
    {"utc_time": "13:00", "chart_type": "24hr_forecast", "description": "24-Hour Surface Forecast", "duration_minutes": 10},
    {"utc_time": "13:30", "chart_type": "wave_chart", "description": "North Pacific Wave Analysis", "duration_minutes": 10},
    {"utc_time": "17:00", "chart_type": "48hr_forecast", "description": "48-Hour Surface Forecast", "duration_minutes": 10},
    {"utc_time": "17:30", "chart_type": "144hr_forecast", "description": "144-Hour Surface Forecast", "duration_minutes": 10},
    {"utc_time": "18:00", "chart_type": "surface_analysis", "description": "North Pacific Surface Analysis", "duration_minutes": 10},
    {"utc_time": "18:30", "chart_type": "wave_chart", "description": "Wind/Wave Forecast", "duration_minutes": 10},
]

NMC_FREQUENCIES = [4346.0, 8682.0, 12786.0, 17151.2]

# ── Hardcoded NOJ Kodiak schedule (UTC times, daily) ──
NOJ_SCHEDULE = [
    {"utc_time": "02:00", "chart_type": "surface_analysis", "description": "Alaska Surface Analysis", "duration_minutes": 10},
    {"utc_time": "02:30", "chart_type": "24hr_forecast", "description": "Alaska 24-Hour Forecast", "duration_minutes": 10},
    {"utc_time": "03:00", "chart_type": "wave_chart", "description": "North Pacific Wave Analysis", "duration_minutes": 10},
    {"utc_time": "08:00", "chart_type": "surface_analysis", "description": "Alaska Surface Analysis", "duration_minutes": 10},
    {"utc_time": "08:30", "chart_type": "48hr_forecast", "description": "Alaska 48-Hour Forecast", "duration_minutes": 10},
    {"utc_time": "14:00", "chart_type": "surface_analysis", "description": "Alaska Surface Analysis", "duration_minutes": 10},
    {"utc_time": "14:30", "chart_type": "24hr_forecast", "description": "Alaska 24-Hour Forecast", "duration_minutes": 10},
    {"utc_time": "15:00", "chart_type": "wave_chart", "description": "North Pacific Wave Analysis", "duration_minutes": 10},
    {"utc_time": "20:00", "chart_type": "surface_analysis", "description": "Alaska Surface Analysis", "duration_minutes": 10},
    {"utc_time": "20:30", "chart_type": "48hr_forecast", "description": "Alaska 48-Hour Forecast", "duration_minutes": 10},
]

NOJ_FREQUENCIES = [2054.0, 4298.0, 8459.0, 12412.0]

# Priority chart types — record all chart types to maximize data collection
PRIORITY_CHART_TYPES = {
    "surface_analysis", "24hr_forecast", "48hr_forecast",
    "96hr_forecast", "144hr_forecast", "wave_chart",
}


def select_frequency(frequencies, utc_hour):
    """Select optimal HF frequency based on time of day.

    Lower frequencies (4 MHz) propagate better at night.
    Higher frequencies (8-12 MHz) propagate better during day.
    UTC 06:00-18:00 = daytime for Pacific region.
    """
    if len(frequencies) <= 1:
        return frequencies[0] if frequencies else None

    if 6 <= utc_hour < 18:
        # Daytime: prefer higher frequencies (8-12 MHz range)
        candidates = [f for f in frequencies if f >= 8000]
        if candidates:
            return min(candidates)  # lowest in the high range
        return max(frequencies)
    else:
        # Nighttime: prefer lower frequencies (2-4 MHz range)
        candidates = [f for f in frequencies if f <= 5000]
        if candidates:
            return max(candidates)  # highest in the low range
        return min(frequencies)


class WefaxScheduler:
    """Parses WEFAX broadcast schedules and triggers recording jobs."""

    def __init__(self, emit_fn=None, on_broadcast_start=None):
        self.emit_fn = emit_fn or (lambda *a, **kw: None)
        self.on_broadcast_start = on_broadcast_start
        self._running = False
        self._thread = None
        self._notified_broadcasts = set()  # keys already notified
        self._triggered_broadcasts = set()  # keys already triggered for recording

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._schedule_loop, daemon=True)
        self._thread.start()
        log.info("WEFAX scheduler started")

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        log.info("WEFAX scheduler stopped")

    def get_upcoming_broadcasts(self, hours=6):
        """Return list of upcoming broadcasts within `hours` window, sorted by start time."""
        now = datetime.datetime.utcnow()
        end = now + datetime.timedelta(hours=hours)
        broadcasts = []

        for station, schedule, frequencies in [
            ("NMC", NMC_SCHEDULE, NMC_FREQUENCIES),
            ("NOJ", NOJ_SCHEDULE, NOJ_FREQUENCIES),
        ]:
            for entry in schedule:
                # Parse UTC time for today and tomorrow
                h, m = map(int, entry["utc_time"].split(":"))

                for day_offset in range(2):  # today and tomorrow
                    start_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
                    start_dt += datetime.timedelta(days=day_offset)

                    if start_dt < now:
                        continue
                    if start_dt > end:
                        continue

                    freq = select_frequency(frequencies, h)
                    is_priority = entry["chart_type"] in PRIORITY_CHART_TYPES

                    broadcasts.append({
                        "station": station,
                        "frequency_khz": freq,
                        "chart_type": entry["chart_type"],
                        "start_utc": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "duration_minutes": entry["duration_minutes"],
                        "description": entry["description"],
                        "priority": is_priority,
                    })

        broadcasts.sort(key=lambda b: b["start_utc"])
        return broadcasts

    def _schedule_loop(self):
        """Check every 30s for upcoming broadcasts and trigger recordings."""
        while self._running:
            try:
                self._check_upcoming_broadcasts()
            except Exception as e:
                log.error("WEFAX scheduler error: %s", e)

            for _ in range(30):
                if not self._running:
                    return
                time.sleep(1)

    def _check_upcoming_broadcasts(self):
        broadcasts = self.get_upcoming_broadcasts(hours=1)
        now = datetime.datetime.utcnow()

        for b in broadcasts:
            start = datetime.datetime.strptime(b["start_utc"], "%Y-%m-%dT%H:%M:%SZ")
            time_until = (start - now).total_seconds()
            broadcast_key = f"{b['station']}_{b['start_utc']}"

            # Emit upcoming event 5 minutes before
            if 0 < time_until <= 300 and broadcast_key not in self._notified_broadcasts:
                self._notified_broadcasts.add(broadcast_key)
                self.emit_fn("wefax_broadcast_upcoming", {
                    "station": b["station"],
                    "frequency_khz": b["frequency_khz"],
                    "chart_type": b["chart_type"],
                    "start_utc": b["start_utc"],
                    "description": b["description"],
                    "minutes_until": round(time_until / 60, 1),
                })
                log.info("Upcoming WEFAX: %s %s at %s on %.1f kHz",
                         b["station"], b["chart_type"], b["start_utc"], b["frequency_khz"])

            # Trigger recording at broadcast time (only for priority charts)
            # Window must be wider than the 30s check interval to avoid missing broadcasts
            if -60 <= time_until <= 60 and b["priority"] and broadcast_key not in self._triggered_broadcasts and self.on_broadcast_start:
                self._triggered_broadcasts.add(broadcast_key)
                self.on_broadcast_start(b)

        # Prune old keys (older than 1 hour)
        cutoff = now - datetime.timedelta(hours=1)
        cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        self._notified_broadcasts = {
            k for k in self._notified_broadcasts
            if k.split("_", 1)[1] > cutoff_str
        }
        self._triggered_broadcasts = {
            k for k in self._triggered_broadcasts
            if k.split("_", 1)[1] > cutoff_str
        }
