"""
Job routes.

POST /api/jobs/surfacing   — stream surfacing G-code via SSE
POST /api/jobs/propose     — propose a machine action (approval gate)
POST /api/jobs/approve     — approve a pending action
POST /api/jobs/reject      — reject a pending action
GET  /api/jobs/pending     — get the current pending action
GET  /api/jobs/bits        — list bit profiles
POST /api/jobs/bits/reset  — reset a bit's wear counter
"""

import uuid

from flask import Blueprint, Response, jsonify, request, current_app

from services import bit_tracker
from services.claude_gcode import stream_surfacing
from utils.logger import log, log_job_event

jobs_bp = Blueprint("jobs", __name__)

_pending: dict = {}

REQUIRES_APPROVAL = {
    "start_job", "pause", "resume", "send_gcode",
    "feed_override", "spindle_speed", "home", "bitsetter_probe", "tool_change",
}


# ── Surfacing ──────────────────────────────────────────────────────────────

@jobs_bp.route("/api/jobs/surfacing", methods=["POST"])
def surfacing():
    params = request.get_json(silent=True) or {}
    return Response(
        stream_surfacing(params),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Approval gate ──────────────────────────────────────────────────────────

@jobs_bp.route("/api/jobs/propose", methods=["POST"])
def propose():
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if not action or action not in REQUIRES_APPROVAL:
        return jsonify({"error": f"unknown action: {action}"}), 400

    action_id = str(uuid.uuid4())
    pending = {
        "action_id": action_id,
        "action": action,
        "parameters": data.get("parameters", {}),
        "explanation": _explain(action, data.get("parameters", {})),
        "status": "pending",
    }
    _pending[action_id] = pending
    log_job_event("proposed", {"action_id": action_id, "action": action})
    return jsonify(pending)


@jobs_bp.route("/api/jobs/approve", methods=["POST"])
def approve():
    data = request.get_json(silent=True) or {}
    action_id = data.get("action_id")
    if not action_id or action_id not in _pending:
        return jsonify({"error": "no matching pending action"}), 404

    action = _pending.pop(action_id)
    socketio = current_app.extensions.get("socketio")
    if socketio:
        socketio.emit("machine_command", {"action": action["action"], "parameters": action["parameters"]})
    log_job_event("approved", {"action_id": action_id, "action": action["action"]})
    return jsonify({"status": "approved", "action_id": action_id})


@jobs_bp.route("/api/jobs/reject", methods=["POST"])
def reject():
    data = request.get_json(silent=True) or {}
    action_id = data.get("action_id")
    if not action_id or action_id not in _pending:
        return jsonify({"error": "no matching pending action"}), 404
    _pending.pop(action_id)
    log_job_event("rejected", {"action_id": action_id})
    return jsonify({"status": "rejected"})


@jobs_bp.route("/api/jobs/pending", methods=["GET"])
def get_pending():
    latest = list(_pending.values())[-1] if _pending else None
    return jsonify({"pending": latest})


# ── Bit tracker ────────────────────────────────────────────────────────────

@jobs_bp.route("/api/jobs/bits", methods=["GET"])
def list_bits():
    return jsonify(bit_tracker.get_all_profiles())


@jobs_bp.route("/api/jobs/bits/reset", methods=["POST"])
def reset_bit():
    data = request.get_json(silent=True) or {}
    bit_name = data.get("bit_name")
    if not bit_name:
        return jsonify({"error": "bit_name required"}), 400
    bit_tracker.reset_bit(bit_name)
    return jsonify({"status": "reset", "bit_name": bit_name})


# ── Helpers ────────────────────────────────────────────────────────────────

def _explain(action: str, params: dict) -> str:
    explanations = {
        "start_job":      "Start running the loaded G-code file.",
        "pause":          "Pause the current job. Spindle keeps running.",
        "resume":         "Resume the paused job.",
        "send_gcode":     f"Send to machine: {params.get('line', '')}",
        "feed_override":  f"Set feed rate to {params.get('percent', 100)}% of programmed speed.",
        "spindle_speed":  f"Set spindle to {params.get('rpm', '?')} RPM.",
        "home":           "Run the homing sequence.",
        "bitsetter_probe":"Run BitSetter probing to measure tool length offset.",
        "tool_change":    f"Tool change — tool {params.get('tool_number', '?')}.",
    }
    return explanations.get(action, action)
