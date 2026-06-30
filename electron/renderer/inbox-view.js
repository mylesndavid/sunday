// Inbox — Sunday's unified activity feed as an email-client master–detail view:
// a persistent list on the left (the calls she placed via VAPI, the texts she
// traded via Sendblue, the email she handled via AgentMail) and the selected
// item's thread/transcript on the right. Filter pills + search up top. One
// frame — the right pane swaps, the list never goes away.
//
// The daemon holds every provider key and serves a merged, normalized feed at
// `GET /v1/inbox?channel=<all|voice|text|email>`; the renderer only ever talks
// to the local daemon. Until that endpoint lands, the Voice facet falls back to
// the original `GET /v1/vapi/calls` so the tab keeps working today.
//
// Non-blocking is the rule: the pills, the list shell, and the empty right pane
// render instantly. The list fetch is async with a per-fetch timeout; while it
// runs only the LIST shows a spinner — pills, search, and the right pane stay
// live. Switching facets is instant even mid-flight (a sequence guard drops the
// stale result), so a slow Voice fetch can never freeze the tab.

let cfg = null, els = null;
let loaded = false;
let channel = 'all';
// True once /v1/inbox has 404'd — we stop probing it and serve the VAPI
// fallback directly so every load doesn't pay for a doomed request.
let inboxMissing = false;
// Monotonic guard: each list load bumps this; a returning fetch only paints if
// it's still the latest, so switching facets mid-flight ignores stale results.
let listSeq = 0;
// Detail guard — same idea for the right pane, so a slow detail can't clobber a
// newer selection.
let detailSeq = 0;
// The items currently shown in the list (for client-side search + selection).
let currentItems = [];
let selectedId = null;
let searchQuery = '';
// Whether the active single-channel facet is configured — set on each empty
// load, drives the "Set up <channel>" CTA vs a plain "nothing here yet".
let currentFacetSetup = true;

// Extra elements not wired through app.js's refs — queried by id directly.
let elSearch = null, elDetailEmpty = null, elDetailBody = null;

export function init(config, refs) {
  cfg = config; els = refs;
  elSearch = document.getElementById('inbox-search');
  elDetailEmpty = document.getElementById('inbox-detail-empty');
  elDetailBody = document.getElementById('inbox-detail-body');
  wire();
}
export function setDaemon(http) { cfg.daemonHttp = http; }
export function isLoaded() { return loaded; }

// Live-refresh the list in place: re-paints the active facet preserving the
// selection (fetchList keeps selectedId), without touching the right pane. Used
// by the WS 'inbox' broadcast when a message is sent/received while the Inbox is
// open. Only meaningful once the view has been opened at least once.
export function refresh() {
  if (!els) return;
  fetchList();
}

// load() paints the shell instantly and kicks the list fetch off async. It
// never awaits the network before returning control, so the tab is interactive
// the moment it's shown.
export function load() {
  showEmptyDetail();
  selectedId = null;
  fetchList();
}

// Fetch the active facet's list. Spinner lives inside the list pane only; the
// pills, search, and right pane stay interactive throughout.
async function fetchList() {
  const seq = ++listSeq;
  els.empty.hidden = true;
  els.error.hidden = true;
  els.rows.innerHTML = '<div class="inbox-loading"><span class="inbox-spinner"></span>Loading…</div>';
  try {
    const items = await fetchItems();
    if (seq !== listSeq) return;            // a newer facet superseded this load
    loaded = true;
    currentItems = items;
    // For an empty single-channel facet, find out whether that channel is even
    // set up — drives the "Set up X" CTA vs a plain "nothing here yet".
    if (!items.length && channel !== 'all') {
      currentFacetSetup = await isFacetSetup(channel);
      if (seq !== listSeq) return;
    } else {
      currentFacetSetup = true;
    }
    renderRows();
  } catch (err) {
    if (seq !== listSeq) return;
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

// The list's empty state, rendered INSIDE the rows area (a flex column) so it
// centers via margin:auto — a sibling element gets shoved to the bottom by the
// flex:1 rows. For a single-channel facet that isn't configured yet it becomes
// an actionable "Set up <channel>" button instead of a dead end.
function paintEmpty() {
  els.empty.hidden = true;
  els.error.hidden = true;
  const inner = document.createElement('div');
  inner.className = 'inbox-empty-inner';
  const h = document.createElement('h3');
  const p = document.createElement('p');
  if (channel !== 'all' && !currentFacetSetup) {
    const noun = nounFor(channel);
    h.textContent = `${cap(noun)} isn't set up yet`;
    p.textContent = `Connect ${noun} and it'll land here.`;
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn btn-primary inbox-empty-cta';
    b.textContent = `Set up ${noun}`;
    b.addEventListener('click', () => gotoChannelSetup(channel));
    inner.append(h, p, b);
  } else {
    h.textContent = 'Nothing here yet';
    p.textContent = 'When Sunday places a call, sends a text, or gets an email, it shows up here.';
    inner.append(h, p);
  }
  els.rows.innerHTML = '';
  els.rows.appendChild(inner);
}

// Is the channel behind this facet actually configured? Best-effort — on any
// error we assume it IS set up (no false "set up X" nag), since an empty inbox
// for a working channel is perfectly normal.
async function isFacetSetup(ch) {
  const url = ch === 'text' ? '/v1/net/status'
    : ch === 'email' ? '/v1/channels/agentmail/status'
    : ch === 'voice' ? '/v1/vapi/status' : null;
  if (!url) return true;
  try {
    const d = await (await fetch(`${cfg.daemonHttp}${url}`, { signal: AbortSignal.timeout(8000) })).json();
    const s = d.sendblue || d;          // /v1/net/status wraps the sendblue block
    return !!(s.connected || s.configured);
  } catch { return true; }
}

// Jump to Settings → Channels and open the relevant channel's form.
function gotoChannelSetup(ch) {
  const panelId = { text: 'sb-panel', email: 'am-panel', voice: 'vapi-panel' }[ch];
  document.querySelector('.tab[data-view="settings"]')?.click();
  document.querySelector('.set-navitem[data-page="page-channels"]')?.click();
  if (!panelId) return;
  const panel = document.getElementById(panelId);
  if (!panel) return;
  panel.classList.remove('is-collapsed');                 // open the form
  const edit = panel.querySelector('.ch-edit');
  if (edit) edit.textContent = 'Done';
  setTimeout(() => panel.scrollIntoView({ behavior: 'smooth', block: 'center' }), 60);
}

function nounFor(ch) {
  return ch === 'text' ? 'text messaging'
    : ch === 'email' ? 'email'
    : ch === 'voice' ? 'calling'
    : channelLabel(ch).toLowerCase();
}
function cap(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : s; }

// Paint the list from currentItems, honouring the client-side search filter and
// the selected-row highlight.
function renderRows() {
  els.rows.innerHTML = '';
  const q = searchQuery;
  const items = q
    ? currentItems.filter((it) =>
        (it.peer || '').toLowerCase().includes(q) || (it.preview || '').toLowerCase().includes(q))
    : currentItems;

  if (!currentItems.length) { paintEmpty(); return; }
  els.empty.hidden = true;
  els.error.hidden = true;
  if (!items.length) {
    els.rows.innerHTML = `<div class="inbox-loading">No matches for “${esc(q)}”.</div>`;
    return;
  }

  for (const it of items) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'inbox-row';
    if (it.id && it.id === selectedId) row.classList.add('is-active');
    if (!it.read) row.classList.add('is-unread');
    row.dataset.id = it.id || '';

    const badge = document.createElement('span');
    badge.className = `inbox-glyph inbox-glyph-${(it.channel || '').toLowerCase()}`;
    badge.textContent = channelGlyph(it.channel);
    badge.title = channelLabel(it.channel);

    const main = document.createElement('span');
    main.className = 'inbox-row-main';

    const top = document.createElement('span');
    top.className = 'inbox-row-top';
    const peer = document.createElement('span');
    peer.className = 'inbox-row-peer';
    peer.textContent = it.peer || '?';
    const when = document.createElement('span');
    when.className = 'inbox-row-when mono';
    when.textContent = fmtTime(it.ts);
    top.append(peer, when);

    const bottom = document.createElement('span');
    bottom.className = 'inbox-row-bottom';
    const preview = document.createElement('span');
    preview.className = 'inbox-row-preview';
    preview.textContent = it.preview || '—';
    if (it.preview) preview.title = it.preview;
    const dot = document.createElement('span');
    dot.className = `inbox-row-dot ${it.read ? 'is-read' : 'is-unread'}`;
    dot.title = it.read ? '' : 'Unread';
    bottom.append(preview, dot);

    main.append(top, bottom);
    row.append(badge, main);
    row.addEventListener('click', () => { selectedId = it.id; markRead(it); renderRows(); openDetail(it.id, it.channel); });
    els.rows.appendChild(row);
  }
}

// Opening an item marks it read — optimistically (the blue dot clears now) and
// persisted via the daemon so it stays read across the 30s refresh and restarts.
function markRead(it) {
  if (!it || it.read || !it.id) return;
  it.read = true;
  fetch(`${cfg.daemonHttp}/v1/inbox/${encodeURIComponent(it.id)}/read`, { method: 'POST' }).catch(() => {});
}

async function openDetail(id, rowChannel) {
  if (!id) return;
  const seq = ++detailSeq;
  showDetailBody();
  els.detailTo.textContent = 'Loading…';
  els.detailMeta.textContent = '';
  // Reset both detail shapes so a stale one never leaks across opens.
  resetVoiceDetail();
  resetThreadDetail();
  try {
    const item = await fetchDetail(id, rowChannel);
    if (seq !== detailSeq) return;          // a newer selection superseded this
    if (item && item.error) {
      els.detailTo.textContent = 'Could not load item';
      showVoiceDetail();
      els.transcript.textContent = item.error;
      return;
    }
    renderDetail(item);
  } catch (err) {
    if (seq !== detailSeq) return;
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

// Right pane: empty placeholder vs. the loaded detail body.
function showEmptyDetail() {
  if (elDetailEmpty) elDetailEmpty.hidden = false;
  if (elDetailBody) elDetailBody.hidden = true;
  els.audio.pause?.();
}
function showDetailBody() {
  if (elDetailEmpty) elDetailEmpty.hidden = true;
  if (elDetailBody) elDetailBody.hidden = false;
  // Stop any audio from a previously open item.
  els.audio.pause?.();
}

function wire() {
  els.refresh?.addEventListener('click', () => fetchList());
  // Back is vestigial in the split layout (the list is always present), but
  // app.js may still pass it — wire it to clear the selection if so.
  els.back?.addEventListener('click', () => { els.audio.pause?.(); selectedId = null; renderRows(); showEmptyDetail(); });
  // Channel pills — switch the active facet and reload the list. Instant even
  // if a prior fetch is still in flight (the sequence guard drops stale results).
  for (const pill of els.filter.querySelectorAll('.inbox-pill')) {
    pill.addEventListener('click', () => {
      channel = pill.dataset.channel || 'all';
      for (const p of els.filter.querySelectorAll('.inbox-pill')) p.classList.toggle('active', p === pill);
      selectedId = null;
      showEmptyDetail();
      fetchList();
    });
  }
  // Client-side search — filters the visible list by peer/preview, no fetch.
  elSearch?.addEventListener('input', () => {
    searchQuery = (elSearch.value || '').trim().toLowerCase();
    renderRows();
  });
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

function channelGlyph(ch) {
  switch ((ch || '').toLowerCase()) {
    case 'voice': return '☎';
    case 'text': return '💬';
    case 'email': return '✉';
    case 'webhook': return '⚓';
    default: return '•';
  }
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
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
