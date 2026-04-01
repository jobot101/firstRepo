"""
G-code static analysis — Phase 1.5.

Runs before any Claude call to catch common problems quickly:
- Feed rates above the configured threshold
- Air cuts (Z never goes below 0 — suggests wrong material origin)
- Basic syntax errors (malformed lines, unknown codes, missing spindle)

Returns a list of issue dicts so the watch_folder service can decide
whether to call Claude for deeper analysis.
"""

import re
from typing import Optional

# G-code line patterns
_COMMENT_RE = re.compile(r"\(.*?\)|;.*$")
_WORD_RE = re.compile(r"([A-Za-z])([+-]?\d*\.?\d+)")

# Codes that are valid in grblHAL
_KNOWN_MOTION = {"G0", "G00", "G1", "G01", "G2", "G02", "G3", "G03", "G38.2", "G38.3"}
_KNOWN_NON_MOTION = {
    "G4", "G04", "G10", "G17", "G18", "G19",
    "G20", "G21", "G28", "G30", "G40", "G41", "G42",
    "G43", "G43.1", "G49", "G54", "G55", "G56", "G57", "G58", "G59",
    "G61", "G80", "G90", "G91", "G93", "G94",
    "M0", "M1", "M2", "M3", "M03", "M4", "M04", "M5", "M05",
    "M6", "M06", "M7", "M07", "M8", "M08", "M9", "M09", "M30",
}
_ALL_KNOWN = _KNOWN_MOTION | _KNOWN_NON_MOTION


def analyse(gcode_text: str, feed_rate_threshold: float = 5000.0) -> list[dict]:
    """
    Analyse G-code text and return a list of issue dicts.

    Each issue:
      {
        "type":     "feed_rate" | "air_cut" | "syntax" | "missing_spindle",
        "line_num": int,
        "line":     str,
        "detail":   str,
        "severity": "warning" | "error"
      }
    """
    issues = []
    lines = gcode_text.splitlines()

    has_spindle_on = False
    has_cutting_move = False
    min_z = None

    for i, raw_line in enumerate(lines, start=1):
        line = _strip_comment(raw_line).strip().upper()
        if not line:
            continue

        words = _parse_words(line)
        word_map = {w[0]: w[1] for w in words}

        # --- Feed rate check ---
        if "F" in word_map:
            feed = word_map["F"]
            if feed > feed_rate_threshold:
                issues.append({
                    "type": "feed_rate",
                    "line_num": i,
                    "line": raw_line.strip(),
                    "detail": f"Feed rate F{feed:.0f} exceeds threshold {feed_rate_threshold:.0f} mm/min",
                    "severity": "warning",
                })

        # --- Track Z position ---
        if "Z" in word_map:
            z = word_map["Z"]
            if min_z is None or z < min_z:
                min_z = z

        # --- Track spindle ---
        if "M" in word_map:
            m_code = f"M{int(word_map['M'])}"
            if m_code in ("M3", "M4"):
                has_spindle_on = True
            if m_code == "M5":
                has_spindle_on = False

        # --- Track cutting moves ---
        if "G" in word_map:
            g_code = _normalise_g(word_map["G"])
            if g_code in ("G1", "G2", "G3"):
                has_cutting_move = True
            # Unknown code check
            if g_code not in _ALL_KNOWN and g_code.startswith("G"):
                issues.append({
                    "type": "syntax",
                    "line_num": i,
                    "line": raw_line.strip(),
                    "detail": f"Unknown G-code: {g_code}",
                    "severity": "error",
                })

    # --- Air cut check: Z never goes negative ---
    if has_cutting_move and min_z is not None and min_z >= 0:
        issues.append({
            "type": "air_cut",
            "line_num": 0,
            "line": "",
            "detail": (
                f"Z never goes below 0 (minimum Z={min_z:.3f}). "
                "This may be an air cut — check your material Z origin."
            ),
            "severity": "warning",
        })

    # --- Missing spindle command ---
    if has_cutting_move and not has_spindle_on:
        issues.append({
            "type": "missing_spindle",
            "line_num": 0,
            "line": "",
            "detail": "No M3/M4 spindle-on command found before cutting moves.",
            "severity": "error",
        })

    return issues


def _strip_comment(line: str) -> str:
    return _COMMENT_RE.sub("", line)


def _parse_words(line: str) -> list[tuple[str, float]]:
    words = []
    for match in _WORD_RE.finditer(line):
        letter = match.group(1).upper()
        try:
            value = float(match.group(2))
            words.append((letter, value))
        except ValueError:
            pass
    return words


def _normalise_g(value: float) -> str:
    """Convert G code number to canonical string form, e.g. 1.0 -> 'G1'."""
    if value == int(value):
        return f"G{int(value)}"
    return f"G{value}"
