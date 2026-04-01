/**
 * topBar.js — Phase 1
 *
 * Manages the pendant top bar:
 *   - Streams Claude's word-by-word error explanations via EventSource (SSE)
 *   - Shows watch-folder alerts ("New file: bracket_v2.nc — 2 issues found")
 *   - Displays daily shop insight
 *   - Hosts the debug-mode on/off toggle
 *   - Shows camera safety verdicts and chatter/crash notifications
 *
 * Depends on: socket.io client (loaded by the pendant app)
 */

const TopBar = (() => {
  // ── DOM refs (populated on init) ──────────────────────────────────────
  let _messageEl = null;   // span that shows streaming text
  let _toggleEl  = null;   // checkbox or button for debug toggle
  let _statusEl  = null;   // small indicator dot (green/red)

  // ── State ──────────────────────────────────────────────────────────────
  let _debugEnabled = true;
  let _currentSource = null;  // active EventSource connection

  // ── Init ───────────────────────────────────────────────────────────────

  function init({ messageId, toggleId, statusId } = {}) {
    _messageEl = document.getElementById(messageId || 'top-bar-message');
    _toggleEl  = document.getElementById(toggleId  || 'debug-toggle');
    _statusEl  = document.getElementById(statusId  || 'debug-status');

    // Fetch initial toggle state
    fetch('/api/debug/status')
      .then(r => r.json())
      .then(data => _setToggleState(data.enabled))
      .catch(() => {});

    // Wire toggle
    if (_toggleEl) {
      _toggleEl.addEventListener('change', _handleToggle);
    }
  }

  // ── Public message setters ─────────────────────────────────────────────

  function showMessage(text, { type = 'info', duration = 0 } = {}) {
    _cancelStream();
    _setMessage(text, type);
    if (duration > 0) {
      setTimeout(clearMessage, duration);
    }
  }

  function clearMessage() {
    _setMessage('', 'idle');
  }

  // ── SSE streaming (Claude error explanation) ───────────────────────────

  /**
   * Open an SSE connection to /api/debug and stream the response
   * word-by-word into the top bar.
   *
   * @param {object} errorBundle  { error_code, message, position, last_gcode, timestamp }
   */
  function streamDebugExplanation(errorBundle) {
    if (!_debugEnabled) return;

    _cancelStream();
    _setMessage('', 'thinking');

    // POST the bundle then switch to SSE
    fetch('/api/debug', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(errorBundle),
    }).then(response => {
      if (!response.ok || response.status === 204) return;

      // Read SSE directly from the fetch response body
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let accumulated = '';

      function pump() {
        reader.read().then(({ done, value }) => {
          if (done) {
            _setMessage(accumulated.trim(), 'info');
            return;
          }
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n\n');
          buffer = lines.pop(); // keep incomplete chunk

          for (const chunk of lines) {
            const dataLine = chunk.replace(/^data: /, '');
            if (dataLine === '[DONE]') {
              _setMessage(accumulated.trim(), 'info');
              return;
            }
            accumulated += dataLine + ' ';
            _setMessage(accumulated.trim(), 'streaming');
          }
          pump();
        }).catch(() => {});
      }
      pump();
    }).catch(() => {});
  }

  // ── Watch folder notification ──────────────────────────────────────────

  /**
   * Show a quick alert when a new G-code file lands in the watch folder.
   * The user taps the message to open the full review panel.
   *
   * @param {object} fileData  { filename, issue_count, issues, filepath }
   */
  function showFileAlert(fileData) {
    const { filename, issue_count } = fileData;
    const text = issue_count > 0
      ? `New file: ${filename} — ${issue_count} issue${issue_count !== 1 ? 's' : ''} found`
      : `New file: ${filename} — no issues detected`;

    _setMessage(text, issue_count > 0 ? 'warning' : 'success');

    if (_messageEl) {
      _messageEl.style.cursor = 'pointer';
      _messageEl.onclick = () => {
        _messageEl.style.cursor = '';
        _messageEl.onclick = null;
        ReviewPanel.open(fileData);
      };
    }
  }

  // ── Safety / crash / chatter notifications ────────────────────────────

  function showCrashNotification(data) {
    _cancelStream();
    _setMessage('⚠ Crash detected — spindle stopped. Check machine before resuming.', 'error');
  }

  function showChatterAlert(data) {
    const diag = data.diagnosis || {};
    const suggestion = diag.suggested_feed_rate_mmpm
      ? ` Suggested feed: ${Math.round(diag.suggested_feed_rate_mmpm)} mm/min.`
      : '';
    _setMessage(`Chatter detected.${suggestion} ${diag.explanation || ''}`, 'warning');
  }

  function showCameraResult(result) {
    if (result.verdict === 'no_go') {
      _setMessage(`⛔ Camera check failed (${result.trigger}): ${result.explanation}`, 'error');
    } else if (result.verdict === 'caution') {
      _setMessage(`⚠ Camera caution (${result.trigger}): ${result.explanation}`, 'warning');
    }
    // go verdict is silent — don't clutter the bar with OK messages
  }

  function showDailyInsight(text) {
    if (text) _setMessage(`💡 ${text}`, 'insight');
  }

  // ── Toggle ─────────────────────────────────────────────────────────────

  function _handleToggle(event) {
    const enabled = event.target.checked !== undefined
      ? event.target.checked
      : !_debugEnabled;

    fetch('/api/debug/toggle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    })
      .then(r => r.json())
      .then(data => _setToggleState(data.enabled))
      .catch(() => {});
  }

  function _setToggleState(enabled) {
    _debugEnabled = enabled;
    if (_toggleEl && _toggleEl.type === 'checkbox') {
      _toggleEl.checked = enabled;
    }
    if (_statusEl) {
      _statusEl.className = enabled ? 'status-dot green' : 'status-dot red';
      _statusEl.title = enabled ? 'AI Debug: on' : 'AI Debug: off';
    }
  }

  // ── Helpers ────────────────────────────────────────────────────────────

  function _setMessage(text, type) {
    if (!_messageEl) return;
    _messageEl.textContent = text;
    _messageEl.className = `top-bar-message top-bar-message--${type}`;
  }

  function _cancelStream() {
    if (_currentSource) {
      _currentSource.close();
      _currentSource = null;
    }
  }

  // ── SocketIO event wiring ──────────────────────────────────────────────

  function wireSocketIO(socket) {
    socket.on('debug_event',       data => streamDebugExplanation(data));
    socket.on('new_gcode_file',    data => showFileAlert(data));
    socket.on('crash_notification', data => showCrashNotification(data));
    socket.on('chatter_detected',  data => showChatterAlert(data));
    socket.on('camera_result',     data => showCameraResult(data));
    socket.on('daily_insight',     data => showDailyInsight(data.text));
    socket.on('bit_warning',       data => showMessage(data.message, { type: 'warning' }));
  }

  return {
    init,
    showMessage,
    clearMessage,
    streamDebugExplanation,
    showFileAlert,
    showCrashNotification,
    showChatterAlert,
    showCameraResult,
    showDailyInsight,
    wireSocketIO,
  };
})();
