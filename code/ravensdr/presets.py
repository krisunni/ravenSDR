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
        # Broadcast FM never stops transmitting, so there is no quiet
        # gap for a floor-relative VAD to measure against — the floor
        # would settle inside the programme audio and gate it all out.
        "continuous": True,
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
    # ── APRS packet (rtl_fm | multimon-ng AFSK1200) ──
    {
        # Stations beacon position/weather/telemetry on a schedule, so unlike the
        # voice repeaters this channel carries data continuously.
        "id": "aprs-144390",
        "label": "APRS 144.390",
        "freq": "144.390M",
        "mode": "aprs",
        "category": "packet",
        "squelch": 0,
        "note": "APRS packet — station positions, weather and telemetry",
        "expected_modulation": "AFSK1200",
    },
    # ── King County analog voice (unencrypted, conventional) ──
    # Seattle/KC police+fire moved to PSERN (encrypted P25 Phase II) and are not
    # receivable. These conventional FM channels are. Squelch is deliberately
    # non-zero: with -l 0 rtl_fm passes full-scale FM hiss (~1440 RMS, ~3x the
    # transcriber's silence gate), so the NPU transcribes static into "(roaring)"
    # instead of idling. Squelch mutes the noise floor at the source.
    {
        "id": "amr-ems-dispatch",
        "label": "AMR EMS Dispatch",
        "freq": "158.835M",
        "mode": "fm",
        "category": "public_safety",
        "squelch": 25,
        "note": "American Medical Response Seattle — private ambulance dispatch, analog FM",
        "expected_modulation": "FM",
    },
    {
        "id": "amr-ems-alt",
        "label": "AMR Seattle (alt)",
        "freq": "155.220M",
        "mode": "fm",
        "category": "public_safety",
        "squelch": 25,
        "note": "AMR Seattle secondary (103.5 PL is transmit-only)",
        "expected_modulation": "FM",
    },
    {
        "id": "kc-mars-interop",
        "label": "KC Mutual Aid (MARS)",
        "freq": "155.190M",
        "mode": "fm",
        "category": "public_safety",
        "squelch": 25,
        "note": "King County Mutual Aid Radio System — interop, active during incidents",
        "expected_modulation": "FM",
    },
    {
        "id": "kc-sar-f2",
        "label": "KC Search & Rescue F-2",
        "freq": "154.965M",
        "mode": "fm",
        "category": "public_safety",
        "squelch": 25,
        "note": "King County SAR F-2 (F-3 is 153.755)",
        "expected_modulation": "FM",
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
        # Travellers' Information Station: a short automated voice loop that
        # repeats continuously, so it suits the continuous segmenter the way NOAA
        # weather radio does. NOTE: 1650 kHz is medium wave — the V4 tunes it
        # directly via its internal upconverter (no -D direct sampling), but the
        # 137 MHz V-dipole is far too short to hear it. Needs an HF/longwire
        # antenna, same caveat as the WEFAX presets.
        "id": "redmond-tis-1650",
        "label": "Redmond Community 1650",
        "freq": "1650k",
        "mode": "am",
        "category": "broadcast",
        "squelch": 0,
        "continuous": True,
        "note": "TIS/HAR community info loop — needs an HF antenna (MW band)",
        "expected_modulation": "AM",
    },
    {
        "id": "kexp-fm",
        "label": "KEXP 90.3",
        "freq": "90.300M",
        "mode": "wbfm",
        "category": "broadcast",
        "squelch": 0,
        # Broadcast FM never stops transmitting, so there is no quiet
        # gap for a floor-relative VAD to measure against — the floor
        # would settle inside the programme audio and gate it all out.
        "continuous": True,
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
    # Additional ACARS channels. acarsdec already listens to all five; giving
    # each its own preset lets the collector gather MSK from five frequencies
    # instead of one, so a model cannot pass by memorising 131.550 MHz.
    {
        "id": "acars-130025",
        "label": "ACARS 130.025",
        "freq": "130.025M",
        "mode": "acars",
        "category": "aviation",
        "squelch": 0,
        "note": "Secondary ACARS channel",
        "expected_modulation": "MSK",
    },
    {
        "id": "acars-131725",
        "label": "ACARS 131.725",
        "freq": "131.725M",
        "mode": "acars",
        "category": "aviation",
        "squelch": 0,
        "note": "European primary, used in North America as secondary",
        "expected_modulation": "MSK",
    },
    # More FM broadcast stations. WFM was the one class that demonstrably
    # generalised across frequency (0.949 trained on 90.3, tested on 94.9), so
    # widening it further is cheap and strengthens the strongest evidence we have.
    {
        "id": "kiro-fm",
        "label": "KIRO 97.3",
        "freq": "97.300M",
        "mode": "wbfm",
        "category": "broadcast",
        "squelch": 0,
        "continuous": True,
        "note": "Seattle news/talk — strong local signal",
        "expected_modulation": "WFM",
    },
    {
        "id": "king-fm",
        "label": "KING 98.1",
        "freq": "98.100M",
        "mode": "wbfm",
        "category": "broadcast",
        "squelch": 0,
        "continuous": True,
        "note": "Seattle classical — strong local signal",
        "expected_modulation": "WFM",
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
        # Itron ERT meters sit near 912.6 MHz. rtl_433 defaults to 250 kHz of
        # bandwidth, so the 915.00M preset only covers ~914.88-915.13 and never
        # hears them. Unlike Gridstream (which yields identity only), ERT
        # SCM/IDM frames carry actual consumption values.
        "id": "ism-ert-912",
        "label": "Utility Meters (ERT)",
        "freq": "912.60M",
        "mode": "ism",
        "category": "ism",
        "squelch": 0,
        "note": "Itron ERT water/gas/electric — SCM/SCMplus/IDM consumption reads",
        "expected_modulation": "OOK/FSK",
    },
    {
        "id": "ism-tpms-315",
        "label": "TPMS 315 MHz",
        "freq": "315.00M",
        "mode": "ism",
        "category": "ism",
        "squelch": 0,
        "note": "Tire pressure sensors from passing vehicles — 25 protocols enabled",
        "expected_modulation": "OOK/FSK",
    },
    {
        "id": "ism-security-345",
        "label": "Security Sensors 345 MHz",
        "freq": "345.00M",
        "mode": "ism",
        "category": "ism",
        "squelch": 0,
        "note": "Honeywell/DSC door, window and smoke sensors",
        "expected_modulation": "OOK",
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
    "packet": "Packet / APRS",
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
