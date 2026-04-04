"""
Bit life tracker service.

Logs cut time per bit, estimates wear based on material hardness and
feed rate, and warns when a bit is likely getting dull.

Data is persisted in config/settings.json under the "bits.profiles" key.
The tracker updates the file at the end of each job.

Material hardness factors (approximate, relative to soft pine = 1.0):
  pine / spruce      0.8
  poplar             1.0
  maple / oak        1.5
  MDF                1.1
  plywood (birch)    1.2
  aluminium          3.0
"""

import json
import os
import time
from typing import Optional

from utils.logger import log_bit_event

_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "settings.json")

_MATERIAL_HARDNESS = {
    "pine": 0.8,
    "spruce": 0.8,
    "poplar": 1.0,
    "maple": 1.5,
    "oak": 1.5,
    "walnut": 1.4,
    "mdf": 1.1,
    "plywood": 1.2,
    "birch": 1.2,
    "aluminium": 3.0,
    "aluminum": 3.0,
    "default": 1.0,
}

_WARN_HOURS_DEFAULT = 5.0

# Active job tracking state
_active_job: Optional[dict] = None


def _load_settings() -> dict:
    try:
        with open(_SETTINGS_PATH, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_settings(settings: dict):
    with open(_SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)


def _get_bit_profile(settings: dict, bit_name: str) -> dict:
    profiles = settings.get("bits", {}).get("profiles", {})
    if bit_name not in profiles:
        profiles[bit_name] = {
            "description": bit_name,
            "hardness_factor": 1.0,
            "total_hours": 0.0,
            "effective_hours": 0.0,
            "last_used": None,
        }
        settings.setdefault("bits", {})["profiles"] = profiles
    return profiles[bit_name]


def start_job(bit_name: str, material: str, feed_rate_mmpm: float):
    """Call when a job starts cutting. Records start time and parameters."""
    global _active_job
    _active_job = {
        "bit_name": bit_name,
        "material": material,
        "feed_rate_mmpm": feed_rate_mmpm,
        "start_time": time.monotonic(),
    }
    log_bit_event(bit_name, "job_start", {"material": material, "feed_rate": feed_rate_mmpm})


def end_job():
    """
    Call when a job ends (completed, aborted, or crashed).
    Updates the bit profile with elapsed cut time, weighted by material hardness.
    Returns a dict with the updated profile and any wear warning.
    """
    global _active_job
    if _active_job is None:
        return {}

    elapsed_seconds = time.monotonic() - _active_job["start_time"]
    elapsed_hours = elapsed_seconds / 3600

    material = _active_job.get("material", "default").lower()
    hardness = _MATERIAL_HARDNESS.get(material, _MATERIAL_HARDNESS["default"])

    # Effective hours account for material hardness
    effective_hours = elapsed_hours * hardness

    bit_name = _active_job["bit_name"]
    settings = _load_settings()
    profile = _get_bit_profile(settings, bit_name)

    profile["total_hours"] = round(profile.get("total_hours", 0) + elapsed_hours, 3)
    profile["effective_hours"] = round(profile.get("effective_hours", 0) + effective_hours, 3)
    profile["last_used"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    warn_hours = settings.get("bits", {}).get("warn_hours", _WARN_HOURS_DEFAULT)
    near_end = profile["effective_hours"] >= warn_hours * 0.8
    at_limit = profile["effective_hours"] >= warn_hours

    warning = None
    if at_limit:
        warning = (
            f"⚠ {bit_name} has {profile['effective_hours']:.1f} effective hours — "
            f"consider replacing before the next job."
        )
    elif near_end:
        warning = (
            f"ℹ {bit_name} has {profile['effective_hours']:.1f} effective hours "
            f"(warn threshold: {warn_hours:.1f}h). Inspect before next use."
        )

    _save_settings(settings)

    result = {
        "bit_name": bit_name,
        "cut_hours_this_job": round(elapsed_hours, 3),
        "effective_hours_this_job": round(effective_hours, 3),
        "total_hours": profile["total_hours"],
        "effective_hours": profile["effective_hours"],
        "warning": warning,
    }

    log_bit_event(bit_name, "job_end", result)
    _active_job = None
    return result


def get_all_profiles() -> dict:
    """Return all bit profiles from settings."""
    settings = _load_settings()
    return settings.get("bits", {}).get("profiles", {})


def reset_bit(bit_name: str):
    """Reset a bit's wear counter (e.g. after replacing with a new one)."""
    settings = _load_settings()
    profiles = settings.get("bits", {}).get("profiles", {})
    if bit_name in profiles:
        profiles[bit_name]["total_hours"] = 0.0
        profiles[bit_name]["effective_hours"] = 0.0
        profiles[bit_name]["last_used"] = None
        _save_settings(settings)
        log_bit_event(bit_name, "reset", {})
