// Sunday — renderer. One window, three tabs. iMessage-style thread with a
// composer pinned to the bottom; messages append incrementally so the
// input never jumps. Memory + settings live in sibling tabs.

import * as memoryView from './memory-view.js';
import * as settingsView from './settings-view.js';
import * as timelineView from './timeline-view.js';
import * as inboxView from './inbox-view.js';

// Renderer error bridge — funnel UI crashes into the shareable app.log so a
// broken window leaves a trail (main-process errors already log; this covers here).
window.addEventListener('error', (e) => {
  window.sunday?.logError?.(`RENDERER_ERROR: ${(e.error && e.error.stack) || e.message || (e.filename + ':' + e.lineno)}`);
});
window.addEventListener('unhandledrejection', (e) => {
  window.sunday?.logError?.(`RENDERER_REJECTION: ${(e.reason && e.reason.stack) || e.reason}`);
});

const $ = (s) => document.querySelector(s);
const chatEl     = $('#chat');
const composerEl = $('#composer');
const sendBtn    = $('#send-btn');
const attachBtn  = $('#attach-btn');
const connBtn    = $('#connectors-btn');
const fileInput  = $('#file-input');
const attachEl   = $('#attachments');
const statusEl   = $('#status');
const brandDot   = document.querySelector('.brand-dot');
const dropzoneEl = $('#dropzone');
const jumpBtn    = $('#jump-btn');

let DAEMON_HTTP = 'http://127.0.0.1:8765';
let DAEMON_WS   = 'ws://127.0.0.1:8765/v1/ws';
let DAEMON_TOKEN = '';

// One-time global fetch wrapper — every call to the daemon (regardless of
// who in the renderer makes it) gets Authorization: Bearer <token> attached
// automatically. Calls to other hosts (Hugging Face for the whisper model,
// Nango, etc.) pass through untouched.
(function installAuthFetch() {
  const orig = window.fetch.bind(window);
  window.fetch = (input, init) => {
    if (!DAEMON_TOKEN) return orig(input, init);
    const url = typeof input === 'string' ? input : (input?.url || '');
    if (!url.startsWith(DAEMON_HTTP)) return orig(input, init);
    const headers = new Headers((init && init.headers) || (input && input.headers) || {});
    if (!headers.has('Authorization')) headers.set('Authorization', `Bearer ${DAEMON_TOKEN}`);
    return orig(input, { ...(init || {}), headers });
  };
})();
let ws = null;
let pending = [];
const renderedIds = new Set();
let lastUserTs = null;
let bootedChat = false;
let currentView = 'chat';

// ─── threads (Slack-style reply branches) ────────────────────────────────
// {root_message_id: reply_count} — refreshed with the main log, drives the
// "N replies" badge under each rooted message.
let replyCounts = {};
// The thread currently open in the full thread view, or null. Holds the thread
// id and the set of reply ids already rendered (so a refresh appends, not
// duplicates). When set, the thread view has taken over the main area.
let openThread = null;
const threadLogEl   = $('#thread-log');
const threadComposer = $('#thread-composer');
const threadSendBtn = $('#thread-send-btn');
const threadSubEl   = $('#thread-panel-sub');
const threadJumpBtn = $('#thread-jump-btn');

// ─── boot ──────────────────────────────────────────────────────────────
async function boot() {
  if (window.sunday) {
    const cfg = await window.sunday.getConfig();
    DAEMON_HTTP  = cfg.daemonHttp || DAEMON_HTTP;
    DAEMON_WS    = cfg.daemonWs   || DAEMON_WS;
    DAEMON_TOKEN = cfg.daemonToken || '';
  }
  wireBootGate();
  await waitForDaemon();   // shows "Starting Sunday…", then runs startApp() once healthy
}

// Poll the local daemon's health before loading anything. This is the fix for
// "nothing works on first launch": the UI used to fire requests while the daemon
// was still booting (dead socket → errors everywhere). Now it waits, with a clear
// failure path if the brain never comes up.
async function waitForDaemon() {
  const overlay = $('#boot-overlay');
  if (overlay) { overlay.hidden = false; overlay.classList.remove('failed'); }
  if ($('#boot-actions')) $('#boot-actions').hidden = true;
  if ($('#boot-logsview')) $('#boot-logsview').hidden = true;
  if ($('#boot-spinner')) $('#boot-spinner').hidden = false;
  if ($('#boot-title')) $('#boot-title').textContent = 'Starting Sunday…';
  if ($('#boot-sub')) $('#boot-sub').textContent = 'Waking the local brain. First launch takes a few seconds.';

  const deadline = Date.now() + 25000;
  const check = async () => {
    let healthy = false;
    try {
      if (window.sunday?.daemonHealth) healthy = (await window.sunday.daemonHealth()).healthy;
      else healthy = (await fetch(`${DAEMON_HTTP}/v1/health`)).ok;
    } catch { healthy = false; }
    if (healthy) { if (overlay) overlay.hidden = true; await startApp(); return; }
    if (Date.now() > deadline) return showBootFailed();
    setTimeout(check, 300);   // tight poll so we grab the daemon the instant it's up
  };
  check();
}

function showBootFailed() {
  const o = $('#boot-overlay'); if (o) o.classList.add('failed');
  if ($('#boot-spinner')) $('#boot-spinner').hidden = true;
  if ($('#boot-title')) $('#boot-title').textContent = 'Sunday couldn’t start';
  if ($('#boot-sub')) $('#boot-sub').textContent = 'The local brain didn’t come up. Retry, check the logs, or reset setup.';
  if ($('#boot-actions')) $('#boot-actions').hidden = false;
}

function wireBootGate() {
  $('#boot-retry')?.addEventListener('click', () => waitForDaemon());
  $('#boot-reset')?.addEventListener('click', () => window.sunday?.resetApp?.());
  $('#boot-logs')?.addEventListener('click', async () => {
    const view = $('#boot-logsview');
    if (!view) return;
    if (!view.hidden) { window.sunday?.revealLogs?.(); return; }   // 2nd click reveals the file
    const text = (await window.sunday?.readLogs?.()) || '';
    view.textContent = text.trim() || 'No logs yet.';
    view.hidden = false;
    $('#boot-logs').textContent = 'Open log file';
  });
}

let _appStarted = false;
async function startApp() {
  if (_appStarted) return;          // health check can fire twice; init once
  _appStarted = true;
  memoryView.init({ daemonHttp: DAEMON_HTTP }, {});
  settingsView.init(DAEMON_HTTP);
  timelineView.init({ daemonHttp: DAEMON_HTTP }, {
    modes: $('#tl-modes'), wrappedPeriod: $('#tl-periods'), search: $('#tl-search'),
    main: $('#tl-main'), detail: $('#tl-detail'),
    empty: $('#tl-empty'), emptyTitle: $('#tl-empty-title'), emptySub: $('#tl-empty-sub'), enable: $('#tl-enable'),
  });
  inboxView.init({ daemonHttp: DAEMON_HTTP }, {
    list: $('#inbox-list'), rows: $('#inbox-rows'), refresh: $('#inbox-refresh'),
    filter: $('#inbox-filter'),
    empty: $('#inbox-empty'), error: $('#inbox-error'), errorSub: $('#inbox-error-sub'),
    detail: $('#inbox-detail'), back: $('#inbox-back'),
    detailTo: $('#inbox-detail-to'), detailMeta: $('#inbox-detail-meta'),
    voiceDetail: $('#inbox-voice-detail'), threadWrap: $('#inbox-thread-wrap'), thread: $('#inbox-thread'),
    audio: $('#inbox-audio'), noAudio: $('#inbox-no-audio'),
    summaryWrap: $('#inbox-summary-wrap'), summary: $('#inbox-summary'), transcript: $('#inbox-transcript'),
  });
  renderSkeleton();
  await refreshLog();
  await refreshStatus();
  connectWs();
}

function setOnline(state) {
  brandDot.dataset.state = state;
  brandDot.title = { online: 'connected', connecting: 'connecting…', offline: 'daemon unreachable' }[state] || state;
  if (state === 'connecting') statusEl.textContent = 'connecting…';
  if (state === 'offline')    statusEl.textContent = 'offline';
  window.sunday?.setOverlayState({ connection: state });
}

async function refreshStatus() {
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/status`);
    if (!res.ok) throw new Error();
    const d = await res.json();
    statusEl.textContent = `${(d.model || '').split('/').slice(-1)[0]} · ${d.messages} msgs`;
    setOnline('online');
  } catch { setOnline('offline'); }
}

// ─── chat: incremental rendering ─────────────────────────────────────────
async function refreshLog() {
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/log?limit=200`);
    const data = await res.json();
    const msgs = data.messages || [];
    replyCounts = data.reply_counts || {};
    // drop transient placeholders; the real rows are about to land
    chatEl.querySelectorAll('.pending, .stream-temp').forEach((n) => n.remove());
    const atBottom = nearBottom();
    removeLogError();
    if (!msgs.length && renderedIds.size === 0) { renderEmptyState(); bootedChat = true; return; }
    removeSkeleton();
    removeEmpty();
    for (const m of msgs) {
      // Email is QUIET: never render email_agentmail:*-modality turns in the
      // main timeline on load (they live in the Inbox). notify_user posts under
      // a normal modality, so it still shows. Texting/normal chat untouched.
      if (isEmailModality(m.modality)) continue;
      if (typeof m.id === 'number' && renderedIds.has(m.id)) {
        if (m.role === 'user') lastUserTs = m.created_at;
        continue;
      }
      appendMessage(m);
      if (typeof m.id === 'number') renderedIds.add(m.id);
    }
    syncThreadBadges();
    if (!bootedChat || atBottom) scrollToEnd(true);
    bootedChat = true;
    // If a thread is open, keep its replies fresh too (a reply just landed).
    if (openThread) loadThread(openThread.id, { quiet: true });
  } catch (err) {
    console.warn('log fetch failed', err);
    // On the FIRST load, a failed fetch leaves the skeleton shimmering forever
    // (the user thinks it's still loading). Replace it with an explicit failure
    // + retry. On later refreshes we already have content rendered, so leave it.
    if (!bootedChat) renderLogError();
  }
}

function renderLogError() {
  removeSkeleton();
  if (chatEl.querySelector('.log-error')) return;
  const w = document.createElement('div');
  w.className = 'log-error';
  w.innerHTML = `<h2>Couldn't reach Sunday</h2><p>The daemon didn't answer. Check that it's running, then try again.</p>`;
  const b = document.createElement('button');
  b.className = 'btn btn-primary';
  b.textContent = 'Retry';
  b.onclick = () => { w.remove(); renderSkeleton(); refreshLog(); refreshStatus(); };
  w.appendChild(b);
  chatEl.appendChild(w);
}
function removeLogError() { chatEl.querySelector('.log-error')?.remove(); }

function connectWs() {
  // The daemon authenticates the WS via ?token= (browsers can't set the
  // Authorization header on a WebSocket handshake).
  const wsUrl = DAEMON_TOKEN ? `${DAEMON_WS}${DAEMON_WS.includes('?') ? '&' : '?'}token=${encodeURIComponent(DAEMON_TOKEN)}` : DAEMON_WS;
  try { ws = new WebSocket(wsUrl); } catch { setOnline('offline'); return; }
  ws.onopen = () => setOnline('online');
  ws.onclose = () => { setOnline('offline'); setTimeout(connectWs, 2000); };
  ws.onerror = () => setOnline('offline');
  ws.onmessage = (e) => { try { handleWs(JSON.parse(e.data)); } catch {} };
}

let stream = null;
// Stream ids that belong to a thread turn — their deltas must NOT render into
// the main chat. We track them so a thread reply streams quietly (the panel
// pulls the finished replies on stream_end) instead of leaking a bubble into
// the main timeline.
const threadStreams = new Set();
// Email is handled QUIETLY: email-driven brain turns carry an
// `email_agentmail:*` modality and must NEVER render in the main chat (email
// lives in the Inbox). Suppress those stream/reply events here, the same way
// thread streams are suppressed above. `notify_user` posts under a normal
// modality (not email_agentmail), so it still shows. Texting (imessage_*) and
// normal chat are unaffected — only this exact prefix is hidden.
function isEmailModality(modality) {
  return typeof modality === 'string' && modality.startsWith('email_agentmail');
}

function handleWs(ev) {
  // Drop main-chat rendering of email-modality stream/reply events outright.
  // These event types all carry `modality`; non-stream events (inbox, device_*)
  // don't, so the guard simply doesn't match them.
  if (isEmailModality(ev.modality)) return;
  switch (ev.type) {
    case 'stream_start':
      // A thread turn streams into the panel, never the main chat. We render the
      // finished replies on stream_end (loadThread) rather than live-splicing
      // the main-chat stream UI, so the main timeline stays clean.
      if (ev.thread_id != null) { threadStreams.add(ev.stream_id); showThreadThinking(true); return; }
      stream = beginStream(ev); showStop(true); return;
    case 'reasoning_delta':
      if (threadStreams.has(ev.stream_id)) return;
      if (stream && stream.id === ev.stream_id) { stream.reason += (ev.content || ''); showThinking(stream); autoScroll(); }
      return;
    case 'stream_delta':
      if (threadStreams.has(ev.stream_id)) return;
      if (stream && stream.id === ev.stream_id) { stream.raw += (ev.content || ''); showText(stream); autoScroll(); }
      return;
    case 'tool_call':
      if (threadStreams.has(ev.stream_id)) return;
      if (stream && stream.id === ev.stream_id) addToolRow(stream, ev); return;
    case 'tool_result':
      if (threadStreams.has(ev.stream_id)) return;
      if (stream && stream.id === ev.stream_id) finishToolRow(stream, ev); return;
    case 'stream_end':
      if (threadStreams.has(ev.stream_id)) {
        threadStreams.delete(ev.stream_id);
        showThreadThinking(false);
        if (openThread) loadThread(openThread.id, { quiet: true });
        // the badge count on the main timeline may have changed
        refreshLog();
        return;
      }
      // Only tear down if this end belongs to the stream we're showing. A
      // stale end for a prior stream (id mismatch) must NOT null out the live
      // stream, hide Stop, or refreshLog (which would rip down the active
      // thinking block and drop the in-flight bubble).
      if (!stream || stream.id !== ev.stream_id) return;
      stream.el.classList.remove('streaming');
      stream = null; showStop(false); refreshLog(); refreshStatus(); return;
    case 'inbox':
      // A text/email/voice message was sent or received. Live-refresh the Inbox
      // list — but ONLY when the Inbox is the active view, so we never fetch in
      // the background while the user is in chat/settings/etc.
      if (currentView === 'inbox') inboxView.refresh();
      return;
    case 'thread_created': refreshLog(); return;
    case 'cleared': if (openThread) closeThreadView(); return;
    case 'reply': if (!stream && ev.thread_id == null) refreshLog(); return;
    case 'interjection':
      // A proactive note Sunday surfaced unprompted (e.g. a time-gap check-in).
      // It's already folded into the chat server-side, so pull it in; if the
      // daemon flagged notify, raise a real desktop notification too.
      if (!stream) refreshLog();
      if (ev.notify && ev.text) showDesktopNotification(ev.notify_title || 'Sunday', ev.text);
      return;
    case 'browser_frame': case 'device_browser_frame': case 'device_screen': showLiveFrame(ev); return;
    case 'device_online': case 'device_offline': refreshStatus(); return;
  }
}

// macOS desktop notification via the renderer's native Notification API
// (Electron maps this to a real system notification). Best-effort — asks once
// for permission, swallows any failure so a missing entitlement can't break
// the chat. Clicking the notification focuses the Sunday window.
function showDesktopNotification(title, body) {
  try {
    const fire = () => {
      const n = new Notification(title, { body, silent: false });
      n.onclick = () => { try { window.focus(); } catch {} };
    };
    if (Notification.permission === 'granted') { fire(); return; }
    if (Notification.permission !== 'denied') {
      Notification.requestPermission().then((p) => { if (p === 'granted') fire(); }).catch(() => {});
    }
  } catch {}
}

function beginStream(ev) {
  removeEmpty();
  const el = document.createElement('div');
  el.className = 'msg sunday streaming stream-temp';

  // Live thinking block (hidden until reasoning tokens arrive).
  const thinking = document.createElement('details');
  thinking.className = 'reasoning live'; thinking.open = true; thinking.hidden = true;
  thinking.innerHTML = '<summary><span class="spin"></span>thinking…</summary><div class="r-body"></div>';

  // Live tool rows (hidden until the first tool call).
  const toolsWrap = document.createElement('div');
  toolsWrap.className = 'live-tools'; toolsWrap.hidden = true;

  // Streaming text bubble (hidden until the first content token).
  const bubble = document.createElement('div');
  bubble.className = 'bubble'; bubble.hidden = true;

  el.append(thinking, toolsWrap, bubble);
  placeRow(el, 'assistant');
  autoScroll(true);
  return { id: ev.stream_id, el, thinking, thinkingBody: thinking.querySelector('.r-body'),
           toolsWrap, body: bubble, raw: '', reason: '', tools: {} };
}

function showThinking(s) { s.thinking.hidden = false; s.thinkingBody.textContent = s.reason; }
function showText(s) { s.body.hidden = false; s.body.textContent = s.raw; }

// One self-replacing line — shows the CURRENT tool + a running count, so it
// never grows into a stack. "⚙ device_run_command  ssh root@…  · 12"
function addToolRow(s, ev) {
  s.toolsWrap.hidden = false;
  s.thinking.querySelector('.spin')?.classList.add('done');  // stop the thinking spinner once acting
  s.toolCount = (s.toolCount || 0) + 1;
  let row = s.toolLine;
  if (!row) {
    row = document.createElement('div');
    row.className = 'live-tool';
    row.innerHTML = '<span class="spin"></span><span class="t-name"></span><span class="t-arg"></span><span class="t-count"></span>';
    s.toolsWrap.innerHTML = '';
    s.toolsWrap.appendChild(row);
    s.toolLine = row;
  }
  row.querySelector('.spin').classList.remove('done');
  let arg = '';
  try { const a = JSON.parse(ev.args_preview || '{}'); arg = Object.values(a)[0] || ''; } catch { arg = ev.args_preview || ''; }
  row.querySelector('.t-name').textContent = ev.tool_name;
  row.querySelector('.t-arg').textContent = String(arg).slice(0, 52);
  row.querySelector('.t-count').textContent = s.toolCount > 1 ? `· ${s.toolCount}` : '';
}

function finishToolRow(s, ev) {
  s.toolLine?.querySelector('.spin')?.classList.add('done');
}

// Append a top-level row, inserting a turn break only when the speaker side
// changes — so all of one turn's activity (thinking, tools, text) clusters.
function placeRow(el, side) {
  const prevSide = chatEl.lastElementChild?.dataset?.side;
  if (prevSide && prevSide !== side) el.classList.add('turn-gap');
  el.dataset.side = side;
  chatEl.appendChild(el);
}

// Stored tool results collapse into one expandable group per run ("N tool
// calls ▸") instead of a row each — so a 12-tool turn is one quiet line.
function appendToolToGroup(m) {
  let group = chatEl.lastElementChild;
  if (!group || !group.classList.contains('tool-group')) {
    group = document.createElement('details');
    group.className = 'tool-group';
    group.dataset.count = '0';
    group.innerHTML = '<summary><span class="t-dot"></span><span class="tg-label"></span>'
      + '<svg class="chev" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg></summary>'
      + '<div class="tg-body"></div>';
    placeRow(group, 'assistant');
  }
  const n = (parseInt(group.dataset.count, 10) || 0) + 1;
  group.dataset.count = String(n);
  group.querySelector('.tg-label').textContent = `${n} tool call${n > 1 ? 's' : ''}`;
  group.querySelector('.tg-body').appendChild(buildToolRow(m));
}

function appendMessage(m) {
  const role = m.role;
  if (role === 'tool')   { appendToolToGroup(m); return; }
  if (role === 'system') { placeRow(buildSystemRow(m), 'assistant'); return; }

  const side = role === 'user' ? 'user' : 'assistant';
  const content = (m.content || '').trim();
  const atts = (m.metadata && m.metadata.attachments) || [];
  const reasoning = (role === 'sunday' && m.metadata && m.metadata.reasoning_content || '').trim();

  // reasoning leads the bubble (it happened first)
  if (reasoning) {
    const det = document.createElement('details');
    det.className = 'reasoning';
    det.innerHTML = `<summary><svg class="chev" viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>thinking</summary><div class="r-body"></div>`;
    det.querySelector('.r-body').textContent = reasoning;
    placeRow(det, side);
  }

  // No text and no attachments → this was a tool-only step; don't draw an
  // empty bubble. (Reasoning, if any, already rendered above.)
  if (!content && !atts.length) { if (role === 'user') lastUserTs = m.created_at; return; }

  const wrap = document.createElement('div');
  wrap.className = `msg ${role}`;
  if (typeof m.id === 'number') wrap.dataset.mid = m.id;

  if (content) {
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.innerHTML = mdLite(m.content || '');
    const copy = document.createElement('button');
    copy.className = 'bubble-copy'; copy.title = 'Copy'; copy.setAttribute('aria-label', 'Copy');
    copy.innerHTML = copyIcon();
    copy.onclick = () => navigator.clipboard.writeText(m.content || '').then(() => { copy.innerHTML = checkIcon(); setTimeout(() => copy.innerHTML = copyIcon(), 1300); });
    bubble.appendChild(copy);
    // Edit + rewind, ChatGPT-style — your messages only, and only real
    // (persisted) ones with an id, not the optimistic pending bubble.
    if (role === 'user' && typeof m.id === 'number') {
      const edit = document.createElement('button');
      edit.className = 'bubble-edit'; edit.title = 'Edit'; edit.setAttribute('aria-label', 'Edit');
      edit.innerHTML = editIcon();
      edit.onclick = () => beginEdit(wrap, m);
      bubble.appendChild(edit);
    }
    // Reply in thread — branch a Slack-style side discussion off any real,
    // main-timeline message (never off a thread reply, and not the pending bubble).
    if (typeof m.id === 'number' && !m.thread_id) {
      const th = document.createElement('button');
      th.className = 'bubble-thread'; th.title = 'Reply in thread'; th.setAttribute('aria-label', 'Reply in thread');
      th.innerHTML = threadIcon();
      th.onclick = (e) => { e.stopPropagation(); openThreadForMessage(m.id); };
      bubble.appendChild(th);
    }
    wrap.appendChild(bubble);
  }

  if (atts.length) {
    const aw = document.createElement('div'); aw.className = 'msg-attachments';
    for (const a of atts) aw.appendChild(buildAttachment(a));
    wrap.appendChild(aw);
  }

  // Resting meta: one subtle relative timestamp ("6h ago"). The exact clock
  // time and the generation latency live in its title tooltip — no clutter at
  // rest, full detail on hover.
  if (m.created_at) {
    let tip = fmtFull(m.created_at);
    if (role === 'sunday' && lastUserTs && m.created_at > lastUserTs) tip += ` · generated in ${fmtDur(m.created_at - lastUserTs)}`;
    const meta = document.createElement('div'); meta.className = 'msg-meta';
    meta.innerHTML = `<span class="time" title="${esc(tip)}">${esc(fmtRel(m.created_at))}</span>`;
    wrap.appendChild(meta);
  }

  placeRow(wrap, side);

  // "N replies" badge if this message roots a thread (rendered/updated by
  // syncThreadBadges, which reads replyCounts after the log lands).
  if (typeof m.id === 'number') updateThreadBadge(wrap, m.id);

  if (role === 'user') lastUserTs = m.created_at;
}

// ─── thread badges on the main timeline ──────────────────────────────────
function updateThreadBadge(wrap, mid) {
  const n = replyCounts[String(mid)];
  let badge = wrap.querySelector(':scope > .thread-badge');
  if (!n) { badge?.remove(); return; }
  if (!badge) {
    badge = document.createElement('button');
    badge.className = 'thread-badge';
    badge.onclick = (e) => { e.stopPropagation(); openThreadForMessage(mid); };
    wrap.appendChild(badge);
  }
  badge.innerHTML = `<svg class="tb-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>`
    + `<span>${n} ${n === 1 ? 'reply' : 'replies'}</span>`;
}

// Re-badge every rendered main-timeline message after a log refresh — a new
// thread may have been created, or a reply count changed.
function syncThreadBadges() {
  chatEl.querySelectorAll('.msg[data-mid]').forEach((wrap) => {
    const mid = wrap.dataset.mid;
    if (mid) updateThreadBadge(wrap, Number(mid));
  });
}

// ─── thread view: open, render, send ──────────────────────────────────────
// Opening a thread REPLACES the main chat with a full thread view — root anchor
// at top, its replies below, and a scoped composer at the bottom. A back button
// (or Esc) restores the main timeline. The view follows the same .view/.active
// pattern as Chat/Calls/etc., so it's fully hidden until a thread is opened —
// nothing renders or intercepts in the default state.

// Open the thread rooted at a main-timeline message — creating it first if one
// doesn't exist yet (idempotent server-side), then loading + showing the view.
async function openThreadForMessage(rootId) {
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/threads`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ root_message_id: rootId }),
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok || !d.thread) { console.warn('create thread failed', d.error || res.status); return; }
    openThreadView(d.thread.id);
  } catch (err) { console.warn('open thread failed', err); }
}

function openThreadView(threadId) {
  openThread = { id: threadId, renderedIds: new Set() };
  threadLogEl.innerHTML = '';
  threadSubEl.textContent = '';
  threadComposer.value = '';
  resizeThreadComposer();
  showThreadStop(false);
  // Loading cue until the first render lands.
  const loading = document.createElement('div');
  loading.className = 'thread-empty thread-loading'; loading.textContent = 'Loading thread…';
  threadLogEl.appendChild(loading);
  // Hand off to the view switcher — hides chat, shows the thread view.
  showThreadView();
  loadThread(threadId);
  updateThreadSend();
  requestAnimationFrame(() => threadComposer.focus());
}

// Reveal the full thread view, hiding whatever view was active. Mirrors the
// .tab/.view pattern but the thread view has no tab — it's entered from a
// message, left via back.
function showThreadView() {
  currentView = 'thread';   // not a tab; disables chat-only affordances (drag-drop)
  document.querySelectorAll('.view').forEach((v) => v.classList.toggle('active', v.id === 'view-thread'));
  document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
}

// Back to the main timeline. Tears down thread state, restores the chat view,
// and re-focuses + reconciles the main composer/scroll.
function closeThreadView() {
  openThread = null;
  threadComposer.value = '';
  showThreadStop(false);
  switchView('chat');
}

async function loadThread(threadId, opts = {}) {
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/threads/${threadId}`);
    const d = await res.json().catch(() => ({}));
    if (!res.ok) { console.warn('load thread failed', d.error || res.status); return; }
    if (!openThread || openThread.id !== threadId) return;   // panel closed/switched mid-flight
    renderThread(d, opts);
  } catch (err) { console.warn('load thread failed', err); }
}

function renderThread(d, opts = {}) {
  const root = d.root;
  const replies = d.messages || [];
  const sub = (root && (root.content || '').trim().slice(0, 60)) || '';
  threadSubEl.textContent = sub;

  // Clear the initial "Loading thread…" cue once the first payload lands.
  threadLogEl.querySelector('.thread-loading')?.remove();

  // The root anchor is rendered once at the top; replies append incrementally.
  const atBottom = threadLogEl.scrollHeight - threadLogEl.scrollTop - threadLogEl.clientHeight < 90;
  if (!threadLogEl.querySelector('.thread-root') && root) {
    threadLogEl.querySelectorAll('.pending').forEach((n) => n.remove());
    const anchor = document.createElement('div');
    anchor.className = 'thread-root';
    const who = root.role === 'user' ? 'You' : 'Sunday';
    anchor.innerHTML = `<div class="thread-root-label">Replying to</div>`
      + `<div class="thread-root-body"><span class="thread-root-who">${esc(who)}: </span>${mdLite(root.content || '')}</div>`;
    threadLogEl.appendChild(anchor);
    const div = document.createElement('div');
    div.className = 'thread-divider';
    div.textContent = 'Thread';
    threadLogEl.appendChild(div);
  }
  if (!replies.length && !threadLogEl.querySelector('.thread-empty') && !threadLogEl.querySelector('.thread-msg-row')) {
    const e = document.createElement('div'); e.className = 'thread-empty';
    e.textContent = 'No replies yet. Start the side discussion here.';
    threadLogEl.appendChild(e);
  }
  for (const m of replies) {
    if (typeof m.id === 'number' && openThread.renderedIds.has(m.id)) continue;
    threadLogEl.querySelector('.thread-empty')?.remove();
    threadLogEl.querySelectorAll('.pending').forEach((n) => n.remove());
    appendThreadMessage(m);
    if (typeof m.id === 'number') openThread.renderedIds.add(m.id);
  }
  if (!opts.quiet || atBottom) threadLogEl.scrollTop = threadLogEl.scrollHeight;
}

// A thread reply bubble — same look as the main thread, scoped to the panel.
function appendThreadMessage(m) {
  const role = m.role;
  if (role === 'tool') return;        // tool steps stay quiet in the panel
  const content = (m.content || '').trim();
  if (role === 'system') {
    const s = document.createElement('div'); s.className = 'msg system thread-msg-row';
    const p = document.createElement('div'); p.className = 'sys-msg'; p.textContent = content;
    s.appendChild(p); threadLogEl.appendChild(s); return;
  }
  if (!content) return;
  const wrap = document.createElement('div');
  wrap.className = `msg ${role} thread-msg-row`;
  if (typeof m.id === 'number') wrap.dataset.mid = m.id;
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.innerHTML = mdLite(m.content || '');
  wrap.appendChild(bubble);
  threadLogEl.appendChild(wrap);
}

async function sendThread() {
  if (!openThread) return;
  const text = threadComposer.value.trim();
  if (!text) return;
  const threadId = openThread.id;
  // optimistic bubble
  const w = document.createElement('div'); w.className = 'msg user pending thread-msg-row';
  const b = document.createElement('div'); b.className = 'bubble'; b.innerHTML = mdLite(text); w.appendChild(b);
  threadLogEl.querySelector('.thread-empty')?.remove();
  threadLogEl.appendChild(w);
  threadScrollToEnd();
  threadComposer.value = ''; updateThreadSend(); resizeThreadComposer();
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/threads/${threadId}/say`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, modality: 'electron' }),
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) { w.classList.remove('pending'); w.classList.add('failed'); w.title = `Couldn't send: ${d.error || res.status}`; return; }
    // Sunday's reply streams over the WS into the thread view; stream_end refreshes.
    setTimeout(() => loadThread(threadId, { quiet: true }), 50);
  } catch (err) {
    w.classList.remove('pending'); w.classList.add('failed'); w.title = `Couldn't send: ${err.message}`;
  }
}

function updateThreadSend() { threadSendBtn.disabled = !threadComposer.value.trim(); }
function resizeThreadComposer() { threadComposer.style.height = 'auto'; threadComposer.style.height = Math.min(threadComposer.scrollHeight, 160) + 'px'; }

// Thread-view scroll helpers — mirror the main timeline's jump pill behaviour.
function threadNearBottom() { return threadLogEl.scrollHeight - threadLogEl.scrollTop - threadLogEl.clientHeight < 90; }
function threadScrollToEnd() { threadLogEl.scrollTop = threadLogEl.scrollHeight; if (threadJumpBtn) threadJumpBtn.hidden = true; }

// Stop the running thread turn. Shown only while a thread turn streams.
const threadStopBtn = $('#thread-stop-btn');
function showThreadStop(on) { if (threadStopBtn) threadStopBtn.hidden = !on; }
threadStopBtn?.addEventListener('click', async () => {
  threadStopBtn.disabled = true;
  try { await fetch(`${DAEMON_HTTP}/v1/task/stop`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }); }
  catch {}
  finally { threadStopBtn.disabled = false; }
});

// A quiet "thinking…" line at the bottom of the thread view while a thread turn
// streams (its tokens render only on stream_end, so this is the live cue). Also
// reveals the Stop pill. No-op if no thread is open.
function showThreadThinking(on) {
  if (!openThread) return;
  showThreadStop(on);
  let el = threadLogEl.querySelector('.thread-thinking');
  if (!on) { el?.remove(); return; }
  if (!el) {
    el = document.createElement('div');
    el.className = 'reasoning live thread-thinking';
    el.innerHTML = '<summary><span class="spin"></span>thinking…</summary>';
    threadLogEl.appendChild(el);
    threadScrollToEnd();
  }
}

$('#thread-back')?.addEventListener('click', closeThreadView);
threadSendBtn?.addEventListener('click', sendThread);
threadComposer?.addEventListener('input', () => { resizeThreadComposer(); updateThreadSend(); });
threadComposer?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendThread(); }
});
threadLogEl?.addEventListener('scroll', () => { if (threadJumpBtn) threadJumpBtn.hidden = threadNearBottom(); });
threadJumpBtn?.addEventListener('click', threadScrollToEnd);
// Open-thread links inside the thread view (mdLite turns URLs into <a>) go to the OS browser.
threadLogEl?.addEventListener('click', (e) => {
  const a = e.target.closest && e.target.closest('a[href]');
  if (!a) return;
  const href = a.getAttribute('href') || '';
  if (!/^https?:\/\//i.test(href)) return;
  e.preventDefault();
  window.sunday?.openExternal(href);
});

function buildToolRow(m) {
  const row = document.createElement('div');
  row.className = 'msg tool';
  const det = document.createElement('details');
  det.className = 'tool-card';
  let name = m.metadata?.tool_name || '';
  let pretty = m.content || '';
  try { const o = JSON.parse(pretty); pretty = JSON.stringify(o, null, 2); if (!name && o.tool) name = o.tool; } catch {}
  name = name || 'tool';
  const lines = pretty.split('\n').length;
  det.innerHTML = `<summary><span class="t-dot"></span><span class="t-name">${esc(name)}</span><span class="t-hint">${lines > 1 ? lines + ' lines' : 'result'}${m.created_at ? ' · ' + esc(fmtClock(m.created_at)) : ''}</span><svg class="chev" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg></summary><div class="t-body"></div>`;
  det.querySelector('.t-body').textContent = pretty;
  row.appendChild(det);
  return row;
}
function buildSystemRow(m) {
  const row = document.createElement('div');
  row.className = 'msg system';
  const p = document.createElement('div');
  const raw = m.content || '';
  p.className = 'sys-msg' + (/error|failed/i.test(raw) ? ' is-error' : '');
  p.textContent = friendlyError(raw);
  row.appendChild(p);
  return row;
}

// Turn raw provider errors into plain language.
function friendlyError(s) {
  if (/support image input|image input|vision/i.test(s) && /404|no endpoints/i.test(s)) {
    return "This model can't look at images. Switch to a vision model in Settings → Model (or send text instead).";
  }
  if (/rate.?limit|429/i.test(s)) return 'The model is rate-limited right now — give it a moment and try again.';
  if (/402|credit|quota|insufficient/i.test(s)) return 'The model provider is out of credit. Check your provider key in Settings.';
  return s;
}
function buildAttachment(a) {
  const kind = a.kind || guessKind(a.mime_type || '');
  if (kind === 'image') { const img = document.createElement('img'); img.src = a.url || (a.path ? `file://${a.path}` : ''); img.alt = a.filename || 'image'; return img; }
  const c = document.createElement('div'); c.className = 'att';
  c.textContent = `${a.filename || 'attachment'} · ${a.size ? Math.round(a.size / 1024) + 'kb · ' : ''}${a.mime_type || ''}`;
  return c;
}
function guessKind(m) { if (m.startsWith('image/')) return 'image'; if (m.startsWith('audio/')) return 'audio'; if (m.startsWith('video/')) return 'video'; return 'file'; }

function renderEmptyState() {
  removeSkeleton();
  if (chatEl.querySelector('.empty-state')) return;
  const sug = ["What's on my screen?", 'What do you know about me?', 'Catch me up on my texts'];
  const w = document.createElement('div'); w.className = 'empty-state';
  w.innerHTML = `<div class="empty-sun"></div><h2>Good to see you</h2><p>Ask me anything, or pick up where we left off. I keep what matters and forget the noise.</p><div class="empty-suggest"></div>`;
  const box = w.querySelector('.empty-suggest');
  for (const s of sug) { const b = document.createElement('button'); b.textContent = s; b.onclick = () => { composerEl.value = s; updateSend(); send(); }; box.appendChild(b); }
  chatEl.appendChild(w);
}
function removeEmpty() { chatEl.querySelector('.empty-state')?.remove(); }
function renderSkeleton() {
  chatEl.innerHTML = '';
  const rows = [['left', 60], ['right', 40], ['left', 72]];
  for (const [side, w] of rows) { const s = document.createElement('div'); s.className = `skeleton ${side}`; s.innerHTML = `<div class="sk-bubble" style="width:${w}%"></div>`; chatEl.appendChild(s); }
}
function removeSkeleton() { chatEl.querySelectorAll('.skeleton').forEach((n) => n.remove()); }

// markdown-lite
function esc(s) { return String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
function mdLite(raw) {
  let s = esc(raw);
  s = s.replace(/```([\s\S]*?)```/g, (_m, c) => `<pre><code>${c.replace(/^\n/, '')}</code></pre>`);
  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2">$1</a>');
  s = s.replace(/(^|[^"=>])(https?:\/\/[^\s<]+)/g, '$1<a href="$2">$2</a>');
  return s;
}
function copyIcon() { return `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>`; }
function checkIcon() { return `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="var(--ok)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>`; }
function editIcon() { return `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>`; }
function threadIcon() { return `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>`; }

// ── edit + rewind ───────────────────────────────────────────────────────────
// Edit a previous user message; on save, the daemon drops that message and
// everything after it and re-runs from the new text — the conversation branches
// from there. Mirrors ChatGPT/Claude. One edit at a time.
let editingMid = null;
function beginEdit(wrap, m) {
  if (editingMid !== null) return;            // already editing something
  editingMid = m.id;
  const original = wrap.innerHTML;
  wrap.classList.add('editing');
  wrap.innerHTML = '';

  const box = document.createElement('div');
  box.className = 'edit-box';
  const ta = document.createElement('textarea');
  ta.className = 'edit-area'; ta.value = m.content || '';
  const row = document.createElement('div'); row.className = 'edit-actions';
  const save = document.createElement('button'); save.className = 'btn btn-primary edit-save'; save.textContent = 'Save & submit';
  const cancel = document.createElement('button'); cancel.className = 'btn edit-cancel'; cancel.textContent = 'Cancel';
  row.append(cancel, save);
  box.append(ta, row);
  wrap.appendChild(box);

  const fit = () => { ta.style.height = 'auto'; ta.style.height = Math.min(320, ta.scrollHeight) + 'px'; };
  ta.addEventListener('input', fit);
  requestAnimationFrame(() => { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); fit(); });

  const restore = () => { editingMid = null; wrap.classList.remove('editing'); wrap.innerHTML = original;
    // re-bind the buttons the innerHTML snapshot lost
    const c = wrap.querySelector('.bubble-copy'); if (c) c.onclick = () => navigator.clipboard.writeText(m.content || '').then(() => { c.innerHTML = checkIcon(); setTimeout(() => c.innerHTML = copyIcon(), 1300); });
    const e = wrap.querySelector('.bubble-edit'); if (e) e.onclick = () => beginEdit(wrap, m);
  };
  cancel.onclick = restore;
  ta.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') { ev.preventDefault(); restore(); }
    else if (ev.key === 'Enter' && (ev.metaKey || ev.ctrlKey)) { ev.preventDefault(); save.click(); }
  });
  save.onclick = () => submitEdit(wrap, m, ta.value.trim(), restore);
}

async function submitEdit(wrap, m, text, restore) {
  if (!text) { restore(); return; }
  // Optimistically rewind the DOM: drop the edited row and everything below it,
  // forget those ids so refreshLog re-renders the branch, then show the edited
  // text + a thinking placeholder while the new turn streams in over the WS.
  let n = wrap;
  while (n) { const next = n.nextElementSibling; const mid = n.dataset && n.dataset.mid; if (mid) renderedIds.delete(Number(mid)); n.remove(); n = next; }
  editingMid = null;
  appendMessage({ id: undefined, role: 'user', content: text, created_at: Date.now() / 1000, modality: 'electron' });
  const w = chatEl.lastElementChild; if (w) w.classList.add('pending');
  scrollToEnd();

  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/message/edit`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message_id: m.id, text }),
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) { appendMessage({ role: 'system', content: `Edit failed: ${d.error || res.status}`, created_at: Date.now() / 1000 }); }
  } catch (err) {
    appendMessage({ role: 'system', content: `Edit failed: ${err.message}`, created_at: Date.now() / 1000 });
  } finally {
    setTimeout(refreshLog, 50);
  }
}

// time
function fmtClock(e) { return new Date(e * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }); }
function fmtFull(e) { return new Date(e * 1000).toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit', second: '2-digit' }); }
function fmtRel(e) { const d = Date.now() / 1000 - e; if (d < 45) return 'just now'; if (d < 3600) return `${Math.round(d / 60)}m ago`; if (d < 86400) return `${Math.round(d / 3600)}h ago`; return `${Math.round(d / 86400)}d ago`; }
function fmtDur(s) { if (s < 1) return `${Math.round(s * 1000)}ms`; if (s < 60) return `${s.toFixed(1)}s`; return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`; }

// scroll
function nearBottom() { return chatEl.scrollHeight - chatEl.scrollTop - chatEl.clientHeight < 90; }
function scrollToEnd() { chatEl.scrollTop = chatEl.scrollHeight; }
function autoScroll(force) { if (force || nearBottom()) chatEl.scrollTop = chatEl.scrollHeight; }
chatEl.addEventListener('scroll', () => { jumpBtn.hidden = nearBottom(); });
jumpBtn.addEventListener('click', () => { scrollToEnd(); jumpBtn.hidden = true; });

// Links rendered inline in the thread (mdLite turns URLs into <a href>) would
// otherwise navigate the whole Electron window away from the app. Intercept
// the click and hand the URL to the OS browser instead. (main.js also guards
// will-navigate as a backstop, but catching it here avoids the flash.)
chatEl.addEventListener('click', (e) => {
  const a = e.target.closest && e.target.closest('a[href]');
  if (!a) return;
  const href = a.getAttribute('href') || '';
  if (!/^https?:\/\//i.test(href)) return;
  e.preventDefault();
  window.sunday?.openExternal(href);
});

function showLiveFrame(ev) {
  removeEmpty();
  const wrap = document.createElement('div'); wrap.className = 'msg system';
  const p = document.createElement('div'); p.className = 'sys-msg'; p.textContent = ev.url ? `live · ${ev.url}` : 'live frame';
  wrap.appendChild(p);
  if (ev.screenshot_path) { const aw = document.createElement('div'); aw.className = 'msg-attachments'; aw.style.justifyContent = 'center'; aw.appendChild(buildAttachment({ kind: 'image', path: ev.screenshot_path, filename: 'frame.png' })); wrap.appendChild(aw); }
  placeRow(wrap, 'assistant'); autoScroll();
}

// ─── send ────────────────────────────────────────────────────────────────
async function send() {
  const text = composerEl.value.trim();
  if (!text && pending.length === 0) return;
  removeEmpty();
  // optimistic bubble (transient — reconciled on refreshLog)
  const w = document.createElement('div'); w.className = 'msg user pending';
  const b = document.createElement('div'); b.className = 'bubble'; b.innerHTML = mdLite(text); w.appendChild(b);
  if (pending.length) { const aw = document.createElement('div'); aw.className = 'msg-attachments'; for (const a of pending) aw.appendChild(buildAttachment(a)); w.appendChild(aw); }
  placeRow(w, 'user'); scrollToEnd();

  const payload = { text, modality: 'electron' };
  if (pending.length) payload.attachments = pending;
  // capture what we sent so a failed bubble can be retried with the same content
  const sentPending = pending.slice();
  composerEl.value = ''; pending = []; renderChips(); resize(); updateSend();

  // Mark this optimistic bubble as failed-to-send with a tap-to-retry
  // affordance. The composer was already cleared, so retry re-uses the captured
  // text + attachments rather than reading the (now-empty) input.
  const markFailed = (reason) => {
    w.classList.remove('pending', 'steer');
    w.classList.add('failed');
    w.title = reason ? `Couldn't send: ${reason}` : "Couldn't send";
    let note = w.querySelector('.send-fail');
    if (!note) {
      note = document.createElement('button');
      note.type = 'button';
      note.className = 'send-fail';
      note.textContent = 'failed — tap to retry';
      w.appendChild(note);
    }
    note.onclick = () => {
      w.remove();
      composerEl.value = text;
      pending = sentPending.slice();
      renderChips(); resize(); updateSend();
      send();
    };
    scrollToEnd();
  };

  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/say`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) { markFailed(d.error || res.status); return; }
    // Typed while a task was running → the daemon folded it in as steering
    // (not a new turn). Do NOT refreshLog here: the steer isn't in the server
    // log until the next step, and a rebuild would tear down the live thinking
    // stream and drop this bubble. Keep the bubble (tagged "steering"); the
    // stream_end refreshLog reconciles it with the real message later.
    if (d.steered) { w.classList.add('steer'); scrollToEnd(); return; }
    setTimeout(refreshLog, 50);
  } catch (err) { markFailed(err.message); }
}

// attachments
function renderChips() {
  attachEl.innerHTML = '';
  for (const a of pending) {
    const chip = document.createElement('span'); chip.className = 'chip';
    chip.appendChild(document.createTextNode(a.filename + ' '));
    const x = document.createElement('button'); x.title = 'Remove'; x.textContent = '×';
    x.onclick = () => { pending = pending.filter((p) => p.id !== a.id); renderChips(); updateSend(); };
    chip.appendChild(x); attachEl.appendChild(chip);
  }
}
const MAX = 16 * 1024 * 1024;
function readURL(f) { return new Promise((res, rej) => { const r = new FileReader(); r.onerror = () => rej(r.error); r.onload = () => res(r.result); r.readAsDataURL(f); }); }
async function addFiles(files) {
  for (const f of files) {
    if (f.size > MAX) { appendMessage({ role: 'system', content: `${f.name}: over the 16MB cap`, created_at: Date.now() / 1000 }); continue; }
    let url; try { url = await readURL(f); } catch { continue; }
    const mime = f.type || 'application/octet-stream';
    pending.push({ id: `att-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`, url, path: '', filename: f.name, mime_type: mime, size: f.size, kind: guessKind(mime) });
  }
  renderChips(); updateSend();
}

// composer
function updateSend() { sendBtn.disabled = !(composerEl.value.trim() || pending.length); }
function resize() { composerEl.style.height = 'auto'; composerEl.style.height = Math.min(composerEl.scrollHeight, 200) + 'px'; }
sendBtn.addEventListener('click', send);

// Stop the running task. Shown only while a stream is live (see handleWs).
const stopBtn = $('#stop-btn');
function showStop(on) { if (stopBtn) stopBtn.hidden = !on; }
stopBtn?.addEventListener('click', async () => {
  stopBtn.disabled = true;
  try { await fetch(`${DAEMON_HTTP}/v1/task/stop`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }); }
  catch {}
  finally { stopBtn.disabled = false; }
});
composerEl.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
composerEl.addEventListener('input', () => { resize(); updateSend(); });
attachBtn.addEventListener('click', () => fileInput.click());

// ─── connectors popover (in-chat toggle menu) ─────────────────────────────
// Small searchable popover anchored to the composer. Lists the user's
// currently-installed connectors (MCP servers + pinned Nango providers),
// each with an on/off toggle. Adding a new connector links to Settings.
const connPop      = $('#connectors-pop');
const connPopList  = $('#connectors-pop-list');
const connPopQ     = $('#connectors-pop-search');
const connPopAdd   = $('#connectors-pop-add');
let connPopOpen = false;
let connPopRows = [];   // last-fetched [{provider, label, kind, enabled, has_tools}]

function positionConnPop() {
  const r = connBtn.getBoundingClientRect();
  // Anchor above the composer button, growing upward.
  connPop.style.left   = `${Math.round(r.left)}px`;
  connPop.style.bottom = `${Math.round(window.innerHeight - r.top + 8)}px`;
}

function renderConnPop(filter = '') {
  const f = filter.trim().toLowerCase();
  const items = connPopRows.filter((r) => !f || r.label.toLowerCase().includes(f) || (r.provider || '').toLowerCase().includes(f));
  if (!items.length) {
    connPopList.innerHTML = `<li class="connectors-pop-empty">${
      connPopRows.length ? 'no matches.' : "you haven't connected anything yet."
    }</li>`;
    return;
  }
  connPopList.innerHTML = items.map((r) => `
    <li class="connectors-pop-row" data-provider="${r.provider}">
      <span class="connectors-pop-name">${(r.label || r.provider).replace(/[<>&]/g, '')}</span>
      <span class="connectors-pop-kind">${r.kind === 'mcp' ? 'mcp' : ''}</span>
      <label class="toggle">
        <input type="checkbox" class="connectors-pop-toggle" data-provider="${r.provider}" ${r.enabled ? 'checked' : ''} ${r.has_tools ? '' : 'disabled'}>
        <span class="toggle-track"><span class="toggle-thumb"></span></span>
      </label>
    </li>
  `).join('');
  connPopList.querySelectorAll('.connectors-pop-toggle').forEach((cb) => {
    cb.addEventListener('change', () => toggleConnPop(cb.dataset.provider, cb.checked, cb));
  });
}

async function loadConnPop() {
  try {
    const d = await (await fetch(`${DAEMON_HTTP}/v1/connectors`)).json();
    connPopRows = (d.connectors || []).map((c) => ({
      provider: c.provider,
      label:    c.label || c.provider,
      kind:     c.kind || 'nango',
      enabled:  !!c.enabled,
      has_tools: c.has_tools !== false,
    }));
    renderConnPop(connPopQ.value);
  } catch (err) {
    connPopList.innerHTML = `<li class="connectors-pop-empty">couldn't load: ${err.message}</li>`;
  }
}

async function toggleConnPop(provider, on, cb) {
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/connectors/toggle`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider, on }),
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      throw new Error(d.error || `HTTP ${res.status}`);
    }
    const row = connPopRows.find((r) => r.provider === provider);
    if (row) row.enabled = on;
  } catch (err) {
    cb.checked = !on;  // revert
    console.warn('toggle failed:', err);
  }
}

function openConnPop() {
  positionConnPop();
  connPop.hidden = false;
  connPopOpen = true;
  loadConnPop();
  setTimeout(() => connPopQ.focus(), 0);
}

function closeConnPop() {
  connPop.hidden = true;
  connPopOpen = false;
  connPopQ.value = '';
}

connBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  connPopOpen ? closeConnPop() : openConnPop();
});

// Meeting mode lives in Memory → Meetings now — not the composer, not the
// main chat. (See memory-view.js.)

connPopQ.addEventListener('input', (e) => renderConnPop(e.target.value));

// Explicit close button (×).
$('#connectors-pop-close').addEventListener('click', (e) => {
  e.stopPropagation();
  closeConnPop();
});

// Close on outside click / Escape / window resize.
document.addEventListener('click', (e) => {
  if (connPopOpen && !connPop.contains(e.target) && e.target !== connBtn && !connBtn.contains(e.target)) closeConnPop();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && connPopOpen) closeConnPop();
  // Esc from inside the full thread view returns to the main timeline (unless a
  // popover claimed it above, or the user is mid-edit in a thread composer with
  // text — handled by the composer's own keydown, which doesn't preventDefault).
  else if (e.key === 'Escape' && openThread) { e.preventDefault(); closeThreadView(); }
});
window.addEventListener('resize', () => connPopOpen && positionConnPop());

// "+ Add a connector" deeplinks into Settings → Tools.
connPopAdd.addEventListener('click', () => {
  closeConnPop();
  document.querySelector('.tab[data-view="settings"]')?.click();
  setTimeout(() => document.querySelector('.set-navitem[data-page="page-tools"]')?.click(), 120);
});
fileInput.addEventListener('change', (e) => addFiles(e.target.files));
composerEl.addEventListener('paste', async (e) => { const items = e.clipboardData?.items || []; const fs = []; for (const it of items) if (it.kind === 'file') { const f = it.getAsFile(); if (f) fs.push(f); } if (fs.length) { e.preventDefault(); await addFiles(fs); } });

// ─── voice mode (live, full-duplex) — lazy + isolated so its heavy deps
// (Three.js / TalkingHead / WebRTC) can't touch the main app's load path ──
let _voiceModule = null;
$('#voice-mode-btn')?.addEventListener('click', async () => {
  const overlay = $('#voice-overlay');
  // Wire close BEFORE the dynamic import. If the import (or open) throws, the
  // catch still reveals the overlay to show the error — so the × must already
  // close it, otherwise the user is trapped with no escape. The handler hides
  // the overlay and calls the module's own teardown if it loaded.
  const closeBtn = $('#voice-close');
  if (closeBtn && !closeBtn.dataset.wired) {
    closeBtn.dataset.wired = '1';
    closeBtn.addEventListener('click', () => {
      try { _voiceModule?.close(); } catch {}
      overlay?.classList.remove('open');
      overlay?.setAttribute('hidden', '');
    });
  }
  try {
    const vm = await import('./voice-mode.js');
    _voiceModule = vm;
    if (vm.isOpen()) return;
    await vm.open({
      daemonHttp: DAEMON_HTTP, daemonToken: DAEMON_TOKEN,
      overlay, avatarMount: $('#voice-avatar'), status: $('#voice-status'),
    });
  } catch (e) {
    const s = $('#voice-status'); if (s) { s.dataset.state = 'fail'; s.textContent = `Voice mode failed to load: ${e.message}`; }
    overlay?.removeAttribute('hidden');
    console.error('voice mode import failed', e);
  }
});
// Settings → Voice "Open voice mode" forwards to the same pill.
$('#set-voice-open')?.addEventListener('click', () => $('#voice-mode-btn')?.click());

// ─── tabs ──────────────────────────────────────────────────────────────
function switchView(name) {
  if (!['chat', 'memory', 'inbox', 'timeline', 'settings'].includes(name)) return;
  // Leaving for a real tab while a thread view is open tears its state down so
  // nothing lingers (e.g. a click on Memory from inside a thread).
  if (openThread && name !== 'chat') { openThread = null; threadComposer.value = ''; showThreadStop(false); }
  currentView = name;
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.view === name));
  document.querySelectorAll('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${name}`));
  if (name === 'memory') { memoryView.refresh(); }
  if (name === 'inbox') inboxView.load();
  if (name === 'timeline') timelineView.load();
  if (name === 'settings') { settingsView.loadAll(); settingsView.startSystemPolling(); } else { settingsView.stopSystemPolling(); }
  if (name === 'chat') scrollToEnd();
}
document.querySelectorAll('.tab').forEach((t) => t.addEventListener('click', () => switchView(t.dataset.view)));

document.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === ',') { e.preventDefault(); switchView('settings'); }
  if ((e.metaKey || e.ctrlKey) && e.key === '1') { e.preventDefault(); switchView('chat'); }
  if ((e.metaKey || e.ctrlKey) && e.key === '2') { e.preventDefault(); switchView('memory'); }
  if ((e.metaKey || e.ctrlKey) && e.key === '3') { e.preventDefault(); switchView('inbox'); }
  if ((e.metaKey || e.ctrlKey) && e.key === '4') { e.preventDefault(); switchView('timeline'); }
});
window.sunday?.onSwitchView?.((name) => switchView(name));
window.sunday?.onOpenAdmin?.(() => switchView('settings'));

// drag & drop (chat only)
document.addEventListener('dragover', (e) => { if (currentView !== 'chat') return; e.preventDefault(); dropzoneEl.hidden = false; });
document.addEventListener('dragleave', (e) => { if (e.target === document || e.target === document.documentElement) dropzoneEl.hidden = true; });
document.addEventListener('drop', async (e) => { e.preventDefault(); dropzoneEl.hidden = true; if (currentView === 'chat' && e.dataTransfer?.files?.length) await addFiles(e.dataTransfer.files); });

updateSend();
boot().catch((err) => {
  console.error('boot failed', err);
  window.sunday?.logError?.(`BOOT_FAILED: ${(err && err.stack) || err}`);
});
