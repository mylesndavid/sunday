// Inbox — Sunday's unified activity feed: the calls she placed (VAPI), the
// texts she traded (Sendblue), and the email she handled (AgentMail), all in
// one list/detail surface. Generalized from the old Calls tab — the list →
// click → detail-with-transcript shape that already worked for voice is
// exactly what text and email threads want too.
//
// The daemon holds every provider key and serves a merged, normalized feed at
// `GET /v1/inbox?channel=<all|voice|text|email>`; the renderer only ever
// talks to the local daemon. Until that endpoint lands, the Voice facet falls
// back to the original `GET /v1/vapi/calls` so the tab keeps working today.

let cfg = null, els = null;
let loaded = false;
let channel = 'all';
// True once /v1/inbox has 404'd — we stop probing it and serve the VAPI
// fallback directly so every load doesn't pay for a doomed request.
let inboxMissing = false;

export function init(config, refs) { cfg = config; els = refs; wire(); }
export function setDaemon(http) { cfg.daemonHttp = http; }
export function isLoaded() { return loaded; }

export async function load() {
  showList();
  els.empty.hidden = true;
  els.error.hidden = true;
  els.rows.innerHTML = '<div class="calls-loading">Loading…</div>';
  // One response carries the whole list — id, channel, direction, peer, ts,
  // preview, status. We render straight from it; the per-item detail endpoint
  // only fires when a row is opened. A timeout turns a slow or hung request
  // into an honest error state instead of an endless spinner.
  try {
    const items = await fetchItems();
    loaded = true;
    renderRows(items);
  } catch (err) {
    console.warn('inbox load failed', err);
    renderError(err.name === 'TimeoutError'
      ? 'Loading the inbox timed out. Try again in a moment.'
      : 'The daemon is unreachable. Is Sunday running?');
  }
}

// Returns a normalized item list for the active channel. Prefers /v1/inbox;
// on a 404 it falls back to the VAPI call list for the Voice facet (and an
// empty list for text/email, which simply read as "nothing here yet").
async function fetchItems() {
  if (!inboxMissing) {
    const res = await fetch(`${cfg.daemonHttp}/v1/inbox?channel=${encodeURIComponent(channel)}&limit=50`, { signal: AbortSignal.timeout(20000) });
    if (res.status === 404) {
      inboxMissing = true;            // backend not landed yet — use the fallback below
    } else {
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.error) throw new Error(data.error || `Request failed (${res.status}).`);
      return data.items || [];
    }
  }
  // Fallback: the only first-party feed that exists today is voice (VAPI).
  if (channel === 'all' || channel === 'voice') {
    const res = await fetch(`${cfg.daemonHttp}/v1/vapi/calls`, { signal: AbortSignal.timeout(20000) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.error) throw new Error(data.error || `Request failed (${res.status}).`);
    return (data.calls || []).map(callToItem);
  }
  // Text/email have no fallback source yet — an honest empty state, not an error.
  return [];
}

// Map a raw VAPI call (today's /v1/vapi/calls shape) onto the inbox item shape.
function callToItem(c) {
  return {
    id: c.id || '',
    channel: 'voice',
    direction: 'outbound',
    peer: c.to || '?',
    ts: c.createdAt,
    preview: c.assistantName || '',
    status: statusLabel(c),
    statusBad: isBad(c.endedReason, c.status),
    durationSeconds: c.durationSeconds,
    endedReason: c.endedReason,
  };
}

function renderError(msg) {
  els.rows.innerHTML = '';
  els.empty.hidden = true;
  els.error.hidden = false;
  els.errorSub.textContent = msg;
}

function renderRows(items) {
  els.rows.innerHTML = '';
  if (!items.length) { els.empty.hidden = false; return; }
  els.empty.hidden = true;
  els.error.hidden = true;
  for (const it of items) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'call-row';
    row.dataset.id = it.id || '';

    const badge = document.createElement('span');
    badge.className = 'call-channel';
    badge.textContent = channelLabel(it.channel);

    const when = document.createElement('span');
    when.className = 'call-when mono';
    when.textContent = fmtTime(it.ts);

    const peer = document.createElement('span');
    peer.className = 'call-to';
    peer.textContent = it.peer || '?';

    const preview = document.createElement('span');
    preview.className = 'call-purpose';
    // CSS ellipsis truncates a long preview; the title surfaces the full text.
    preview.textContent = it.preview || '—';
    if (it.preview) preview.title = it.preview;

    const dur = document.createElement('span');
    dur.className = 'call-dur mono';
    // Voice rows show duration; text/email leave the slot blank.
    dur.textContent = it.channel === 'voice' ? fmtDuration(it.durationSeconds) : '';

    const status = document.createElement('span');
    const bad = it.statusBad === true;
    status.className = `call-status ${bad ? 'call-status-bad' : 'call-status-ok'}`;
    status.textContent = prettify(it.status) || '—';
    // The raw ended reason is the useful field for voice; keep it within reach.
    if (it.endedReason) status.title = it.endedReason;

    row.append(badge, when, peer, preview, dur, status);
    row.addEventListener('click', () => openDetail(it.id, it.channel));
    els.rows.appendChild(row);
  }
}

async function openDetail(id, rowChannel) {
  if (!id) return;
  showDetail();
  els.detailTo.textContent = 'Loading…';
  els.detailMeta.textContent = '';
  // Reset both detail shapes so a stale one never leaks across opens.
  resetVoiceDetail();
  resetThreadDetail();
  try {
    const item = await fetchDetail(id, rowChannel);
    if (item && item.error) {
      els.detailTo.textContent = 'Could not load item';
      showVoiceDetail();
      els.transcript.textContent = item.error;
      return;
    }
    renderDetail(item);
  } catch (err) {
    console.warn('inbox detail failed', err);
    els.detailTo.textContent = 'Could not load item';
    showVoiceDetail();
    els.transcript.textContent = err.name === 'TimeoutError'
      ? 'Loading this item timed out. Try again in a moment.'
      : 'The daemon is unreachable.';
  }
}

async function fetchDetail(id, rowChannel) {
  if (!inboxMissing) {
    const res = await fetch(`${cfg.daemonHttp}/v1/inbox/${encodeURIComponent(id)}`, { signal: AbortSignal.timeout(20000) });
    if (res.status === 404) {
      inboxMissing = true;            // fall through to the VAPI detail below
    } else {
      const d = await res.json().catch(() => ({}));
      if (!res.ok || d.error) return { error: d.error || `Request failed (${res.status}).` };
      return d;
    }
  }
  // Fallback — the only detail source today is a VAPI call.
  const res = await fetch(`${cfg.daemonHttp}/v1/vapi/calls/${encodeURIComponent(id)}`, { signal: AbortSignal.timeout(20000) });
  const c = await res.json().catch(() => ({}));
  if (!res.ok || c.error) return { error: c.error || `Request failed (${res.status}).` };
  return { ...c, channel: rowChannel || 'voice' };
}

function renderDetail(d) {
  const ch = d.channel || (d.recordingUrl || d.transcript ? 'voice' : 'text');
  els.detailTo.textContent = d.peer || d.to || '?';

  const bits = [];
  const ts = d.ts || d.createdAt;
  if (ts) bits.push(fmtTime(ts));
  if (ch === 'voice') {
    bits.push(fmtDuration(d.durationSeconds));
    bits.push(statusLabel(d));
    if (d.assistantName) bits.push(d.assistantName);
  } else {
    bits.push(channelLabel(ch));
    if (d.subject) bits.push(d.subject);
    if (d.status) bits.push(prettify(d.status));
  }
  els.detailMeta.textContent = bits.filter(Boolean).join(' · ');

  if (ch === 'voice') renderVoiceDetail(d);
  else renderThreadDetail(d);
}

function renderVoiceDetail(c) {
  showVoiceDetail();
  if (c.recordingUrl) {
    els.audio.src = c.recordingUrl;
    els.audio.hidden = false;
    els.noAudio.hidden = true;
  } else {
    els.audio.hidden = true; els.audio.removeAttribute('src');
    els.noAudio.hidden = false;
  }

  if (c.summary) {
    els.summary.textContent = c.summary;
    els.summaryWrap.hidden = false;
  } else {
    els.summaryWrap.hidden = true;
  }

  els.transcript.innerHTML = '';
  const text = (c.transcript || '').trim();
  if (text) {
    for (const line of text.split('\n')) {
      const p = document.createElement('p');
      p.className = 'call-line';
      p.textContent = line;
      els.transcript.appendChild(p);
    }
  } else {
    els.transcript.textContent = 'No transcript is available for this call.';
  }
}

function renderThreadDetail(d) {
  showThreadDetail();
  els.thread.innerHTML = '';
  // A thread is a list of messages; a single text/email may arrive as just a
  // body, which we render as one inbound bubble.
  let msgs = Array.isArray(d.messages) ? d.messages : null;
  if (!msgs) {
    const body = (d.body || d.preview || '').trim();
    msgs = body ? [{ direction: d.direction || 'inbound', body, ts: d.ts || d.createdAt }] : [];
  }
  if (!msgs.length) {
    els.thread.textContent = 'No messages are available for this thread.';
    return;
  }
  for (const m of msgs) {
    const bubble = document.createElement('div');
    const out = (m.direction || '').toLowerCase() === 'outbound';
    bubble.className = `inbox-msg ${out ? 'inbox-msg-out' : 'inbox-msg-in'}`;
    bubble.textContent = (m.body || m.text || '').trim() || '—';
    const meta = document.createElement('span');
    meta.className = 'inbox-msg-meta';
    meta.textContent = [out ? 'Sunday' : (m.peer || d.peer || ''), fmtTime(m.ts)].filter(Boolean).join(' · ');
    bubble.appendChild(meta);
    els.thread.appendChild(bubble);
  }
}

// ─── detail mode toggles ───────────────────────────────────────────────

function showVoiceDetail() { els.voiceDetail.hidden = false; els.threadWrap.hidden = true; }
function showThreadDetail() { els.voiceDetail.hidden = true; els.threadWrap.hidden = false; }

function resetVoiceDetail() {
  els.audio.hidden = true; els.audio.removeAttribute('src');
  els.noAudio.hidden = true;
  els.summaryWrap.hidden = true; els.summary.textContent = '';
  els.transcript.textContent = '—';
}
function resetThreadDetail() { els.thread.innerHTML = '—'; }

function showList() { els.list.hidden = false; els.detail.hidden = true; }
function showDetail() {
  els.list.hidden = true; els.detail.hidden = false;
  // Stop any audio from a previously open item.
  els.audio.pause?.();
}

function wire() {
  els.refresh.addEventListener('click', () => load());
  els.back.addEventListener('click', () => { els.audio.pause?.(); showList(); });
  // Channel pills — switch the active facet and reload the list.
  for (const pill of els.filter.querySelectorAll('.inbox-pill')) {
    pill.addEventListener('click', () => {
      channel = pill.dataset.channel || 'all';
      for (const p of els.filter.querySelectorAll('.inbox-pill')) p.classList.toggle('active', p === pill);
      load();
    });
  }
}

// ─── formatting ────────────────────────────────────────────────────────

function channelLabel(ch) {
  switch ((ch || '').toLowerCase()) {
    case 'voice': return 'Voice';
    case 'text': return 'Text';
    case 'email': return 'Email';
    case 'webhook': return 'Hook';
    default: return ch ? prettify(ch) : '—';
  }
}

function isBad(endedReason, status) {
  if (status && status !== 'ended') return false;          // in-progress isn't "bad"
  if (!endedReason) return false;
  const r = String(endedReason).toLowerCase();
  if (r.includes('customer-ended') || r.includes('assistant-ended') || r.includes('hangup')) return false;
  // Everything else — no-answer, busy, error, failed, voicemail, etc. — reads as a miss.
  return true;
}

function statusLabel(c) {
  if (c.status && c.status !== 'ended') return prettify(c.status);
  if (c.endedReason) return prettify(c.endedReason);
  return prettify(c.status || 'unknown');
}

function prettify(s) {
  return String(s || '').replace(/[-_]/g, ' ').replace(/\./g, ' ').trim() || '—';
}

function fmtDuration(secs) {
  if (secs == null || isNaN(secs)) return '—';
  const total = Math.round(secs);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '—';
  const day = d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  return `${day} · ${time}`;
}
