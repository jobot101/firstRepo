"""
Smart CNC Shop Assistant — main Flask + SocketIO application.

Architecture:
  - Pi is the Claude brain
  - ESP32 / grblHAL is the machine brain
  - They communicate via this app (USB serial bridge handled externally)

All input (voice, text, automated triggers) flows through Claude before
anything touches the machine.  The only exception is crash detection:
spindle shutoff fires immediately with no approval gate.

Phase coverage in this file:
  Phase 1   — Debug endpoint + toggle (routes/debug.py)
  Phase 1.5 — Watch folder + G-code review (services/watch_folder.py)
  Phase 2   — Human approval gate (routes/jobs.py)
  Phase 3   — Crash detection + spindle shutoff (SocketIO event handler)
  Phase 4   — Voice, camera, audio monitor (services/*)
"""

import json
import os
import threading

from flask import Flask, render_template_string
from flask_socketio import SocketIO, emit

from routes.debug import debug_bp
from routes.jobs import jobs_bp
from services import audio_monitor, bit_tracker, camera, watch_folder
from utils.logger import log

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(settings: dict | None = None) -> tuple[Flask, SocketIO]:
    app = Flask(__name__, static_folder="frontend", static_url_path="/static")

    # Load settings from disk, optionally override for testing
    if settings is None:
        settings = _load_settings()
    app.config["SETTINGS"] = settings

    # Register blueprints
    app.register_blueprint(debug_bp)
    app.register_blueprint(jobs_bp)

    # Flask-SocketIO
    socketio = SocketIO(
        app,
        cors_allowed_origins="*",
        async_mode="threading",
    )
    app.extensions["socketio"] = socketio

    _register_socketio_events(socketio, app)
    _start_background_services(app, socketio, settings)

    return app, socketio


# ---------------------------------------------------------------------------
# Settings loader
# ---------------------------------------------------------------------------

def _load_settings() -> dict:
    path = os.path.join(os.path.dirname(__file__), "config", "settings.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[WARN] Could not load settings.json: {exc} — using defaults")
        return {}


# ---------------------------------------------------------------------------
# SocketIO event handlers
# ---------------------------------------------------------------------------

def _register_socketio_events(socketio: SocketIO, app: Flask):

    @socketio.on("connect")
    def on_connect():
        log("socketio", {"event": "client_connected"})
        emit("status", {"connected": True, "debug_enabled": app.config["SETTINGS"].get("debug", {}).get("enabled", True)})

    @socketio.on("disconnect")
    def on_disconnect():
        log("socketio", {"event": "client_disconnected"})

    # -----------------------------------------------------------------------
    # grblHAL alarm forwarding — machine emits alarms, we forward to Claude
    # -----------------------------------------------------------------------

    @socketio.on("grbl_alarm")
    def on_grbl_alarm(data):
        """
        Fired by the serial bridge when grblHAL sends ALARM:n.
        data: { error_code, message, position, last_gcode, timestamp }
        """
        settings = app.config["SETTINGS"]
        error_code = data.get("error_code", "")

        # --- Phase 3: hard limit alarms trigger immediate feed hold ---
        if error_code in ("ALARM:1", "ALARM:2"):
            emit("machine_command", {"action": "feed_hold", "parameters": {}}, broadcast=True)
            log("hard_limit_alarm", {"alarm": error_code})

        # --- Forward to debug service (will be deduped + gated inside the route) ---
        if settings.get("debug", {}).get("enabled", True):
            socketio.emit("debug_event", data, broadcast=True)

    # -----------------------------------------------------------------------
    # Phase 3: crash detection → immediate spindle shutoff
    # No approval gate. This is the only hardcoded bypass.
    # -----------------------------------------------------------------------

    @socketio.on("crash_detected")
    def on_crash_detected(data):
        """
        Fired by the audio monitor service when a crash signature is detected.
        Immediately shuts down the spindle — no human gate.
        """
        log("crash_detected", data)

        # Immediate spindle off
        socketio.emit("machine_command", {"action": "spindle_off_emergency", "parameters": {}}, broadcast=True)
        socketio.emit("machine_command", {"action": "feed_hold", "parameters": {}}, broadcast=True)

        # Tell the UI what happened (after the fact)
        socketio.emit("crash_notification", {
            "message": "Crash detected — spindle stopped immediately.",
            "details": data,
        }, broadcast=True)

        log("crash_response", {"action": "spindle_off_emergency"})

    # -----------------------------------------------------------------------
    # Watch folder: new file detected
    # -----------------------------------------------------------------------

    @socketio.on("watch_folder_file")
    def on_watch_folder_file(data):
        """Forwarded from the background watch_folder service."""
        socketio.emit("new_gcode_file", data, broadcast=True)

    # -----------------------------------------------------------------------
    # Camera trigger
    # -----------------------------------------------------------------------

    @socketio.on("camera_trigger")
    def on_camera_trigger(data):
        """
        Fired when a trigger point is reached (homing_complete, job_start, etc.)
        data: { trigger: str }
        """
        trigger = data.get("trigger", "job_start")
        cam_settings = app.config["SETTINGS"].get("camera", {})
        device = cam_settings.get("device_index", 0)

        def run():
            result = camera.capture_and_analyse(trigger, device_index=device)
            if result:
                socketio.emit("camera_result", result, broadcast=True)
                if result.get("verdict") in ("no_go", "caution"):
                    socketio.emit("safety_alert", result, broadcast=True)

        threading.Thread(target=run, daemon=True).start()

    # -----------------------------------------------------------------------
    # Job lifecycle events for bit tracker
    # -----------------------------------------------------------------------

    @socketio.on("job_started")
    def on_job_started(data):
        bit_name = data.get("bit_name", "unknown")
        material = data.get("material", "default")
        feed_rate = data.get("feed_rate_mmpm", 0)
        bit_tracker.start_job(bit_name, material, feed_rate)

    @socketio.on("job_ended")
    def on_job_ended(data):
        result = bit_tracker.end_job()
        if result:
            socketio.emit("bit_update", result, broadcast=True)
            if result.get("warning"):
                socketio.emit("bit_warning", {"message": result["warning"]}, broadcast=True)


# ---------------------------------------------------------------------------
# Background services
# ---------------------------------------------------------------------------

def _start_background_services(app: Flask, socketio: SocketIO, settings: dict):
    # Watch folder
    wf_settings = settings.get("watch_folder", {})
    watch_path = wf_settings.get("path", os.path.expanduser("~/vcarve_output"))
    extensions = wf_settings.get("extensions", [".nc", ".gcode"])

    def on_new_file(path, issues):
        """Called from watchdog thread when a new G-code file appears."""
        import os as _os
        data = {
            "filepath": path,
            "filename": _os.path.basename(path),
            "issue_count": len(issues),
            "issues": issues,
        }
        socketio.emit("new_gcode_file", data)
        log("watch_folder_file", data)

    watch_folder.start(watch_path, extensions, on_new_file)

    # Audio monitor crash callback
    audio_settings = settings.get("audio", {})

    def on_crash():
        socketio.emit("crash_detected", {"source": "audio_monitor"})

    def on_chatter(description):
        from services.audio_monitor import diagnose_chatter_with_claude
        result = diagnose_chatter_with_claude(description, {})
        socketio.emit("chatter_detected", {"description": description, "diagnosis": result})

    # Audio monitor is started/stopped per-job via SocketIO events above.
    # Store a reference so routes can start/stop it.
    app.config["AUDIO_MONITOR"] = audio_monitor.AudioMonitor(
        sample_rate=audio_settings.get("sample_rate", 44100),
        channels=audio_settings.get("channels", 1),
        chunk_size=audio_settings.get("chunk_size", 1024),
        on_crash=on_crash,
        on_chatter=on_chatter,
    )


# ---------------------------------------------------------------------------
# Simple health check page
# ---------------------------------------------------------------------------

_INDEX_HTML = """<!DOCTYPE html>
<html>
<head><title>CNC Assistant</title></head>
<body>
  <h2>Smart CNC Shop Assistant</h2>
  <p>Backend running. Open the pendant UI to get started.</p>
  <ul>
    <li><a href="/api/debug/status">Debug status</a></li>
    <li><a href="/api/jobs/bits">Bit profiles</a></li>
    <li><a href="/api/jobs/pending">Pending actions</a></li>
  </ul>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app, socketio = create_app()

    host = os.environ.get("CNC_HOST", "0.0.0.0")
    port = int(os.environ.get("CNC_PORT", "5000"))

    print(f"[CNC Assistant] Starting on {host}:{port}")
    socketio.run(app, host=host, port=port, debug=False, use_reloader=False)
