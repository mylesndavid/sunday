// Timeline — a semantic activity view of what you were doing, styled after
// Dayflow: a vertical rail of hour rows, each activity a card with a category-
// colored left bar, title, and time range. Clicking a card opens a detail pane
// with the minute-by-minute play-by-play (scenes) plus the raw screenshots as
// evidence. "Wrapped" rolls a period up into a reflection.
//
// Capture + frames are the same on-device engine that powered Rewind; the
// screenshot scrubber survives only inside the detail pane now. Frames load via
// the same IPC image bridge (files live under ~/.sunday/rewind).

let cfg = null, els = null;
let loaded = false;
let mode = 'today';                 // today | yesterday | week | wrapped
let events = [];                    // current event list
let selected = null;                // selected event id
let summarizeTimer = null;
let imgCache = new Map();

// Activity type → accent colour (Dayflow-style category palette).
const TYPE_COLOR = {
  coding:    '#6A7EFF',
  browsing:  '#C787F7',
  writing:   '#B984FF',
  design:    '#F78CC7',
  meeting:   '#56CFEE',
  email:     '#FFAE8C',
  messaging: '#4FCFA6',
  media:     '#FF5950',
  admin:     '#A0AEC0',
  other:     '#9AA6B2',
};
const TYPE_LABEL = {
  coding: 'Coding', browsing: 'Research', writing: 'Writing', design: 'Design',
  meeting: 'Meeting', email: 'Email', messaging: 'Messaging', media: 'Media',
  admin: 'Admin', other: 'Activity',
};
const colorFor = (t) => TYPE_COLOR[t] || TYPE_COLOR.other;
const labelFor = (t) => TYPE_LABEL[t] || 'Activity';

export function init(config, refs) { cfg = config; els = refs; wire(); }
export function isLoaded() { return loaded; }

export async function load() {
  loaded = true;
  setMode(mode, true);
}

function wire() {
  els.modes?.querySelectorAll('[data-mode]').forEach((b) =>
    b.addEventListener('click', () => setMode(b.dataset.mode)));
  els.search?.addEventListener('input', debounce(() => {
    const q = els.search.value.trim();
    if (q) runSearch(q); else setMode(mode, true);
  }, 300));
  els.enable?.addEventListener('click', enableCapture);
  els.wrappedPeriod?.querySelectorAll('[data-period]').forEach((b) =>
    b.addEventListener('click', () => loadWrapped(b.dataset.period)));
  els.detailClose?.addEventListener('click', closeDetail);
}

function setMode(m, force) {
  if (m === mode && !force && m !== 'wrapped') return;
  mode = m;
  els.modes?.querySelectorAll('[data-mode]').forEach((b) =>
    b.classList.toggle('active', b.dataset.mode === m));
  els.wrappedPeriod.hidden = m !== 'wrapped';
  closeDetail();
  if (m === 'wrapped') { loadWrapped('week'); return; }
  loadRange(m);
}

// ─── loading ────────────────────────────────────────────────────────────

async function loadRange(m, opts = {}) {
  const { silent = false } = opts;
  if (!silent) showLoading();
  try {
    let url, grouped = false;
    if (m === 'week') {
      const to = Date.now() / 1000;
      const from = to - 7 * 86400;
      url = `${cfg.daemonHttp}/v1/timeline/events?from_ts=${from}&to_ts=${to}&limit=800`;
      grouped = true;
    } else {
      const d = new Date();
      if (m === 'yesterday') d.setDate(d.getDate() - 1);
      url = `${cfg.daemonHttp}/v1/timeline/day?date=${ymd(d)}`;
    }
    const res = await fetch(url);
    const data = await res.json();
    if (data.error) return showState(data.error);
    events = (data.events || []).slice();
    if (!grouped) events.sort((a, b) => a.start_ts - b.start_ts);
    if (!events.length) return silent ? undefined : showEmpty();
    renderTimeline(events, grouped);
    if (!silent) startSummaryPolling();
  } catch (err) {
    console.warn('timeline load failed', err);
    if (!silent) showState('Could not reach the timeline.');
  }
}

async function runSearch(q) {
  showLoading();
  try {
    const res = await fetch(`${cfg.daemonHttp}/v1/timeline/search?q=${encodeURIComponent(q)}`);
    const data = await res.json();
    events = (data.events || []).slice().sort((a, b) => b.start_ts - a.start_ts);
    if (!events.length) return showState(`No moments match “${q}”.`);
    renderTimeline(events, true, `${events.length} result${events.length > 1 ? 's' : ''} for “${q}”`);
  } catch { showState('Search failed.'); }
}

// If any events are still on their heuristic title, kick ONE background
// summarize pass (the daemon drains a batch through the local vision CLI), then
// quietly re-fetch a few times so real titles + play-by-play stream in without
// stacking requests or flashing the loading state.
function startSummaryPolling() {
  clearTimeout(summarizeTimer);
  if (!events.some((e) => !e.summarized)) return;
  fetch(`${cfg.daemonHttp}/v1/timeline/summarize`, { method: 'POST' }).catch(() => {});
  let tries = 0;
  const tick = async () => {
    if (mode === 'wrapped' || els.search.value.trim()) return;
    await loadRange(mode, { silent: true });
    tries += 1;
    if (tries < 8 && events.some((e) => !e.summarized)) summarizeTimer = setTimeout(tick, 15000);
  };
  summarizeTimer = setTimeout(tick, 15000);
}

// ─── timeline rendering ───────────────────────────────────────────────────

function renderTimeline(list, grouped, banner) {
  els.detail.hidden = true;
  els.empty.hidden = true;
  const main = els.main;
  main.innerHTML = '';
  if (banner) {
    const b = document.createElement('div');
    b.className = 'tl-banner';
    b.textContent = banner;
    main.appendChild(b);
  }
  const rail = document.createElement('div');
  rail.className = 'tl-rail';

  let lastDay = null;
  for (const ev of list) {
    if (grouped) {
      const day = new Date(ev.start_ts * 1000).toDateString();
      if (day !== lastDay) {
        lastDay = day;
        const h = document.createElement('div');
        h.className = 'tl-dayhead';
        h.textContent = dayLabel(ev.start_ts);
        rail.appendChild(h);
      }
    }
    rail.appendChild(cardRow(ev));
  }
  main.appendChild(rail);
}

function cardRow(ev) {
  const row = document.createElement('div');
  row.className = 'tl-row';

  const gutter = document.createElement('div');
  gutter.className = 'tl-gutter mono';
  gutter.textContent = clock(ev.start_ts);
  row.appendChild(gutter);

  const card = document.createElement('button');
  card.className = 'tl-card';
  card.style.setProperty('--cat', colorFor(ev.type));
  card.dataset.id = ev.id;
  if (ev.id === selected) card.classList.add('sel');

  const badge = document.createElement('span');
  badge.className = 'tl-badge';
  badge.style.background = colorFor(ev.type);
  badge.textContent = (ev.dominant_app || labelFor(ev.type) || '?').trim().charAt(0).toUpperCase();

  const body = document.createElement('div');
  body.className = 'tl-card-body';
  const title = document.createElement('div');
  title.className = 'tl-card-title';
  title.textContent = ev.title || labelFor(ev.type);
  const meta = document.createElement('div');
  meta.className = 'tl-card-meta';
  meta.textContent = `${clock(ev.start_ts)} – ${clock(ev.end_ts)} · ${labelFor(ev.type)}`;
  body.appendChild(title);
  body.appendChild(meta);

  card.appendChild(badge);
  card.appendChild(body);
  if (!ev.summarized) {
    const dot = document.createElement('span');
    dot.className = 'tl-pending';
    dot.title = 'summarizing…';
    card.appendChild(dot);
  }
  card.addEventListener('click', () => openDetail(ev));
  row.appendChild(card);
  return row;
}

// ─── detail pane (play-by-play + evidence) ────────────────────────────────

async function openDetail(ev) {
  selected = ev.id;
  els.main.querySelectorAll('.tl-card').forEach((c) =>
    c.classList.toggle('sel', Number(c.dataset.id) === ev.id));
  const d = els.detail;
  d.hidden = false;
  d.style.setProperty('--cat', colorFor(ev.type));
  d.innerHTML = '';

  const head = document.createElement('div');
  head.className = 'tl-d-head';
  head.innerHTML = `
    <button class="tl-d-close" id="tl-d-close" title="Close">✕</button>
    <div class="tl-d-kicker mono">${labelFor(ev.type)} · ${clock(ev.start_ts)} – ${clock(ev.end_ts)}</div>
    <h2 class="tl-d-title"></h2>`;
  head.querySelector('.tl-d-title').textContent = ev.title || labelFor(ev.type);
  head.querySelector('#tl-d-close').addEventListener('click', closeDetail);
  d.appendChild(head);

  if (ev.summary) {
    const p = document.createElement('p');
    p.className = 'tl-d-summary';
    p.textContent = ev.summary;
    d.appendChild(p);
  }

  const chips = [...(ev.apps || []), ...(ev.projects || []).map((x) => '#' + x)];
  if (chips.length) {
    const wrap = document.createElement('div');
    wrap.className = 'tl-chips';
    chips.slice(0, 8).forEach((c) => {
      const s = document.createElement('span'); s.className = 'tl-chip'; s.textContent = c; wrap.appendChild(s);
    });
    d.appendChild(wrap);
  }

  // Play-by-play scenes
  if ((ev.scenes || []).length) {
    const h = document.createElement('h3'); h.className = 'tl-d-h'; h.textContent = 'Play-by-play';
    d.appendChild(h);
    const ul = document.createElement('div');
    ul.className = 'tl-scenes';
    ev.scenes.forEach((s) => {
      const row = document.createElement('div');
      row.className = 'tl-scene';
      row.innerHTML = `<span class="tl-scene-t mono"></span><span class="tl-scene-x"></span>`;
      row.querySelector('.tl-scene-t').textContent = s.time || '';
      row.querySelector('.tl-scene-x').textContent = s.text || '';
      ul.appendChild(row);
    });
    d.appendChild(ul);
  } else if (!ev.summarized) {
    const p = document.createElement('p');
    p.className = 'tl-d-pending';
    p.textContent = 'Reconstructing this session…';
    d.appendChild(p);
  }

  // Evidence: the raw frames, scrubbable (demoted from the old main view).
  const eh = document.createElement('h3'); eh.className = 'tl-d-h'; eh.textContent = 'Evidence';
  d.appendChild(eh);
  const stage = document.createElement('div');
  stage.className = 'tl-evidence';
  stage.innerHTML = `
    <img class="tl-ev-img" id="tl-ev-img" alt="screenshot" hidden>
    <div class="tl-ev-controls">
      <input type="range" class="tl-ev-slider" id="tl-ev-slider" min="0" max="0" value="0">
      <span class="tl-ev-time mono" id="tl-ev-time">—</span>
    </div>`;
  d.appendChild(stage);
  loadEvidence(ev.id, stage);
}

async function loadEvidence(eventId, stage) {
  try {
    const res = await fetch(`${cfg.daemonHttp}/v1/timeline/event-frames?event_id=${eventId}`);
    const data = await res.json();
    const frames = (data.frames || []).slice().sort((a, b) => a.ts - b.ts);
    if (!frames.length) { stage.hidden = true; return; }
    const img = stage.querySelector('#tl-ev-img');
    const slider = stage.querySelector('#tl-ev-slider');
    const timeEl = stage.querySelector('#tl-ev-time');
    slider.max = String(frames.length - 1);
    let seq = 0;
    const show = async (i) => {
      const f = frames[i]; if (!f) return;
      const my = ++seq;
      timeEl.textContent = clock(f.ts);
      const url = await imageFor(f.image_path);
      if (my !== seq) return;
      if (url) { img.src = url; img.hidden = false; }
    };
    slider.addEventListener('input', () => show(parseInt(slider.value, 10)));
    slider.value = String(frames.length - 1);
    show(frames.length - 1);
  } catch { stage.hidden = true; }
}

async function imageFor(path) {
  if (!path) return null;
  if (imgCache.has(path)) return imgCache.get(path);
  const url = await window.sunday?.rewindImage(path);
  if (url) imgCache.set(path, url);
  return url;
}

function closeDetail() { selected = null; if (els.detail) els.detail.hidden = true;
  els.main?.querySelectorAll('.tl-card').forEach((c) => c.classList.remove('sel')); }

// ─── Wrapped ───────────────────────────────────────────────────────────────

async function loadWrapped(period) {
  els.wrappedPeriod?.querySelectorAll('[data-period]').forEach((b) =>
    b.classList.toggle('active', b.dataset.period === period));
  showLoading('Generating your ' + period + '…');
  try {
    const res = await fetch(`${cfg.daemonHttp}/v1/timeline/wrapped?period=${period}`);
    const w = await res.json();
    if (w.error) return showState(w.error);
    if (w.empty) return showState('Not enough activity yet for a ' + period + ' Wrapped.');
    renderWrapped(w, period);
  } catch { showState('Could not generate Wrapped.'); }
}

function renderWrapped(w, period) {
  els.detail.hidden = true;
  els.empty.hidden = true;
  const m = els.main;
  m.innerHTML = '';
  const box = document.createElement('div');
  box.className = 'tl-wrapped';
  const stats = w.stats || {};
  const hrs = Math.round((stats.active_minutes || 0) / 6) / 10;
  box.innerHTML = `
    <div class="tl-w-kicker mono">Your ${period}</div>
    <h1 class="tl-w-title"></h1>
    <p class="tl-w-summary"></p>`;
  box.querySelector('.tl-w-title').textContent = w.title || `Your ${period}`;
  box.querySelector('.tl-w-summary').textContent = w.summary || '';

  const statRow = document.createElement('div');
  statRow.className = 'tl-w-stats';
  statRow.appendChild(stat(`${hrs}h`, 'active'));
  statRow.appendChild(stat(String(stats.event_count || 0), 'sessions'));
  if (stats.busiest_day) statRow.appendChild(stat(shortDay(stats.busiest_day), 'busiest day'));
  box.appendChild(statRow);

  if ((w.highlights || []).length) box.appendChild(listBlock('Highlights', w.highlights));
  if ((w.observations || []).length) box.appendChild(listBlock('What Sunday noticed', w.observations));

  if ((stats.longest_sessions || []).length) {
    const h = document.createElement('h3'); h.className = 'tl-d-h'; h.textContent = 'Longest sessions';
    box.appendChild(h);
    const ul = document.createElement('div'); ul.className = 'tl-scenes';
    stats.longest_sessions.forEach((s) => {
      const r = document.createElement('div'); r.className = 'tl-scene';
      r.innerHTML = `<span class="tl-scene-t mono"></span><span class="tl-scene-x"></span>`;
      r.querySelector('.tl-scene-t').textContent = `${s.minutes}m`;
      r.querySelector('.tl-scene-x').textContent = `${s.title} · ${s.day}`;
      ul.appendChild(r);
    });
    box.appendChild(ul);
  }

  if ((w.apps || []).length) {
    const wrap = document.createElement('div'); wrap.className = 'tl-chips';
    w.apps.slice(0, 8).forEach((a) => {
      const s = document.createElement('span'); s.className = 'tl-chip';
      s.textContent = `${a.app} · ${Math.round(a.minutes / 6) / 10}h`; wrap.appendChild(s);
    });
    box.appendChild(wrap);
  }
  m.appendChild(box);
}

function stat(big, small) {
  const el = document.createElement('div'); el.className = 'tl-w-stat';
  el.innerHTML = `<div class="tl-w-big"></div><div class="tl-w-small mono"></div>`;
  el.querySelector('.tl-w-big').textContent = big;
  el.querySelector('.tl-w-small').textContent = small;
  return el;
}
function listBlock(title, items) {
  const wrap = document.createElement('div');
  const h = document.createElement('h3'); h.className = 'tl-d-h'; h.textContent = title;
  wrap.appendChild(h);
  const ul = document.createElement('ul'); ul.className = 'tl-w-list';
  items.forEach((t) => { const li = document.createElement('li'); li.textContent = t; ul.appendChild(li); });
  wrap.appendChild(ul);
  return wrap;
}

// ─── states ────────────────────────────────────────────────────────────────

function showLoading(msg) {
  els.detail.hidden = true; els.empty.hidden = true;
  els.main.innerHTML = `<div class="tl-loading mono">${msg || 'Loading…'}</div>`;
}
function showState(msg) {
  els.detail.hidden = true; els.empty.hidden = true;
  els.main.innerHTML = `<div class="tl-state">${msg}</div>`;
}
async function showEmpty() {
  els.main.innerHTML = '';
  try {
    const res = await fetch(`${cfg.daemonHttp}/v1/timeline/state`);
    const s = await res.json();
    if (s.error) { els.emptyTitle.textContent = 'Connect your Mac';
      els.emptySub.textContent = 'Open Sunday on the Mac you want a timeline for, then turn capture on.';
      els.enable.hidden = true; }
    else if (s.running) { els.emptyTitle.textContent = 'Capturing — nothing yet';
      els.emptySub.textContent = 'Timeline is on. Your first activity cards appear within a few minutes.';
      els.enable.hidden = true; }
    else { els.emptyTitle.textContent = 'Your timeline is off';
      els.emptySub.textContent = 'Turn it on and Sunday quietly builds a private, on-device timeline of what you actually worked on — with a weekly Wrapped. Nothing leaves your Mac.';
      els.enable.hidden = false; }
  } catch { els.enable.hidden = false; }
  els.empty.hidden = false;
}
async function enableCapture() {
  els.enable.disabled = true; els.enable.textContent = 'turning on…';
  try {
    await fetch(`${cfg.daemonHttp}/v1/timeline/toggle`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ on: true, interval_seconds: 60 }),
    });
    els.emptyTitle.textContent = 'Capturing — nothing yet';
    els.emptySub.textContent = 'Timeline is on. Your first cards appear within a couple of minutes. (Allow Screen Recording for Sunday if prompted.)';
    els.enable.hidden = true;
  } catch { els.enable.textContent = 'Turn on timeline'; els.enable.disabled = false; }
}

// ─── helpers ────────────────────────────────────────────────────────────────

function clock(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}
function dayLabel(ts) {
  const d = new Date(ts * 1000);
  const t = new Date(), y = new Date(); y.setDate(y.getDate() - 1);
  if (d.toDateString() === t.toDateString()) return 'Today';
  if (d.toDateString() === y.toDateString()) return 'Yesterday';
  return d.toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' });
}
function shortDay(ymdStr) {
  const [Y, M, D] = ymdStr.split('-').map(Number);
  return new Date(Y, M - 1, D).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
}
function ymd(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
function debounce(fn, ms) {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
