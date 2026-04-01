"""
Claude G-code service — surfacing / planing only.

Takes stock dimensions and thickness, generates a complete surfacing
(facing) program via Claude Sonnet, streams it back as SSE.

Inputs the user provides:
  stock_width_mm        X dimension of stock
  stock_length_mm       Y dimension of stock
  current_thickness_mm  how thick the stock is right now
  target_thickness_mm   how thick you want it after surfacing
  bit_diameter_mm       your surfacing bit (e.g. 25.4 for a 1-inch bit)
  feed_rate_mmpm        cutting feed rate in mm/min
  spindle_rpm           spindle speed
  depth_per_pass_mm     (optional) max cut per pass, default 0.5mm

Origin is always XY0 of the current workspace — no configuration needed.
Z0 is always the spoilboard surface.
"""

import json
import math
import os
from typing import Generator

import anthropic

from utils.logger import log_claude_response

_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

_SYSTEM = """You are generating a surfacing (face milling) program for a Shapeoko 5 Pro
running grblHAL. Keep the output focused and practical.

Machine rules:
- Units: G21 (mm)
- XY0 is the bottom-left corner of the stock, already set in the workspace
- Z0 is the spoilboard surface
- Strategy: raster passes along X, stepping in Y each pass
- Lead in from X = -2mm (slightly outside stock edge) at safe height
- Safe travel height: Z5
- Final retract: Z10, then G0 X0 Y0
- Spindle on (M3) before any cutting move; spindle off (M5) at the end
- No straight plunges — lower to cut depth during the X lead-in move

Output format:
1. A short line stating what the program will do (e.g. "Surfacing 300×400mm stock, removing 3mm in 6 passes")
2. The complete G-code block (fenced with ```gcode)
3. A plain-English section titled "What each part does:" explaining the program in plain language
"""


def stream_surfacing(params: dict) -> Generator[str, None, None]:
    """
    Generate a surfacing program and stream it as SSE.

    Yields 'data: <text>\\n\\n' chunks.
    Final chunk: 'data: [DONE]\\n\\n'
    """
    # Validate required inputs
    required = [
        "stock_width_mm", "stock_length_mm",
        "current_thickness_mm", "target_thickness_mm",
        "bit_diameter_mm", "feed_rate_mmpm", "spindle_rpm",
    ]
    missing = [k for k in required if k not in params]
    if missing:
        yield f"data: Error — missing: {', '.join(missing)}\n\n"
        yield "data: [DONE]\n\n"
        return

    removal = params["current_thickness_mm"] - params["target_thickness_mm"]
    if removal <= 0:
        yield "data: Error — target thickness must be less than current thickness.\n\n"
        yield "data: [DONE]\n\n"
        return

    depth_per_pass = params.get("depth_per_pass_mm", 0.5)
    z_passes = math.ceil(removal / depth_per_pass)
    stepover = params["bit_diameter_mm"] * 0.45          # 45% stepover default
    y_passes = math.ceil(params["stock_length_mm"] / stepover) + 1

    summary = (
        f"Stock: {params['stock_width_mm']} × {params['stock_length_mm']} mm\n"
        f"Remove: {removal:.2f} mm in {z_passes} Z-pass(es), "
        f"{y_passes} Y-strips per pass\n"
        f"Bit: {params['bit_diameter_mm']} mm diameter, "
        f"{stepover:.1f} mm stepover\n"
        f"Feed: {params['feed_rate_mmpm']} mm/min  Spindle: {params['spindle_rpm']} RPM\n"
        f"Depth per pass: {depth_per_pass} mm"
    )

    user_msg = f"Generate a surfacing program with these parameters:\n\n{summary}\n\nXY0 is already set. Go."

    full_response = []
    try:
        with _client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_msg}],
        ) as stream:
            for text in stream.text_stream:
                full_response.append(text)
                yield f"data: {text.replace(chr(10), '\\n')}\n\n"

        log_claude_response("surfacing", summary[:80], "".join(full_response))

    except anthropic.APIError as exc:
        yield f"data: Claude error: {exc}\n\n"

    yield "data: [DONE]\n\n"
