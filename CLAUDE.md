# Smart CNC Shop Assistant — Session Handoff

## What this is
Flask + SocketIO backend running on a Raspberry Pi alongside a Shapeoko 5 Pro.
The Pi is the Claude brain. The ESP32 running grblHAL is the machine brain.

Branch: `claude/smart-cnc-assistant-h3XeM`

---

## What is built and working

### Vibration monitor (`services/audio_monitor.py`)
MPU-6050 accelerometer on the router body via I2C. Runs entirely without Claude API.
- **Crash detection** — magnitude spike >6× baseline → immediate spindle off, no approval gate
- **Chatter detection** — FFT spectral flux sustained 3 windows → rule-based feed/RPM hint
- **Resonance fingerprinting** — per-bit/material spectrum profile built with EMA across jobs; stored in `config/fingerprints.json`; becomes the flux reference once 30+ stable windows are collected
- **Z-drift detection** — magnitude trending up on a spectrally stable cut → bit pullout / Z step loss; fires at ~2 seconds (4 windows × ~0.5s)
- **Feed autotune** — sustained elevated flux → ▼5% feed suggestion; sustained clean flux → ▲5% suggestion; 40-window cooldown

### Approval gate (`routes/jobs.py`)
Only 3 actions require operator tap: `start_job`, `home`, `tool_change`.
Everything else (`pause`, `resume`, `feed_override`, `spindle_speed`, etc.) executes directly.

### Surfacing G-code (`services/claude_gcode.py`)
Claude Sonnet generates planing passes. Inputs: stock dimensions + target thickness.
XY0 is always the workspace origin. Pre-calculates pass count and stepover before calling Claude.

### Bit tracker (`services/bit_tracker.py`)
Cut-time logging with material hardness weighting. Wear warnings at 80% and 100% of `warn_hours`.

### Top bar (`frontend/ui/topBar.js`)
Shows crash, chatter, Z-drift, and feed suggestions. Clears after 5–10s except crash/no_go which persist.

---

## What is NOT built yet

### 1. VCarve tool optimization loop — PENDING USER INPUT
The owner uses VCarve Pro with a **custom post-processor** stored on the machine.
The post-processor file path needs to be shared before this feature can be built.

**Ask the user:** "What is the path to your custom VCarve post-processor file on the machine?"

The feature works like this:
- `job_started` sends `gcode_path` with the job
- Pi reads the G-code header (VCarve embeds tool name, feed, plunge, RPM, pass depth in comments)
- Job runs, vibration monitor collects flux/magnitude data
- `job_ended` triggers background Claude Sonnet analysis
- Claude returns specific VCarve tool database values (feed rate, plunge rate, RPM, pass depth) ready to type in
- Gets more accurate across multiple runs of the same bit/material

### 2. Hardware not yet wired
- MPU-6050 (GY-521 breakout): VCC→Pin1, GND→Pin6, SDA→Pin3, SCL→Pin5. Enable I2C via `raspi-config`.
- grblHAL serial: `/dev/ttyUSB0` at 115200 baud (set in `config/settings.json`)

---

## Architecture decisions to preserve

- **No Claude in the sensor loop** — all vibration interpretation is algorithmic
- **Invisible during normal runs** — Claude only speaks when something needs attention
- **Feed adjustments are 500 RPM steps** — never 1000, never a range
- **No alarm explanations** — alarms are self-explanatory on the pendant
- **No camera** — operator is physically present; vibration monitor handles cut quality
- **Approval gate is narrow** — only 3 actions, not 8

---

## Key files

| File | Purpose |
|------|---------|
| `app.py` | Flask app, SocketIO events, vibration monitor wiring |
| `services/audio_monitor.py` | MPU-6050 + FFT + all 5 vibration features |
| `services/claude_gcode.py` | Surfacing G-code via Claude Sonnet |
| `services/bit_tracker.py` | Bit wear tracking |
| `routes/jobs.py` | Approval gate, surfacing endpoint |
| `config/settings.json` | Thresholds, serial port, I2C address |
| `config/fingerprints.json` | Resonance fingerprint store (auto-populated at runtime) |
| `frontend/ui/topBar.js` | Pendant top bar — alerts and suggestions |
| `frontend/ui/approvalGate.js` | Three-button approval modal |

---

## Models in use
- `claude-sonnet-4-6` — surfacing G-code generation (and future VCarve optimization)
- No Haiku usage currently (alarm explanation was removed as unnecessary)
