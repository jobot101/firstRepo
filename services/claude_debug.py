"""
Claude debug service — Phase 1.

Builds the error-explanation prompt, calls Claude Haiku via SSE streaming,
and yields Server-Sent Event chunks for the Flask route to forward to the
frontend top bar word by word.

Prompt caching is applied to the stable system prompt so the first-token
latency is fast even on a Pi.
"""

import json
import os
from typing import Generator

import anthropic

from utils.logger import log_claude_response, log_error_bundle

# Shared Anthropic client (reused across requests)
_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

_HAIKU_MODEL = "claude-haiku-4-5"

# The system prompt is identical for every debug call — perfect for caching.
_SYSTEM_PROMPT = """You are the built-in diagnostic assistant for a Shapeoko 5 Pro CNC machine
running grblHAL on an ESP32, controlled via a Raspberry Pi pendant app (Flask + SocketIO).

Your job is to explain machine errors and alarms in plain English to a hobbyist woodworker.
Keep explanations concise (2–4 sentences). Focus on:
1. What the error means in plain language
2. The most likely cause given the position and recent G-code
3. The single most important next action to take

Do not use jargon without explaining it. Do not suggest rebooting unless it is genuinely
the right answer. Be direct and calm — this person is mid-job and needs fast, clear guidance.

Machine context:
- X/Y zero: bottom-left corner of stock
- Z zero: spoilboard surface
- Units: mm (default)
"""


def stream_error_explanation(bundle: dict) -> Generator[str, None, None]:
    """
    Given an error bundle, stream an SSE explanation from Claude Haiku.

    Yields SSE-formatted strings: 'data: <text>\\n\\n'
    Yields 'data: [DONE]\\n\\n' when finished.

    bundle keys:
      error_code    str   e.g. "ALARM:1"
      message       str   human-readable error string from grblHAL
      position      dict  {x, y, z}  machine position at time of error
      last_gcode    list  last 10 G-code lines executed
      timestamp     str   ISO timestamp
    """
    log_error_bundle(bundle)

    user_content = _build_user_message(bundle)
    full_response = []

    try:
        with _client.messages.stream(
            model=_HAIKU_MODEL,
            max_tokens=512,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    # Cache the system prompt — it never changes between requests.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        ) as stream:
            for text in stream.text_stream:
                full_response.append(text)
                # Escape newlines so SSE doesn't break mid-sentence
                safe = text.replace("\n", " ")
                yield f"data: {safe}\n\n"

        log_claude_response(
            "debug_explanation",
            f"error={bundle.get('error_code')}",
            "".join(full_response),
        )

    except anthropic.APIError as exc:
        error_msg = f"Claude API error: {exc}"
        yield f"data: {error_msg}\n\n"

    yield "data: [DONE]\n\n"


def _build_user_message(bundle: dict) -> str:
    pos = bundle.get("position", {})
    pos_str = f"X={pos.get('x', '?')} Y={pos.get('y', '?')} Z={pos.get('z', '?')}"

    last_gcode = bundle.get("last_gcode", [])
    gcode_str = "\n".join(last_gcode[-10:]) if last_gcode else "(none)"

    return (
        f"Error code: {bundle.get('error_code', 'unknown')}\n"
        f"Message: {bundle.get('message', '')}\n"
        f"Machine position at error: {pos_str}\n"
        f"Timestamp: {bundle.get('timestamp', '')}\n\n"
        f"Last 10 G-code lines executed:\n{gcode_str}\n\n"
        "Please explain this error in plain English and tell me what to do."
    )
