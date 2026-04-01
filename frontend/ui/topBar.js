/**
 * topBar.js
 *
 * Pendant top bar — shows crash and chatter alerts.
 * Messages clear after 5s unless they are crash/no_go (operator must see those).
 *
 * Requires: socket.io client
 */

const TopBar = (() => {
  let _messageEl = null;
  let _clearTimer = null;

  const _PERSISTENT_TYPES = new Set(['error', 'no_go']);

  // ── Init ───────────────────────────────────────────────────────────────

  function init({ messageId } = {}) {
    _messageEl = document.getElementById(messageId || 'top-bar-message');
  }

  // ── Public ─────────────────────────────────────────────────────────────

  function showMessage(text, { type = 'info' } = {}) {
    _setMessage(text, type);
    if (!_PERSISTENT_TYPES.has(type)) {
      _scheduleClear(5000);
    }
  }

  function clearMessage() {
    _setMessage('', 'idle');
  }

  // ── Safety notifications ───────────────────────────────────────────────

  function showCrashNotification() {
    _setMessage('⚠ Crash detected — spindle stopped. Check machine before resuming.', 'error');
  }

  function showChatterAlert(data) {
    const diag = data.diagnosis || {};
    const hint = diag.feed_hint || diag.rpm_hint
      ? ` ${diag.explanation || ''}`
      : '';
    _setMessage(`Chatter detected.${hint}`.trim(), 'warning');
    _scheduleClear(8000);
  }

  // ── SocketIO wiring ────────────────────────────────────────────────────

  function wireSocketIO(socket) {
    socket.on('crash_notification', ()   => showCrashNotification());
    socket.on('chatter_detected',   data => showChatterAlert(data));
    socket.on('bit_warning',        data => showMessage(data.message, { type: 'warning' }));
  }

  // ── Helpers ────────────────────────────────────────────────────────────

  function _scheduleClear(ms) {
    if (_clearTimer) clearTimeout(_clearTimer);
    _clearTimer = setTimeout(clearMessage, ms);
  }

  function _setMessage(text, type) {
    if (!_messageEl) return;
    _messageEl.textContent = text;
    _messageEl.className = `top-bar-message top-bar-message--${type}`;
  }

  return { init, showMessage, clearMessage, wireSocketIO };
})();
