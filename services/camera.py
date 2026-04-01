"""
Camera service — Phase 4.

The USB camera is spindle-mounted, so it always points at the tool position.
Frames are captured only at specific trigger points — not continuously.
Between triggers the camera is idle; no API calls are made.

Trigger points (as per plan):
  homing_complete     — check bed clear, no obstructions in travel path
  out_of_job          — confirm setup operations completed correctly
  job_start           — verify stock present, clamps visible, correct bit loaded
  tool_change         — confirm new bit seated, monitor BitSetter sequence

Claude Sonnet is used for image analysis (vision capability required).
"""

import base64
import os
from typing import Optional

import anthropic

from utils.logger import log

_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
_SONNET_MODEL = "claude-sonnet-4-6"

# Trigger-specific prompts — each is a focused checklist for that moment
_TRIGGER_PROMPTS = {
    "homing_complete": (
        "The CNC machine just completed homing. The camera is spindle-mounted. "
        "Check the image and answer:\n"
        "1. Is the bed clear? Are there any objects in the machine's travel path?\n"
        "2. Are any clamps, tools, or materials left in a dangerous position?\n"
        "3. Is it safe to proceed?\n"
        "Be specific about what you see. If anything is wrong, describe exactly where it is."
    ),
    "out_of_job": (
        "The CNC machine just completed an out-of-job procedure. "
        "Check the image and answer:\n"
        "1. Does the setup look correct for the next operation?\n"
        "2. Is the material positioned as expected?\n"
        "3. Are clamps secure and clear of the toolpath?\n"
        "Give a clear go/no-go recommendation with reasoning."
    ),
    "job_start": (
        "The CNC machine is about to start a job. The camera is spindle-mounted. "
        "Check the image and answer:\n"
        "1. Is stock material present and positioned correctly?\n"
        "2. Are clamps visible and secure?\n"
        "3. Does the bit appear to be the correct type (describe what you see)?\n"
        "4. Is there anything that would prevent safe operation?\n"
        "Give a clear go/no-go recommendation."
    ),
    "tool_change": (
        "A tool change just occurred on the CNC machine. "
        "Check the image and answer:\n"
        "1. Is the new bit visibly seated correctly in the collet?\n"
        "2. Does the collet nut appear tightened?\n"
        "3. Is the bit length appropriate (not excessively long)?\n"
        "4. Is the BitSetter probe area clear?\n"
        "Give a clear go/no-go recommendation before the BitSetter probing sequence runs."
    ),
}

_VISION_SYSTEM = (
    "You are a CNC machine safety inspector reviewing camera images from a "
    "spindle-mounted USB camera on a Shapeoko 5 Pro. "
    "Be precise and safety-focused. Use plain language. Keep responses under 100 words."
)


def capture_and_analyse(trigger: str, device_index: int = 0) -> Optional[dict]:
    """
    Capture a frame from the camera and send it to Claude Sonnet for analysis.

    Returns:
      {
        "trigger":     str,
        "verdict":     "go" | "no_go" | "caution",
        "explanation": str,
        "raw_response": str
      }
    or None if capture fails.
    """
    frame_b64 = _capture_frame(device_index)
    if frame_b64 is None:
        log("camera_error", {"trigger": trigger, "error": "frame capture failed"})
        return None

    prompt = _TRIGGER_PROMPTS.get(trigger, "Describe what you see in this CNC machine image.")

    try:
        response = _client.messages.create(
            model=_SONNET_MODEL,
            max_tokens=256,
            system=_VISION_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": frame_b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )
    except anthropic.APIError as exc:
        log("camera_error", {"trigger": trigger, "error": str(exc)})
        return None

    raw = ""
    for block in response.content:
        if block.type == "text":
            raw = block.text
            break

    verdict = _extract_verdict(raw)
    result = {"trigger": trigger, "verdict": verdict, "explanation": raw, "raw_response": raw}
    log("camera_analysis", result)
    return result


def _capture_frame(device_index: int) -> Optional[str]:
    """
    Capture a single JPEG frame from the USB camera.
    Returns base64-encoded JPEG string, or None on failure.

    Uses OpenCV if available; falls back gracefully so the service
    can still load on machines without a camera attached.
    """
    try:
        import cv2  # type: ignore

        cap = cv2.VideoCapture(device_index)
        if not cap.isOpened():
            return None

        ret, frame = cap.read()
        cap.release()

        if not ret or frame is None:
            return None

        ret2, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ret2:
            return None

        return base64.standard_b64encode(buffer.tobytes()).decode("utf-8")

    except ImportError:
        log("camera_error", {"error": "OpenCV not installed — camera unavailable"})
        return None
    except Exception as exc:
        log("camera_error", {"error": str(exc)})
        return None


def _extract_verdict(text: str) -> str:
    """Parse a go/no-go/caution verdict from the response text."""
    lower = text.lower()
    if "no-go" in lower or "no go" in lower or "do not" in lower or "stop" in lower:
        return "no_go"
    if "caution" in lower or "check" in lower or "verify" in lower:
        return "caution"
    return "go"
