# Frequency preset definitions (SDR + stream URLs)

PRESETS = [
    # ── Weather ──
    {
        "id": "noaa-seattle",
        "label": "NOAA Seattle",
        "freq": "162.550M",
        "mode": "fm",
        "category": "weather",
        "squelch": 0,
        "parser": "noaa",
        "stream_url": "https://wxradio.org/streams/seattle.mp3",
        "note": "NWS Seattle 24/7 weather radio",
        "expected_modulation": "FM",
    },
    {
        "id": "noaa-monterey",
        "label": "NOAA Monterey",
        "freq": "162.400M",
        "mode": "fm",
        "category": "weather",
        "squelch": 0,
        "parser": "noaa",
        "stream_url": "https://wxradio.org/streams/monterey.mp3",
        "note": "NWS Monterey — primary dev/test stream",
        "expected_modulation": "FM",
    },
    {
        "id": "noaa-portland",
        "label": "NOAA Portland",
        "freq": "162.475M",
        "mode": "fm",
        "category": "weather",
        "squelch": 0,
        "parser": "noaa",
        "stream_url": "https://wxradio.org/streams/portland.mp3",
        "note": "NWS Portland weather radio",
        "expected_modulation": "FM",
    },
    {
        "id": "kuow-fm",
        "label": "KUOW 94.9",
        "freq": "94.900M",
        "mode": "wbfm",
        "category": "broadcast",
        "squelch": 0,
        "stream_url": "https://npr-ice.streamguys1.com/live.mp3",
        "note": "NPR Seattle",
        "expected_modulation": "WFM",
    },
    # ── Aviation ──
    # Ground-control AM presets (ATIS/tower/approach) were removed: the antenna is
    # a horizontal 137 MHz satellite V-dipole (wrong polarization for aviation) and
    # the airports are 10-20 mi line-of-sight, so nothing was audible. ADS-B and
    # ACARS below work well and stay. Re-add AM voice with a vertical airband antenna.
    {
        "id": "adsb-1090",
        "label": "ADS-B 1090 MHz",
        "freq": "1090M",
        "mode": "adsb",
        "category": "aviation",
        "squelch": 0,
        "note": "ADS-B aircraft tracking (dump1090) — map-only mode",
        "device_index": 1,
        "expected_modulation": "ADSB",
    },
    # ── Marine ──
    {
        "id": "ais-marine",
        "label": "AIS Marine Traffic",
        "freq": "162.000M",
        "mode": "ais",
        "category": "marine",
        "squelch": 0,
        "note": "AIS vessel tracking (rtl_ais) — map-only mode",
        "expected_modulation": "FM",
    },
    {
        "id": "marine-ch16",
        "label": "Marine CH 16",
        "freq": "156.800M",
        "mode": "fm",
        "category": "marine",
        "squelch": 20,
        "note": "International distress/calling — SDR only",
        "expected_modulation": "FM",
    },
    {
        "id": "marine-ch22a",
        "label": "Marine CH 22A",
        "freq": "157.100M",
        "mode": "fm",
        "category": "marine",
        "squelch": 20,
        "note": "US Coast Guard liaison — SDR only",
        "expected_modulation": "FM",
    },
    # ── WEFAX (HF Weather Charts) ──
    {
        "id": "wefax-nmc",
        "label": "NMC Point Reyes",
        "freq": "8682.0k",
        "mode": "usb",
        "category": "wefax",
        "squelch": 0,
        "note": "WEFAX weather charts — HF direct sampling, auto-scheduled",
        "expected_modulation": "WEFAX",
    },
    {
        "id": "wefax-noj",
        "label": "NOJ Kodiak",
        "freq": "4298.0k",
        "mode": "usb",
        "category": "wefax",
        "squelch": 0,
        "note": "WEFAX weather charts — HF direct sampling, auto-scheduled",
        "expected_modulation": "WEFAX",
    },
    # ── Public Safety ──
    # King County / Seattle police + fire dispatch were removed: they run encrypted
    # P25 trunking, so an FM demod only hears silence. The unencrypted amateur
    # emergency-comms nets below (Seattle ACS / ARES) are the receivable alternative.
    # ── Amateur emergency comms (unencrypted FM voice; Whisper-transcribable) ──
    # Seattle ACS (Seattle OEM auxiliary comms) + nearby King County ARES/RACES.
    # Tune the repeater OUTPUT; the 103.5 Hz PL tone is transmit-only (no effect
    # on receive). Seattle ACS practice net: Mondays 7:00-7:30pm PT on 146.960.
    {
        "id": "seattle-acs-psrg",
        "label": "Seattle ACS (PSRG)",
        "freq": "146.960M",
        "mode": "fm",
        "category": "public_safety",
        "squelch": 0,
        "note": "Seattle ACS primary — PSRG 146.96 (Mon 7pm net). Seattle OEM aux comms.",
        "expected_modulation": "FM",
    },
    {
        "id": "kc-ares-primary",
        "label": "KC ARES/RACES",
        "freq": "147.080M",
        "mode": "fm",
        "category": "public_safety",
        "squelch": 0,
        "note": "King County ARES/RACES primary repeater (147.000 is the backup)",
        "expected_modulation": "FM",
    },
    {
        "id": "redmond-ares",
        "label": "Redmond ARES",
        "freq": "145.310M",
        "mode": "fm",
        "category": "public_safety",
        "squelch": 0,
        "note": "Redmond ARES — local to this node (Redmond WA)",
        "expected_modulation": "FM",
    },
    {
        "id": "kirkland-ares",
        "label": "Kirkland ARES",
        "freq": "145.490M",
        "mode": "fm",
        "category": "public_safety",
        "squelch": 0,
        "note": "Kirkland ARES 2m repeater",
        "expected_modulation": "FM",
    },
    {
        "id": "bellevue-simplex",
        "label": "Bellevue CommSup",
        "freq": "146.580M",
        "mode": "fm",
        "category": "public_safety",
        "squelch": 0,
        "note": "Bellevue Communications Support simplex",
        "expected_modulation": "FM",
    },
    {
        "id": "shoreline-acs",
        "label": "Shoreline ACS",
        "freq": "442.825M",
        "mode": "fm",
        "category": "public_safety",
        "squelch": 0,
        "note": "Shoreline ACS 70cm repeater (Mon nets)",
        "expected_modulation": "FM",
    },
    {
        "id": "pager-pocsag",
        "label": "Pagers (POCSAG)",
        "freq": "152.0075M",
        "mode": "pager",
        "category": "public_safety",
        "squelch": 0,
        "note": "POCSAG/FLEX pager text via multimon-ng. Edit freq per local paging channel.",
        "expected_modulation": "FSK",
    },
    # ── Science ──
    {
        "id": "meteor-scatter",
        "label": "Meteor Scatter",
        "freq": "143.050M",
        "mode": "fm",
        "category": "science",
        "squelch": 0,
        "note": "Passive meteor detection — forward scatter on 143.050 MHz carrier",
        "expected_modulation": "FM",
    },
    # ── Broadcast ──
    {
        "id": "kexp-fm",
        "label": "KEXP 90.3",
        "freq": "90.300M",
        "mode": "wbfm",
        "category": "broadcast",
        "squelch": 0,
        "note": "KEXP Seattle",
        "expected_modulation": "WFM",
    },
    # ── ACARS aircraft messaging (acarsdec) ──
    {
        "id": "acars-vhf",
        "label": "ACARS (aircraft msgs)",
        "freq": "131.550M",
        "mode": "acars",
        "category": "aviation",
        "squelch": 0,
        "note": "acarsdec — VHF ACARS text; correlates with ADS-B map",
        "expected_modulation": "MSK",
    },
    # ── ISM / Sensors (rtl_433) ──
    {
        "id": "ism-433",
        "label": "ISM 433 MHz",
        "freq": "433.92M",
        "mode": "ism",
        "category": "ism",
        "squelch": 0,
        "note": "rtl_433 — weather stations, TPMS, meters, remotes",
        "expected_modulation": "OOK/FSK",
    },
    {
        "id": "ism-915",
        "label": "ISM 915 MHz",
        "freq": "915.00M",
        "mode": "ism",
        "category": "ism",
        "squelch": 0,
        "note": "rtl_433 — US 915 MHz ISM (utility meters, sensors)",
        "expected_modulation": "OOK/FSK",
    },
]

CATEGORIES = ["weather", "wefax", "aviation", "marine", "science", "public_safety", "ism", "broadcast"]

CATEGORY_LABELS = {
    "weather": "Weather",
    "wefax": "WEFAX",
    "aviation": "Aviation",
    "marine": "Marine",
    "science": "Science",
    "public_safety": "Public Safety",
    "ism": "ISM / Sensors",
    "broadcast": "Broadcast",
}


def get_presets():
    return PRESETS


def get_presets_by_category():
    grouped = {cat: [] for cat in CATEGORIES}
    for preset in PRESETS:
        grouped[preset["category"]].append(preset)
    return grouped


def get_preset_by_id(preset_id):
    for preset in PRESETS:
        if preset["id"] == preset_id:
            return preset
    return None
