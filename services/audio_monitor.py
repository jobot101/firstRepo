"""
Vibration monitor — MPU-6050 accelerometer on the router body.

Runs entirely without Claude. All interpretation is algorithmic —
signal processing does not need a language model.

Why accelerometer instead of microphone:
  The vacuum dominates any microphone signal.
  The MPU-6050 is mounted on the router body and measures
  structure-borne vibration directly — the vacuum is irrelevant.

Wiring (GY-521 breakout board → Raspberry Pi):
  VCC  → Pin 1  (3.3V)
  GND  → Pin 6  (GND)
  SDA  → Pin 3  (GPIO 2, I2C SDA)
  SCL  → Pin 5  (GPIO 3, I2C SCL)

  Enable I2C:  sudo raspi-config → Interface Options → I2C → Enable

How it works:
  Samples at 500Hz, runs FFT on 512-sample windows with 50% overlap.
  Tracks spectral flux — how much the frequency profile changes
  window-to-window. A smooth cut is spectrally stable. Chatter and
  wrong feeds show up as instability.

  Five outputs, all rule-based, no API calls:
    crash          — magnitude spike >6× baseline → spindle off immediately
    chatter        — high flux sustained 3 windows → suggests feed/RPM change
    z_drift        — magnitude trending up on a stable cut → bit pullout / Z steps
    feed_suggestion — flux in elevated zone → propose ±5% feed override
    fingerprint    — per-bit/material spectrum profile, built across jobs,
                     used as a more sensitive reference than the job baseline
"""

import json
import math
import os
import threading
import time
from collections import deque
from typing import Callable, Optional

from utils.logger import log

# ── MPU-6050 registers ────────────────────────────────────────────────────────
_MPU_ADDR      = 0x68
_PWR_MGMT_1    = 0x6B
_ACCEL_XOUT_H  = 0x3B
_ACCEL_CONFIG  = 0x1C
_SMPLRT_DIV    = 0x19
_CONFIG_REG    = 0x1A
_DLPF_CFG      = 0x02   # Low-pass filter: 94Hz bandwidth, 3ms delay

# ── Tuning constants ──────────────────────────────────────────────────────────
_SAMPLE_RATE_HZ   = 500       # samples per second
_FFT_WINDOW       = 512       # samples per FFT (must be power of 2)
_BASELINE_WINDOWS = 10        # windows to collect before baseline is ready
_CRASH_MULTIPLIER = 6.0       # magnitude spike → crash
_FLUX_WARN        = 0.45      # spectral flux threshold → chatter alert
_FLUX_SUSTAIN     = 3         # consecutive high-flux windows → chatter alert

# Resonance fingerprinting
_FP_EMA_ALPHA    = 0.08   # how fast the fingerprint adapts (slow — resists transients)
_FP_MIN_WINDOWS  = 30     # stable windows needed before fingerprint is trusted
_FP_PATH         = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "config", "fingerprints.json"
)

# Z-drift detection
_ZDRIFT_HISTORY   = 8     # rolling window (~4s at 500ms/window)
_ZDRIFT_MIN_CHECK = 4     # start checking after this many windows (~2s)
_ZDRIFT_THRESHOLD = 0.12  # fractional magnitude increase (first → second half) → alert

# Feed autotune
_AUTOTUNE_FLUX_HIGH  = 0.30   # elevated but below chatter → suggest reduce
_AUTOTUNE_FLUX_LOW   = 0.10   # very clean cut → suggest increase
_AUTOTUNE_SUSTAIN    = 6      # consecutive windows before suggesting
_AUTOTUNE_COOLDOWN   = 40     # windows before another suggestion in the same direction
_AUTOTUNE_STEP_PCT   = 5      # % feed override adjustment per suggestion


class VibrationMonitor:
    """
    Reads the MPU-6050 and watches for cutting anomalies using FFT.
    Call start(bit_name, material) when a job begins, stop() when it ends.
    The fingerprint for this bit/material combo is loaded on start and
    saved on stop — it gets more accurate with every run.
    """

    def __init__(
        self,
        i2c_address: int = _MPU_ADDR,
        on_crash: Optional[Callable] = None,
        on_chatter: Optional[Callable[[str, dict], None]] = None,
        on_z_drift: Optional[Callable[[dict], None]] = None,
        on_feed_suggestion: Optional[Callable[[dict], None]] = None,
    ):
        self._addr = i2c_address
        self.on_crash          = on_crash
        self.on_chatter        = on_chatter
        self.on_z_drift        = on_z_drift
        self.on_feed_suggestion = on_feed_suggestion

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._bus = None

        # Job context (set on start)
        self._bit_name = "unknown"
        self._material = "default"

        # Baseline state
        self._baseline_spectrum: Optional[list] = None
        self._baseline_magnitude: Optional[float] = None
        self._baseline_windows: list = []
        self._baseline_ready = False

        # Chatter sustain
        self._high_flux_count = 0

        # Resonance fingerprint
        self._fingerprint: Optional[dict] = None   # loaded from file
        self._fp_stable_windows = 0                # stable windows this job

        # Z-drift
        self._mag_history: deque = deque(maxlen=_ZDRIFT_HISTORY)
        self._zdrift_fired = False

        # Feed autotune
        self._autotune_high_count  = 0
        self._autotune_low_count   = 0
        self._autotune_cd_reduce   = 0
        self._autotune_cd_increase = 0

    # ── Public ─────────────────────────────────────────────────────────────────

    def start(self, bit_name: str = "unknown", material: str = "default"):
        if self._running:
            return
        try:
            import smbus2
            self._bus = smbus2.SMBus(1)
            self._init_mpu()
        except Exception as exc:
            log("vibration_monitor", {"event": "unavailable", "reason": str(exc)})
            return

        self._bit_name = bit_name
        self._material = material

        self._running = True
        self._baseline_ready = False
        self._baseline_windows = []
        self._high_flux_count = 0
        self._mag_history.clear()
        self._zdrift_fired = False
        self._autotune_high_count  = 0
        self._autotune_low_count   = 0
        self._autotune_cd_reduce   = 0
        self._autotune_cd_increase = 0

        self._fingerprint = _fp_load(bit_name, material)
        self._fp_stable_windows = 0

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log("vibration_monitor", {
            "event": "started",
            "addr": hex(self._addr),
            "bit": bit_name,
            "material": material,
            "fingerprint": "loaded" if self._fingerprint else "none",
        })

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        if self._bus:
            self._bus.close()
            self._bus = None

        # Persist fingerprint if this job contributed stable data
        if self._fingerprint and self._fp_stable_windows >= 5:
            _fp_save(self._bit_name, self._material, self._fingerprint)
            log("vibration_monitor", {
                "event": "fingerprint_saved",
                "bit": self._bit_name,
                "material": self._material,
                "total_windows": self._fingerprint["window_count"],
            })

        log("vibration_monitor", {"event": "stopped"})

    def reset_baseline(self):
        self._baseline_ready = False
        self._baseline_windows = []
        self._baseline_spectrum = None
        self._baseline_magnitude = None

    # ── MPU-6050 init ───────────────────────────────────────────────────────────

    def _init_mpu(self):
        self._bus.write_byte_data(self._addr, _PWR_MGMT_1, 0x00)
        time.sleep(0.1)
        self._bus.write_byte_data(self._addr, _ACCEL_CONFIG, 0x08)  # ±4g
        div = max(0, int(1000 / _SAMPLE_RATE_HZ) - 1)
        self._bus.write_byte_data(self._addr, _SMPLRT_DIV, div)
        self._bus.write_byte_data(self._addr, _CONFIG_REG, _DLPF_CFG)

    # ── Sampling loop ───────────────────────────────────────────────────────────

    def _run(self):
        interval = 1.0 / _SAMPLE_RATE_HZ
        samples = []

        while self._running:
            t0 = time.monotonic()

            raw = self._read_accel()
            if raw:
                samples.append(raw)

            if len(samples) >= _FFT_WINDOW:
                self._process_window(samples[:_FFT_WINDOW])
                samples = samples[_FFT_WINDOW // 2:]

            elapsed = time.monotonic() - t0
            sleep = interval - elapsed
            if sleep > 0:
                time.sleep(sleep)

    def _read_accel(self) -> Optional[float]:
        try:
            data = self._bus.read_i2c_block_data(self._addr, _ACCEL_XOUT_H, 6)
            x = _s16(data[0], data[1])
            y = _s16(data[2], data[3])
            z = _s16(data[4], data[5])
            return math.sqrt(x*x + y*y + z*z)
        except Exception:
            return None

    # ── FFT analysis ────────────────────────────────────────────────────────────

    def _process_window(self, samples: list):
        try:
            import numpy as np
        except ImportError:
            log("vibration_monitor", {"event": "numpy_missing"})
            return

        magnitudes = np.array(samples, dtype=np.float32)
        mean_mag   = float(np.mean(magnitudes))

        # ── Crash: sudden magnitude spike ─────────────────────────────────────
        if self._baseline_magnitude and mean_mag > self._baseline_magnitude * _CRASH_MULTIPLIER:
            log("vibration_monitor", {
                "event": "crash_detected",
                "mag_ratio": round(mean_mag / self._baseline_magnitude, 2),
            })
            if self.on_crash:
                self.on_crash()
            return

        # ── FFT ───────────────────────────────────────────────────────────────
        signal   = magnitudes - np.mean(magnitudes)
        windowed = signal * np.hanning(len(signal))
        spectrum = np.abs(np.fft.rfft(windowed))
        spectrum = spectrum / (np.max(spectrum) + 1e-9)

        # ── Baseline collection ───────────────────────────────────────────────
        if not self._baseline_ready:
            self._baseline_windows.append((spectrum, mean_mag))
            if len(self._baseline_windows) >= _BASELINE_WINDOWS:
                all_specs = np.array([s for s, _ in self._baseline_windows])
                self._baseline_spectrum  = np.mean(all_specs, axis=0)
                self._baseline_magnitude = float(np.mean([m for _, m in self._baseline_windows]))
                self._baseline_ready = True
                log("vibration_monitor", {
                    "event": "baseline_ready",
                    "baseline_magnitude": round(self._baseline_magnitude, 1),
                })
            return

        # ── Choose reference spectrum ─────────────────────────────────────────
        # Prefer fingerprint once it has enough history — it's more stable
        # than a single-job baseline because it averages across many runs.
        fp_trusted = (
            self._fingerprint is not None
            and self._fingerprint["window_count"] >= _FP_MIN_WINDOWS
        )
        reference = (
            np.array(self._fingerprint["spectrum"])
            if fp_trusted
            else np.array(self._baseline_spectrum)
        )

        # ── Spectral flux ─────────────────────────────────────────────────────
        diff = spectrum - reference
        flux = float(np.sqrt(np.mean(diff ** 2)))

        freq_resolution = _SAMPLE_RATE_HZ / _FFT_WINDOW
        peak_idx        = int(np.argmax(np.abs(diff)))
        peak_freq_hz    = peak_idx * freq_resolution

        # ── Tick autotune cooldowns ───────────────────────────────────────────
        if self._autotune_cd_reduce   > 0: self._autotune_cd_reduce   -= 1
        if self._autotune_cd_increase > 0: self._autotune_cd_increase -= 1

        # ── Chatter alert ─────────────────────────────────────────────────────
        if flux > _FLUX_WARN:
            self._high_flux_count += 1
            self._autotune_high_count += 1
            self._autotune_low_count   = 0
            if self._high_flux_count >= _FLUX_SUSTAIN:
                metrics = {
                    "spectral_flux":      round(flux, 3),
                    "peak_shift_hz":      round(peak_freq_hz, 1),
                    "baseline_magnitude": round(self._baseline_magnitude, 1),
                    "current_magnitude":  round(mean_mag, 1),
                    "reference":          "fingerprint" if fp_trusted else "job_baseline",
                }
                description = (
                    f"Cutting vibration changed — spectral flux {flux:.2f} "
                    f"(threshold {_FLUX_WARN}). Dominant shift at {peak_freq_hz:.0f} Hz."
                )
                log("vibration_monitor", {"event": "chatter_detected", **metrics})
                if self.on_chatter:
                    self.on_chatter(description, metrics)
                self._high_flux_count      = 0
                self._autotune_high_count  = 0
                self._autotune_cd_reduce   = _AUTOTUNE_COOLDOWN
        else:
            self._high_flux_count = 0

            # ── Z-drift: magnitude trending up on a stable cut ────────────────
            self._mag_history.append(mean_mag)
            n = len(self._mag_history)
            if n >= _ZDRIFT_MIN_CHECK and not self._zdrift_fired:
                half    = n // 2
                early   = sum(list(self._mag_history)[:half]) / half
                late    = sum(list(self._mag_history)[half:]) / half
                if early > 0 and (late - early) / early > _ZDRIFT_THRESHOLD:
                    log("vibration_monitor", {
                        "event":      "z_drift_detected",
                        "early_mag":  round(early, 1),
                        "late_mag":   round(late, 1),
                        "drift_pct":  round((late - early) / early * 100, 1),
                    })
                    if self.on_z_drift:
                        self.on_z_drift({
                            "early_magnitude": round(early, 1),
                            "late_magnitude":  round(late, 1),
                            "drift_pct":       round((late - early) / early * 100, 1),
                        })
                    self._zdrift_fired = True   # once per job is enough

            # ── Feed autotune ─────────────────────────────────────────────────
            if flux > _AUTOTUNE_FLUX_HIGH:
                # Elevated but below chatter — cut is rougher than ideal
                self._autotune_high_count += 1
                self._autotune_low_count   = 0
                if (self._autotune_high_count >= _AUTOTUNE_SUSTAIN
                        and self._autotune_cd_reduce == 0):
                    log("vibration_monitor", {"event": "feed_suggest_reduce", "flux": round(flux, 3)})
                    if self.on_feed_suggestion:
                        self.on_feed_suggestion({
                            "direction": "reduce",
                            "step_pct":  _AUTOTUNE_STEP_PCT,
                            "reason":    f"Flux {flux:.2f} elevated for {self._autotune_high_count} windows — cut rougher than baseline.",
                        })
                    self._autotune_high_count = 0
                    self._autotune_cd_reduce  = _AUTOTUNE_COOLDOWN
            elif flux < _AUTOTUNE_FLUX_LOW:
                # Very clean — cut is smooth, could try a little more feed
                self._autotune_low_count  += 1
                self._autotune_high_count  = 0
                if (self._autotune_low_count >= _AUTOTUNE_SUSTAIN * 2
                        and self._autotune_cd_increase == 0):
                    log("vibration_monitor", {"event": "feed_suggest_increase", "flux": round(flux, 3)})
                    if self.on_feed_suggestion:
                        self.on_feed_suggestion({
                            "direction": "increase",
                            "step_pct":  _AUTOTUNE_STEP_PCT,
                            "reason":    f"Flux {flux:.2f} very stable — cut is smooth, could try more feed.",
                        })
                    self._autotune_low_count  = 0
                    self._autotune_cd_increase = _AUTOTUNE_COOLDOWN
            else:
                self._autotune_high_count = 0
                self._autotune_low_count  = 0

            # ── Fingerprint update (stable window only) ───────────────────────
            if self._fingerprint is None:
                self._fingerprint = {
                    "spectrum":      spectrum.tolist(),
                    "magnitude":     mean_mag,
                    "window_count":  1,
                }
            else:
                a = _FP_EMA_ALPHA
                fp_spec = np.array(self._fingerprint["spectrum"])
                self._fingerprint["spectrum"]     = (a * spectrum + (1 - a) * fp_spec).tolist()
                self._fingerprint["magnitude"]    = a * mean_mag + (1 - a) * self._fingerprint["magnitude"]
                self._fingerprint["window_count"] += 1
            self._fp_stable_windows += 1


# ── Fingerprint persistence ────────────────────────────────────────────────────

def _fp_key(bit_name: str, material: str) -> str:
    return f"{bit_name}:{material}"


def _fp_load(bit_name: str, material: str) -> Optional[dict]:
    try:
        with open(_FP_PATH) as f:
            db = json.load(f)
        return db.get(_fp_key(bit_name, material))
    except (OSError, json.JSONDecodeError):
        return None


def _fp_save(bit_name: str, material: str, fingerprint: dict):
    try:
        try:
            with open(_FP_PATH) as f:
                db = json.load(f)
        except (OSError, json.JSONDecodeError):
            db = {}
        db[_fp_key(bit_name, material)] = fingerprint
        with open(_FP_PATH, "w") as f:
            json.dump(db, f)
    except OSError as exc:
        log("vibration_monitor", {"event": "fingerprint_save_error", "reason": str(exc)})


# ── Helpers ────────────────────────────────────────────────────────────────────

def _s16(high: int, low: int) -> int:
    val = (high << 8) | low
    return val - 65536 if val >= 32768 else val


def diagnose_chatter(description: str, metrics: dict) -> dict:
    """
    Rule-based chatter diagnosis from FFT metrics. No API calls.
    """
    flux     = metrics.get("spectral_flux", 0)
    baseline = metrics.get("baseline_magnitude", 1)
    current  = metrics.get("current_magnitude", baseline)
    peak_hz  = metrics.get("peak_shift_hz", 0)

    mag_ratio = current / baseline if baseline else 1.0

    if mag_ratio > 1.15:
        diagnosis   = "Vibration amplitude increased with spectral change — likely feed rate too high."
        explanation = "Reduce feed rate 10–15% and observe whether vibration stabilises."
        feed_hint   = "reduce_10_to_15_pct"
        rpm_hint    = None
    elif mag_ratio < 0.90:
        diagnosis   = "Low amplitude but unstable spectrum — possible rubbing or RPM too low for chip load."
        explanation = "Try increasing spindle RPM by 500 RPM to improve chip evacuation."
        feed_hint   = None
        rpm_hint    = "increase_500_rpm"
    else:
        diagnosis   = (
            f"Spectral shift at {peak_hz:.0f} Hz without major amplitude change — "
            "resonance or chatter at current feed/RPM combination."
        )
        explanation = "Adjust feed rate ±10% or spindle RPM ±500 RPM to move away from the resonant frequency."
        feed_hint   = "adjust_plus_minus_10_pct"
        rpm_hint    = "adjust_plus_minus_500_rpm"

    return {
        "diagnosis":     diagnosis,
        "explanation":   explanation,
        "feed_hint":     feed_hint,
        "rpm_hint":      rpm_hint,
        "spectral_flux": round(flux, 3),
        "peak_shift_hz": round(peak_hz, 1),
    }
