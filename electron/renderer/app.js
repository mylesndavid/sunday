// Sunday desktop — renderer.
//
// One WS to the daemon, one chat rendered in order, one composer. Every
// turn shows its finish time + how long it took on hover. Tool calls
// collapse into cards; the model's reasoning tucks behind a disclosure.
// The mic button does a single one-shot utterance — no always-on listener.

const $ = (sel) => document.querySelector(sel);
const chatEl        = $('#chat');
const composerEl    = $('#composer');
const sendBtn       = $('#send-btn');
const micBtn        = $('#mic-btn');
const attachBtn     = $('#attach-btn');
const fileInput     = $('#file-input');
const attachmentsEl = $('#attachments');
const statusEl      = $('#status');
const brandDot      = document.querySelector('.brand-dot');
const dropzoneEl    = $('#dropzone');
const adminBtn      = $('#admin-btn');
const settingsBtn   = $('#settings-btn');
const adminPanel    = $('#admin-panel');
const adminCloseBtn = $('#admin-close');
const jumpBtn       = $('#jump-btn');

let DAEMON_HTTP = 'http://127.0.0.1:8765';
let DAEMON_WS   = 'ws://127.0.0.1:8765/v1/ws';
let ws = null;
let pendingAttachments = [];
let firstRender = true;

// ─── boot ──────────────────────────────────────────────────────────────

async function boot() {
  if (window.sunday) {
    const cfg = await window.sunday.getConfig();
    DAEMON_HTTP = cfg.daemonHttp || DAEMON_HTTP;
    DAEMON_WS   = cfg.daemonWs   || DAEMON_WS;
  }
  renderSkeleton();
  await refreshLog();
  await refreshStatus();
  connectWs();
}

function setOnline(state) {
  brandDot.dataset.state = state;
  brandDot.title = { online: 'connected', connecting: 'connecting…', offline: 'daemon unreachable' }[state] || state;
  if (state === 'connecting') statusEl.textContent = 'connecting…';
  if (state === 'offline')    statusEl.textContent = 'offline — daemon unreachable';
  if (window.sunday) window.sunday.setOverlayState({ connection: state });
}

async function refreshStatus() {
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/status`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const model = (data.model || '').split('/').slice(-1)[0] || data.model;
    statusEl.textContent = `${model} · ${data.messages} msgs · ${data.tools.length} tools`;
    setOnline('online');
  } catch {
    setOnline('offline');
  }
}

async function refreshLog() {
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/log?limit=120`);
    const data = await res.json();
    const msgs = data.messages || [];
    chatEl.innerHTML = '';
    if (!msgs.length) { renderEmptyState(); firstRender = false; return; }
    let prevUserTs = null;
    for (const msg of msgs) {
      const el = renderMessage(msg, prevUserTs);
      if (firstRender) el?.classList.add('enter');
      if (msg.role === 'user') prevUserTs = msg.created_at;
    }
    firstRender = false;
    scrollToEnd(true);
  } catch (err) {
    console.warn('log fetch failed', err);
  }
}

function connectWs() {
  try { ws = new WebSocket(DAEMON_WS); } catch { setOnline('offline'); return; }
  ws.onopen    = () => setOnline('online');
  ws.onclose   = () => { setOnline('offline'); setTimeout(connectWs, 2000); };
  ws.onerror   = () => setOnline('offline');
  ws.onmessage = (evt) => {
    try { handleWsEvent(JSON.parse(evt.data)); } catch {}
  };
}

let activeStream = null;

function handleWsEvent(event) {
  switch (event.type) {
    case 'stream_start':
      activeStream = beginStreamBubble(event);
      return;
    case 'stream_delta':
      if (activeStream && activeStream.id === event.stream_id) {
        activeStream.raw += (event.content || '');
        activeStream.bodyEl.textContent = activeStream.raw;
        autoScroll();
      }
      return;
    case 'stream_end':
      if (activeStream && activeStream.id === event.stream_id) {
        activeStream.articleEl.classList.remove('streaming');
      }
      activeStream = null;
      refreshLog();
      refreshStatus();
      return;
    case 'reply':
      if (!activeStream) refreshLog();
      return;
    case 'browser_frame':
    case 'device_browser_frame':
    case 'device_screen':
      showLiveFrame(event);
      return;
    case 'device_online':
    case 'device_offline':
      refreshStatus();
      return;
  }
}

function beginStreamBubble(event) {
  // Clear the empty state if it's showing.
  const es = chatEl.querySelector('.empty-state');
  if (es) es.remove();
  const article = document.createElement('article');
  article.className = 'msg sunday streaming';
  article.dataset.streamId = event.stream_id;
  article.innerHTML = `<div class="msg-meta"><span class="who">sunday</span>${
    event.modality ? `<span class="mod">${esc(event.modality)}</span>` : ''
  }</div>`;
  const body = document.createElement('div');
  body.className = 'msg-body';
  article.appendChild(body);
  chatEl.appendChild(article);
  autoScroll(true);
  return { id: event.stream_id, bodyEl: body, articleEl: article, raw: '' };
}

// ─── rendering ─────────────────────────────────────────────────────────

function renderMessage(msg, prevUserTs) {
  const el = document.createElement('article');
  el.className = `msg ${msg.role}`;
  el.dataset.id = msg.id;

  // tool calls render as their own collapsible card
  if (msg.role === 'tool') {
    el.appendChild(renderToolCard(msg));
    chatEl.appendChild(el);
    return el;
  }

  // meta row: who · modality · (finish time + duration, revealed on hover)
  const who = msg.role === 'user' ? 'you' : (msg.role === 'system' ? 'system' : 'sunday');
  const meta = document.createElement('div');
  meta.className = 'msg-meta';
  let metaHtml = `<span class="who">${esc(who)}</span>`;
  if (msg.modality) metaHtml += `<span class="mod">${esc(msg.modality)}</span>`;
  // duration: only on assistant replies, measured from the preceding user turn
  if (msg.role === 'sunday' && prevUserTs && msg.created_at > prevUserTs) {
    metaHtml += `<span class="dur" title="time to respond">${fmtDuration(msg.created_at - prevUserTs)}</span>`;
  }
  if (msg.created_at) {
    metaHtml += `<span class="time" title="${esc(fmtFull(msg.created_at))}">${esc(fmtClock(msg.created_at))} · ${esc(fmtRelative(msg.created_at))}</span>`;
  }
  meta.innerHTML = metaHtml;
  el.appendChild(meta);

  // body (markdown-lite)
  const body = document.createElement('div');
  body.className = 'msg-body';
  if (msg.role === 'system' && /error|failed/i.test(msg.content || '')) body.classList.add('is-error');
  body.innerHTML = mdLite(msg.content || '');
  el.appendChild(body);

  // attachments
  const atts = (msg.metadata && msg.metadata.attachments) || [];
  if (atts.length) {
    const wrap = document.createElement('div');
    wrap.className = 'msg-attachments';
    for (const a of atts) wrap.appendChild(renderAttachment(a));
    el.appendChild(wrap);
  }

  // reasoning disclosure (real data from metadata.reasoning_content)
  const reasoning = msg.metadata && msg.metadata.reasoning_content;
  if (reasoning && reasoning.trim()) {
    const det = document.createElement('details');
    det.className = 'reasoning';
    det.innerHTML = `<summary><svg class="chev" viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>thinking</summary><div class="reasoning-body"></div>`;
    det.querySelector('.reasoning-body').textContent = reasoning.trim();
    el.appendChild(det);
  }

  // copy action (real)
  if (msg.content && (msg.role === 'sunday' || msg.role === 'user')) {
    const actions = document.createElement('div');
    actions.className = 'msg-actions';
    const copy = document.createElement('button');
    copy.className = 'iconbtn';
    copy.title = 'Copy';
    copy.setAttribute('aria-label', 'Copy message');
    copy.innerHTML = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>`;
    copy.onclick = () => {
      navigator.clipboard.writeText(msg.content).then(() => {
        copy.innerHTML = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="var(--ok)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>`;
        setTimeout(() => { copy.innerHTML = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>`; }, 1400);
      });
    };
    actions.appendChild(copy);
    el.appendChild(actions);
  }

  chatEl.appendChild(el);
  return el;
}

function renderToolCard(msg) {
  const det = document.createElement('details');
  det.className = 'tool-card';
  let name = msg.metadata?.tool_name || '';
  let payload = msg.content || '';
  // content is often JSON — pretty-print + sniff the tool name
  let pretty = payload;
  try {
    const obj = JSON.parse(payload);
    pretty = JSON.stringify(obj, null, 2);
    if (!name && obj.tool) name = obj.tool;
  } catch {}
  name = name || 'tool';
  const lines = pretty.split('\n').length;
  det.innerHTML = `
    <summary>
      <span class="t-dot"></span>
      <span class="t-name">${esc(name)}</span>
      <span class="t-hint">${lines > 1 ? lines + ' lines' : 'result'}${msg.created_at ? ' · ' + esc(fmtClock(msg.created_at)) : ''}</span>
      <svg class="chev" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>
    </summary>
    <div class="t-body"></div>`;
  det.querySelector('.t-body').textContent = pretty;
  return det;
}

function renderAttachment(a) {
  const kind = a.kind || guessKind(a.mime_type || '');
  if (kind === 'image') {
    const img = document.createElement('img');
    img.src = a.url || (a.path ? `file://${a.path}` : '');
    img.alt = a.filename || 'image attachment';
    return img;
  }
  const chip = document.createElement('div');
  chip.className = 'att';
  const sizeBit = a.size ? `${Math.round(a.size / 1024)}kb · ` : '';
  chip.textContent = `${a.filename || 'attachment'} · ${sizeBit}${a.mime_type || ''}`;
  return chip;
}

function guessKind(mime) {
  if (mime.startsWith('image/')) return 'image';
  if (mime.startsWith('audio/')) return 'audio';
  if (mime.startsWith('video/')) return 'video';
  return 'file';
}

// ── empty + skeleton states ──
function renderEmptyState() {
  const suggestions = [
    "What's on my screen?",
    'What do you remember about me?',
    'Catch me up on my recent texts',
  ];
  const wrap = document.createElement('div');
  wrap.className = 'empty-state';
  wrap.innerHTML = `
    <div class="empty-sun" aria-hidden="true"></div>
    <h2>Good to see you</h2>
    <p>Ask me anything, or pick up where we left off. I keep what matters and forget the noise.</p>
    <div class="empty-suggest"></div>`;
  const sug = wrap.querySelector('.empty-suggest');
  for (const s of suggestions) {
    const b = document.createElement('button');
    b.textContent = s;
    b.onclick = () => { composerEl.value = s; updateSendState(); send(); };
    sug.appendChild(b);
  }
  chatEl.appendChild(wrap);
}

function renderSkeleton() {
  chatEl.innerHTML = '';
  for (let i = 0; i < 3; i++) {
    const sk = document.createElement('div');
    sk.className = 'skeleton';
    sk.innerHTML = `<div class="sk-line short"></div><div class="sk-line mid"></div><div class="sk-line"></div>`;
    chatEl.appendChild(sk);
  }
}

// ── markdown-lite: escape first, then a safe subset ──
function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
function mdLite(raw) {
  let s = esc(raw);
  // fenced code blocks
  s = s.replace(/```([\s\S]*?)```/g, (_m, code) => `<pre><code>${code.replace(/^\n/, '')}</code></pre>`);
  // inline code
  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  // bold
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  // [text](url) links
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2">$1</a>');
  // bare urls (avoid ones already inside an href)
  s = s.replace(/(^|[^"=>])(https?:\/\/[^\s<]+)/g, '$1<a href="$2">$2</a>');
  return s;
}

// ── time helpers ──
function fmtClock(epoch) {
  return new Date(epoch * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}
function fmtFull(epoch) {
  return new Date(epoch * 1000).toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit', second: '2-digit' });
}
function fmtRelative(epoch) {
  const diff = (Date.now() / 1000) - epoch;
  if (diff < 45) return 'just now';
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86400)}d ago`;
}
function fmtDuration(sec) {
  if (sec < 1) return `${Math.round(sec * 1000)}ms`;
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return `${m}m ${s}s`;
}

// ── scrolling ──
function nearBottom() {
  return chatEl.scrollHeight - chatEl.scrollTop - chatEl.clientHeight < 80;
}
function scrollToEnd(force) {
  if (force || nearBottom()) chatEl.scrollTop = chatEl.scrollHeight;
}
function autoScroll(force) {
  // Only yank to the bottom if the user is already there (or forced) —
  // don't interrupt someone reading back through history.
  if (force || nearBottom()) chatEl.scrollTop = chatEl.scrollHeight;
}
chatEl.addEventListener('scroll', () => { jumpBtn.hidden = nearBottom(); });
jumpBtn.addEventListener('click', () => { chatEl.scrollTop = chatEl.scrollHeight; jumpBtn.hidden = true; });

function showLiveFrame(event) {
  renderMessage({
    id: `frame-${Date.now()}`,
    role: 'system',
    modality: 'live',
    content: event.url ? `live · ${event.url}` : 'live frame',
    created_at: Date.now() / 1000,
    metadata: {
      attachments: event.screenshot_path
        ? [{ kind: 'image', path: event.screenshot_path, filename: 'frame.png', mime_type: 'image/png' }]
        : [],
    },
  }, null);
  autoScroll();
}

// ─── sending ───────────────────────────────────────────────────────────

async function send() {
  const text = composerEl.value.trim();
  if (!text && pendingAttachments.length === 0) return;

  const es = chatEl.querySelector('.empty-state');
  if (es) es.remove();

  renderMessage({
    id: `local-${Date.now()}`,
    role: 'user',
    modality: 'electron',
    content: text,
    created_at: Date.now() / 1000,
    metadata: pendingAttachments.length ? { attachments: pendingAttachments } : null,
  }, null);
  scrollToEnd(true);

  const payload = { text, modality: 'electron' };
  if (pendingAttachments.length) payload.attachments = pendingAttachments;

  composerEl.value = '';
  pendingAttachments = [];
  renderAttachmentChips();
  resizeComposer();
  updateSendState();

  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/say`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      renderMessage({ id: `err-${Date.now()}`, role: 'system', modality: 'electron', content: `Error: ${data.error || res.status}`, created_at: Date.now() / 1000 }, null);
      scrollToEnd(true);
      return;
    }
    setTimeout(refreshLog, 50);
  } catch (err) {
    renderMessage({ id: `err-${Date.now()}`, role: 'system', modality: 'electron', content: `Network error: ${err.message}`, created_at: Date.now() / 1000 }, null);
    scrollToEnd(true);
  }
}

// ─── attachments ───────────────────────────────────────────────────────

function renderAttachmentChips() {
  attachmentsEl.innerHTML = '';
  for (const a of pendingAttachments) {
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.appendChild(document.createTextNode(a.filename + ' '));
    const x = document.createElement('button');
    x.title = 'Remove';
    x.textContent = '×';
    x.onclick = () => { pendingAttachments = pendingAttachments.filter((p) => p.id !== a.id); renderAttachmentChips(); updateSendState(); };
    chip.appendChild(x);
    attachmentsEl.appendChild(chip);
  }
}

const MAX_ATTACH_BYTES = 16 * 1024 * 1024;
function readAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onerror = () => reject(r.error || new Error('read failed'));
    r.onload = () => resolve(r.result);
    r.readAsDataURL(file);
  });
}
async function fileToAttachmentMeta(file) {
  if (file.size > MAX_ATTACH_BYTES) return { error: `${file.name}: ${Math.round(file.size / 1024 / 1024)}MB exceeds the 16MB cap` };
  let dataUrl;
  try { dataUrl = await readAsDataURL(file); } catch (err) { return { error: `${file.name}: read failed (${err.message})` }; }
  const mime = file.type || 'application/octet-stream';
  return {
    id: `att-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    url: dataUrl, path: '', filename: file.name, mime_type: mime, size: file.size, kind: guessKind(mime),
  };
}
async function addFiles(files) {
  for (const file of files) {
    const meta = await fileToAttachmentMeta(file);
    if (meta.error) {
      renderMessage({ id: `warn-${Date.now()}`, role: 'system', modality: 'electron', content: meta.error, created_at: Date.now() / 1000 }, null);
      scrollToEnd(true);
      continue;
    }
    pendingAttachments.push(meta);
  }
  renderAttachmentChips();
  updateSendState();
}

// ─── voice (one-shot) ────────────────────────────────────────────────────

let recog = null, listening = false;
function startVoice() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) {
    renderMessage({ id: `warn-${Date.now()}`, role: 'system', modality: 'electron', content: 'Voice input is not available in this build — type instead.', created_at: Date.now() / 1000 }, null);
    scrollToEnd(true);
    return;
  }
  recog = new Recognition();
  recog.lang = 'en-US'; recog.interimResults = true; recog.continuous = false;
  let finalText = '';
  recog.onstart = () => { listening = true; micBtn.dataset.active = 'true'; window.sunday?.setOverlayState({ listening: true }); };
  recog.onresult = (e) => {
    finalText = ''; let interim = '';
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const t = e.results[i][0].transcript;
      if (e.results[i].isFinal) finalText += t; else interim += t;
    }
    composerEl.value = (finalText + interim).trim();
    resizeComposer(); updateSendState();
  };
  recog.onerror = () => { listening = false; micBtn.dataset.active = 'false'; };
  recog.onend = () => {
    listening = false; micBtn.dataset.active = 'false';
    window.sunday?.setOverlayState({ listening: false });
    if (finalText.trim()) send();
  };
  recog.start();
}
function stopVoice() { if (recog && listening) recog.stop(); }

// ─── composer interactions ───────────────────────────────────────────────

function updateSendState() {
  const has = composerEl.value.trim().length > 0 || pendingAttachments.length > 0;
  sendBtn.disabled = !has;
}
function resizeComposer() {
  composerEl.style.height = 'auto';
  composerEl.style.height = Math.min(composerEl.scrollHeight, 240) + 'px';
}

sendBtn.addEventListener('click', send);
composerEl.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
composerEl.addEventListener('input', () => { resizeComposer(); updateSendState(); });
attachBtn.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', (e) => addFiles(e.target.files));
composerEl.addEventListener('paste', async (e) => {
  const items = e.clipboardData?.items || [];
  const files = [];
  for (const item of items) { if (item.kind === 'file') { const f = item.getAsFile(); if (f) files.push(f); } }
  if (files.length) { e.preventDefault(); await addFiles(files); }
});
micBtn.addEventListener('click', () => (listening ? stopVoice() : startVoice()));

// ─── system panel ─────────────────────────────────────────────────────────

let adminRefreshTimer = null;
function adminOpen() {
  adminPanel.hidden = false;
  requestAnimationFrame(() => requestAnimationFrame(() => adminPanel.classList.add('open')));
  refreshAdmin();
  adminRefreshTimer = setInterval(refreshAdmin, 5000);
}
function adminClose() {
  adminPanel.classList.remove('open');
  if (adminRefreshTimer) { clearInterval(adminRefreshTimer); adminRefreshTimer = null; }
  setTimeout(() => { adminPanel.hidden = true; }, 320);
}
function adminToggle() { adminPanel.classList.contains('open') ? adminClose() : adminOpen(); }

async function refreshAdmin() {
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/health`);
    if (!res.ok) return;
    const h = await res.json();
    renderAdminDaemon(h.daemon || {});
    renderAdminSatellites(h.devices || []);
    renderAdminMemory(h.memory || {});
    renderAdminSkills(h.skills || []);
    renderAdminActivity(h.recent_tool_calls || []);
  } catch {}
}
function renderAdminDaemon(d) {
  $('#admin-daemon-grid').innerHTML = `
    <span class="k">version</span><span class="v">${esc(d.version || '—')}</span>
    <span class="k">model</span><span class="v">${esc((d.model || '—').split('/').slice(-1)[0])}</span>
    <span class="k">messages</span><span class="v">${d.messages ?? '—'}</span>
    <span class="k">tools</span><span class="v">${d.tools_count ?? '—'}</span>
    <span class="k">uptime</span><span class="v">${d.uptime_s ? fmtUptime(d.uptime_s) : '—'}</span>`;
}
function renderAdminSatellites(devices) {
  $('#admin-satellites-count').textContent = devices.length;
  const ul = $('#admin-satellites-list');
  if (!devices.length) { ul.innerHTML = '<li class="empty">no devices connected</li>'; return; }
  ul.innerHTML = devices.map((d) => `
    <li>
      <div><strong>${esc(d.device_id)}</strong></div>
      <div>${(d.capabilities || []).map((c) => `<span class="cap">${esc(c)}</span>`).join('')}</div>
      <span class="meta">${esc((d.platform || '').slice(0, 60))}</span>
    </li>`).join('');
}
function renderAdminMemory(m) {
  $('#admin-memory-count').textContent = m.total ?? '—';
  const ul = $('#admin-memory-list');
  if (!m.available) { ul.innerHTML = '<li class="empty">memory unavailable</li>'; return; }
  const recent = m.recent || [];
  if (!recent.length) { ul.innerHTML = '<li class="empty">nothing remembered yet</li>'; return; }
  ul.innerHTML = recent.slice(0, 12).map((mem) => `
    <li>${esc(mem.content)}<span class="meta">${esc(mem.source)} · ${esc(fmtRelative(mem.created_at))}</span></li>`).join('');
}
function renderAdminSkills(skills) {
  $('#admin-skills-count').textContent = skills.length;
  const ul = $('#admin-skills-list');
  if (!skills.length) { ul.innerHTML = '<li class="empty">no skills installed</li>'; return; }
  ul.innerHTML = skills.map((s) => `<li><div><strong>${esc(s.name)}</strong></div><span class="meta">${esc(s.description || s.slug)}</span></li>`).join('');
}
function renderAdminActivity(tools) {
  const ul = $('#admin-activity-list');
  if (!tools.length) { ul.innerHTML = '<li class="empty">no recent tool calls</li>'; return; }
  ul.innerHTML = tools.slice().reverse().map((t) => `<li><strong>${esc(t.tool_name)}</strong><span class="meta">${esc(t.modality)} · ${esc(fmtRelative(t.created_at))}</span></li>`).join('');
}
function fmtUptime(s) {
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

adminBtn.addEventListener('click', adminToggle);
adminCloseBtn.addEventListener('click', adminClose);
settingsBtn.addEventListener('click', () => window.sunday?.openSettings());
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && adminPanel.classList.contains('open')) adminClose();
  if ((e.metaKey || e.ctrlKey) && e.key === '.') { e.preventDefault(); adminToggle(); }
  if ((e.metaKey || e.ctrlKey) && e.key === ',') { e.preventDefault(); window.sunday?.openSettings(); }
});
if (window.sunday?.onOpenAdmin) window.sunday.onOpenAdmin(() => adminOpen());

// ─── drag & drop ──────────────────────────────────────────────────────────

document.addEventListener('dragover', (e) => { e.preventDefault(); dropzoneEl.hidden = false; });
document.addEventListener('dragleave', (e) => {
  if (e.target === document || e.target === document.documentElement) dropzoneEl.hidden = true;
});
document.addEventListener('drop', async (e) => {
  e.preventDefault(); dropzoneEl.hidden = true;
  if (e.dataTransfer?.files?.length) await addFiles(e.dataTransfer.files);
});

updateSendState();
boot().catch((err) => console.error('boot failed', err));
