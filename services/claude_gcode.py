"""
Claude G-code generation service.

Uses Claude Sonnet for complex reasoning tasks:
- Surfacing toolpath generation from plain-English dimensions
- Full job planning from a natural-language description
- Real-time job narration (teaching mode)
- Voice command interpretation

All generated G-code is presented to the human approval gate before
any commands are sent to the machine.
"""

import json
import os
from typing import Generator

import anthropic

from utils.logger import log_claude_response

_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

_SONNET_MODEL = "claude-sonnet-4-6"

_GCODE_SYSTEM = """You are the G-code generation engine for a Shapeoko 5 Pro CNC machine
running grblHAL (G-code dialect close to standard RS-274).

Machine specs:
- Controller: grblHAL on ESP32
- Work area: 838mm x 838mm (Shapeoko 5 Pro)
- X/Y zero: bottom-left corner of stock
- Z zero: spoilboard surface
- Spindle: trim router (variable speed)
- BitSetter: present (use G38.2 probing sequence when tool changes occur)

G-code rules you must follow:
1. Always start with spindle on (M3 Sxxx) before any cutting moves
2. Always end with spindle off (M5), then safe Z retract, then G0 to X0Y0
3. Use G21 (mm) unless explicitly asked for inches
4. Ramp into cuts — never plunge straight down into material at full feed
5. Leave tabs on profile cuts unless explicitly told not to
6. Use G28 for homing references, not hard-coded coordinates

When generating G-code:
- Show the complete program, no placeholders
- After the G-code block, add a plain-English explanation of every section
- Flag any assumptions you made (material thickness, bit diameter, etc.)
- Warn if any parameter seems aggressive for the material
"""


def stream_gcode_generation(task_description: str, parameters: dict) -> Generator[str, None, None]:
    """
    Generate G-code from a plain-English task description.

    Yields SSE chunks. The final chunk is 'data: [DONE]\\n\\n'.

    parameters may include:
      material, thickness_mm, target_thickness_mm, bit_diameter_mm,
      feed_rate_mmpm, spindle_rpm, stepover_percent, stock_width_mm,
      stock_length_mm, depth_of_cut_mm
    """
    param_str = json.dumps(parameters, indent=2) if parameters else "(none provided)"
    user_msg = (
        f"Task: {task_description}\n\n"
        f"Known parameters:\n{param_str}\n\n"
        "Generate the complete G-code program and explain every section in plain English."
    )

    full_response = []

    try:
        with _client.messages.stream(
            model=_SONNET_MODEL,
            max_tokens=4096,
            system=[
                {
                    "type": "text",
                    "text": _GCODE_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_msg}],
        ) as stream:
            for text in stream.text_stream:
                full_response.append(text)
                safe = text.replace("\n", "\\n")
                yield f"data: {safe}\n\n"

        log_claude_response("gcode_generation", task_description[:100], "".join(full_response))

    except anthropic.APIError as exc:
        yield f"data: Error generating G-code: {exc}\n\n"

    yield "data: [DONE]\n\n"


def stream_job_narration(gcode_line: str, operation_type: str) -> Generator[str, None, None]:
    """
    Narrate what a single G-code line is doing, for teaching mode.
    Uses Haiku for speed and low cost during active cutting.
    """
    from services.claude_debug import _HAIKU_MODEL  # reuse haiku model constant

    msg = (
        f"G-code line: {gcode_line}\n"
        f"Operation type: {operation_type}\n"
        "Explain what this line does in one plain-English sentence. "
        "Be specific — mention what the machine is physically doing."
    )

    try:
        with _client.messages.stream(
            model=_HAIKU_MODEL,
            max_tokens=128,
            system="You are a CNC teaching assistant. Explain G-code in plain English for a hobbyist.",
            messages=[{"role": "user", "content": msg}],
        ) as stream:
            for text in stream.text_stream:
                safe = text.replace("\n", " ")
                yield f"data: {safe}\n\n"
    except anthropic.APIError:
        pass

    yield "data: [DONE]\n\n"


def generate_daily_insight(job_log: list) -> str:
    """
    Generate one focused shop insight from the day's job log.
    Returns a single sentence string (blocking call, called once per day).
    """
    from services.claude_debug import _HAIKU_MODEL

    if not job_log:
        return ""

    log_summary = json.dumps(job_log, indent=2)

    response = _client.messages.create(
        model=_HAIKU_MODEL,
        max_tokens=128,
        system=(
            "You are a CNC shop advisor. Review today's job log and produce ONE specific, "
            "actionable insight. Write exactly one sentence. Be concrete — mention actual "
            "numbers, bit names, or operations from the log. No preamble."
        ),
        messages=[
            {
                "role": "user",
                "content": f"Today's job log:\n{log_summary}\n\nGive me one shop insight.",
            }
        ],
    )

    for block in response.content:
        if block.type == "text":
            return block.text.strip()
    return ""


def interpret_voice_command(transcript: str, machine_state: dict) -> dict:
    """
    Interpret a voice command (English or Spanish) and return a structured
    action proposal for the human approval gate.

    Returns:
      {
        "language": "en" | "es",
        "intent": str,
        "action": str,
        "parameters": dict,
        "explanation": str,
        "requires_approval": bool
      }
    """
    state_str = json.dumps(machine_state, indent=2)

    response = _client.messages.create(
        model=_SONNET_MODEL,
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": (
                    _GCODE_SYSTEM
                    + "\n\nYou also interpret voice commands in English and Spanish. "
                    "Detect the language automatically. Return a JSON object with keys: "
                    "language, intent, action, parameters, explanation, requires_approval. "
                    "Return ONLY valid JSON, no surrounding text."
                ),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": (
                    f"Voice transcript: \"{transcript}\"\n\n"
                    f"Current machine state:\n{state_str}\n\n"
                    "Interpret this command and return the JSON action proposal."
                ),
            }
        ],
    )

    for block in response.content:
        if block.type == "text":
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                return {
                    "language": "unknown",
                    "intent": "parse_error",
                    "action": "none",
                    "parameters": {},
                    "explanation": block.text,
                    "requires_approval": True,
                }
    return {}
