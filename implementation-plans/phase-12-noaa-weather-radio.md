# Phase 12 — NOAA Weather Radio Integration

## Overview
Continuous monitoring of NOAA weather radio on 162.550 MHz (KHB60 Seattle primary) with Whisper transcription. Weather broadcasts run 24/7 and contain structured forecast data — ravenSDR transcribes, parses, and surfaces weather conditions, alerts, and marine forecasts in a dedicated UI panel.

## SDR Configuration
- **Primary frequency:** 162.550 MHz (KHB60 Seattle)
- **Backup:** 162.400 MHz
- **Alternate:** 162.475 MHz
- **Mode:** FM, squelch 0 (continuous broadcast, no gaps)
- **rtl_fm flags:** `-f 162.550M -M fm -s 200k -r 16k -l 0`
- **Web stream fallback:** `https://wxradio.org/CA-Monterey-KEC49` for dev/test (no active Seattle feeder currently)

## New Files
| File | Purpose |
|------|---------|
| `code/ravensdr/noaa_parser.py` | Regex + keyword parser for NWS broadcast format, extracts structured weather fields from raw Whisper transcript |
| `code/static/weather.js` | Weather panel UI, displays current conditions, active alerts highlighted in red |

## Modified Files
| File | Changes |
|------|---------|
| `code/ravensdr/app.py` | `weather_update` Socket.IO event, `/api/weather/current` route returning latest parsed conditions |
| `code/ravensdr/presets.py` | Add parser flag to trigger noaa_parser on transcripts from weather category presets |
| `code/ravensdr/transcriber.py` | Route completed transcripts to category-specific post-processors |
| `code/templates/index.html` | Weather panel section in layout |
| `code/static/ravensdr.js` | Weather panel integration |

## Known Limitations
- NOAA broadcasts use synthesized TTS voice — Whisper tiny accuracy is good but not perfect on synthesized speech
- No active wxradio.org feeder for Seattle KHB60 currently — SDR required for Seattle-specific coverage
- Transcript parser is heuristic, not a formal NWS product parser

---

## Tasks

### T064 — Create NOAA broadcast transcript parser
**File:** `code/ravensdr/noaa_parser.py`
**Status:** Not Started

Create `noaa_parser.py` with a `parse_weather_transcript()` function that accepts raw Whisper transcript text and returns a structured dict of parsed weather fields.

Parser extracts:
- **Temperature** — current temp in °F, regex for patterns like "temperature 45 degrees", "currently 52"
- **Wind** — speed (mph/knots) and direction, regex for "winds north at 15 miles per hour", "northwest winds 10 to 20 knots"
- **Visibility** — miles, regex for "visibility 10 miles", "visibility one quarter mile in fog"
- **Active alerts** — watches, warnings, advisories. Keyword scan for "warning", "watch", "advisory", "hazardous weather". Extract alert type and affected area
- **Marine forecast segments** — detect and extract Puget Sound, Strait of Juan de Fuca, coastal waters sections
- **Forecast periods** — "tonight", "tomorrow", "Saturday" with associated conditions

Return format:
```python
{
    "temperature": {"value": 45, "unit": "F"},
    "wind": {"speed": 15, "direction": "north", "unit": "mph"},
    "visibility": {"value": 10, "unit": "miles"},
    "alerts": [
        {"type": "warning", "name": "Wind Advisory", "area": "Puget Sound"}
    ],
    "marine": [
        {"zone": "Puget Sound", "forecast": "Small craft advisory..."}
    ],
    "raw_transcript": "...",
    "parsed_at": "2026-02-28T12:00:00Z",
    "confidence": "partial"  # "full" | "partial" | "low" based on how many fields parsed
}
```

Include a `detect_priority_alert()` function that returns `True` when transcript contains warning/watch/advisory keywords. This drives the alert banner in the UI.

---

### T065 — Add weather category parser flag to presets
**File:** `code/ravensdr/presets.py`
**Status:** Not Started

NOAA presets already exist in the weather category. Add a `parser` field to weather category presets indicating that transcripts from these frequencies should be routed through `noaa_parser`.

```python
{
    "name": "NOAA KHB60 Seattle",
    "freq": 162550000,
    "mode": "fm",
    "category": "weather",
    "parser": "noaa"  # triggers noaa_parser post-processing
}
```

Ensure all three NOAA frequencies (162.550, 162.400, 162.475) have the `parser: "noaa"` flag. Squelch should be 0 for weather presets (continuous broadcast).

---

### T066 — Route transcripts to category-specific post-processors
**File:** `code/ravensdr/transcriber.py`
**Status:** Not Started

After Whisper produces a transcript, check whether the current preset has a `parser` field. If `parser == "noaa"`, pass the transcript through `noaa_parser.parse_weather_transcript()` before emitting.

Add a `_post_process()` method to the transcriber that:
1. Looks up the active preset's `parser` field
2. If `"noaa"`, imports and calls `noaa_parser.parse_weather_transcript()`
3. Returns both the raw transcript and structured parsed data
4. If no parser specified, passes transcript through unchanged

This keeps the transcriber generic — future parsers (ATC, marine VHF) can be added the same way.

---

### T067 — Add weather API route and Socket.IO event
**File:** `code/ravensdr/app.py`
**Status:** Not Started

Add to Flask app:

**REST endpoint:**
- `GET /api/weather/current` — returns latest parsed weather conditions as JSON. Store the most recent parsed weather update in an app-level variable. Returns `404` if no weather data received yet.

**Socket.IO events (server → client):**
- `weather_update` — emitted when a new weather transcript is parsed. Payload is the full parsed dict from `noaa_parser`.
- `priority_alert` — emitted when `detect_priority_alert()` returns `True`. Payload includes alert type, affected area, raw transcript snippet, timestamp.

Wire up the transcriber's post-processor output: when a NOAA transcript is parsed, emit `weather_update`. If it contains a priority alert, also emit `priority_alert`.

---

### T068 — Create weather panel UI component
**File:** `code/static/weather.js`
**Status:** Not Started

Create `weather.js` with a `WeatherPanel` class that:

1. **Listens** for `weather_update` and `priority_alert` Socket.IO events
2. **Renders current conditions** — temperature, wind, visibility in a compact card format
3. **Displays active alerts** — warning/watch/advisory banners highlighted in red with pulsing border
4. **Shows marine forecast** — Puget Sound and Strait of Juan de Fuca segments in expandable sections
5. **Shows raw transcript** — collapsible section with the last 3 raw weather transcripts for reference
6. **Timestamps** — "Last updated: 2 min ago" relative time display

Alert banner behavior:
- `priority_alert` events trigger a red banner at the top of the weather panel
- Banner persists until dismissed or superseded by a new update without alerts
- Alert type and affected area displayed prominently

On page load, fetch `/api/weather/current` to populate initial state.

---

### T069 — Integrate weather panel into main layout
**Files:** `code/templates/index.html`, `code/static/ravensdr.js`
**Status:** Not Started

**index.html:**
- Add a weather panel section to the dashboard layout. Place it as a collapsible sidebar panel or a tab alongside existing panels.
- Include `<script src="/static/weather.js"></script>`
- Add a weather panel container div with id `weather-panel`

**ravensdr.js:**
- Initialize `WeatherPanel` on page load
- Wire up panel visibility toggle — weather panel can be shown/hidden via a toolbar button or tab
- When NOAA preset is active, auto-expand the weather panel

---

### T070 — Add structured weather logging
**File:** `code/ravensdr/app.py`
**Status:** Not Started

When a `priority_alert` is detected, log to the structured intelligence log with:
- Timestamp (ISO 8601)
- Frequency (162.550 MHz etc.)
- Alert type (warning/watch/advisory)
- Alert name (e.g., "Wind Advisory")
- Affected area (e.g., "Puget Sound")
- Raw transcript snippet (first 200 chars)
- Source (SDR or web stream)

Use the existing logging pattern from ADS-B correlation (Phase 10). Weather alerts are priority intelligence items.

---

### T071 — Create NWS broadcast test fixtures
**File:** `code/tests/fixtures/noaa_transcripts.py`
**Status:** Not Started

Create test fixture file with 4 sample NOAA broadcast transcripts (realistic text matching what Whisper would output from synthesized TTS):

1. **Clear conditions** — standard forecast, no alerts. Temperature, wind, visibility all present.
2. **Wind advisory** — active advisory for Puget Sound, elevated winds.
3. **Winter storm warning** — warning with snow accumulation, hazardous travel.
4. **Marine forecast** — Puget Sound and Strait of Juan de Fuca small craft advisory, wave heights, wind forecast.

Each fixture should be a raw string that looks like Whisper output — no punctuation perfection, occasional minor transcription artifacts typical of synthesized speech recognition.

---

### T072 — Unit tests for noaa_parser
**File:** `code/tests/unit/test_noaa_parser.py`
**Status:** Not Started

Unit tests covering:
- `parse_weather_transcript()` extracts temperature from clear conditions fixture
- `parse_weather_transcript()` extracts wind speed and direction
- `parse_weather_transcript()` extracts visibility
- `detect_priority_alert()` returns `False` for clear conditions
- `detect_priority_alert()` returns `True` for wind advisory fixture
- `detect_priority_alert()` returns `True` for winter storm warning fixture
- Marine forecast segments extracted from marine fixture
- Parser returns `confidence: "partial"` when only some fields parse
- Parser returns `confidence: "low"` when transcript is mostly unparseable
- Parser handles empty string input gracefully

---

### T073 — Integration test with web stream source
**File:** `code/tests/integration/test_noaa_stream.py`
**Status:** Not Started

Integration test that:
1. Configures a web stream source pointing to `https://wxradio.org/CA-Monterey-KEC49`
2. Captures 30 seconds of audio
3. Runs through Whisper transcription (CPU fallback)
4. Passes transcript through `noaa_parser`
5. Asserts that at least some weather fields were extracted (temperature or wind or visibility)

Mark test with `@pytest.mark.integration` and `@pytest.mark.network` — requires internet access and may be slow. Skip gracefully if stream is unavailable.

---

### T074 — NOAA squelch-zero continuous capture mode
**File:** `code/ravensdr/tuner.py`
**Status:** Not Started

NOAA weather radio broadcasts continuously with no squelch gaps. The existing voice-activity detection (VAD) segmentation from Phase 10 may not segment well on continuous synthesized speech.

Add a `continuous` capture mode option to the tuner:
- When preset has `squelch: 0` and `parser: "noaa"`, use time-based segmentation instead of VAD
- Segment audio into fixed 30-second chunks for transcription
- Overlap chunks by 2 seconds to avoid cutting words at boundaries
- This ensures regular transcript updates even without silence gaps

---

## Dependency Chain
```
T071 (fixtures) → T072 (unit tests)
T064 (parser) → T066 (post-processor routing) → T067 (API + events)
T065 (preset flags) → T066
T067 → T068 (weather UI) → T069 (layout integration)
T067 → T070 (structured logging)
T074 (continuous capture) → T073 (integration test)
```

## Success Criteria
- NOAA weather radio transcribed continuously on 162.550 MHz
- Parsed weather conditions displayed in dedicated UI panel
- Watch/warning/advisory alerts trigger red banner and priority logging
- Marine forecast segments for Puget Sound surfaced in UI
- Web stream fallback works for development without SDR hardware
- All unit tests pass against fixture transcripts
