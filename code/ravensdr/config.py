# Persistent configuration for ravenSDR
#
# Reads from config.json with env var fallback for backwards compatibility.
# Supports runtime changes via save_config().

import json
import logging
import os
import tempfile

log = logging.getLogger(__name__)

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")

DEFAULT_CONFIG = {
    "secondary_dongle": {
        "enabled": False,
        "task": None,       # "adsb", "meteor", "wefax", or None
        "device_index": 1,
    },
    "startup": {
        "auto_tune": True,
        "default_preset": None,   # preset id to pin; None = resume last tuned
    },
    "last_preset": None,          # preset id, updated on every tune (any mode)
    # Master switch for automation that TAKES THE SDR on its own initiative.
    # With this off the node only ever does what the operator asked: schedulers
    # still predict and report (satellite passes are still listed), they just
    # don't preempt the dongle out from under the current preset.
    "automation": {
        "enabled": True,
        "apt": True,        # NOAA satellite pass recording
        "wefax": True,      # HF weather-fax broadcasts
        "adsb_scan": True,  # opportunistic ADS-B scanning between tunes
        # Background IQ collection for the training corpus. OFF by default: it
        # seizes the dongle on a rotation, and one dongle cannot stream IQ and
        # demodulate audio at once, so the node stops listening while it runs.
        "iq_collect": False,
    },
    # Runtime-tunable NPU/analysis settings, editable from the Settings tab.
    # Defaults mirror the original module constants so behaviour is unchanged
    # until the user overrides them.
    "settings": {
        # Keyword watchlist — transcripts (Whisper NPU output) are scanned for
        # these terms; a hit raises an alert. Each entry: {term, severity, enabled}.
        "keywords": [],
        "keywords_enabled": True,
        # Model / pipeline thresholds (see the owning module for meaning).
        "sei_match_threshold": 0.85,      # sei_model.MATCH_THRESHOLD
        "segmenter_threshold_db": 10,     # iq_segmenter.DEFAULT_THRESHOLD_DB
        "silence_threshold": 500,         # transcriber.SILENCE_THRESHOLD (absolute fallback)
        "vad_threshold_db": 8.0,          # transcriber.VAD_THRESHOLD_DB — dB over noise floor
        "classifier_confidence": 0.7,     # signal_classifier.CONFIDENCE_THRESHOLD
    },
}


def load_config():
    """Load config from config.json, falling back to env vars then defaults.

    Precedence: config.json > env vars > defaults
    """
    config = _deep_copy(DEFAULT_CONFIG)

    # Try loading from file
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                file_config = json.load(f)
            _merge(config, file_config)
            # DEBUG, not INFO: this is read on every automation check, and the
            # console polls radio activity every 5s per open tab. At INFO it
            # buried the transcriber's own output in the journal.
            log.debug("Config loaded from %s", CONFIG_FILE)
            return config
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Failed to load config.json: %s — using env vars", e)

    # Fall back to env vars (backwards compatible)
    adsb_dual = os.environ.get("ADSB_DUAL_DONGLE", "false").lower() == "true"
    meteor_dual = os.environ.get("METEOR_DUAL_DONGLE", "false").lower() == "true"
    meteor_enabled = os.environ.get("METEOR_ENABLED", "false").lower() == "true"

    if adsb_dual:
        config["secondary_dongle"]["enabled"] = True
        config["secondary_dongle"]["task"] = "adsb"
    elif meteor_dual and meteor_enabled:
        config["secondary_dongle"]["enabled"] = True
        config["secondary_dongle"]["task"] = "meteor"

    return config


def save_config(config):
    """Save config to config.json atomically."""
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(CONFIG_FILE), suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(config, f, indent=2)
            os.replace(tmp_path, CONFIG_FILE)
            log.info("Config saved to %s", CONFIG_FILE)
        except Exception:
            os.unlink(tmp_path)
            raise
    except OSError as e:
        log.error("Failed to save config: %s", e)


def get_secondary_task(config=None):
    """Get the configured secondary dongle task.

    Returns: "adsb", "meteor", "wefax", or None
    """
    if config is None:
        config = load_config()
    sec = config.get("secondary_dongle", {})
    if sec.get("enabled") and sec.get("task"):
        return sec["task"]
    return None


def get_secondary_device_index(config=None):
    """Get the device index for the secondary dongle."""
    if config is None:
        config = load_config()
    return config.get("secondary_dongle", {}).get("device_index", 1)


def set_secondary_task(task):
    """Set the secondary dongle task and save config.

    Args:
        task: "adsb", "meteor", "wefax", or None (to disable)

    Returns:
        Updated config dict
    """
    config = load_config()
    if task and task in ("adsb", "meteor", "wefax"):
        config["secondary_dongle"]["enabled"] = True
        config["secondary_dongle"]["task"] = task
    else:
        config["secondary_dongle"]["enabled"] = False
        config["secondary_dongle"]["task"] = None
    save_config(config)
    return config


def get_startup_preset(config=None):
    """Preset id to tune when the app starts, or None to start idle.

    A pinned `startup.default_preset` wins; otherwise the last audio preset
    tuned from the UI is resumed, so a restart picks up where it left off.
    """
    if config is None:
        config = load_config()
    startup = config.get("startup", {})
    if not startup.get("auto_tune", True):
        return None
    return startup.get("default_preset") or config.get("last_preset")



def get_automation(config=None):
    """Return the automation block with defaults applied."""
    if config is None:
        config = load_config()
    auto = dict(DEFAULT_CONFIG["automation"])
    auto.update(config.get("automation") or {})
    return auto


def is_automation_enabled(task, config=None):
    """True if `task` (apt/wefax/adsb_scan) may seize the SDR on its own.

    The master switch wins: turning automation off disables every task without
    having to clear each flag.
    """
    auto = get_automation(config)
    if not auto.get("enabled", True):
        return False
    return bool(auto.get(task, True))


def set_automation(patch):
    """Merge a partial automation update and persist it."""
    config = load_config()
    auto = get_automation(config)
    for key, value in (patch or {}).items():
        if key in DEFAULT_CONFIG["automation"]:
            auto[key] = bool(value)
    config["automation"] = auto
    save_config(config)
    return auto

def set_last_preset(preset_id):
    """Remember the last preset tuned, for resume-on-restart.

    Recorded for EVERY mode, not just audio ones. Dedicated modes (ISM, APRS,
    pager, ACARS, AIS, ADS-B) used to return early without recording, so a
    restart resurrected whatever audio preset preceded them — the node appeared
    to swap itself back to a default.
    """
    config = load_config()
    if config.get("last_preset") == preset_id:
        return config
    config["last_preset"] = preset_id
    save_config(config)
    return config


def get_settings(config=None):
    """Return the runtime settings block, backfilling any missing defaults.

    Configs written by older versions may lack `settings` (or individual keys),
    so merge onto the defaults rather than returning the stored block directly.
    """
    if config is None:
        config = load_config()
    merged = _deep_copy(DEFAULT_CONFIG["settings"])
    stored = config.get("settings")
    if isinstance(stored, dict):
        _merge(merged, stored)
    return merged


def update_settings(patch):
    """Merge `patch` into the settings block and persist. Returns new settings.

    Only keys present in `patch` are changed; everything else is preserved.
    """
    config = load_config()
    current = get_settings(config)
    if isinstance(patch, dict):
        _merge(current, patch)
    config["settings"] = current
    save_config(config)
    return current


def _deep_copy(d):
    """Simple deep copy for nested dicts."""
    return json.loads(json.dumps(d))


def _merge(base, override):
    """Merge override dict into base dict recursively."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _merge(base[key], value)
        else:
            base[key] = value
