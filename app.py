"""
Smart CNC Shop Assistant — Flask + SocketIO app.

The Pi is the Claude brain. The ESP32 / grblHAL is the machine brain.

What's active:
  Approval gate  — start_job / home / tool_change require a tap
  Crash detection — vibration spike → immediate spindle off, no gate
  Chatter alerts  — spectral flux anomaly → rule-based feed/RPM hint
  Surfacing G-code — stream planing passes from stock dimensions
  Bit tracker     — cut-time logging with wear warnings
"""

import json
import os
import threading

from flask import Flask
from flask_socketio import SocketIO, emit

from routes.jobs import jobs_bp
from services import bit_tracker
from services.audio_monitor import VibrationMonitor, diagnose_chatter
from utils.logger import log


def create_app(settings: dict | None = None) -> tuple[Flask, SocketIO]:
    app = Flask(__name__, static_folder="frontend", static_url_path="/static")

    if settings is None:
        settings = _load_settings()
    app.config["SETTINGS"] = settings

    app.register_blueprint(jobs_bp)

    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
    app.extensions["socketio"] = socketio

    _register_events(socketio, app)
    _start_vibration_monitor(app, socketio, settings)

    return app, socketio


def _load_settings() -> dict:
    path = os.path.join(os.path.dirname(__file__), "config", "settings.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[WARN] settings.json: {exc} — using defaults")
        return {}


def _register_events(socketio: SocketIO, app: Flask):

    @socketio.on("connect")
    def on_connect():
        log("socketio", {"event": "connected"})
        emit("status", {"connected": True})

    @socketio.on("disconnect")
    def on_disconnect():
        log("socketio", {"event": "disconnected"})

    # ── Phase 3: crash → immediate spindle off, no approval gate ──────────
    @socketio.on("crash_detected")
    def on_crash_detected(data):
        log("crash_detected", data)
        socketio.emit("machine_command", {"action": "spindle_off_emergency", "parameters": {}}, broadcast=True)
        socketio.emit("machine_command", {"action": "feed_hold", "parameters": {}}, broadcast=True)
        socketio.emit("crash_notification", {
            "message": "Crash detected — spindle stopped immediately.",
            "details": data,
        }, broadcast=True)

    # ── Job lifecycle → bit tracker + vibration monitor ───────────────────
    @socketio.on("job_started")
    def on_job_started(data):
        bit_tracker.start_job(
            data.get("bit_name", "unknown"),
            data.get("material", "default"),
            data.get("feed_rate_mmpm", 0),
        )
        monitor = app.config.get("VIBRATION_MONITOR")
        if monitor:
            monitor.start()

    @socketio.on("job_ended")
    def on_job_ended(data):
        monitor = app.config.get("VIBRATION_MONITOR")
        if monitor:
            monitor.stop()
        result = bit_tracker.end_job()
        if result:
            socketio.emit("bit_update", result, broadcast=True)
            if result.get("warning"):
                socketio.emit("bit_warning", {"message": result["warning"]}, broadcast=True)


def _start_vibration_monitor(app: Flask, socketio: SocketIO, settings: dict):
    def on_crash():
        socketio.emit("crash_detected", {"source": "vibration_monitor"})

    def on_chatter(description, metrics):
        result = diagnose_chatter(description, metrics)
        socketio.emit("chatter_detected", {"description": description, "diagnosis": result})

    app.config["VIBRATION_MONITOR"] = VibrationMonitor(
        i2c_address=settings.get("vibration", {}).get("i2c_address", 0x68),
        on_crash=on_crash,
        on_chatter=on_chatter,
    )


if __name__ == "__main__":
    app, socketio = create_app()
    host = os.environ.get("CNC_HOST", "0.0.0.0")
    port = int(os.environ.get("CNC_PORT", "5000"))
    print(f"[CNC Assistant] Starting on {host}:{port}")
    socketio.run(app, host=host, port=port, debug=False, use_reloader=False)
