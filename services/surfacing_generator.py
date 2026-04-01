"""
Surfacing toolpath generator — Phase 1.5.

Takes stock dimensions, bit diameter, and cutting parameters, then uses
Claude Sonnet to calculate the pass count, generate complete G-code, and
explain every block in plain English.

Origin convention (as per plan):
  X/Y zero: bottom-left corner of stock
  Z zero: spoilboard surface

The generated G-code is returned for display in the approval gate —
nothing is sent to the machine until the user taps Approve.
"""

import json
import math
import os
from typing import Generator

import anthropic

from utils.logger import log_claude_response

_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
_SONNET_MODEL = "claude-sonnet-4-6"

_SURFACING_SYSTEM = """You are a CNC surfacing specialist for a Shapeoko 5 Pro running grblHAL.

Your task: generate a surfacing (facing) toolpath as complete, ready-to-run G-code.

Rules:
- Units: G21 (mm)
- Origin: X0 Y0 at bottom-left corner of stock, Z0 at spoilboard surface
- Movement strategy: raster passes (back-and-forth) across the X axis, stepping in Y
- Lead-in: approach from slightly outside the stock boundary (–2mm X) at feed height
- Retract to Z5 between passes, Z10 for final retract
- End: M5 spindle off, G0 Z10, G0 X0 Y0
- Use a ramp-down on the first plunge (helical or angled entry, not straight plunge)

After the G-code block, add a section titled "What this program does:" and explain
each section in plain English. State the total estimated cut time if calculable.
"""


def stream_surfacing_gcode(params: dict) -> Generator[str, None, None]:
    """
    Generate a surfacing G-code program via Claude Sonnet.

    params keys:
      stock_width_mm      float  X dimension of stock
      stock_length_mm     float  Y dimension of stock
      current_thickness_mm float  current stock thickness (Z height above spoilboard)
      target_thickness_mm float  desired final thickness
      bit_diameter_mm     float  surfacing bit (e.g. 25.4 for 1-inch spoilboard bit)
      stepover_percent    float  0–100, typically 40–50 for surfacing
      feed_rate_mmpm      float  mm/min
      spindle_rpm         int
      depth_per_pass_mm   float  max depth removed per pass (e.g. 0.5)

    Yields SSE-formatted strings. Final chunk: 'data: [DONE]\\n\\n'
    """
    # Pre-calculate pass count locally so we can validate before calling Claude
    total_removal = params.get("current_thickness_mm", 0) - params.get("target_thickness_mm", 0)
    depth_per_pass = params.get("depth_per_pass_mm", 0.5)

    if total_removal <= 0:
        yield "data: Error: target thickness must be less than current thickness.\n\n"
        yield "data: [DONE]\n\n"
        return

    pass_count = math.ceil(total_removal / depth_per_pass)
    stepover_mm = params.get("bit_diameter_mm", 25.4) * (params.get("stepover_percent", 45) / 100)
    y_passes = math.ceil(params.get("stock_length_mm", 0) / stepover_mm) + 1

    params_with_calc = {
        **params,
        "_calculated_z_passes": pass_count,
        "_calculated_y_passes_per_z_level": y_passes,
        "_calculated_stepover_mm": round(stepover_mm, 3),
        "_calculated_total_removal_mm": round(total_removal, 3),
    }

    user_msg = (
        f"Generate a surfacing program with these parameters:\n"
        f"{json.dumps(params_with_calc, indent=2)}\n\n"
        f"Pre-calculated values for reference:\n"
        f"- Total material to remove: {total_removal:.2f}mm\n"
        f"- Z passes required: {pass_count}\n"
        f"- Y passes per Z level: {y_passes}\n"
        f"- Stepover: {stepover_mm:.1f}mm\n\n"
        "Generate the complete G-code and explain every section."
    )

    full_response = []

    try:
        with _client.messages.stream(
            model=_SONNET_MODEL,
            max_tokens=4096,
            system=[
                {
                    "type": "text",
                    "text": _SURFACING_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_msg}],
        ) as stream:
            for text in stream.text_stream:
                full_response.append(text)
                safe = text.replace("\n", "\\n")
                yield f"data: {safe}\n\n"

        log_claude_response(
            "surfacing_generation",
            f"stock={params.get('stock_width_mm')}x{params.get('stock_length_mm')}",
            "".join(full_response),
        )

    except anthropic.APIError as exc:
        yield f"data: Error generating surfacing program: {exc}\n\n"

    yield "data: [DONE]\n\n"
