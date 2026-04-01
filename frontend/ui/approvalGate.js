/**
 * approvalGate.js — Phase 2
 *
 * The human approval gate UI.
 *
 * Every action that could affect machine motion surfaces here before
 * anything happens.  Three buttons: Approve, Modify, Reject.
 *
 * The pending action is always visible in the top bar.
 * Nothing executes until the user taps Approve.
 *
 * Crash detection (spindle shutoff) bypasses this gate entirely — that
 * happens at the SocketIO layer in app.py with no delay.
 */

const ApprovalGate = (() => {
  let _modalEl    = null;
  let _pendingId  = null;
  let _onApprove  = null;
  let _onReject   = null;

  // ── Build DOM once ─────────────────────────────────────────────────────

  function _ensureModal() {
    if (_modalEl) return;

    _modalEl = document.createElement('div');
    _modalEl.id = 'approval-gate';
    _modalEl.className = 'approval-gate approval-gate--hidden';
    _modalEl.innerHTML = `
      <div class="approval-gate__backdrop"></div>
      <div class="approval-gate__dialog" role="dialog" aria-modal="true">
        <div class="approval-gate__header">
          <span class="approval-gate__icon" id="ag-icon">⚙</span>
          <span class="approval-gate__title" id="ag-title">Pending Action</span>
        </div>
        <div class="approval-gate__body">
          <div class="approval-gate__action" id="ag-action"></div>
          <div class="approval-gate__explanation" id="ag-explanation"></div>
          <div class="approval-gate__params" id="ag-params"></div>
        </div>
        <div class="approval-gate__modify" id="ag-modify-section" style="display:none">
          <textarea id="ag-modify-input" placeholder="Describe the modification…" rows="2"></textarea>
          <button id="ag-modify-send" class="btn btn--secondary">Apply Modification</button>
        </div>
        <div class="approval-gate__footer">
          <button id="ag-reject"  class="btn btn--danger">   Reject  </button>
          <button id="ag-modify"  class="btn btn--secondary"> Modify  </button>
          <button id="ag-approve" class="btn btn--success">   Approve </button>
        </div>
        <div class="approval-gate__timer" id="ag-timer"></div>
      </div>
    `;

    document.body.appendChild(_modalEl);
    _wireButtons();
  }

  // ── Show a pending action ──────────────────────────────────────────────

  /**
   * @param {object} pending  Response from POST /api/jobs/propose
   * @param {object} options
   * @param {Function} [options.onApprove]  Called after successful approval
   * @param {Function} [options.onReject]   Called after rejection
   */
  function show(pending, { onApprove, onReject } = {}) {
    _ensureModal();

    _pendingId = pending.action_id;
    _onApprove = onApprove || null;
    _onReject  = onReject  || null;

    // Populate dialog
    document.getElementById('ag-icon').textContent     = _actionIcon(pending.action);
    document.getElementById('ag-title').textContent    = _actionTitle(pending.action);
    document.getElementById('ag-action').textContent   = pending.action;
    document.getElementById('ag-explanation').textContent = pending.explanation || '';

    const paramsEl = document.getElementById('ag-params');
    const params = pending.parameters || {};
    if (Object.keys(params).length > 0) {
      paramsEl.innerHTML = `<pre>${JSON.stringify(params, null, 2)}</pre>`;
    } else {
      paramsEl.innerHTML = '';
    }

    // Reset modify section
    document.getElementById('ag-modify-section').style.display = 'none';
    document.getElementById('ag-modify-input').value = '';

    // Show
    _modalEl.classList.remove('approval-gate--hidden');
    _modalEl.classList.add('approval-gate--visible');

    // Auto-focus approve for touchscreen use
    setTimeout(() => document.getElementById('ag-approve').focus(), 100);

    // Highlight in top bar
    TopBar.showMessage(`Waiting for approval: ${_actionTitle(pending.action)}`, { type: 'pending' });
  }

  // ── Hide ───────────────────────────────────────────────────────────────

  function hide() {
    if (_modalEl) {
      _modalEl.classList.remove('approval-gate--visible');
      _modalEl.classList.add('approval-gate--hidden');
    }
    _pendingId = null;
    TopBar.clearMessage();
  }

  // ── Approve ────────────────────────────────────────────────────────────

  function _approve() {
    if (!_pendingId) return;

    fetch('/api/jobs/approve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action_id: _pendingId }),
    })
      .then(r => r.json())
      .then(data => {
        hide();
        TopBar.showMessage('Action approved — executing.', { type: 'success', duration: 3000 });
        if (_onApprove) _onApprove(data);
      })
      .catch(() => {
        TopBar.showMessage('Approval failed — check connection.', { type: 'error' });
      });
  }

  // ── Reject ─────────────────────────────────────────────────────────────

  function _reject() {
    if (!_pendingId) return;

    fetch('/api/jobs/reject', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action_id: _pendingId }),
    })
      .then(() => {
        hide();
        TopBar.showMessage('Action rejected.', { type: 'info', duration: 2000 });
        if (_onReject) _onReject();
      })
      .catch(() => {});
  }

  // ── Modify ─────────────────────────────────────────────────────────────

  function _toggleModify() {
    const section = document.getElementById('ag-modify-section');
    section.style.display = section.style.display === 'none' ? 'block' : 'none';
    if (section.style.display === 'block') {
      document.getElementById('ag-modify-input').focus();
    }
  }

  function _applyModification() {
    // Reject the current action and re-propose with the modification note
    const note = document.getElementById('ag-modify-input').value.trim();
    if (!note) return;

    fetch('/api/jobs/reject', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action_id: _pendingId }),
    }).then(() => {
      hide();
      TopBar.showMessage(`Modification requested: "${note}"`, { type: 'info', duration: 4000 });
      // The user or voice system will re-propose with the updated parameters
    }).catch(() => {});
  }

  // ── SocketIO wiring ────────────────────────────────────────────────────

  /**
   * Wire SocketIO events so the gate opens automatically when the backend
   * proposes an action (e.g. from a voice command or automated trigger).
   */
  function wireSocketIO(socket) {
    socket.on('action_proposed', pending => {
      show(pending);
    });
    socket.on('action_approved', data => {
      if (data.action_id === _pendingId) hide();
    });
  }

  // ── Button wiring ──────────────────────────────────────────────────────

  function _wireButtons() {
    document.getElementById('ag-approve').onclick      = _approve;
    document.getElementById('ag-reject').onclick       = _reject;
    document.getElementById('ag-modify').onclick       = _toggleModify;
    document.getElementById('ag-modify-send').onclick  = _applyModification;

    // Close on backdrop click
    _modalEl.querySelector('.approval-gate__backdrop').onclick = _reject;
  }

  // ── Helpers ────────────────────────────────────────────────────────────

  function _actionIcon(action) {
    const icons = {
      start_job:      '▶',
      pause:          '⏸',
      resume:         '▶',
      send_gcode:     '📤',
      feed_override:  '⚡',
      spindle_speed:  '🌀',
      home:           '🏠',
      bitsetter_probe:'📏',
      tool_change:    '🔧',
    };
    return icons[action] || '⚙';
  }

  function _actionTitle(action) {
    const titles = {
      start_job:      'Start Job',
      pause:          'Pause Job',
      resume:         'Resume Job',
      send_gcode:     'Send G-code',
      feed_override:  'Feed Rate Override',
      spindle_speed:  'Change Spindle Speed',
      home:           'Home Machine',
      bitsetter_probe:'Run BitSetter Probe',
      tool_change:    'Tool Change',
    };
    return titles[action] || action;
  }

  return { show, hide, wireSocketIO };
})();
