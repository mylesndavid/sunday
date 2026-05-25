// Sunday desktop — renderer.
//
// One WS to the daemon, one chat rendered in order, one composer that
// sends text + attachments. Voice goes through the browser's
// SpeechRecognition for now (Whisper via the daemon is the upgrade path).
// "Hey Sunday" wake word is a continuous SpeechRecognition listener that
// hands focus to the composer and arms an immediate voice utterance.

import { initWakeWord, stopWakeWord } from './wake.js';

const $ = (sel) => document.querySelector(sel);
const chatEl       = $('#chat');
const composerEl   = $('#composer');
const sendBtn      = $('#send-btn');
const micBtn       = $('#mic-btn');
const attachBtn    = $('#attach-btn');
const fileInput    = $('#file-input');
const attachmentsEl = $('#attachments');
const statusEl     = $('#status');
const brandDot     = document.querySelector('.brand-dot');
const dropzoneEl   = $('#dropzone');
const adminBtn     = $('#admin-btn');
const adminPanel   = $('#admin-panel');
const adminCloseBtn= $('#admin-close');

let DAEMON_HTTP = 'http://127.0.0.1:8765';
let DAEMON_WS   = 'ws://127.0.0.1:8765/v1/ws';
let ws = null;
let pendingAttachments = []; // {path, filename, mime_type, size, kind, id}

// ─── boot ──────────────────────────────────────────────────────────────

async function boot() {
  if (window.sunday) {
    const cfg = await window.sunday.getConfig();
    DAEMON_HTTP = cfg.daemonHttp || DAEMON_HTTP;
    DAEMON_WS   = cfg.daemonWs   || DAEMON_WS;
  }
  await refreshLog();
  await refreshStatus();
  connectWs();
  initWakeWord({ onTrigger: handleWakeTrigger });
}

function setOnline(state) {
  brandDot.dataset.state = state;
  if (state === 'online')     statusEl.textContent = 'online';
  if (state === 'connecting') statusEl.textContent = 'connecting…';
  if (state === 'offline')    statusEl.textContent = 'offline — daemon down?';
  if (window.sunday) window.sunday.setOverlayState({ connection: state });
}

async function refreshStatus() {
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/status`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    statusEl.textContent = `${data.model}  ·  ${data.messages} msgs  ·  ${data.tools.length} tools`;
    setOnline('online');
  } catch (err) {
    setOnline('offline');
  }
}

async function refreshLog() {
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/log?limit=80`);
    const data = await res.json();
    chatEl.innerHTML = '';
    for (const msg of data.messages || []) renderMessage(msg);
    scrollToEnd();
  } catch (err) {
    console.warn('log fetch failed', err);
  }
}

function connectWs() {
  try { ws = new WebSocket(DAEMON_WS); } catch (err) { setOnline('offline'); return; }
  ws.onopen    = () => setOnline('online');
  ws.onclose   = () => { setOnline('offline'); setTimeout(connectWs, 2000); };
  ws.onerror   = ()  => setOnline('offline');
  ws.onmessage = (evt) => {
    try {
      const event = JSON.parse(evt.data);
      handleWsEvent(event);
    } catch {}
  };
}

let activeStream = null;  // { id, bodyEl, articleEl }

function handleWsEvent(event) {
  if (event.type === 'stream_start') {
    activeStream = beginStreamBubble(event);
    return;
  }
  if (event.type === 'stream_delta') {
    if (activeStream && activeStream.id === event.stream_id) {
      activeStream.bodyEl.textContent += (event.content || '');
      scrollToEnd();
    }
    return;
  }
  if (event.type === 'stream_end') {
    if (activeStream && activeStream.id === event.stream_id) {
      activeStream.articleEl.classList.remove('streaming');
    }
    activeStream = null;
    // Pull the final, DB-backed message so we get the real id + metadata.
    refreshLog();
    refreshStatus();
    return;
  }
  if (event.type === 'reply') {
    // Non-streaming fallback path (Hermes runtime) — no stream_* events
    // arrived, so just refresh the whole log.
    if (!activeStream) refreshLog();
    return;
  }
  if (event.type === 'browser_frame' || event.type === 'device_browser_frame' || event.type === 'device_screen') {
    showLiveFrame(event);
    return;
  }
  if (event.type === 'device_online' || event.type === 'device_offline') {
    refreshStatus();
    return;
  }
}

function beginStreamBubble(event) {
  const article = document.createElement('article');
  article.className = 'msg sunday streaming';
  article.dataset.streamId = event.stream_id;

  const meta = document.createElement('div');
  meta.className = 'msg-meta';
  meta.innerHTML = `<span class="who">sunday</span><span class="mod">${event.modality || ''}</span>`;
  article.appendChild(meta);

  const body = document.createElement('div');
  body.className = 'msg-body';
  article.appendChild(body);

  chatEl.appendChild(article);
  scrollToEnd();
  return { id: event.stream_id, bodyEl: body, articleEl: article };
}

// ─── rendering ─────────────────────────────────────────────────────────

function renderMessage(msg) {
  const el = document.createElement('article');
  el.className = `msg ${msg.role}`;
  el.dataset.id = msg.id;

  const meta = document.createElement('div');
  meta.className = 'msg-meta';
  const who = msg.role === 'user' ? 'you' : msg.role;
  meta.innerHTML = `<span class="who">${who}</span><span class="mod">${msg.modality}</span>`;
  el.appendChild(meta);

  const body = document.createElement('div');
  body.className = 'msg-body';
  body.textContent = msg.content || '';
  el.appendChild(body);

  const atts = (msg.metadata && msg.metadata.attachments) || [];
  if (atts.length) {
    const wrap = document.createElement('div');
    wrap.className = 'msg-attachments';
    for (const a of atts) wrap.appendChild(renderAttachment(a));
    el.appendChild(wrap);
  }

  chatEl.appendChild(el);
}

function renderAttachment(a) {
  const kind = a.kind || guessKind(a.mime_type || '');
  if (kind === 'image') {
    const img = document.createElement('img');
    img.src = a.url || (a.path ? `file://${a.path}` : '');
    img.alt = a.filename || '';
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

function scrollToEnd() {
  chatEl.scrollTop = chatEl.scrollHeight;
}

function showLiveFrame(event) {
  // Floating live frame; a future iteration could pin this somewhere
  // permanent. For now, append a synthetic system message with the image.
  const fake = {
    id: `frame-${Date.now()}`,
    role: 'system',
    modality: 'browser',
    content: event.url ? `live: ${event.url}` : 'live frame',
    metadata: {
      attachments: event.screenshot_path
        ? [{ kind: 'image', path: event.screenshot_path, filename: 'frame.png', mime_type: 'image/png' }]
        : [],
    },
  };
  renderMessage(fake);
  scrollToEnd();
}

// ─── sending ───────────────────────────────────────────────────────────

async function send() {
  const text = composerEl.value.trim();
  if (!text && pendingAttachments.length === 0) return;

  // Optimistic local render so the user sees their message immediately.
  renderMessage({
    id: `local-${Date.now()}`,
    role: 'user',
    modality: 'electron',
    content: text,
    metadata: pendingAttachments.length ? { attachments: pendingAttachments } : null,
  });
  scrollToEnd();

  const payload = { text, modality: 'electron' };
  if (pendingAttachments.length) payload.attachments = pendingAttachments;

  composerEl.value = '';
  pendingAttachments = [];
  renderAttachmentChips();
  resizeComposer();

  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/say`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      renderMessage({ id: `err-${Date.now()}`, role: 'system', modality: 'electron', content: `error: ${data.error || res.status}` });
      scrollToEnd();
      return;
    }
    // The WS reply event will trigger refreshLog() — that re-renders.
    setTimeout(refreshLog, 50);
  } catch (err) {
    renderMessage({ id: `err-${Date.now()}`, role: 'system', modality: 'electron', content: `network error: ${err.message}` });
    scrollToEnd();
  }
}

// ─── attachments ───────────────────────────────────────────────────────

function renderAttachmentChips() {
  attachmentsEl.innerHTML = '';
  for (const a of pendingAttachments) {
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.innerHTML = `${a.filename} <button title="remove">×</button>`;
    chip.querySelector('button').onclick = () => {
      pendingAttachments = pendingAttachments.filter((x) => x.id !== a.id);
      renderAttachmentChips();
    };
    attachmentsEl.appendChild(chip);
  }
}

// Read a File as a data: URL so we can send it INSIDE the say payload —
// no path dependency, works across a remote daemon. The daemon's
// Attachment.to_llm_image_url already accepts data: URLs natively for
// vision content. ~16MB cap so we don't blow up the request size.
const MAX_ATTACH_BYTES = 16 * 1024 * 1024;

function readAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error || new Error('read failed'));
    reader.onload  = () => resolve(reader.result);   // already a data:<mime>;base64,... URL
    reader.readAsDataURL(file);
  });
}

async function fileToAttachmentMeta(file) {
  if (file.size > MAX_ATTACH_BYTES) {
    return { error: `${file.name}: ${Math.round(file.size / 1024 / 1024)}MB exceeds the 16MB attachment cap` };
  }
  let dataUrl;
  try { dataUrl = await readAsDataURL(file); }
  catch (err) { return { error: `${file.name}: read failed (${err.message})` }; }

  const mime = file.type || 'application/octet-stream';
  return {
    id:        `att-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    url:       dataUrl,           // daemon reads URL, not path
    path:      '',                // empty — we don't rely on filesystem paths
    filename:  file.name,
    mime_type: mime,
    size:      file.size,
    kind:      guessKind(mime),
  };
}

async function addFiles(files) {
  for (const file of files) {
    const meta = await fileToAttachmentMeta(file);
    if (meta.error) {
      renderMessage({
        id: `warn-${Date.now()}`, role: 'system', modality: 'electron',
        content: meta.error,
      });
      scrollToEnd();
      continue;
    }
    pendingAttachments.push(meta);
  }
  renderAttachmentChips();
}

// ─── voice + wake word ─────────────────────────────────────────────────

let recog = null;
let listening = false;

function startVoice(immediate = false) {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) {
    renderMessage({ id: `warn-${Date.now()}`, role: 'system', modality: 'electron', content: 'voice not supported in this build — use text for now.' });
    scrollToEnd();
    return;
  }
  recog = new Recognition();
  recog.lang = 'en-US';
  recog.interimResults = true;
  recog.continuous = false;
  let finalText = '';
  recog.onstart = () => { listening = true; micBtn.dataset.active = 'true'; if (window.sunday) window.sunday.setOverlayState({ listening: true }); };
  recog.onresult = (e) => {
    finalText = '';
    let interim = '';
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const t = e.results[i][0].transcript;
      if (e.results[i].isFinal) finalText += t; else interim += t;
    }
    composerEl.value = (finalText + interim).trim();
    resizeComposer();
  };
  recog.onerror = (e) => { console.warn('voice error', e); listening = false; micBtn.dataset.active = 'false'; };
  recog.onend   = () => {
    listening = false;
    micBtn.dataset.active = 'false';
    if (window.sunday) window.sunday.setOverlayState({ listening: false });
    if (finalText.trim()) send();
  };
  recog.start();
}

function stopVoice() {
  if (recog && listening) recog.stop();
}

function handleWakeTrigger() {
  composerEl.focus();
  startVoice(true);
}

// ─── interactions ──────────────────────────────────────────────────────

sendBtn.addEventListener('click', send);
composerEl.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});
composerEl.addEventListener('input', resizeComposer);
function resizeComposer() {
  composerEl.style.height = 'auto';
  composerEl.style.height = Math.min(composerEl.scrollHeight, 240) + 'px';
}

attachBtn.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', (e) => addFiles(e.target.files));
// Paste an image directly into the composer — common UX shortcut.
composerEl.addEventListener('paste', async (e) => {
  const items = e.clipboardData?.items || [];
  const files = [];
  for (const item of items) {
    if (item.kind === 'file') {
      const f = item.getAsFile();
      if (f) files.push(f);
    }
  }
  if (files.length) {
    e.preventDefault();
    await addFiles(files);
  }
});

micBtn.addEventListener('click', () => (listening ? stopVoice() : startVoice()));

// ─── admin panel ───────────────────────────────────────────────────────

let adminRefreshTimer = null;

function adminOpen() {
  adminPanel.hidden = false;
  // Two-frame delay so the transition triggers from translateX(100%) → 0
  requestAnimationFrame(() => requestAnimationFrame(() => adminPanel.classList.add('open')));
  refreshAdmin();
  adminRefreshTimer = setInterval(refreshAdmin, 5000);
}
function adminClose() {
  adminPanel.classList.remove('open');
  if (adminRefreshTimer) { clearInterval(adminRefreshTimer); adminRefreshTimer = null; }
  setTimeout(() => { adminPanel.hidden = true; }, 220);
}
function adminToggle() {
  if (adminPanel.classList.contains('open')) adminClose(); else adminOpen();
}

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
  const grid = $('#admin-daemon-grid');
  const uptime = d.uptime_s ? formatUptime(d.uptime_s) : '—';
  grid.innerHTML = `
    <span class="k">version</span><span class="v">${d.version || '—'}</span>
    <span class="k">model</span><span class="v">${d.model || '—'}</span>
    <span class="k">messages</span><span class="v">${d.messages ?? '—'}</span>
    <span class="k">tools</span><span class="v">${d.tools_count ?? '—'}</span>
    <span class="k">uptime</span><span class="v">${uptime}</span>
  `;
}

function renderAdminSatellites(devices) {
  $('#admin-satellites-count').textContent = devices.length;
  const ul = $('#admin-satellites-list');
  if (!devices.length) { ul.innerHTML = '<li class="empty">no satellites connected</li>'; return; }
  ul.innerHTML = devices.map((d) => `
    <li>
      <div><strong>${d.device_id}</strong></div>
      <div>${(d.capabilities || []).map((c) => `<span class="cap">${c}</span>`).join('')}</div>
      <span class="meta">${(d.platform || '').slice(0, 60)}</span>
    </li>
  `).join('');
}

function renderAdminMemory(m) {
  $('#admin-memory-count').textContent = m.total ?? '—';
  const ul = $('#admin-memory-list');
  if (!m.available) { ul.innerHTML = '<li class="empty">memory disabled (set OPENAI_API_KEY)</li>'; return; }
  const recent = m.recent || [];
  if (!recent.length) { ul.innerHTML = '<li class="empty">no memories yet — talk to Sunday</li>'; return; }
  ul.innerHTML = recent.slice(0, 12).map((mem) => `
    <li>
      ${escapeHtml(mem.content)}
      <span class="meta">${mem.source} · ${formatRelative(mem.created_at)}</span>
    </li>
  `).join('');
}

function renderAdminSkills(skills) {
  $('#admin-skills-count').textContent = skills.length;
  const ul = $('#admin-skills-list');
  if (!skills.length) { ul.innerHTML = '<li class="empty">no skills installed (~/.sunday/skills/)</li>'; return; }
  ul.innerHTML = skills.map((s) => `
    <li>
      <div><strong>${s.name}</strong></div>
      <span class="meta">${escapeHtml(s.description || s.slug)}</span>
    </li>
  `).join('');
}

function renderAdminActivity(tools) {
  const ul = $('#admin-activity-list');
  if (!tools.length) { ul.innerHTML = '<li class="empty">no recent tool calls</li>'; return; }
  ul.innerHTML = tools.slice().reverse().map((t) => `
    <li>
      <strong>${t.tool_name}</strong>
      <span class="meta">${t.modality} · ${formatRelative(t.created_at)}</span>
    </li>
  `).join('');
}

function formatUptime(seconds) {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

function formatRelative(epoch) {
  const diff = (Date.now() / 1000) - epoch;
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86400)}d ago`;
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

adminBtn.addEventListener('click', adminToggle);
adminCloseBtn.addEventListener('click', adminClose);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && adminPanel.classList.contains('open')) adminClose();
  if ((e.metaKey || e.ctrlKey) && e.key === '.') { e.preventDefault(); adminToggle(); }
});

// Tray menu can ask us to open the admin panel
if (window.sunday?.onOpenAdmin) {
  window.sunday.onOpenAdmin(() => adminOpen());
}

document.addEventListener('dragover', (e) => { e.preventDefault(); dropzoneEl.hidden = false; });
document.addEventListener('dragleave', (e) => {
  if (e.target === document || e.target === document.documentElement) dropzoneEl.hidden = true;
});
document.addEventListener('drop', async (e) => {
  e.preventDefault();
  dropzoneEl.hidden = true;
  if (e.dataTransfer?.files?.length) await addFiles(e.dataTransfer.files);
});

// Boot.
boot().catch((err) => console.error('boot failed', err));
