// Memory map — an interactive force-directed graph of who/what Sunday
// knows about you. Pure canvas, no libraries. Drag the background to pan,
// drag a node to move it, scroll to zoom, click a node to see the plain
// facts behind it. No graph jargon surfaces — categories read as People,
// Pets, Places, etc.

const KIND = {
  person:       { label: 'People',  varName: '--g-person' },
  pet:          { label: 'Pets',    varName: '--g-pet' },
  place:        { label: 'Places',  varName: '--g-place' },
  organization: { label: 'Work',    varName: '--g-org' },
  project:      { label: 'Projects',varName: '--g-project' },
  thing:        { label: 'Things',  varName: '--g-thing' },
  event:        { label: 'Events',  varName: '--g-event' },
  topic:        { label: 'Topics',  varName: '--g-topic' },
};

let cfg = null;          // { daemonHttp }
let els = null;          // dom refs
let canvas, ctx, dpr = 1;
let nodes = [], links = [];
let selected = null, hover = null;
let view = { x: 0, y: 0, scale: 1 };
let drag = null;         // { node } | { pan:true, sx, sy, ox, oy }
let raf = null, alpha = 0;
let colorCache = {};
let loaded = false;

function cssColor(varName) {
  if (colorCache[varName]) return colorCache[varName];
  const c = getComputedStyle(document.documentElement).getPropertyValue(varName).trim() || '#999';
  colorCache[varName] = c;
  return c;
}
function kindColor(kind) { return cssColor((KIND[kind] || KIND.thing).varName); }

export function init(config, refs) {
  cfg = config; els = refs;
  canvas = refs.canvas; ctx = canvas.getContext('2d');
  bindEvents();
  buildLegend();
  wireSubtabs();
  refreshAtomCount();
}

// ── Memory sub-tabs (Map / Atoms) ─────────────────────────────────────────
function wireSubtabs() {
  const tabs = document.querySelectorAll('.mem-subtab');
  tabs.forEach((t) => t.addEventListener('click', () => switchSubtab(t.dataset.mempane)));
}
function switchSubtab(name) {
  document.querySelectorAll('.mem-subtab').forEach((t) => t.classList.toggle('active', t.dataset.mempane === name));
  const map = document.getElementById('mem-pane-map');
  const detail = document.getElementById('mem-detail');
  const atoms = document.getElementById('mem-pane-atoms');
  const convs = document.getElementById('mem-pane-conversations');
  const mtgs = document.getElementById('mem-pane-meetings');
  const hideAll = () => { map.hidden = true; detail.hidden = true; atoms.hidden = true; convs.hidden = true; mtgs.hidden = true; };
  if (name === 'atoms') {
    hideAll(); atoms.hidden = false; loadAtoms();
  } else if (name === 'conversations') {
    hideAll(); convs.hidden = false; loadConversations();
  } else if (name === 'meetings') {
    hideAll(); mtgs.hidden = false; loadMeetings();
  } else {
    hideAll(); map.hidden = false;
    // mem-detail visibility is owned by selectNode; leave it alone
  }
}

// ── Meetings tab ─────────────────────────────────────────────────────────
let _mtgRecording = false;
let _mtgTimer = null;
function fmtElapsed(secs) {
  const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60), s = secs % 60;
  return h > 0 ? `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}` : `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
}
async function loadMeetings() {
  const btn = document.getElementById('mtg-rec-btn');
  const titleEl = document.getElementById('mtg-rec-title');
  const subEl = document.getElementById('mtg-rec-sub');
  // Wire the record button once.
  if (btn && !btn.dataset.wired) {
    btn.dataset.wired = '1';
    btn.addEventListener('click', () => toggleMeetingRecording());
  }
  // Reflect current recording state.
  try {
    const st = await window.sunday.meetingState();
    setMeetingRecordingUI(!!st.recording, st.since);
  } catch {}
  // List past meetings (conversations with category=meeting).
  const list = document.getElementById('mtg-list');
  const empty = document.getElementById('mtg-empty');
  const cb = document.getElementById('mem-mtg-count');
  try {
    const d = await (await fetch(`${cfg.daemonHttp}/v1/conversations?limit=100&source=meeting&min_value=all`)).json();
    const mtgs = (d.conversations || []);
    if (cb) { if (mtgs.length) { cb.textContent = mtgs.length; cb.hidden = false; } else cb.hidden = true; }
    if (!mtgs.length) { empty.hidden = false; list.innerHTML = ''; return; }
    empty.hidden = true;
    list.innerHTML = mtgs.map((c) => `
      <li class="conv-card mtg-card" data-cid="${c.id}">
        <div class="conv-head"><div class="c-title">${esc(c.title || 'Meeting')}</div><div class="c-time">${esc(fmtTime(c.started_at))}</div></div>
        <div class="conv-summary">${esc((c.summary || '').split('\n')[0])}</div>
        <div class="mtg-card-open">Open meeting →</div>
      </li>`).join('');
    list.querySelectorAll('.mtg-card').forEach((card) => {
      card.addEventListener('click', () => openMeetingView(card.dataset.cid));
    });
  } catch (e) {
    list.innerHTML = `<li class="conv-card"><div class="conv-summary" style="color:var(--error)">couldn't load: ${esc(e.message)}</div></li>`;
  }
}
function setMeetingRecordingUI(recording, since) {
  _mtgRecording = recording;
  const btn = document.getElementById('mtg-rec-btn');
  const titleEl = document.getElementById('mtg-rec-title');
  const subEl = document.getElementById('mtg-rec-sub');
  if (_mtgTimer) { clearInterval(_mtgTimer); _mtgTimer = null; }
  if (recording) {
    btn.textContent = 'Stop recording'; btn.dataset.recording = 'true';
    const start = since ? since : Date.now();
    const tick = () => { titleEl.textContent = `● Recording — ${fmtElapsed(Math.max(0, Math.floor((Date.now() - start) / 1000)))}`; };
    tick(); _mtgTimer = setInterval(tick, 1000);
    subEl.textContent = 'Both sides, on this Mac. Stop when the meeting ends and your notes will appear below.';
  } else {
    btn.textContent = 'Start recording'; btn.dataset.recording = 'false';
    titleEl.textContent = 'Record a meeting';
    subEl.textContent = 'Captures both sides — you and everyone on the call — on this Mac. A summary, action items, and the full transcript land here when you stop.';
  }
}
// Full meeting view — pops over the tab, Exit to close.
async function openMeetingView(cid) {
  const view = document.getElementById('mtg-view');
  view.hidden = false;
  document.getElementById('mtg-view-title').textContent = 'Loading…';
  document.getElementById('mtg-view-notes').innerHTML = '';
  document.getElementById('mtg-view-transcript').textContent = '';
  document.getElementById('mtg-view-exit').onclick = () => { view.hidden = true; };
  try {
    const c = await (await fetch(`${cfg.daemonHttp}/v1/conversations/${cid}`)).json();
    document.getElementById('mtg-view-title').textContent = c.title || 'Meeting';
    document.getElementById('mtg-view-time').textContent = fmtTime(c.started_at);
    // Render the structured summary as readable blocks (it was stored with
    // newlines + bullets).
    document.getElementById('mtg-view-notes').innerHTML =
      esc(c.summary || '').replace(/\n/g, '<br>');
    // Speaker-labeled transcript → styled lines.
    const tx = c.transcript || '(no transcript)';
    document.getElementById('mtg-view-transcript').innerHTML = tx.split('\n').map((line) => {
      const who = line.startsWith('You:') ? 'you' : (line.startsWith('Others:') ? 'others' : '');
      return `<div class="mtg-line${who ? ' ' + who : ''}">${esc(line)}</div>`;
    }).join('');
    // Audio playback if this meeting's recording is still on disk.
    const audio = document.getElementById('mtg-view-audio');
    try {
      const a = await window.sunday.meetingAudio(cid);
      if (a && a.url) { audio.src = a.url; audio.hidden = false; } else { audio.hidden = true; audio.removeAttribute('src'); }
    } catch { audio.hidden = true; }
  } catch (e) {
    document.getElementById('mtg-view-title').textContent = `Error: ${e.message}`;
  }
}

// Capture state lives here in the main renderer — getDisplayMedia (system
// audio) needs the user gesture from THIS button click, which a hidden
// window doesn't have.
let _mtgSysStream = null, _mtgMicStream = null, _mtgSysRec = null, _mtgMicRec = null;

async function toggleMeetingRecording() {
  const btn = document.getElementById('mtg-rec-btn');
  const subEl = document.getElementById('mtg-rec-sub');
  btn.disabled = true;
  try {
    if (!_mtgRecording) {
      // Acquire BOTH streams first (user gesture is live right now).
      let sysTrack = null, micErr = null, sysErr = null;
      try {
        _mtgSysStream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
        _mtgSysStream.getVideoTracks().forEach((t) => t.stop());
        sysTrack = _mtgSysStream.getAudioTracks()[0] || null;
      } catch (e) { sysErr = e.name || String(e); }
      try {
        _mtgMicStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      } catch (e) { micErr = e.name || String(e); }

      if (!sysTrack && !_mtgMicStream) {
        subEl.textContent = `Couldn't capture audio (system: ${sysErr || 'none'}, mic: ${micErr || 'none'}). Grant Screen Recording + Microphone in Permissions.`;
        return;
      }
      // Tell main to set up files.
      const begin = await window.sunday.meetingBegin();
      if (!begin.ok) { subEl.textContent = `Couldn't start: ${begin.error}`; return; }
      // Stream each track to main.
      if (sysTrack) _mtgSysRec = recordTrack(new MediaStream([sysTrack]), 'system');
      if (_mtgMicStream) _mtgMicRec = recordTrack(_mtgMicStream, 'mic');
      setMeetingRecordingUI(true, Date.now());
      if (!sysTrack) subEl.textContent = 'Note: system audio not captured (only your mic). Grant Screen Recording for both sides.';
    } else {
      await stopAndFinalizeMeeting();
    }
  } finally { btn.disabled = false; }
}

function recordTrack(stream, label) {
  let mr;
  try { mr = new MediaRecorder(stream, { mimeType: 'audio/webm' }); }
  catch { mr = new MediaRecorder(stream); }
  mr.ondataavailable = async (e) => {
    if (!e.data || !e.data.size) return;
    try { const buf = await e.data.arrayBuffer(); await window.sunday.meetingChunk(label, new Uint8Array(buf)); } catch {}
  };
  mr.start(4000);
  return mr;
}

async function stopAndFinalizeMeeting() {
  setMeetingRecordingUI(false);
  document.getElementById('mtg-rec-title').textContent = 'Writing your notes…';
  // Stop recorders + tracks, flush.
  for (const mr of [_mtgSysRec, _mtgMicRec]) { try { mr && mr.state !== 'inactive' && mr.stop(); } catch {} }
  for (const s of [_mtgSysStream, _mtgMicStream]) { try { s && s.getTracks().forEach((t) => t.stop()); } catch {} }
  _mtgSysRec = _mtgMicRec = _mtgSysStream = _mtgMicStream = null;
  await new Promise((r) => setTimeout(r, 700));   // let last chunks land
  const r = await window.sunday.meetingFinalize();
  if (r.ok) { document.getElementById('mtg-rec-title').textContent = 'Record a meeting'; loadMeetings(); }
  else { document.getElementById('mtg-rec-title').textContent = 'Record a meeting'; document.getElementById('mtg-rec-sub').textContent = `Stopped — ${r.error || 'no notes produced'}.`; }
}
// The notch's stop-request comes here.
if (window.sunday && window.sunday.onMeetingStopNow) {
  window.sunday.onMeetingStopNow(() => { if (_mtgRecording) stopAndFinalizeMeeting(); });
}

let _convShowAll = false;
async function loadConversations() {
  const ul = document.getElementById('conv-list');
  const empty = document.getElementById('conv-empty');
  ul.innerHTML = '';
  try {
    const param = _convShowAll ? '&min_value=all' : '';
    const r = await fetch(`${cfg.daemonHttp}/v1/conversations?limit=200${param}`);
    const d = await r.json();
    const convs = d.conversations || [];
    const hiddenLow = (d.hidden && d.hidden.low) || 0;
    const cb = document.getElementById('mem-conv-count');
    if (convs.length) { cb.textContent = convs.length; cb.hidden = false; } else { cb.hidden = true; }
    if (!convs.length) { empty.hidden = false; return; }
    empty.hidden = true;

    // Header row with a "show N hidden" disclosure when the filter is active
    // and there's actual junk being suppressed.
    let headerHtml = '';
    if (!_convShowAll && hiddenLow > 0) {
      headerHtml = `<li class="conv-toggle">
        <button class="conv-toggle-btn" id="conv-show-all">Show ${hiddenLow} low-value (TikToks, ambient noise, fragments)</button>
      </li>`;
    } else if (_convShowAll) {
      headerHtml = `<li class="conv-toggle">
        <button class="conv-toggle-btn" id="conv-show-all">Hide low-value</button>
      </li>`;
    }

    ul.innerHTML = headerHtml + convs.map((c) => {
      const people = (c.participants || []).join(', ') || '—';
      const valueDot = c.value ? `<span class="conv-value" data-v="${esc(c.value)}" title="${esc(c.value)}"></span>` : '';
      return `
        <li class="conv-card" data-cid="${c.id}" data-value="${esc(c.value || '')}">
          <div class="conv-head">
            ${valueDot}
            <div class="c-title">${esc(c.title || 'Untitled')}</div>
            <div class="c-time">${esc(fmtTime(c.started_at))}</div>
          </div>
          <div class="conv-meta">
            <span class="conv-cat" data-c="${esc(c.category || 'unclear')}">${esc(c.category || 'unclear')}</span>
            <span class="c-people">${esc(people)}</span>
          </div>
          <div class="conv-summary">${esc(c.summary || '')}</div>
          <details class="conv-transcript">
            <summary>Show transcript</summary>
            <pre data-loaded="false">loading…</pre>
          </details>
        </li>`;
    }).join('');
    document.getElementById('conv-show-all')?.addEventListener('click', () => {
      _convShowAll = !_convShowAll;
      loadConversations();
    });
    // Lazy-load transcript when a card's <details> is opened.
    ul.querySelectorAll('.conv-card').forEach((card) => {
      const d = card.querySelector('details');
      const pre = d.querySelector('pre');
      d.addEventListener('toggle', async () => {
        if (!d.open || pre.dataset.loaded === 'true') return;
        pre.dataset.loaded = 'true';
        try {
          const r = await fetch(`${cfg.daemonHttp}/v1/conversations/${card.dataset.cid}`);
          const c = await r.json();
          pre.textContent = c.transcript || '(no transcript)';
        } catch (err) { pre.textContent = `(error: ${err.message})`; }
      });
    });
  } catch (err) {
    ul.innerHTML = `<li class="conv-card"><div class="conv-summary" style="color:var(--error)">couldn't load conversations: ${esc(err.message)}</div></li>`;
  }
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
  const isYesterday = d.toDateString() === yesterday.toDateString();
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  if (sameDay) return time;
  if (isYesterday) return `Yesterday ${time}`;
  return `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${time}`;
}

async function loadAtoms() {
  const ul = document.getElementById('atoms-list');
  const empty = document.getElementById('atoms-empty');
  ul.innerHTML = '';
  try {
    const r = await fetch(`${cfg.daemonHttp}/v1/atoms?limit=200`);
    const d = await r.json();
    const atoms = d.atoms || [];
    if (!atoms.length) { empty.hidden = false; return; }
    empty.hidden = true;
    ul.innerHTML = atoms.map((a) => `
      <li class="atom">
        <span class="a-state" data-s="${esc(a.state || 'active')}">${esc(a.state || 'active')}</span>
        <span class="a-kind">${esc(a.kind || '')}</span>
        <span class="a-text">${esc(a.text || '')}</span>
        <span class="a-time">${esc(ago(a.updated_at))}</span>
      </li>`).join('');
  } catch (err) {
    ul.innerHTML = `<li class="atom"><span class="a-text" style="color:var(--error)">couldn't load atoms: ${esc(err.message)}</span></li>`;
  }
}

async function refreshAtomCount() {
  try {
    const r = await fetch(`${cfg.daemonHttp}/v1/atoms?state=active&limit=1`);
    const d = await r.json();
    const s = document.getElementById('mem-atoms-count');
    // ask once more for the open count via status (cheaper, includes atoms_open)
    const st = await (await fetch(`${cfg.daemonHttp}/v1/status`)).json();
    const n = st.atoms_open ?? (d.atoms || []).length;
    if (n > 0) { s.textContent = n; s.hidden = false; } else { s.hidden = true; }
  } catch {}
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
function ago(ts) {
  if (!ts) return '';
  const s = Math.max(0, Date.now()/1000 - ts);
  if (s < 60) return 'now';
  if (s < 3600) return `${Math.floor(s/60)}m`;
  if (s < 86400) return `${Math.floor(s/3600)}h`;
  return `${Math.floor(s/86400)}d`;
}

export function setDaemon(http) { cfg.daemonHttp = http; }

function buildLegend() {
  els.legend.innerHTML = Object.values(KIND).map((k) =>
    `<span class="lg"><span class="sw" style="background:${cssColor(k.varName)}"></span>${k.label}</span>`
  ).join('');
}

export async function load(force) {
  els.refresh.classList.add('spin');
  try {
    const url = `${cfg.daemonHttp}/v1/memory/graph${force ? '/rebuild' : ''}`;
    const res = await fetch(url, { method: force ? 'POST' : 'GET' });
    const data = await res.json();
    ingest(data);
  } catch (err) {
    console.warn('memory graph load failed', err);
  } finally {
    els.refresh.classList.remove('spin');
  }
}

function ingest(data) {
  const incoming = data.nodes || [];
  els.empty.hidden = incoming.length > 0;
  // preserve positions of nodes we already have
  const prev = new Map(nodes.map((n) => [n.id, n]));
  const rect = canvas.getBoundingClientRect();
  const cx = rect.width / 2, cy = rect.height / 2;
  nodes = incoming.map((n) => {
    const p = prev.get(n.id);
    return {
      ...n,
      x: p ? p.x : cx + (Math.random() - 0.5) * 240,
      y: p ? p.y : cy + (Math.random() - 0.5) * 240,
      vx: 0, vy: 0,
      r: 13 + Math.min(n.degree || 0, 8) * 2.6,
    };
  });
  const byId = new Map(nodes.map((n) => [n.id, n]));
  links = (data.links || []).filter((l) => byId.has(l.source) && byId.has(l.target))
    .map((l) => ({ ...l, a: byId.get(l.source), b: byId.get(l.target) }));
  loaded = true;
  alpha = 1;
  resize();
  kick();
}

// ── physics ──
function step() {
  const rect = canvas.getBoundingClientRect();
  const cx = rect.width / 2, cy = rect.height / 2;
  const REPULSE = 5200, SPRING = 0.012, REST = 118, GRAV = 0.015, DAMP = 0.86;
  for (let i = 0; i < nodes.length; i++) {
    const a = nodes[i];
    if (drag && drag.node === a) continue;
    for (let j = i + 1; j < nodes.length; j++) {
      const b = nodes[j];
      let dx = a.x - b.x, dy = a.y - b.y;
      let d2 = dx * dx + dy * dy || 0.01;
      const f = REPULSE / d2;
      const d = Math.sqrt(d2);
      const fx = (dx / d) * f, fy = (dy / d) * f;
      a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
    }
    a.vx += (cx - a.x) * GRAV; a.vy += (cy - a.y) * GRAV;
  }
  for (const l of links) {
    let dx = l.b.x - l.a.x, dy = l.b.y - l.a.y;
    const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
    const f = (d - REST) * SPRING;
    const fx = (dx / d) * f, fy = (dy / d) * f;
    if (!(drag && drag.node === l.a)) { l.a.vx += fx; l.a.vy += fy; }
    if (!(drag && drag.node === l.b)) { l.b.vx -= fx; l.b.vy -= fy; }
  }
  for (const n of nodes) {
    if (drag && drag.node === n) continue;
    n.vx *= DAMP; n.vy *= DAMP;
    n.x += n.vx * alpha; n.y += n.vy * alpha;
  }
  alpha *= 0.992;
  if (alpha < 0.02) alpha = 0.02;
}

function draw() {
  const rect = canvas.getBoundingClientRect();
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, rect.width, rect.height);
  ctx.save();
  ctx.translate(view.x, view.y);
  ctx.scale(view.scale, view.scale);

  // links
  ctx.lineWidth = 1.4;
  for (const l of links) {
    const active = selected && (l.a.id === selected.id || l.b.id === selected.id);
    ctx.strokeStyle = active ? cssColor('--accent') : 'rgba(120,110,90,0.28)';
    ctx.beginPath(); ctx.moveTo(l.a.x, l.a.y); ctx.lineTo(l.b.x, l.b.y); ctx.stroke();
    if (active && l.label) {
      const mx = (l.a.x + l.b.x) / 2, my = (l.a.y + l.b.y) / 2;
      ctx.font = '11px ' + cssColor('--font-mono');
      ctx.fillStyle = cssColor('--text-3');
      ctx.textAlign = 'center';
      ctx.fillText(l.label, mx, my - 4);
    }
  }
  // nodes
  for (const n of nodes) {
    const col = kindColor(n.kind);
    const isSel = selected && n.id === selected.id;
    const isHov = hover && n.id === hover.id;
    const dim = selected && !isSel && !links.some((l) => (l.a.id === selected.id && l.b.id === n.id) || (l.b.id === selected.id && l.a.id === n.id));
    ctx.globalAlpha = dim ? 0.35 : 1;
    if (isSel || isHov) { ctx.beginPath(); ctx.arc(n.x, n.y, n.r + 5, 0, Math.PI * 2); ctx.fillStyle = col + '33'; ctx.fill(); }
    ctx.beginPath(); ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
    ctx.fillStyle = col; ctx.fill();
    ctx.lineWidth = 2.5; ctx.strokeStyle = cssColor('--surface'); ctx.stroke();
    // label
    ctx.globalAlpha = dim ? 0.4 : 1;
    ctx.font = (isSel ? '600 13px ' : '500 12px ') + cssColor('--font-sans');
    ctx.fillStyle = cssColor('--text');
    ctx.textAlign = 'center';
    ctx.fillText(n.name, n.x, n.y + n.r + 14);
    ctx.globalAlpha = 1;
  }
  ctx.restore();
}

function tick() {
  step(); draw();
  raf = requestAnimationFrame(tick);
}
function kick() { if (!raf) tick(); }

export function resize() {
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  dpr = window.devicePixelRatio || 1;
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  if (!raf) draw();
}

// ── interaction ──
function toWorld(e) {
  const rect = canvas.getBoundingClientRect();
  return { x: (e.clientX - rect.left - view.x) / view.scale, y: (e.clientY - rect.top - view.y) / view.scale };
}
function nodeAt(p) {
  for (let i = nodes.length - 1; i >= 0; i--) {
    const n = nodes[i];
    if ((p.x - n.x) ** 2 + (p.y - n.y) ** 2 <= (n.r + 4) ** 2) return n;
  }
  return null;
}
function bindEvents() {
  let downPos = null, moved = false;
  canvas.addEventListener('mousedown', (e) => {
    const p = toWorld(e); const n = nodeAt(p); downPos = { x: e.clientX, y: e.clientY }; moved = false;
    if (n) drag = { node: n }; else drag = { pan: true, sx: e.clientX, sy: e.clientY, ox: view.x, oy: view.y };
  });
  window.addEventListener('mousemove', (e) => {
    if (downPos && (Math.abs(e.clientX - downPos.x) > 3 || Math.abs(e.clientY - downPos.y) > 3)) moved = true;
    if (drag?.node) { const p = toWorld(e); drag.node.x = p.x; drag.node.y = p.y; drag.node.vx = 0; drag.node.vy = 0; alpha = Math.max(alpha, 0.4); }
    else if (drag?.pan) { view.x = drag.ox + (e.clientX - drag.sx); view.y = drag.oy + (e.clientY - drag.sy); if (!raf) draw(); }
    else { const old = hover; hover = nodeAt(toWorld(e)); if (hover !== old && !raf) draw(); canvas.style.cursor = hover ? 'pointer' : 'grab'; }
  });
  window.addEventListener('mouseup', (e) => {
    if (drag?.node && !moved) selectNode(drag.node);
    else if (drag?.pan && !moved) selectNode(null);
    drag = null; downPos = null;
  });
  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    const factor = e.deltaY < 0 ? 1.1 : 0.9;
    const ns = Math.min(2.4, Math.max(0.4, view.scale * factor));
    view.x = mx - (mx - view.x) * (ns / view.scale);
    view.y = my - (my - view.y) * (ns / view.scale);
    view.scale = ns;
    if (!raf) draw();
  }, { passive: false });
}

function selectNode(n) {
  selected = n;
  // Don't perturb the simulation on click — just redraw the highlight so
  // nodes stay put. (The detail panel is an overlay, so no resize either.)
  if (!raf) draw();
  if (!n) { els.detail.hidden = true; return; }
  els.detail.hidden = false;
  const k = KIND[n.kind] || KIND.thing;
  els.detailKind.textContent = k.label.replace(/s$/, '');
  els.detailKind.style.background = kindColor(n.kind) + '22';
  els.detailKind.style.color = kindColor(n.kind);
  els.detailName.textContent = n.name;
  els.detailFacts.innerHTML = (n.facts && n.facts.length)
    ? n.facts.map((f) => `<li>${escapeHtml(f)}</li>`).join('')
    : '<li style="background:none;color:var(--text-3);padding-left:0">No specific notes yet.</li>';
  const conns = links.filter((l) => l.a.id === n.id || l.b.id === n.id).map((l) => {
    const other = l.a.id === n.id ? l.b : l.a;
    return { other, rel: l.label };
  });
  els.detailConns.innerHTML = conns.length
    ? conns.map((c) => `<li data-id="${c.other.id}"><span class="sw" style="background:${kindColor(c.other.kind)}"></span><span>${escapeHtml(c.other.name)}</span><span class="rel">${escapeHtml(c.rel || '')}</span></li>`).join('')
    : '<li style="cursor:default;color:var(--text-3)">Nothing connected yet.</li>';
  els.detailConns.querySelectorAll('li[data-id]').forEach((li) => {
    li.onclick = () => { const t = nodes.find((x) => x.id == li.dataset.id); if (t) selectNode(t); };
  });
}

function escapeHtml(s) { return String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

export function closeDetail() { selectNode(null); }
export function isLoaded() { return loaded; }
