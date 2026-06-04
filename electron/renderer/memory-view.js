// Memory tab — Conversations, Atoms, and Meetings panes. (The old
// force-directed memory map was removed when memory moved to flat facts +
// hybrid recall; a map of extracted entities no longer reflected how Sunday
// actually remembers.)

let cfg = null;          // { daemonHttp }
let els = null;          // dom refs

export function init(config, refs) {
  cfg = config; els = refs;
  wireSubtabs();
  refreshAtomCount();
}

// Refresh whatever pane is active — called when the Memory tab is opened.
export function refresh() {
  const active = document.querySelector('.mem-subtab.active');
  switchSubtab(active ? active.dataset.mempane : 'conversations');
}

// ── Memory sub-tabs (Map / Atoms) ─────────────────────────────────────────
function wireSubtabs() {
  const tabs = document.querySelectorAll('.mem-subtab');
  tabs.forEach((t) => t.addEventListener('click', () => switchSubtab(t.dataset.mempane)));
}
function switchSubtab(name) {
  document.querySelectorAll('.mem-subtab').forEach((t) => t.classList.toggle('active', t.dataset.mempane === name));
  const atoms = document.getElementById('mem-pane-atoms');
  const convs = document.getElementById('mem-pane-conversations');
  const mtgs = document.getElementById('mem-pane-meetings');
  const hideAll = () => { atoms.hidden = true; convs.hidden = true; mtgs.hidden = true; };
  if (name === 'atoms') {
    hideAll(); atoms.hidden = false; loadAtoms();
  } else if (name === 'meetings') {
    hideAll(); mtgs.hidden = false; loadMeetings();
  } else {
    hideAll(); convs.hidden = false; loadConversations();
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
  // Always land on the record card + list — never a stuck detail overlay. The
  // pop-out (mtg-view) only re-hid itself on its own Exit, so opening one
  // meeting and switching tabs left it covering the whole pane (no record
  // button, "can't start a meeting"). Reset it on every entry.
  const mtgView = document.getElementById('mtg-view');
  if (mtgView) mtgView.hidden = true;
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
// Tray "Start/Stop meeting" lands here. Main calls this via executeJavaScript
// with the userGesture flag set, so getDisplayMedia's transient-activation
// requirement is satisfied even though the click was in the menu bar. Brings
// the Meetings tab forward first so you can see the recording state.
window.__sundayTrayMeeting = async function () {
  try { switchSubtab('meetings'); } catch {}
  // Make sure the record card is wired + state is fresh, then toggle.
  try { await loadMeetings(); } catch {}
  return toggleMeetingRecording();
};

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
