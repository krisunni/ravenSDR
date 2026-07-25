# APT pass scheduler — TLE fetching + ephem satellite pass prediction

import datetime
import logging
import os
import threading
import time

import ephem
import requests

log = logging.getLogger(__name__)

# Celestrak TLE source. The 'weather' group no longer carries the POES APT birds
# (it's JPSS/NOAA-20/21 now, which don't transmit 137 MHz APT); query by name so
# we get NOAA 15 / 18 / 19.
TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?NAME=NOAA&FORMAT=tle"
TLE_CACHE_FILE = "/tmp/ravensdr/apt/tle_cache.txt"
TLE_REFRESH_HOURS = 24

# Observer location — Redmond, WA
OBSERVER_LAT = "47.6740"
OBSERVER_LON = "-122.1215"
OBSERVER_ELEV = 46  # meters

# Satellites to track (NOAA-18 decommissioned June 2025)
NOAA_SATS = {
    "NOAA 15": "137.6200M",
    "NOAA 19": "137.1000M",  # NOAA-19 APT downlink. (137.9125 is NOAA-18, decommissioned.)
}

MIN_ELEVATION_DEG = 20


class AptScheduler:
    """Predicts NOAA satellite passes and triggers APT recording jobs."""

    def __init__(self, emit_fn=None, on_pass_start=None):
        self.emit_fn = emit_fn or (lambda *a, **kw: None)
        self.on_pass_start = on_pass_start
        self._tle_data = {}  # satellite_name -> (line1, line2)
        self._tle_last_fetch = None
        self._running = False
        self._thread = None
        self._notified_passes = set()  # stable pass keys already notified
        self._recorded_passes = set()  # stable pass keys recording was triggered for

    def start(self):
        if self._running:
            return
        self._running = True
        self._fetch_tles()
        self._thread = threading.Thread(target=self._schedule_loop, daemon=True)
        self._thread.start()
        log.info("APT scheduler started")

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        log.info("APT scheduler stopped")

    def get_next_passes(self, hours=24):
        """Return list of upcoming passes within `hours` window."""
        self._refresh_tles_if_stale()
        if not self._tle_data:
            return []

        observer = self._make_observer()
        now = datetime.datetime.utcnow()
        end = now + datetime.timedelta(hours=hours)
        passes = []

        for sat_name, (line1, line2) in self._tle_data.items():
            try:
                sat = ephem.readtle(sat_name, line1, line2)
            except Exception as e:
                log.warning("Failed to parse TLE for %s: %s", sat_name, e)
                continue

            search_start = ephem.Date(now)
            while True:
                try:
                    info = observer.next_pass(sat, singlepass=True)
                except Exception:
                    break

                # info: (rise_time, rise_az, max_alt_time, max_alt, set_time, set_az)
                aos_time = info[0]
                max_alt = info[3]
                los_time = info[4]

                if aos_time is None or los_time is None:
                    break

                aos_dt = ephem.Date(aos_time).datetime()
                los_dt = ephem.Date(los_time).datetime()

                if aos_dt > end:
                    break

                max_elev_deg = float(max_alt) * 180.0 / 3.14159265

                if max_elev_deg >= MIN_ELEVATION_DEG:
                    duration = (los_dt - aos_dt).total_seconds()
                    passes.append({
                        "satellite": sat_name,
                        "frequency": NOAA_SATS.get(sat_name, ""),
                        "aos": aos_dt.isoformat() + "Z",
                        "los": los_dt.isoformat() + "Z",
                        "max_elevation": round(max_elev_deg, 1),
                        "duration": round(duration),
                    })

                # Move observer past this pass to find the next one
                observer.date = los_time + ephem.minute
                sat = ephem.readtle(sat_name, line1, line2)

            # Reset observer for next satellite
            observer = self._make_observer()

        passes.sort(key=lambda p: p["aos"])
        return passes

    POLL_INTERVAL_S = 15

    def _schedule_loop(self):
        """Poll for upcoming passes and trigger recordings.

        Polls every POLL_INTERVAL_S. The AOS trigger window is far wider than
        one interval (see _check_upcoming_passes), so a missed or delayed poll
        still catches the pass on the next tick.
        """
        while self._running:
            try:
                self._check_upcoming_passes()
            except Exception as e:
                log.error("APT scheduler error: %s", e)

            for _ in range(self.POLL_INTERVAL_S):
                if not self._running:
                    return
                time.sleep(1)

    @staticmethod
    def _pass_key(p):
        """Stable identity for a pass.

        ephem recomputes AOS to microsecond precision on every poll, so the raw
        ISO timestamp differs each time and can't be used to dedupe. Truncate to
        the minute — passes are ~15 min apart, so this never collides.
        """
        return f"{p['satellite']}_{p['aos'][:16]}"  # YYYY-MM-DDTHH:MM

    def _check_upcoming_passes(self):
        passes = self.get_next_passes(hours=1)
        now = datetime.datetime.utcnow()

        for p in passes:
            aos = datetime.datetime.fromisoformat(p["aos"].rstrip("Z"))
            time_until = (aos - now).total_seconds()
            pass_key = self._pass_key(p)

            # Emit upcoming event 10 minutes before
            if 0 < time_until <= 600 and pass_key not in self._notified_passes:
                self._notified_passes.add(pass_key)
                self.emit_fn("satellite_pass_upcoming", {
                    "satellite": p["satellite"],
                    "frequency": p["frequency"],
                    "aos": p["aos"],
                    "max_elevation": p["max_elevation"],
                    "duration": p["duration"],
                    "minutes_until": round(time_until / 60, 1),
                })
                log.info("Upcoming pass: %s at %s (%.0f° max elev, %ds)",
                         p["satellite"], p["aos"], p["max_elevation"], p["duration"])

            # Trigger recording once at AOS. Fire on the first poll from ~15s
            # before AOS through the first ~3 min of the pass, rather than a
            # narrow ±10s window: the 30s poll loop drifts under load (eventlet
            # greenthread starved by WEFAX decode / Hailo inference), so a tight
            # window gets skipped past entirely. The recorded-key set guarantees
            # we still only start once per pass.
            if -180 <= time_until <= 15 and pass_key not in self._recorded_passes:
                self._recorded_passes.add(pass_key)
                log.info("Triggering APT recording: %s (T%+.0fs from AOS, %.0f° max elev)",
                         p["satellite"], time_until, p["max_elevation"])
                if self.on_pass_start:
                    self.on_pass_start(p)

    def _make_observer(self):
        observer = ephem.Observer()
        observer.lat = OBSERVER_LAT
        observer.lon = OBSERVER_LON
        observer.elevation = OBSERVER_ELEV
        observer.date = ephem.now()
        return observer

    def _fetch_tles(self):
        """Fetch TLE data from Celestrak."""
        os.makedirs(os.path.dirname(TLE_CACHE_FILE), exist_ok=True)

        try:
            resp = requests.get(TLE_URL, timeout=15)
            resp.raise_for_status()
            tle_text = resp.text
            with open(TLE_CACHE_FILE, "w") as f:
                f.write(tle_text)
            self._tle_last_fetch = datetime.datetime.utcnow()
            log.info("TLE data fetched from Celestrak")
        except Exception as e:
            log.warning("Failed to fetch TLEs from Celestrak: %s", e)
            # Try cached file
            if os.path.exists(TLE_CACHE_FILE):
                with open(TLE_CACHE_FILE) as f:
                    tle_text = f.read()
                self._tle_last_fetch = datetime.datetime.utcnow()
                log.info("Using cached TLE data")
            else:
                log.error("No TLE data available (no cache)")
                return

        self._parse_tles(tle_text)

    def _parse_tles(self, tle_text):
        """Parse TLE text into satellite dict. Only keep NOAA-15 and NOAA-19."""
        lines = [l.strip() for l in tle_text.strip().split("\n") if l.strip()]
        self._tle_data = {}

        i = 0
        while i < len(lines) - 2:
            name = lines[i]
            line1 = lines[i + 1]
            line2 = lines[i + 2]

            if not line1.startswith("1 ") or not line2.startswith("2 "):
                i += 1
                continue

            # Match against our tracked satellites
            for sat_name in NOAA_SATS:
                if sat_name in name and "18" not in name:
                    self._tle_data[sat_name] = (line1, line2)
                    log.info("Loaded TLE for %s", sat_name)
                    break

            i += 3

    def _refresh_tles_if_stale(self):
        if self._tle_last_fetch is None:
            self._fetch_tles()
            return

        age = (datetime.datetime.utcnow() - self._tle_last_fetch).total_seconds()
        if age > TLE_REFRESH_HOURS * 3600:
            self._fetch_tles()
