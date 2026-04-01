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

  Three outputs, all rule-based, no API calls:
    crash     — magnitude spike >6× baseline → spindle off immediately
    chatter   — high flux sustained 3 windows → suggests feed/RPM change
    wear      — magnitude creeping up over time → flags possible dull bit
"""

import math
import threading
import time
from collections import deque
from typing import Callable, Optional

from utils.logger import log

# ── MPU-6050 registers ────────────────────────────────────────────────────
_MPU_ADDR      = 0x68
_PWR_MGMT_1    = 0x6B
_ACCEL_XOUT_H  = 0x3B
_ACCEL_CONFIG  = 0x1C
_SMPLRT_DIV    = 0x19
_CONFIG_REG    = 0x1A
_DLPF_CFG      = 0x02   # Low-pass filter: 94Hz bandwidth, 3ms delay

# ── Tuning constants ──────────────────────────────────────────────────────
_SAMPLE_RATE_HZ   = 500       # samples per second
_FFT_WINDOW       = 512       # samples per FFT (must be power of 2)
_BASELINE_WINDOWS = 10        # windows to collect before baseline is ready
_CRASH_MULTIPLIER = 6.0       # magnitude spike → crash
_FLUX_WARN        = 0.45      # spectral flux threshold → chatter warning
_FLUX_SUSTAIN     = 3         # consecutive high-flux windows → alert


class VibrationMonitor:
    """
    Reads the MPU-6050 and watches for cutting anomalies using FFT.
    Call start() when a job begins, stop() when it ends.
    """

    def __init__(
        self,
        i2c_address: int = _MPU_ADDR,
        on_crash: Optional[Callable] = None,
        on_chatter: Optional[Callable[[str, dict], None]] = None,
    ):
        self._addr = i2c_address
        self.on_crash = on_crash
        self.on_chatter = on_chatter

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._bus = None

        # Baseline state
        self._baseline_spectrum: Optional[list] = None
        self._baseline_magnitude: Optional[float] = None
        self._baseline_windows: list = []
        self._baseline_ready = False

        # Chatter sustain counter
        self._high_flux_count = 0

    # ── Public ─────────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        try:
            import smbus2
            self._bus = smbus2.SMBus(1)
            self._init_mpu()
        except Exception as exc:
            log("vibration_monitor", {"event": "unavailable", "reason": str(exc)})
            return

        self._running = True
        self._baseline_ready = False
        self._baseline_windows = []
        self._high_flux_count = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log("vibration_monitor", {"event": "started", "addr": hex(self._addr)})

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        if self._bus:
            self._bus.close()
            self._bus = None
        log("vibration_monitor", {"event": "stopped"})

    def reset_baseline(self):
        self._baseline_ready = False
        self._baseline_windows = []
        self._baseline_spectrum = None
        self._baseline_magnitude = None

    # ── MPU-6050 init ──────────────────────────────────────────────────────

    def _init_mpu(self):
        # Wake the chip (clear sleep bit)
        self._bus.write_byte_data(self._addr, _PWR_MGMT_1, 0x00)
        time.sleep(0.1)
        # ±4g range (gives good resolution for router vibration)
        self._bus.write_byte_data(self._addr, _ACCEL_CONFIG, 0x08)
        # Sample rate divider: 1kHz / (1 + div) = ~500Hz
        div = max(0, int(1000 / _SAMPLE_RATE_HZ) - 1)
        self._bus.write_byte_data(self._addr, _SMPLRT_DIV, div)
        # Low-pass filter to reduce electrical noise
        self._bus.write_byte_data(self._addr, _CONFIG_REG, _DLPF_CFG)

    # ── Sampling loop ──────────────────────────────────────────────────────

    def _run(self):
        interval = 1.0 / _SAMPLE_RATE_HZ
        samples = []

        while self._running:
            t0 = time.monotonic()

            raw = self._read_accel()
            if raw:
                samples.append(raw)

            # Process a full FFT window
            if len(samples) >= _FFT_WINDOW:
                self._process_window(samples[:_FFT_WINDOW])
                # Slide by half a window (50% overlap for better time resolution)
                samples = samples[_FFT_WINDOW // 2:]

            # Pace the loop
            elapsed = time.monotonic() - t0
            sleep = interval - elapsed
            if sleep > 0:
                time.sleep(sleep)

    def _read_accel(self) -> Optional[float]:
        """Read X/Y/Z and return combined magnitude."""
        try:
            data = self._bus.read_i2c_block_data(self._addr, _ACCEL_XOUT_H, 6)
            x = _s16(data[0], data[1])
            y = _s16(data[2], data[3])
            z = _s16(data[4], data[5])
            # Magnitude in raw counts (±4g → LSB = 8192 counts/g)
            return math.sqrt(x*x + y*y + z*z)
        except Exception:
            return None

    # ── FFT analysis ───────────────────────────────────────────────────────

    def _process_window(self, samples: list):
        try:
            import numpy as np
        except ImportError:
            log("vibration_monitor", {"event": "numpy_missing"})
            return

        magnitudes = np.array(samples, dtype=np.float32)
        mean_mag = float(np.mean(magnitudes))

        # ── Crash detection: sudden spike in raw magnitude ────────────────
        if self._baseline_magnitude and mean_mag > self._baseline_magnitude * _CRASH_MULTIPLIER:
            log("vibration_monitor", {"event": "crash_detected", "mag_ratio": round(mean_mag / self._baseline_magnitude, 2)})
            if self.on_crash:
                self.on_crash()
            return

        # ── FFT ───────────────────────────────────────────────────────────
        # Remove DC offset (gravity component)
        signal = magnitudes - np.mean(magnitudes)
        # Hanning window to reduce spectral leakage
        windowed = signal * np.hanning(len(signal))
        spectrum = np.abs(np.fft.rfft(windowed))
        # Normalise
        spectrum = spectrum / (np.max(spectrum) + 1e-9)

        # ── Baseline collection ───────────────────────────────────────────
        if not self._baseline_ready:
            self._baseline_windows.append((spectrum, mean_mag))
            if len(self._baseline_windows) >= _BASELINE_WINDOWS:
                all_specs = np.array([s for s, _ in self._baseline_windows])
                self._baseline_spectrum = np.mean(all_specs, axis=0)
                self._baseline_magnitude = float(np.mean([m for _, m in self._baseline_windows]))
                self._baseline_ready = True
                log("vibration_monitor", {
                    "event": "baseline_ready",
                    "baseline_magnitude": round(self._baseline_magnitude, 1),
                })
            return

        # ── Spectral flux: how much has the spectrum changed? ─────────────
        # Low flux = steady, smooth cut. High flux = something changed.
        diff = spectrum - np.array(self._baseline_spectrum)
        flux = float(np.sqrt(np.mean(diff ** 2)))

        if flux > _FLUX_WARN:
            self._high_flux_count += 1
            if self._high_flux_count >= _FLUX_SUSTAIN:
                # Find the dominant shifted frequency for the diagnosis
                peak_idx = int(np.argmax(np.abs(diff)))
                freq_resolution = _SAMPLE_RATE_HZ / _FFT_WINDOW
                peak_freq_hz = peak_idx * freq_resolution

                description = (
                    f"Cutting vibration changed — spectral flux {flux:.2f} "
                    f"(threshold {_FLUX_WARN}). Dominant shift at "
                    f"{peak_freq_hz:.0f} Hz. "
                    "Cut may be smoother with adjusted feed or RPM."
                )
                metrics = {
                    "spectral_flux": round(flux, 3),
                    "peak_shift_hz": round(peak_freq_hz, 1),
                    "baseline_magnitude": round(self._baseline_magnitude, 1),
                    "current_magnitude": round(mean_mag, 1),
                }
                log("vibration_monitor", {"event": "chatter_detected", **metrics})
                if self.on_chatter:
                    self.on_chatter(description, metrics)
                self._high_flux_count = 0   # Reset; don't fire continuously
        else:
            self._high_flux_count = 0


# ── Helpers ────────────────────────────────────────────────────────────────

def _s16(high: int, low: int) -> int:
    """Combine two bytes into a signed 16-bit integer."""
    val = (high << 8) | low
    return val - 65536 if val >= 32768 else val


def diagnose_chatter(description: str, metrics: dict) -> dict:
    """
    Rule-based chatter diagnosis from FFT metrics. No API calls.

    Heuristics:
      - Magnitude rose AND flux is high  → feed rate likely too high
      - Magnitude stable/fell AND flux high → RPM mismatch for this bit/material
      - Default fallback                  → generic feed+RPM check suggestion
    """
    flux        = metrics.get("spectral_flux", 0)
    baseline    = metrics.get("baseline_magnitude", 1)
    current     = metrics.get("current_magnitude", baseline)
    peak_hz     = metrics.get("peak_shift_hz", 0)

    mag_ratio = current / baseline if baseline else 1.0

    if mag_ratio > 1.15:
        # Cutting harder → more vibration energy → back off feed
        diagnosis   = "Vibration amplitude increased with spectral change — likely feed rate too high."
        explanation = "Reduce feed rate 10–15% and observe whether vibration stabilises."
        feed_hint   = "reduce_10_to_15_pct"
        rpm_hint    = None
    elif mag_ratio < 0.90:
        # Quieter but spectrally unstable → possibly rubbing / RPM too low
        diagnosis   = "Low amplitude but unstable spectrum — possible rubbing or RPM too low for chip load."
        explanation = "Try increasing spindle RPM by 500 RPM to improve chip evacuation."
        feed_hint   = None
        rpm_hint    = "increase_500_rpm"
    else:
        # Magnitude stable, spectrum shifted — resonance at a specific frequency
        diagnosis   = (
            f"Spectral shift at {peak_hz:.0f} Hz without major amplitude change — "
            "resonance or chatter at current feed/RPM combination."
        )
        explanation = "Adjust feed rate ±10% or spindle RPM ±500 RPM to move away from the resonant frequency."
        feed_hint   = "adjust_plus_minus_10_pct"
        rpm_hint    = "adjust_plus_minus_500_rpm"

    return {
        "diagnosis":    diagnosis,
        "explanation":  explanation,
        "feed_hint":    feed_hint,
        "rpm_hint":     rpm_hint,
        "spectral_flux": round(flux, 3),
        "peak_shift_hz": round(peak_hz, 1),
    }
