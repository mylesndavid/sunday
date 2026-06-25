// Calls — phone calls Sunday placed on your behalf, pulled live from VAPI.
//
// The daemon holds the VAPI API key and proxies the call list, transcripts,
// and recordings; the renderer only ever talks to the local daemon. List
// view shows the recent calls; clicking a row opens the detail with the
// transcript, the summary, and an audio player for the recording.

let cfg = null, els = null;
let loaded = false;

export function init(config, refs) { cfg = config; els = refs; wire(); }
export function setDaemon(http) { cfg.daemonHttp = http; }
export function isLoaded() { return loaded; }

export async function load() {
  showList();
  els.empty.hidden = true;
  els.error.hidden = true;
  els.rows.innerHTML = '<div class="calls-loading">Loading calls…</div>';
  // The whole list comes from this one response — id, time, number, status,
  // ended reason, duration, name, and whether a recording exists. We render
  // straight from it and never touch the per-call detail endpoint here; that
  // only fires when a row is opened. A timeout turns a slow or hung request
  // into an honest error state instead of an endless spinner.
  try {
    const res = await fetch(`${cfg.daemonHttp}/v1/vapi/calls`, { signal: AbortSignal.timeout(20000) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.error) {
      renderError(data.error || `Request failed (${res.status}).`);
      return;
    }
    loaded = true;
    renderRows(data.calls || []);
  } catch (err) {
    console.warn('calls load failed', err);
    renderError(err.name === 'TimeoutError'
      ? 'Loading calls timed out. Try again in a moment.'
      : 'The daemon is unreachable. Is Sunday running?');
  }
}

function renderError(msg) {
  els.rows.innerHTML = '';
  els.empty.hidden = true;
  els.error.hidden = false;
  els.errorSub.textContent = msg;
}

function renderRows(calls) {
  els.rows.innerHTML = '';
  if (!calls.length) { els.empty.hidden = false; return; }
  els.empty.hidden = true;
  els.error.hidden = true;
  for (const c of calls) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'call-row';
    row.dataset.id = c.id || '';

    const when = document.createElement('span');
    when.className = 'call-when mono';
    when.textContent = fmtTime(c.createdAt);

    const to = document.createElement('span');
    to.className = 'call-to';
    to.textContent = c.to || '?';

    const purpose = document.createElement('span');
    purpose.className = 'call-purpose';
    // CSS ellipsis truncates a long name; the title surfaces the full text.
    purpose.textContent = c.assistantName || '—';
    if (c.assistantName) purpose.title = c.assistantName;

    const dur = document.createElement('span');
    dur.className = 'call-dur mono';
    dur.textContent = fmtDuration(c.durationSeconds);

    const status = document.createElement('span');
    const bad = isBad(c.endedReason, c.status);
    status.className = `call-status ${bad ? 'call-status-bad' : 'call-status-ok'}`;
    status.textContent = statusLabel(c);
    // The ended reason is the useful field (e.g. call.start.error-get-transport).
    // The pill prettifies it; the title keeps the raw value within reach.
    if (c.endedReason) status.title = c.endedReason;

    row.append(when, to, purpose, dur, status);
    row.addEventListener('click', () => openDetail(c.id));
    els.rows.appendChild(row);
  }
}

async function openDetail(id) {
  if (!id) return;
  showDetail();
  els.detailTo.textContent = 'Loading…';
  els.detailMeta.textContent = '';
  els.audio.hidden = true; els.audio.removeAttribute('src');
  els.noAudio.hidden = true;
  els.summaryWrap.hidden = true;
  els.summary.textContent = '';
  els.transcript.textContent = '—';
  try {
    const res = await fetch(`${cfg.daemonHttp}/v1/vapi/calls/${encodeURIComponent(id)}`, { signal: AbortSignal.timeout(20000) });
    const c = await res.json().catch(() => ({}));
    if (!res.ok || c.error) {
      els.detailTo.textContent = 'Could not load call';
      els.transcript.textContent = c.error || `Request failed (${res.status}).`;
      return;
    }
    renderDetail(c);
  } catch (err) {
    console.warn('call detail failed', err);
    els.detailTo.textContent = 'Could not load call';
    els.transcript.textContent = err.name === 'TimeoutError'
      ? 'Loading this call timed out. Try again in a moment.'
      : 'The daemon is unreachable.';
  }
}

function renderDetail(c) {
  els.detailTo.textContent = c.to || '?';
  const bits = [];
  if (c.createdAt) bits.push(fmtTime(c.createdAt));
  bits.push(fmtDuration(c.durationSeconds));
  bits.push(statusLabel(c));
  if (c.assistantName) bits.push(c.assistantName);
  els.detailMeta.textContent = bits.filter(Boolean).join(' · ');

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

function showList() { els.list.hidden = false; els.detail.hidden = true; }
function showDetail() {
  els.list.hidden = true; els.detail.hidden = false;
  // Stop any audio from a previously open call.
  els.audio.pause?.();
}

function wire() {
  els.refresh.addEventListener('click', () => load());
  els.back.addEventListener('click', () => { els.audio.pause?.(); showList(); });
}

// ─── formatting ────────────────────────────────────────────────────────

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
