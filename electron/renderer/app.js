// Sunday — renderer. One window, three tabs. iMessage-style thread with a
// composer pinned to the bottom; messages append incrementally so the
// input never jumps. Memory + settings live in sibling tabs.

import * as memoryView from './memory-view.js';
import * as settingsView from './settings-view.js';
import * as rewindView from './rewind-view.js';

const $ = (s) => document.querySelector(s);
const chatEl     = $('#chat');
const composerEl = $('#composer');
const sendBtn    = $('#send-btn');
const micBtn     = $('#mic-btn');
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

// ─── boot ──────────────────────────────────────────────────────────────
async function boot() {
  if (window.sunday) {
    const cfg = await window.sunday.getConfig();
    DAEMON_HTTP  = cfg.daemonHttp || DAEMON_HTTP;
    DAEMON_WS    = cfg.daemonWs   || DAEMON_WS;
    DAEMON_TOKEN = cfg.daemonToken || '';
  }
  memoryView.init({ daemonHttp: DAEMON_HTTP }, {
    canvas: $('#mem-canvas'), legend: $('#mem-legend'), refresh: $('#mem-refresh'),
    empty: $('#mem-empty'), detail: $('#mem-detail'),
    detailKind: $('#mem-detail-kind'), detailName: $('#mem-detail-name'),
    detailFacts: $('#mem-detail-facts'), detailConns: $('#mem-detail-conns'),
  });
  settingsView.init(DAEMON_HTTP);
  rewindView.init({ daemonHttp: DAEMON_HTTP }, {
    img: $('#rw-img'), empty: $('#rw-empty'), emptyTitle: $('#rw-empty-title'), emptySub: $('#rw-empty-sub'),
    enable: $('#rw-enable'), controls: $('#rw-controls'), text: $('#rw-text'), ocr: $('#rw-ocr'),
    slider: $('#rw-slider'), time: $('#rw-time'), play: $('#rw-play'), prev: $('#rw-prev'), next: $('#rw-next'), stop: $('#rw-stop'),
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
    // drop transient placeholders; the real rows are about to land
    chatEl.querySelectorAll('.pending, .stream-temp').forEach((n) => n.remove());
    const atBottom = nearBottom();
    if (!msgs.length && renderedIds.size === 0) { renderEmptyState(); bootedChat = true; return; }
    removeSkeleton();
    removeEmpty();
    for (const m of msgs) {
      if (typeof m.id === 'number' && renderedIds.has(m.id)) {
        if (m.role === 'user') lastUserTs = m.created_at;
        continue;
      }
      appendMessage(m);
      if (typeof m.id === 'number') renderedIds.add(m.id);
    }
    if (!bootedChat || atBottom) scrollToEnd(true);
    bootedChat = true;
  } catch (err) { console.warn('log fetch failed', err); }
}

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
function handleWs(ev) {
  switch (ev.type) {
    case 'stream_start': stream = beginStream(ev); showStop(true); return;
    case 'reasoning_delta':
      if (stream && stream.id === ev.stream_id) { stream.reason += (ev.content || ''); showThinking(stream); autoScroll(); }
      return;
    case 'stream_delta':
      if (stream && stream.id === ev.stream_id) { stream.raw += (ev.content || ''); showText(stream); autoScroll(); }
      return;
    case 'tool_call':
      if (stream && stream.id === ev.stream_id) addToolRow(stream, ev); return;
    case 'tool_result':
      if (stream && stream.id === ev.stream_id) finishToolRow(stream, ev); return;
    case 'stream_end':
      if (stream && stream.id === ev.stream_id) stream.el.classList.remove('streaming');
      stream = null; showStop(false); refreshLog(); refreshStatus(); return;
    case 'reply': if (!stream) refreshLog(); return;
    case 'browser_frame': case 'device_browser_frame': case 'device_screen': showLiveFrame(ev); return;
    case 'device_online': case 'device_offline': refreshStatus(); return;
  }
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

  if (content) {
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.innerHTML = mdLite(m.content || '');
    const copy = document.createElement('button');
    copy.className = 'bubble-copy'; copy.title = 'Copy'; copy.setAttribute('aria-label', 'Copy');
    copy.innerHTML = copyIcon();
    copy.onclick = () => navigator.clipboard.writeText(m.content || '').then(() => { copy.innerHTML = checkIcon(); setTimeout(() => copy.innerHTML = copyIcon(), 1300); });
    bubble.appendChild(copy);
    wrap.appendChild(bubble);
  }

  if (atts.length) {
    const aw = document.createElement('div'); aw.className = 'msg-attachments';
    for (const a of atts) aw.appendChild(buildAttachment(a));
    wrap.appendChild(aw);
  }

  // finish time + duration (floats on hover)
  let html = '';
  if (role === 'sunday' && lastUserTs && m.created_at > lastUserTs) html += `<span class="dur">${fmtDur(m.created_at - lastUserTs)}</span>`;
  if (m.created_at) html += `<span class="time" title="${esc(fmtFull(m.created_at))}">${esc(fmtClock(m.created_at))} · ${esc(fmtRel(m.created_at))}</span>`;
  if (html) { const meta = document.createElement('div'); meta.className = 'msg-meta'; meta.innerHTML = html; wrap.appendChild(meta); }

  placeRow(wrap, side);

  if (role === 'user') lastUserTs = m.created_at;
}

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
  composerEl.value = ''; pending = []; renderChips(); resize(); updateSend();

  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/say`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    const d = await res.json();
    if (!res.ok) { appendMessage({ role: 'system', content: `Error: ${d.error || res.status}`, created_at: Date.now() / 1000 }); scrollToEnd(); return; }
    // Typed while a task was running → the daemon folded it in as steering
    // (not a new turn). Do NOT refreshLog here: the steer isn't in the server
    // log until the next step, and a rebuild would tear down the live thinking
    // stream and drop this bubble. Keep the bubble (tagged "steering"); the
    // stream_end refreshLog reconciles it with the real message later.
    if (d.steered) { w.classList.add('steer'); scrollToEnd(); return; }
    setTimeout(refreshLog, 50);
  } catch (err) { appendMessage({ role: 'system', content: `Network error: ${err.message}`, created_at: Date.now() / 1000 }); scrollToEnd(); }
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

// voice
let recog = null, listening = false;
function startVoice() {
  const R = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!R) { appendMessage({ role: 'system', content: 'Voice input is not available in this build.', created_at: Date.now() / 1000 }); return; }
  recog = new R(); recog.lang = 'en-US'; recog.interimResults = true; recog.continuous = false;
  let final = '';
  recog.onstart = () => { listening = true; micBtn.dataset.active = 'true'; window.sunday?.setOverlayState({ listening: true }); };
  recog.onresult = (e) => { final = ''; let it = ''; for (let i = e.resultIndex; i < e.results.length; i++) { const t = e.results[i][0].transcript; if (e.results[i].isFinal) final += t; else it += t; } composerEl.value = (final + it).trim(); resize(); updateSend(); };
  recog.onerror = () => { listening = false; micBtn.dataset.active = 'false'; };
  recog.onend = () => { listening = false; micBtn.dataset.active = 'false'; window.sunday?.setOverlayState({ listening: false }); if (final.trim()) send(); };
  recog.start();
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
micBtn.addEventListener('click', () => (listening ? recog?.stop() : startVoice()));

// ─── voice mode (live, full-duplex) — lazy + isolated so its heavy deps
// (Three.js / TalkingHead / WebRTC) can't touch the main app's load path ──
$('#voice-mode-btn')?.addEventListener('click', async () => {
  try {
    const vm = await import('./voice-mode.js');
    if (vm.isOpen()) return;
    await vm.open({
      daemonHttp: DAEMON_HTTP, daemonToken: DAEMON_TOKEN,
      overlay: $('#voice-overlay'), avatarMount: $('#voice-avatar'), status: $('#voice-status'),
    });
    $('#voice-close')?.addEventListener('click', () => vm.close(), { once: true });
  } catch (e) {
    const s = $('#voice-status'); if (s) { s.dataset.state = 'fail'; s.textContent = `Voice mode failed to load: ${e.message}`; }
    $('#voice-overlay')?.removeAttribute('hidden');
    console.error('voice mode import failed', e);
  }
});
// Settings → Voice "Open voice mode" forwards to the same pill.
$('#set-voice-open')?.addEventListener('click', () => $('#voice-mode-btn')?.click());

// ─── tabs ──────────────────────────────────────────────────────────────
function switchView(name) {
  if (!['chat', 'memory', 'rewind', 'settings'].includes(name)) return;
  currentView = name;
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.view === name));
  document.querySelectorAll('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${name}`));
  if (name === 'memory') { memoryView.resize(); if (!memoryView.isLoaded()) memoryView.load(); }
  if (name === 'rewind') rewindView.load();
  if (name === 'settings') { settingsView.loadAll(); settingsView.startSystemPolling(); } else { settingsView.stopSystemPolling(); }
  if (name === 'chat') scrollToEnd();
}
document.querySelectorAll('.tab').forEach((t) => t.addEventListener('click', () => switchView(t.dataset.view)));
$('#mem-refresh').addEventListener('click', () => memoryView.load(true));
$('#mem-detail-close').addEventListener('click', () => memoryView.closeDetail());

document.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === ',') { e.preventDefault(); switchView('settings'); }
  if ((e.metaKey || e.ctrlKey) && e.key === '1') { e.preventDefault(); switchView('chat'); }
  if ((e.metaKey || e.ctrlKey) && e.key === '2') { e.preventDefault(); switchView('memory'); }
  if ((e.metaKey || e.ctrlKey) && e.key === '3') { e.preventDefault(); switchView('rewind'); }
});
window.sunday?.onSwitchView?.((name) => switchView(name));
window.sunday?.onOpenAdmin?.(() => switchView('settings'));
window.addEventListener('resize', () => { if (currentView === 'memory') memoryView.resize(); });

// drag & drop (chat only)
document.addEventListener('dragover', (e) => { if (currentView !== 'chat') return; e.preventDefault(); dropzoneEl.hidden = false; });
document.addEventListener('dragleave', (e) => { if (e.target === document || e.target === document.documentElement) dropzoneEl.hidden = true; });
document.addEventListener('drop', async (e) => { e.preventDefault(); dropzoneEl.hidden = true; if (currentView === 'chat' && e.dataTransfer?.files?.length) await addFiles(e.dataTransfer.files); });

updateSend();
boot().catch((err) => console.error('boot failed', err));
