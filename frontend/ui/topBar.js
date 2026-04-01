/**
 * topBar.js — Phase 1
 *
 * Manages the pendant top bar:
 *   - Streams Claude error explanations word-by-word via SSE
 *   - Shows crash / chatter / camera safety alerts
 *   - Hosts the debug on/off toggle
 *
 * Requires: socket.io client
 */

const TopBar = (() => {
  let _messageEl = null;
  let _toggleEl  = null;
  let _statusEl  = null;
  let _debugEnabled = true;

  // ── Init ───────────────────────────────────────────────────────────────

  function init({ messageId, toggleId, statusId } = {}) {
    _messageEl = document.getElementById(messageId || 'top-bar-message');
    _toggleEl  = document.getElementById(toggleId  || 'debug-toggle');
    _statusEl  = document.getElementById(statusId  || 'debug-status');

    fetch('/api/debug/status')
      .then(r => r.json())
      .then(data => _setToggleState(data.enabled))
      .catch(() => {});

    if (_toggleEl) {
      _toggleEl.addEventListener('change', _handleToggle);
    }
  }

  // ── Public ─────────────────────────────────────────────────────────────

  function showMessage(text, { type = 'info', duration = 0 } = {}) {
    _setMessage(text, type);
    if (duration > 0) setTimeout(clearMessage, duration);
  }

  function clearMessage() {
    _setMessage('', 'idle');
  }

  // ── SSE streaming (Claude error explanation) ───────────────────────────

  function streamDebugExplanation(errorBundle) {
    if (!_debugEnabled) return;
    _setMessage('', 'thinking');

    fetch('/api/debug', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(errorBundle),
    }).then(response => {
      if (!response.ok || response.status === 204) return;

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let accumulated = '';

      function pump() {
        reader.read().then(({ done, value }) => {
          if (done) return;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n\n');
          buffer = lines.pop();

          for (const chunk of lines) {
            const text = chunk.replace(/^data: /, '');
            if (text === '[DONE]') { _setMessage(accumulated.trim(), 'info'); return; }
            accumulated += text + ' ';
            _setMessage(accumulated.trim(), 'streaming');
          }
          pump();
        }).catch(() => {});
      }
      pump();
    }).catch(() => {});
  }

  // ── Safety notifications ───────────────────────────────────────────────

  function showCrashNotification() {
    _setMessage('⚠ Crash detected — spindle stopped. Check machine before resuming.', 'error');
  }

  function showChatterAlert(data) {
    const diag = data.diagnosis || {};
    const feed = diag.suggested_feed_rate_mmpm
      ? ` Try F${Math.round(diag.suggested_feed_rate_mmpm)}.`
      : '';
    _setMessage(`Chatter detected.${feed} ${diag.explanation || ''}`.trim(), 'warning');
  }

  function showCameraResult(result) {
    if (result.verdict === 'no_go') {
      _setMessage(`⛔ Camera (${result.trigger}): ${result.explanation}`, 'error');
    } else if (result.verdict === 'caution') {
      _setMessage(`⚠ Camera (${result.trigger}): ${result.explanation}`, 'warning');
    }
  }

  // ── Toggle ─────────────────────────────────────────────────────────────

  function _handleToggle(e) {
    const enabled = e.target.type === 'checkbox' ? e.target.checked : !_debugEnabled;
    fetch('/api/debug/toggle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    }).then(r => r.json()).then(d => _setToggleState(d.enabled)).catch(() => {});
  }

  function _setToggleState(enabled) {
    _debugEnabled = enabled;
    if (_toggleEl && _toggleEl.type === 'checkbox') _toggleEl.checked = enabled;
    if (_statusEl) {
      _statusEl.className = `status-dot ${enabled ? 'green' : 'red'}`;
      _statusEl.title = `AI Debug: ${enabled ? 'on' : 'off'}`;
    }
  }

  // ── Helpers ────────────────────────────────────────────────────────────

  function _setMessage(text, type) {
    if (!_messageEl) return;
    _messageEl.textContent = text;
    _messageEl.className = `top-bar-message top-bar-message--${type}`;
  }

  // ── SocketIO wiring ────────────────────────────────────────────────────

  function wireSocketIO(socket) {
    socket.on('debug_event',        data => streamDebugExplanation(data));
    socket.on('crash_notification', ()   => showCrashNotification());
    socket.on('chatter_detected',   data => showChatterAlert(data));
    socket.on('camera_result',      data => showCameraResult(data));
    socket.on('bit_warning',        data => showMessage(data.message, { type: 'warning' }));
  }

  return { init, showMessage, clearMessage, streamDebugExplanation, wireSocketIO };
})();
