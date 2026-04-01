/**
 * reviewPanel.js — Phase 1.5
 *
 * Slide-in panel for G-code file review results.
 *
 * Shows:
 *   - Filename and issue summary
 *   - Expandable list of static-analysis issues (from gcode_review.py)
 *   - A "Full Claude Review" section that streams via SSE
 *   - Three action buttons: Ask Claude More, Dismiss, Run Anyway
 *
 * Opens when the user taps a watch-folder alert in topBar.js.
 * Extends topBar.js — requires TopBar to be loaded first.
 */

const ReviewPanel = (() => {
  let _panelEl = null;
  let _fileData = null;
  let _reviewStream = null;

  // ── Build DOM if it doesn't exist ─────────────────────────────────────

  function _ensurePanel() {
    if (_panelEl) return;

    _panelEl = document.createElement('div');
    _panelEl.id = 'review-panel';
    _panelEl.className = 'review-panel review-panel--hidden';
    _panelEl.innerHTML = `
      <div class="review-panel__header">
        <span class="review-panel__title" id="review-panel-title"></span>
        <button class="review-panel__close" id="review-panel-close">✕</button>
      </div>
      <div class="review-panel__body">
        <div class="review-issues" id="review-issues"></div>
        <div class="review-claude" id="review-claude">
          <div class="review-claude__label">Full AI Review</div>
          <div class="review-claude__text" id="review-claude-text">
            Tap "Full Review" to analyse with AI.
          </div>
        </div>
      </div>
      <div class="review-panel__actions">
        <button id="review-btn-full"    class="btn btn--secondary">Full Review</button>
        <button id="review-btn-ask"     class="btn btn--secondary">Ask Claude</button>
        <button id="review-btn-dismiss" class="btn btn--secondary">Dismiss</button>
        <button id="review-btn-run"     class="btn btn--danger">Run Anyway</button>
      </div>
      <div class="review-panel__ask" id="review-ask" style="display:none">
        <textarea id="review-ask-input" placeholder="Ask a question about this file…" rows="2"></textarea>
        <button id="review-ask-send" class="btn btn--primary">Send</button>
      </div>
    `;

    document.body.appendChild(_panelEl);
    _wireButtons();
  }

  // ── Open ───────────────────────────────────────────────────────────────

  function open(fileData) {
    _ensurePanel();
    _fileData = fileData;
    _cancelStream();

    const { filename, issues = [], issue_count = 0 } = fileData;

    // Title
    document.getElementById('review-panel-title').textContent =
      `${filename} — ${issue_count > 0 ? issue_count + ' issue(s)' : 'No issues'}`;

    // Issues list
    const issuesEl = document.getElementById('review-issues');
    if (issues.length === 0) {
      issuesEl.innerHTML = '<p class="review-issues__none">No static issues found.</p>';
    } else {
      issuesEl.innerHTML = issues.map(iss => `
        <div class="review-issue review-issue--${iss.severity}">
          <span class="review-issue__type">${_typeLabel(iss.type)}</span>
          <span class="review-issue__detail">${_escape(iss.detail)}</span>
          ${iss.line_num > 0 ? `<span class="review-issue__line">Line ${iss.line_num}</span>` : ''}
        </div>
      `).join('');
    }

    // Reset Claude text
    document.getElementById('review-claude-text').textContent =
      'Tap "Full Review" to analyse with AI.';

    // Show panel
    _panelEl.classList.remove('review-panel--hidden');
    _panelEl.classList.add('review-panel--visible');
  }

  // ── Close ──────────────────────────────────────────────────────────────

  function close() {
    _cancelStream();
    if (_panelEl) {
      _panelEl.classList.remove('review-panel--visible');
      _panelEl.classList.add('review-panel--hidden');
    }
    _fileData = null;
  }

  // ── Stream full Claude review ──────────────────────────────────────────

  function _streamFullReview() {
    if (!_fileData) return;

    _cancelStream();
    const claudeText = document.getElementById('review-claude-text');
    claudeText.textContent = '';

    fetch('/api/debug/gcode-review', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        filepath: _fileData.filepath,
        issues:   _fileData.issues || [],
      }),
    }).then(response => {
      if (!response.ok) {
        claudeText.textContent = 'Review failed. Check backend logs.';
        return;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let accumulated = '';

      function pump() {
        reader.read().then(({ done, value }) => {
          if (done) return;
          buffer += decoder.decode(value, { stream: true });
          const chunks = buffer.split('\n\n');
          buffer = chunks.pop();

          for (const chunk of chunks) {
            const data = chunk.replace(/^data: /, '');
            if (data === '[DONE]') return;
            accumulated += data;
            claudeText.textContent = accumulated;
          }
          pump();
        }).catch(() => {});
      }
      pump();
    }).catch(err => {
      claudeText.textContent = `Error: ${err.message}`;
    });
  }

  // ── Ask Claude more ────────────────────────────────────────────────────

  function _toggleAskBox() {
    const askDiv = document.getElementById('review-ask');
    askDiv.style.display = askDiv.style.display === 'none' ? 'block' : 'none';
  }

  function _sendAsk() {
    const input = document.getElementById('review-ask-input');
    const question = input.value.trim();
    if (!question) return;

    const claudeText = document.getElementById('review-claude-text');
    claudeText.textContent = '';
    input.value = '';

    // Reuse the /api/jobs/gcode endpoint with the question as the task
    fetch('/api/jobs/gcode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        task_description: question,
        parameters: { context_file: _fileData ? _fileData.filename : '' },
      }),
    }).then(response => {
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let accumulated = '';

      function pump() {
        reader.read().then(({ done, value }) => {
          if (done) return;
          buffer += decoder.decode(value, { stream: true });
          const chunks = buffer.split('\n\n');
          buffer = chunks.pop();

          for (const chunk of chunks) {
            const data = chunk.replace(/^data: /, '');
            if (data === '[DONE]') return;
            accumulated += data.replace(/\\n/g, '\n');
            claudeText.textContent = accumulated;
          }
          pump();
        }).catch(() => {});
      }
      pump();
    }).catch(() => {});
  }

  // ── Run Anyway ────────────────────────────────────────────────────────

  function _runAnyway() {
    if (!_fileData) return;
    // Propose a start_job action through the approval gate
    fetch('/api/jobs/propose', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action: 'start_job',
        parameters: { filepath: _fileData.filepath },
        source: 'review_panel',
      }),
    })
      .then(r => r.json())
      .then(pending => {
        close();
        ApprovalGate.show(pending);
      })
      .catch(() => {});
  }

  // ── Button wiring ──────────────────────────────────────────────────────

  function _wireButtons() {
    document.getElementById('review-panel-close').onclick = close;
    document.getElementById('review-btn-full').onclick    = _streamFullReview;
    document.getElementById('review-btn-ask').onclick     = _toggleAskBox;
    document.getElementById('review-btn-dismiss').onclick = close;
    document.getElementById('review-btn-run').onclick     = _runAnyway;
    document.getElementById('review-ask-send').onclick    = _sendAsk;
  }

  // ── Helpers ────────────────────────────────────────────────────────────

  function _cancelStream() {
    if (_reviewStream) {
      _reviewStream.close();
      _reviewStream = null;
    }
  }

  function _typeLabel(type) {
    const labels = {
      feed_rate:       '⚡ Feed Rate',
      air_cut:         '🌬 Air Cut',
      syntax:          '🔴 Syntax',
      missing_spindle: '⚠ Spindle',
    };
    return labels[type] || type;
  }

  function _escape(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  return { open, close };
})();
