# Frequency preset definitions (SDR + stream URLs)

# "duty" says whether a preset carries a signal continuously or only while
# somebody transmits. It decides how training samples may be collected:
#
#   continuous — a random window contains the signal, so it can be sampled
#                directly (NOAA's loop, broadcast FM).
#   burst      — a random window is almost always EMPTY, so only the segmenter
#                may collect, and only once it has detected a transmission.
#
# Ignoring this produced 1921 "OOK" samples with zero bursts in them, and
# "AFSK1200" samples that were a steady carrier parked on 144.390 rather than
# any APRS packet. For a bursty protocol the label is only true DURING a burst.
PRESETS = [
    # ── Weather ──
    {
        "id": "noaa-seattle",
        "duty": "continuous",
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
        "duty": "continuous",
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
        "duty": "continuous",
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
        "duty": "continuous",
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
        "duty": "burst",
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
        "duty": "burst",
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
        "duty": "burst",
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
        "duty": "burst",
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
        "duty": "burst",
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
        "duty": "burst",
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
        "duty": "burst",
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
        "duty": "burst",
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
        "duty": "burst",
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
        "duty": "burst",
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
        "duty": "burst",
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
        "duty": "burst",
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
        "duty": "burst",
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
        "duty": "burst",
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
        "duty": "burst",
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
        "duty": "burst",
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
        "duty": "burst",
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
        "duty": "burst",
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
        "duty": "burst",
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
        "duty": "continuous",
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
        "duty": "continuous",
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
        "duty": "burst",
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
        "duty": "burst",
        "label": "ACARS 130.025",
        "freq": "130.025M",
        "mode": "acars",
        "category": "aviation",
        "squelch": 0,
        "note": "Secondary ACARS channel",
        "expected_modulation": "MSK",
    },
    {
        "id": "acars-131125",
        "duty": "burst",
        "label": "ACARS 131.125",
        "freq": "131.125M",
        "mode": "acars",
        "category": "aviation",
        "squelch": 0,
        "note": "ARINC secondary — replaced 131.725, which is SITA/Europe and "
                "silent over North America",
        "expected_modulation": "MSK",
    },
    # More FM broadcast stations. WFM was the one class that demonstrably
    # generalised across frequency (0.949 trained on 90.3, tested on 94.9), so
    # widening it further is cheap and strengthens the strongest evidence we have.
    {
        "id": "kiro-fm",
        "duty": "continuous",
        "label": "KIRO 97.3",
        "freq": "97.300M",
        "mode": "wbfm",
        "category": "broadcast",
        "squelch": 0,
        "continuous": True,
        "note": "Seattle news/talk — strong local signal",
        "expected_modulation": "WFM",
    },
    # Seattle's non-English broadcasting lives on AM, not FM. An FM band scan
    # from this location returned only mainstream English stations, so these
    # are the realistic targets for the translator.
    #
    # All three need a wire antenna on the SDR input: the 137 MHz satellite
    # dipole is effectively deaf at ~1 MHz, where the wavelength is ~220 m. A
    # sweep with the dipole attached read a flat ~180 RMS at 710/1000/1360/
    # 1490/1540 kHz alike — pure noise floor, no station anywhere. No direct
    # sampling flag is needed: the Blog V4 has a built-in HF upconverter and
    # tunes these frequencies directly.
    {
        # Antenna reference for the MW band — the strongest AM signal receivable
        # here by a wide margin. Band scan, relative to a -15.8 dB noise floor:
        #
        #    880 KIXI   +16.8 dB   RMS 4400   <- this
        #    950 KJR     +6.0 dB   RMS 1690   marginal; Whisper hallucinates
        #   1490 KBRO    +2.9 dB              Spanish, not receivable
        #   1360 KKMO    +1.5 dB              Spanish, not receivable
        #   1540 KXPA    +1.5 dB              multilingual, not receivable
        #   1090 (none)  +1.8 dB              control, no licensed station
        #
        # The Spanish stations sit at the level of a frequency with no
        # transmitter on it, so they are not being heard at all. Tune this one
        # to tell the two failure modes apart: if 880 decodes and they do not,
        # the AM path is fine and the antenna is the limit.
        #
        # Content is oldies music, so expect "[Music]" to be filtered rather
        # than text. That is the path working, not failing.
        "id": "kixi-880",
        "duty": "continuous",
        "label": "KIXI 880 (MW reference)",
        "freq": "880k",
        "mode": "am",
        "category": "broadcast",
        "squelch": 0,
        "continuous": True,
        "note": "Strongest local AM — tune to verify the MW path and antenna",
        "expected_modulation": "AM",
    },
    {
        "id": "kkmo-1360",
        "duty": "continuous",
        "label": "El Rey 1360 (Spanish)",
        "freq": "1360k",
        "mode": "am",
        "category": "broadcast",
        "squelch": 0,
        "continuous": True,
        "note": "Spanish-language AM — needs a wire antenna (MW band)",
        "expected_modulation": "AM",
    },
    {
        "id": "kxpa-1540",
        "duty": "continuous",
        "label": "KXPA 1540 (multilingual)",
        "freq": "1540k",
        "mode": "am",
        "category": "broadcast",
        "squelch": 0,
        "continuous": True,
        "note": "Spanish/Chinese/Vietnamese/Korean/Russian by daypart — "
                "the best translator test on the dial. Needs a wire antenna.",
        "expected_modulation": "AM",
    },
    {
        "id": "kbro-1490",
        "duty": "continuous",
        "label": "KBRO 1490 (Spanish)",
        "freq": "1490k",
        "mode": "am",
        "category": "broadcast",
        "squelch": 0,
        "continuous": True,
        "note": "Spanish-language AM — needs a wire antenna (MW band)",
        "expected_modulation": "AM",
    },
    # A Spanish-language preset on 99.3 was added here and removed again: every
    # segment the language detector returned from that frequency came back as
    # English at 0.98+ confidence (CDC public-health advertising), so whatever
    # is receivable on 99.3 at this location is not the station intended. A
    # preset that claims a language it does not carry is worse than no preset —
    # it would make the translator look broken. Finding a genuine non-English
    # source here means HF/shortwave, which needs an antenna this node
    # currently lacks.
    {
        "id": "king-fm",
        "duty": "continuous",
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
        "duty": "burst",
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
        #
        # ERT is frequency-hopping across roughly 910-920 MHz, so even centred
        # correctly the default 250 kHz catches only a sliver of the hop set and
        # absence of decodes proves nothing. 1024 kHz is the widest rate the
        # RTL-SDR holds without dropped samples on this Pi.
        "id": "ism-ert-912",
        "duty": "burst",
        "label": "Utility Meters (ERT)",
        "freq": "912.60M",
        "sample_rate": "1024k",
        "mode": "ism",
        "category": "ism",
        "squelch": 0,
        "note": "Itron ERT water/gas/electric — SCM/SCMplus/IDM consumption reads",
        "expected_modulation": "OOK/FSK",
    },
    {
        "id": "ism-tpms-315",
        "duty": "burst",
        "label": "TPMS 315 MHz",
        "freq": "315.00M",
        "mode": "ism",
        "category": "ism",
        "squelch": 0,
        "note": "Tire pressure sensors from passing vehicles — 25 protocols enabled",
        "expected_modulation": "OOK/FSK",
    },
    {
        # Garage remotes and car key fobs. Which frequency yours uses depends on
        # make and age with no way to tell from outside, so cover all three and
        # let rtl_433 hop: 315 MHz for most US/Japanese fobs and Chamberlain /
        # LiftMaster openers, 390 MHz for older Genie and Overhead Door, 433.92
        # for European fobs and newer openers.
        #
        # What you will and will not get: a press always shows up as a decode
        # attempt, and older fixed-code openers decode outright (rtl_433 carries
        # EV1527, HT680, Interlogix and friends). Anything from roughly the last
        # 25 years — every current car fob, Security+ 2.0, KeeLoq — is rolling
        # code, so the payload differs on every press by design. You see the
        # event, not a reusable code.
        #
        # Hopping means a 100 ms burst on 433 is missed while the tuner sits on
        # 315. To catch one specific device reliably, pin its frequency instead.
        "id": "remotes-fobs",
        "duty": "burst",
        "label": "Remotes & car fobs",
        "freq": "315.00M",
        "freqs": ["315.00M", "390.00M", "433.92M"],
        "hop_s": 10,
        "mode": "ism",
        "category": "ism",
        "squelch": 0,
        "note": "Garage openers and key fobs — hops 315/390/433.92 MHz",
        "expected_modulation": "OOK",
    },
    {
        "id": "ism-390",
        "duty": "burst",
        "label": "Garage 390 MHz",
        "freq": "390.00M",
        "mode": "ism",
        "category": "ism",
        "squelch": 0,
        "note": "Older Genie / Overhead Door openers — pinned, no hopping",
        "expected_modulation": "OOK",
    },
    {
        "id": "ism-security-345",
        "duty": "burst",
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
        "duty": "burst",
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
