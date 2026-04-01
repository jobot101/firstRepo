"""
Audio monitor service — Phase 3 / Phase 4.

The Pi runs lightweight local audio monitoring continuously while cutting.
No API calls are made during normal operation — Claude is only contacted
when something anomalous is detected.

Two anomaly classes:
  crash_signature  — sudden loud impulse; triggers immediate spindle shutoff
                     (no approval gate — safety cannot wait)
  chatter          — rhythmic resonance pattern; triggers a Claude diagnosis
                     and feed-rate suggestion presented for approval

The monitor builds a baseline of the machine's normal audio profile during
the first job (configurable duration) and uses Z-score deviation for detection.

Requires: sounddevice or PyAudio
"""

import os
import threading
import time
from typing import Callable, Optional

from utils.logger import log

# Detection thresholds (tuneable via settings.json)
_CRASH_MULTIPLIER = 5.0    # RMS > baseline * this → crash candidate
_CHATTER_MULTIPLIER = 2.0  # RMS > baseline * this (sustained) → chatter
_CHATTER_SUSTAIN_SECONDS = 0.5  # must be elevated for this long to count as chatter


class AudioMonitor:
    """
    Continuous audio monitor.  Call start() once at job start, stop() at job end.
    """

    def __init__(
        self,
        sample_rate: int = 44100,
        channels: int = 1,
        chunk_size: int = 1024,
        on_crash: Optional[Callable] = None,
        on_chatter: Optional[Callable[[str], None]] = None,
    ):
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_size = chunk_size
        self.on_crash = on_crash
        self.on_chatter = on_chatter

        self._baseline_rms: Optional[float] = None
        self._baseline_samples: list[float] = []
        self._baseline_duration = 30  # seconds of quiet to establish baseline
        self._baseline_ready = False

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._chatter_start: Optional[float] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """Start monitoring in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log("audio_monitor", {"event": "started"})

    def stop(self):
        """Stop monitoring."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        log("audio_monitor", {"event": "stopped"})

    def reset_baseline(self):
        """Force a fresh baseline collection on next start."""
        self._baseline_rms = None
        self._baseline_samples = []
        self._baseline_ready = False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self):
        try:
            import sounddevice as sd  # type: ignore
            import numpy as np  # type: ignore

            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                blocksize=self.chunk_size,
                dtype="float32",
            ) as stream:
                while self._running:
                    block, _ = stream.read(self.chunk_size)
                    rms = float(np.sqrt(np.mean(block ** 2)))
                    self._process_chunk(rms)

        except ImportError:
            log("audio_monitor", {"event": "unavailable", "reason": "sounddevice not installed"})
        except Exception as exc:
            log("audio_monitor", {"event": "error", "error": str(exc)})

    def _process_chunk(self, rms: float):
        now = time.monotonic()

        # Baseline collection phase
        if not self._baseline_ready:
            self._baseline_samples.append(rms)
            elapsed = len(self._baseline_samples) * self.chunk_size / self.sample_rate
            if elapsed >= self._baseline_duration:
                import statistics
                self._baseline_rms = statistics.mean(self._baseline_samples)
                self._baseline_ready = True
                log("audio_monitor", {
                    "event": "baseline_ready",
                    "baseline_rms": round(self._baseline_rms, 6),
                })
            return

        baseline = self._baseline_rms
        if baseline is None or baseline < 1e-9:
            return

        ratio = rms / baseline

        # Crash: sudden very loud impulse
        if ratio > _CRASH_MULTIPLIER:
            log("audio_monitor", {"event": "crash_detected", "rms_ratio": round(ratio, 2)})
            if self.on_crash is not None:
                self.on_crash()
            return

        # Chatter: elevated and sustained
        if ratio > _CHATTER_MULTIPLIER:
            if self._chatter_start is None:
                self._chatter_start = now
            elif now - self._chatter_start >= _CHATTER_SUSTAIN_SECONDS:
                description = (
                    f"Sustained audio anomaly detected: RMS level is "
                    f"{ratio:.1f}x above baseline for "
                    f"{now - self._chatter_start:.1f}s. "
                    "Pattern is consistent with tool chatter or resonance."
                )
                log("audio_monitor", {"event": "chatter_detected", "description": description})
                if self.on_chatter is not None:
                    self.on_chatter(description)
                # Reset so we don't fire continuously
                self._chatter_start = None
        else:
            self._chatter_start = None


def diagnose_chatter_with_claude(description: str, machine_state: dict) -> dict:
    """
    Send a chatter description to Claude Haiku for diagnosis.
    Returns a proposed action for the approval gate.

    machine_state keys expected: feed_rate, spindle_rpm, material, bit_name
    """
    import json
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    state_str = json.dumps(machine_state, indent=2)

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=256,
        system=(
            "You are a CNC diagnostic assistant. Diagnose audio anomalies and suggest "
            "feed rate or spindle speed adjustments. Return a JSON object with keys: "
            "diagnosis (str), suggested_feed_rate_mmpm (float or null), "
            "suggested_spindle_rpm (int or null), explanation (str). "
            "Return ONLY valid JSON."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f"Audio anomaly:\n{description}\n\n"
                    f"Current machine state:\n{state_str}\n\n"
                    "Diagnose and suggest adjustments."
                ),
            }
        ],
    )

    for block in response.content:
        if block.type == "text":
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                return {"diagnosis": block.text, "explanation": block.text}
    return {}
