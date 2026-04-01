/**
 * deduplicator.js — Phase 1
 *
 * Client-side deduplication filter.
 *
 * Mirrors the backend logic in utils/deduplicator.py:
 *   Key:    error_code + position rounded to nearest 1mm
 *   Window: 10 seconds (default, matches settings.json)
 *
 * The server also deduplicates, so this is a fast first-pass filter
 * that avoids unnecessary network requests when the same error fires
 * multiple times in rapid succession.
 */

const Deduplicator = (() => {
  // { key: timestamp_ms }
  const _seen = new Map();

  const _WINDOW_MS = 10_000;  // 10 seconds

  // ── Public API ─────────────────────────────────────────────────────────

  /**
   * Returns true if this error+position has been seen within the window.
   * Registers the event if it is new.
   *
   * @param {string} errorCode
   * @param {{ x: number, y: number, z: number }} position
   * @param {number} [windowMs]  override default 10 000 ms
   */
  function isDuplicate(errorCode, position, windowMs = _WINDOW_MS) {
    _evictExpired(windowMs);

    const key = _makeKey(errorCode, position);
    if (_seen.has(key)) return true;

    _seen.set(key, Date.now());
    return false;
  }

  /** Clear all state (useful for testing). */
  function clear() {
    _seen.clear();
  }

  // ── Helpers ────────────────────────────────────────────────────────────

  function _makeKey(errorCode, position) {
    const x = Math.round(Number(position?.x) || 0);
    const y = Math.round(Number(position?.y) || 0);
    const z = Math.round(Number(position?.z) || 0);
    return `${errorCode}:${x},${y},${z}`;
  }

  function _evictExpired(windowMs) {
    const cutoff = Date.now() - windowMs;
    for (const [key, ts] of _seen.entries()) {
      if (ts < cutoff) _seen.delete(key);
    }
  }

  return { isDuplicate, clear };
})();
