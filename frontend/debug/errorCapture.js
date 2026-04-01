/**
 * errorCapture.js — Phase 1
 *
 * Catches and bundles frontend errors (JS exceptions, SocketIO failures,
 * unhandled promise rejections) and forwards them to the debug endpoint.
 *
 * The bundle sent to /api/debug mirrors the format the backend expects:
 *   {
 *     error_code:  string   e.g. "UI:TypeError" or "SOCKET:connect_error"
 *     message:     string   human-readable error message
 *     position:    object   last known machine position { x, y, z }
 *     last_gcode:  string[] last 10 G-code lines (maintained by machineState)
 *     timestamp:   string   ISO 8601
 *   }
 *
 * Deduplication is also applied client-side (deduplicator.js) before the
 * bundle is posted — the server deduplicates again as a second layer.
 */

const ErrorCapture = (() => {
  // Shared machine state (updated by the SocketIO status handler elsewhere)
  const _machineState = {
    position: { x: 0, y: 0, z: 0 },
    lastGcode: [],
  };

  let _enabled = true;

  // ── Init ───────────────────────────────────────────────────────────────

  function init({ enabled = true } = {}) {
    _enabled = enabled;
    _attachGlobalHandlers();
  }

  // ── Update shared machine state ────────────────────────────────────────

  function updatePosition(pos) {
    if (pos && typeof pos === 'object') {
      _machineState.position = {
        x: Number(pos.x) || 0,
        y: Number(pos.y) || 0,
        z: Number(pos.z) || 0,
      };
    }
  }

  function pushGcodeLine(line) {
    _machineState.lastGcode.push(line);
    if (_machineState.lastGcode.length > 10) {
      _machineState.lastGcode.shift();
    }
  }

  // ── Capture and send ───────────────────────────────────────────────────

  /**
   * Build an error bundle and POST it to /api/debug.
   * Client-side dedup runs first.
   */
  function capture(errorCode, message) {
    if (!_enabled) return;

    // Client-side dedup — same logic as backend (error_code + rounded position)
    if (Deduplicator.isDuplicate(errorCode, _machineState.position)) return;

    const bundle = {
      error_code: errorCode,
      message:    String(message),
      position:   { ..._machineState.position },
      last_gcode: [..._machineState.lastGcode],
      timestamp:  new Date().toISOString(),
    };

    // Use sendBeacon for reliability on page unload; fetch otherwise
    const payload = JSON.stringify(bundle);
    if (navigator.sendBeacon) {
      navigator.sendBeacon('/api/debug', new Blob([payload], { type: 'application/json' }));
    } else {
      fetch('/api/debug', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: payload,
        keepalive: true,
      }).catch(() => {});
    }
  }

  // ── Global error handlers ──────────────────────────────────────────────

  function _attachGlobalHandlers() {
    // Uncaught JS exceptions
    window.addEventListener('error', event => {
      capture(
        `UI:${event.error ? event.error.name : 'Error'}`,
        `${event.message} (${event.filename}:${event.lineno})`
      );
    });

    // Unhandled promise rejections
    window.addEventListener('unhandledrejection', event => {
      const reason = event.reason;
      const msg = reason instanceof Error
        ? `${reason.name}: ${reason.message}`
        : String(reason);
      capture('UI:UnhandledRejection', msg);
    });
  }

  // ── SocketIO error wiring ──────────────────────────────────────────────

  /**
   * Wire SocketIO transport-level errors so they flow through capture().
   * Call this after creating the socket.
   */
  function wireSocketIO(socket) {
    socket.on('connect_error', err => {
      capture('SOCKET:connect_error', err.message || String(err));
    });

    socket.on('error', err => {
      capture('SOCKET:error', String(err));
    });

    // Keep machine state in sync
    socket.on('machine_status', status => {
      if (status.position) updatePosition(status.position);
    });

    socket.on('gcode_sent', line => {
      if (typeof line === 'string') pushGcodeLine(line);
    });
  }

  return {
    init,
    capture,
    updatePosition,
    pushGcodeLine,
    wireSocketIO,
  };
})();
