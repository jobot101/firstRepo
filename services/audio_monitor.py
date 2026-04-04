"""
Vibration monitor — piezo contact mic on the router body via Behringer UCA22.

Runs entirely without Claude. All interpretation is algorithmic —
signal processing does not need a language model.

Why piezo + audio interface instead of MEMS accelerometer:
  The UCA22 samples at 44,100 Hz — 88× faster than the MPU-6050.
  That gives FFT frequency resolution up to 22kHz, covering every
  spindle harmonic cleanly. The piezo reads structure-borne vibration
  only (contact sensor) so the vacuum is completely irrelevant.

Hardware:
  Piezo contact mic → UCA22 instrument input → USB → Raspberry Pi
  The UCA22 appears as a standard ALSA audio device. No configuration
  needed beyond plugging it in.

How it works:
  Streams audio at 44.1kHz, runs FFT on 4096-sample windows (~93ms)
  with 50% overlap — a new window every ~46ms.
  Tracks spectral flux — how much the frequency profile changes
  window-to-window. A smooth cut is spectrally stable. Chatter and
  wrong feeds show up as instability in the spectrum.

Five outputs, all rule-based, no API calls:
  crash          — RMS spike >6× baseline → spindle off immediately
  chatter        — high flux sustained 3 windows (~140ms) → feed/RPM hint
  z_drift        — RMS trending up on a stable cut → bit pullout / Z steps
                   first check at ~185ms into a stable cut
  feed_suggestion — flux in elevated zone → propose ±5% feed override
  fingerprint    — per-bit/material spectrum profile, built across jobs
"""

import json
import math
import os
import queue
import threading
from collections import deque
from typing import Callable, Optional

from utils.logger import log

# ── Audio constants ───────────────────────────────────────────────────────────
_SAMPLE_RATE_HZ = 44100
_FFT_WINDOW     = 4096    # ~93ms per window, 10.8Hz frequency resolution
_BLOCK_SIZE     = 1024    # callback block size (low latency)

# ── Tuning constants ──────────────────────────────────────────────────────────
_BASELINE_WINDOWS = 20        # windows before baseline is ready (~930ms)
_CRASH_MULTIPLIER = 6.0       # RMS spike → crash
_FLUX_WARN        = 0.45      # spectral flux threshold → chatter alert
_FLUX_SUSTAIN     = 3         # consecutive high-flux windows → alert (~140ms)

# Resonance fingerprinting
_FP_EMA_ALPHA    = 0.08   # how fast fingerprint adapts (slow — resists transients)
_FP_MIN_WINDOWS  = 30     # stable windows before fingerprint is trusted
_FP_PATH         = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "config", "fingerprints.json"
)

# Z-drift detection — window every ~46ms so this is very fast
_ZDRIFT_HISTORY   = 8     # rolling window (~370ms at 46ms/window)
_ZDRIFT_MIN_CHECK = 4     # start checking after ~185ms
_ZDRIFT_THRESHOLD = 0.12  # fractional RMS increase (first → second half) → alert

# Feed autotune
_AUTOTUNE_FLUX_HIGH  = 0.30   # elevated but below chatter → suggest reduce
_AUTOTUNE_FLUX_LOW   = 0.10   # very clean → suggest increase
_AUTOTUNE_SUSTAIN    = 10     # consecutive windows before suggesting (~460ms)
_AUTOTUNE_COOLDOWN   = 200    # windows before another suggestion (~9s)
_AUTOTUNE_STEP_PCT   = 5      # % feed override adjustment per suggestion


class VibrationMonitor:
    """
    Reads the UCA22 piezo input and watches for cutting anomalies using FFT.
    Call start(bit_name, material) when a job begins, stop() when it ends.
    The fingerprint for this bit/material combo is loaded on start and
    saved on stop — it gets more accurate with every run.
    """

    def __init__(
        self,
        device=None,
        on_crash: Optional[Callable] = None,
        on_chatter: Optional[Callable[[str, dict], None]] = None,
        on_z_drift: Optional[Callable[[dict], None]] = None,
        on_feed_suggestion: Optional[Callable[[dict], None]] = None,
    ):
        # device: None = system default, or name string / index int
        self._device = device
        self.on_crash           = on_crash
        self.on_chatter         = on_chatter
        self.on_z_drift         = on_z_drift
        self.on_feed_suggestion = on_feed_suggestion

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._q: queue.Queue = queue.Queue()

        # Job context
        self._bit_name = "unknown"
        self._material = "default"

        # Baseline
        self._baseline_spectrum  = None
        self._baseline_rms: Optional[float] = None
        self._baseline_windows: list = []
        self._baseline_ready = False

        # Chatter sustain
        self._high_flux_count = 0

        # Resonance fingerprint
        self._fingerprint: Optional[dict] = None
        self._fp_stable_windows = 0

        # Z-drift
        self._rms_history: deque = deque(maxlen=_ZDRIFT_HISTORY)
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
            import sounddevice as sd  # noqa: F401 — verify import before starting thread
        except ImportError:
            log("vibration_monitor", {"event": "unavailable", "reason": "sounddevice not installed"})
            return

        self._bit_name = bit_name
        self._material = material
        self._running  = True

        self._baseline_ready    = False
        self._baseline_windows  = []
        self._high_flux_count   = 0
        self._rms_history.clear()
        self._zdrift_fired      = False
        self._autotune_high_count  = 0
        self._autotune_low_count   = 0
        self._autotune_cd_reduce   = 0
        self._autotune_cd_increase = 0

        self._fingerprint       = _fp_load(bit_name, material)
        self._fp_stable_windows = 0

        # Drain any stale samples from a previous run
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log("vibration_monitor", {
            "event":       "started",
            "device":      self._device or "default",
            "bit":         bit_name,
            "material":    material,
            "fingerprint": "loaded" if self._fingerprint else "none",
        })

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

        if self._fingerprint and self._fp_stable_windows >= 5:
            _fp_save(self._bit_name, self._material, self._fingerprint)
            log("vibration_monitor", {
                "event":         "fingerprint_saved",
                "bit":           self._bit_name,
                "material":      self._material,
                "total_windows": self._fingerprint["window_count"],
            })

        log("vibration_monitor", {"event": "stopped"})

    def reset_baseline(self):
        self._baseline_ready   = False
        self._baseline_windows = []
        self._baseline_spectrum = None
        self._baseline_rms      = None

    # ── Audio stream ────────────────────────────────────────────────────────────

    def _run(self):
        try:
            import sounddevice as sd
            import numpy as np
        except ImportError:
            log("vibration_monitor", {"event": "unavailable", "reason": "sounddevice not installed"})
            return

        def _callback(indata, frames, time_info, status):
            # indata shape: (frames, channels) — take left channel only
            self._q.put(indata[:, 0].copy())

        try:
            stream = sd.InputStream(
                device=self._device,
                channels=1,
                samplerate=_SAMPLE_RATE_HZ,
                blocksize=_BLOCK_SIZE,
                dtype="float32",
                callback=_callback,
            )
        except Exception as exc:
            log("vibration_monitor", {"event": "stream_error", "reason": str(exc)})
            self._running = False
            return

        buffer = []
        with stream:
            while self._running:
                try:
                    chunk = self._q.get(timeout=1.0)
                    buffer.extend(chunk.tolist())
                    while len(buffer) >= _FFT_WINDOW:
                        self._process_window(buffer[:_FFT_WINDOW])
                        buffer = buffer[_FFT_WINDOW // 2:]  # 50% overlap
                except queue.Empty:
                    continue

    # ── FFT analysis ────────────────────────────────────────────────────────────

    def _process_window(self, samples: list):
        try:
            import numpy as np
        except ImportError:
            return

        sig     = np.array(samples, dtype=np.float32)
        rms     = float(np.sqrt(np.mean(sig ** 2)))

        # ── Crash: sudden RMS spike ───────────────────────────────────────────
        if self._baseline_rms and rms > self._baseline_rms * _CRASH_MULTIPLIER:
            log("vibration_monitor", {
                "event":     "crash_detected",
                "rms_ratio": round(rms / self._baseline_rms, 2),
            })
            if self.on_crash:
                self.on_crash()
            return

        # ── FFT ───────────────────────────────────────────────────────────────
        windowed = sig * np.hanning(len(sig))
        spectrum = np.abs(np.fft.rfft(windowed))
        spectrum = spectrum / (np.max(spectrum) + 1e-9)

        # ── Baseline collection ───────────────────────────────────────────────
        if not self._baseline_ready:
            self._baseline_windows.append((spectrum, rms))
            if len(self._baseline_windows) >= _BASELINE_WINDOWS:
                all_specs = np.array([s for s, _ in self._baseline_windows])
                self._baseline_spectrum = np.mean(all_specs, axis=0)
                self._baseline_rms      = float(np.mean([r for _, r in self._baseline_windows]))
                self._baseline_ready    = True
                log("vibration_monitor", {
                    "event":        "baseline_ready",
                    "baseline_rms": round(self._baseline_rms, 4),
                })
            return

        # ── Choose reference spectrum ─────────────────────────────────────────
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
        diff         = spectrum - reference
        flux         = float(np.sqrt(np.mean(diff ** 2)))
        peak_idx     = int(np.argmax(np.abs(diff)))
        peak_freq_hz = peak_idx * (_SAMPLE_RATE_HZ / _FFT_WINDOW)

        # ── Tick autotune cooldowns ───────────────────────────────────────────
        if self._autotune_cd_reduce   > 0: self._autotune_cd_reduce   -= 1
        if self._autotune_cd_increase > 0: self._autotune_cd_increase -= 1

        # ── Chatter alert ─────────────────────────────────────────────────────
        if flux > _FLUX_WARN:
            self._high_flux_count      += 1
            self._autotune_high_count  += 1
            self._autotune_low_count    = 0
            if self._high_flux_count >= _FLUX_SUSTAIN:
                metrics = {
                    "spectral_flux":   round(flux, 3),
                    "peak_shift_hz":   round(peak_freq_hz, 1),
                    "baseline_rms":    round(self._baseline_rms, 4),
                    "current_rms":     round(rms, 4),
                    "reference":       "fingerprint" if fp_trusted else "job_baseline",
                }
                description = (
                    f"Cutting vibration changed — spectral flux {flux:.2f} "
                    f"(threshold {_FLUX_WARN}). Dominant shift at {peak_freq_hz:.0f} Hz."
                )
                log("vibration_monitor", {"event": "chatter_detected", **metrics})
                if self.on_chatter:
                    self.on_chatter(description, metrics)
                self._high_flux_count     = 0
                self._autotune_high_count = 0
                self._autotune_cd_reduce  = _AUTOTUNE_COOLDOWN
        else:
            self._high_flux_count = 0

            # ── Z-drift: RMS trending up on a stable cut ──────────────────────
            self._rms_history.append(rms)
            n = len(self._rms_history)
            if n >= _ZDRIFT_MIN_CHECK and not self._zdrift_fired:
                half  = n // 2
                early = sum(list(self._rms_history)[:half]) / half
                late  = sum(list(self._rms_history)[half:]) / half
                if early > 0 and (late - early) / early > _ZDRIFT_THRESHOLD:
                    payload = {
                        "early_rms":  round(early, 4),
                        "late_rms":   round(late, 4),
                        "drift_pct":  round((late - early) / early * 100, 1),
                    }
                    log("vibration_monitor", {"event": "z_drift_detected", **payload})
                    if self.on_z_drift:
                        self.on_z_drift(payload)
                    self._zdrift_fired = True

            # ── Feed autotune ─────────────────────────────────────────────────
            if flux > _AUTOTUNE_FLUX_HIGH:
                self._autotune_high_count += 1
                self._autotune_low_count   = 0
                if (self._autotune_high_count >= _AUTOTUNE_SUSTAIN
                        and self._autotune_cd_reduce == 0):
                    log("vibration_monitor", {"event": "feed_suggest_reduce", "flux": round(flux, 3)})
                    if self.on_feed_suggestion:
                        self.on_feed_suggestion({
                            "direction": "reduce",
                            "step_pct":  _AUTOTUNE_STEP_PCT,
                            "reason":    f"Flux {flux:.2f} elevated for {self._autotune_high_count} windows.",
                        })
                    self._autotune_high_count = 0
                    self._autotune_cd_reduce  = _AUTOTUNE_COOLDOWN
            elif flux < _AUTOTUNE_FLUX_LOW:
                self._autotune_low_count  += 1
                self._autotune_high_count  = 0
                if (self._autotune_low_count >= _AUTOTUNE_SUSTAIN * 2
                        and self._autotune_cd_increase == 0):
                    log("vibration_monitor", {"event": "feed_suggest_increase", "flux": round(flux, 3)})
                    if self.on_feed_suggestion:
                        self.on_feed_suggestion({
                            "direction": "increase",
                            "step_pct":  _AUTOTUNE_STEP_PCT,
                            "reason":    f"Flux {flux:.2f} very stable — cut is smooth.",
                        })
                    self._autotune_low_count  = 0
                    self._autotune_cd_increase = _AUTOTUNE_COOLDOWN
            else:
                self._autotune_high_count = 0
                self._autotune_low_count  = 0

            # ── Fingerprint update (stable windows only) ──────────────────────
            if self._fingerprint is None:
                self._fingerprint = {
                    "spectrum":      spectrum.tolist(),
                    "rms":           rms,
                    "window_count":  1,
                }
            else:
                a        = _FP_EMA_ALPHA
                fp_spec  = np.array(self._fingerprint["spectrum"])
                self._fingerprint["spectrum"]     = (a * spectrum + (1 - a) * fp_spec).tolist()
                self._fingerprint["rms"]          = a * rms + (1 - a) * self._fingerprint["rms"]
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

def diagnose_chatter(description: str, metrics: dict) -> dict:
    """Rule-based chatter diagnosis from FFT metrics. No API calls."""
    flux     = metrics.get("spectral_flux", 0)
    baseline = metrics.get("baseline_rms", 1)
    current  = metrics.get("current_rms", baseline)
    peak_hz  = metrics.get("peak_shift_hz", 0)

    mag_ratio = current / baseline if baseline else 1.0

    if mag_ratio > 1.15:
        diagnosis   = "Cutting amplitude increased with spectral change — likely feed rate too high."
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
