# Persistent first/last-seen history for decoded emitters.
#
# The decoder tables (rtl_433 devices, APRS stations) are in-memory with a TTL,
# so a sensor that beacons every 15 minutes vanishes between transmissions and
# everything is lost on restart. That makes it impossible to answer the questions
# that actually matter on a collection node: how many distinct meters are around,
# which ones are persistent vs transient, when did this ID first appear, is it
# still here a week later.
#
# This keeps a small durable record per emitter — identity, counts, signal
# extremes, first/last seen — that survives restarts and outlives the TTL.
#
# Writes are debounced because the node runs 24x7 off an SD card: a popular
# channel can produce several packets a second, and syncing each one would burn
# through write cycles for no benefit.

import json
import logging
import os
import tempfile
import time

log = logging.getLogger(__name__)

DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "data", "observations.json")
DEFAULT_MAX_ENTRIES = 5000     # bounded so an open channel can't grow forever
DEFAULT_SAVE_INTERVAL_S = 30   # debounce window for SD-card writes


class ObservationLog:
    """Durable per-emitter sighting history, keyed by (source, id)."""

    def __init__(self, path=DEFAULT_PATH, max_entries=DEFAULT_MAX_ENTRIES,
                 save_interval_s=DEFAULT_SAVE_INTERVAL_S, clock=time.time):
        self.path = path
        self.max_entries = max_entries
        self.save_interval_s = save_interval_s
        self._clock = clock
        self._entries = {}      # "source/key" -> entry dict
        self._dirty = False
        self._last_save = 0.0

    # ── Recording ──

    def observe(self, source, key, meta=None, rssi=None):
        """Record one sighting. Returns the updated entry.

        `meta` fields (model, label, ...) are merged in so the newest
        description wins without discarding history.
        """
        if key in (None, ""):
            return None
        now = self._clock()
        ident = f"{source}/{key}"
        entry = self._entries.get(ident)

        if entry is None:
            entry = {
                "source": source,
                "key": str(key),
                "first_seen": now,
                "last_seen": now,
                "count": 0,
            }
            self._entries[ident] = entry
            self._evict_if_needed()

        entry["last_seen"] = now
        entry["count"] += 1
        if meta:
            for field, value in meta.items():
                if value is not None:
                    entry[field] = value
        if rssi is not None:
            entry["last_rssi"] = rssi
            best = entry.get("best_rssi")
            entry["best_rssi"] = rssi if best is None else max(best, rssi)

        self._dirty = True
        self.maybe_save()
        return entry

    def _evict_if_needed(self):
        """Drop the least recently seen entries once the cap is exceeded."""
        overflow = len(self._entries) - self.max_entries
        if overflow <= 0:
            return
        stale = sorted(self._entries.items(), key=lambda kv: kv[1].get("last_seen", 0))
        for ident, _ in stale[:overflow]:
            del self._entries[ident]
        log.info("Observation log trimmed to %d entries", self.max_entries)

    # ── Reading ──

    def entries(self, source=None, limit=None):
        """Sightings, most recently heard first."""
        rows = [e for e in self._entries.values()
                if source is None or e.get("source") == source]
        rows.sort(key=lambda e: e.get("last_seen", 0), reverse=True)
        return rows[:limit] if limit else rows

    def get(self, source, key):
        return self._entries.get(f"{source}/{key}")

    def stats(self):
        by_source = {}
        for entry in self._entries.values():
            by_source[entry.get("source", "?")] = by_source.get(entry.get("source", "?"), 0) + 1
        return {
            "total": len(self._entries),
            "by_source": by_source,
            "path": self.path,
        }

    # ── Persistence ──

    def load(self):
        if not os.path.exists(self.path):
            return self
        try:
            with open(self.path) as fh:
                data = json.load(fh)
        except (OSError, ValueError) as e:
            log.warning("Could not read observation log %s: %s", self.path, e)
            return self
        entries = data.get("entries") if isinstance(data, dict) else data
        if isinstance(entries, list):
            for entry in entries:
                key = entry.get("key")
                source = entry.get("source")
                if key is None or source is None:
                    continue
                self._entries[f"{source}/{key}"] = entry
        log.info("Loaded %d observations from %s", len(self._entries), self.path)
        return self

    def maybe_save(self, force=False):
        """Persist if dirty and the debounce window has elapsed."""
        if not self._dirty:
            return False
        now = self._clock()
        if not force and (now - self._last_save) < self.save_interval_s:
            return False
        return self.save()

    def save(self):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        payload = {"version": 1, "entries": list(self._entries.values())}
        try:
            # Write-then-rename: a power cut mid-write must not corrupt the log.
            fd, tmp = tempfile.mkstemp(dir=directory or ".", suffix=".tmp")
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("Could not write observation log %s: %s", self.path, e)
            return False
        self._dirty = False
        self._last_save = self._clock()
        return True
