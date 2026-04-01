"""
Job control routes — Phase 2.

All actions that affect machine motion are gated through the human
approval gate. The route validates the proposed action, asks Claude
to explain it in plain English, then returns a pending_action object
that the frontend displays in the approvalGate UI.

The frontend sends a follow-up to /api/jobs/approve or /api/jobs/reject.
Nothing moves until /approve is called.

Exception: crash detection (handled in the SocketIO layer in app.py)
bypasses this gate entirely — spindle shutoff fires immediately.

Routes:
  POST /api/jobs/propose     — propose an action (returns pending_action)
  POST /api/jobs/approve     — approve pending_action (executes it)
  POST /api/jobs/reject      — reject pending_action (discards it)
  GET  /api/jobs/pending     — get current pending action (if any)
  POST /api/jobs/surfacing   — stream surfacing G-code generation
  POST /api/jobs/gcode       — stream arbitrary G-code generation
  GET  /api/jobs/bits        — list bit profiles
  POST /api/jobs/bits/reset  — reset a bit's wear counter
"""

import json
import uuid

from flask import Blueprint, Response, jsonify, request, current_app

from services import bit_tracker
from services.claude_gcode import stream_gcode_generation, stream_surfacing_gcode_proxy
from services.surfacing_generator import stream_surfacing_gcode
from utils.logger import log, log_job_event

jobs_bp = Blueprint("jobs", __name__)

# In-memory pending action store (one at a time)
# { action_id: { ...action_dict } }
_pending: dict = {}

REQUIRES_APPROVAL = {
    "start_job", "pause", "resume", "send_gcode",
    "feed_override", "spindle_speed", "home", "bitsetter_probe", "tool_change",
}


# ---------------------------------------------------------------------------
# Propose an action — returns pending_action for the approval gate
# ---------------------------------------------------------------------------

@jobs_bp.route("/api/jobs/propose", methods=["POST"])
def propose():
    data = request.get_json(silent=True) or {}
    action = data.get("action")

    if not action:
        return jsonify({"error": "action required"}), 400

    if action not in REQUIRES_APPROVAL:
        return jsonify({"error": f"unknown action: {action}"}), 400

    action_id = str(uuid.uuid4())
    pending = {
        "action_id": action_id,
        "action": action,
        "parameters": data.get("parameters", {}),
        "source": data.get("source", "ui"),  # "ui" | "voice" | "auto"
        "explanation": _explain_action(action, data.get("parameters", {})),
        "status": "pending",
    }

    _pending[action_id] = pending
    log_job_event("proposed", {"action_id": action_id, "action": action})
    return jsonify(pending)


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------

@jobs_bp.route("/api/jobs/approve", methods=["POST"])
def approve():
    data = request.get_json(silent=True) or {}
    action_id = data.get("action_id")

    if not action_id or action_id not in _pending:
        return jsonify({"error": "no matching pending action"}), 404

    action = _pending.pop(action_id)
    action["status"] = "approved"

    socketio = current_app.extensions.get("socketio")
    if socketio:
        socketio.emit("action_approved", action)

    log_job_event("approved", {"action_id": action_id, "action": action["action"]})
    _execute_action(action, current_app._get_current_object())
    return jsonify({"status": "approved", "action_id": action_id})


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------

@jobs_bp.route("/api/jobs/reject", methods=["POST"])
def reject():
    data = request.get_json(silent=True) or {}
    action_id = data.get("action_id")

    if not action_id or action_id not in _pending:
        return jsonify({"error": "no matching pending action"}), 404

    action = _pending.pop(action_id)
    action["status"] = "rejected"

    log_job_event("rejected", {"action_id": action_id, "action": action["action"]})
    return jsonify({"status": "rejected", "action_id": action_id})


# ---------------------------------------------------------------------------
# Get current pending action
# ---------------------------------------------------------------------------

@jobs_bp.route("/api/jobs/pending", methods=["GET"])
def get_pending():
    if not _pending:
        return jsonify({"pending": None})
    # Return the most recent pending action
    latest = list(_pending.values())[-1]
    return jsonify({"pending": latest})


# ---------------------------------------------------------------------------
# Surfacing G-code generation (SSE stream)
# ---------------------------------------------------------------------------

@jobs_bp.route("/api/jobs/surfacing", methods=["POST"])
def surfacing():
    params = request.get_json(silent=True) or {}
    required = ["stock_width_mm", "stock_length_mm", "current_thickness_mm",
                "target_thickness_mm", "bit_diameter_mm", "feed_rate_mmpm"]

    missing = [k for k in required if k not in params]
    if missing:
        return jsonify({"error": f"missing parameters: {missing}"}), 400

    return Response(
        stream_surfacing_gcode(params),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# General G-code generation (SSE stream)
# ---------------------------------------------------------------------------

@jobs_bp.route("/api/jobs/gcode", methods=["POST"])
def generate_gcode():
    data = request.get_json(silent=True) or {}
    task = data.get("task_description", "")
    parameters = data.get("parameters", {})

    if not task:
        return jsonify({"error": "task_description required"}), 400

    return Response(
        stream_gcode_generation(task, parameters),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Bit tracker routes
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _explain_action(action: str, parameters: dict) -> str:
    """Return a plain-English explanation of what an action will do."""
    explanations = {
        "start_job": "Start running the loaded G-code file. The machine will begin moving.",
        "pause": "Pause the current job. The spindle will continue running. Resume when ready.",
        "resume": "Resume the paused job from where it left off.",
        "send_gcode": f"Send this G-code to the machine: {parameters.get('line', '')}",
        "feed_override": f"Change feed rate to {parameters.get('percent', 100)}% of programmed speed.",
        "spindle_speed": f"Change spindle speed to {parameters.get('rpm', '?')} RPM.",
        "home": "Run the homing sequence. The machine will move to all limit switches.",
        "bitsetter_probe": "Run the BitSetter probing sequence to measure tool length offset.",
        "tool_change": f"Perform a tool change. New tool: {parameters.get('tool_number', '?')}.",
    }
    return explanations.get(action, f"Perform action: {action}")


def _execute_action(action: dict, app):
    """
    Dispatch an approved action to the machine via SocketIO.
    The actual grblHAL command is emitted as a 'machine_command' event
    that the serial bridge (outside this app) picks up and forwards.
    """
    socketio = app.extensions.get("socketio")
    if socketio is None:
        log("execute_error", {"error": "socketio not available"})
        return

    socketio.emit("machine_command", {
        "action": action["action"],
        "parameters": action["parameters"],
    })
    log_job_event("executed", {"action": action["action"], "parameters": action["parameters"]})
