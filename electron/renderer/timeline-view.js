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

// Calendar geometry — matched to Dayflow: a proportional time axis anchored at
// 4 AM, 168px per hour (2.8px/min), so a card's HEIGHT is its real duration and
// empty time shows as real vertical space. Week compresses to fit 7 columns.
const DAY_START_HOUR = 4;
const HOURS = 24;
const HOUR_PX = 168;
const PX_PER_MIN = HOUR_PX / 60;   // 2.8
const MIN_CARD_PX = 12;
const COMPACT_MIN = 13;            // shorter than this → single-line card
const WEEK_HOUR_PX = 64;

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
    const win = windowFor(m);
    const url = `${cfg.daemonHttp}/v1/timeline/events?from_ts=${win.from}&to_ts=${win.to}&limit=800`;
    const res = await fetch(url);
    const data = await res.json();
    if (data.error) return showState(data.error);
    events = (data.events || []).slice().sort((a, b) => a.start_ts - b.start_ts);
    if (!events.length) return silent ? undefined : showEmpty();
    if (m === 'week') renderWeek(events, win); else renderDay(events, win);
    if (!silent) startSummaryPolling();
  } catch (err) {
    console.warn('timeline load failed', err);
    if (!silent) showState('Could not reach the timeline.');
  }
}

// The 4 AM–anchored window(s) for a mode. Anchoring at 4 AM (Dayflow's rule)
// keeps past-midnight work on the same "day" instead of splitting it.
function windowFor(m) {
  const now = new Date();
  const base = new Date(now);
  if (now.getHours() < DAY_START_HOUR) base.setDate(base.getDate() - 1);
  const origin = new Date(base.getFullYear(), base.getMonth(), base.getDate(), DAY_START_HOUR, 0, 0);
  if (m === 'yesterday') origin.setDate(origin.getDate() - 1);
  if (m === 'week') {
    const start = new Date(origin); start.setDate(start.getDate() - 6);
    return { from: start.getTime() / 1000, to: origin.getTime() / 1000 + 86400,
             origin: start.getTime() / 1000, week: true };
  }
  const originTs = origin.getTime() / 1000;
  return { from: originTs, to: originTs + 86400, origin: originTs };
}

async function runSearch(q) {
  showLoading();
  try {
    const res = await fetch(`${cfg.daemonHttp}/v1/timeline/search?q=${encodeURIComponent(q)}`);
    const data = await res.json();
    events = (data.events || []).slice().sort((a, b) => b.start_ts - a.start_ts);
    if (!events.length) return showState(`No moments match “${q}”.`);
    renderList(events, `${events.length} result${events.length > 1 ? 's' : ''} for “${q}”`);
  } catch { showState('Search failed.'); }
}

// When there are captured frames not yet turned into cards, kick ONE background
// process pass (the daemon drains it through the local vision CLI: transcribe →
// synthesize) and quietly re-fetch a few times so cards stream in — without
// stacking requests or flashing the loading state.
async function startSummaryPolling() {
  clearTimeout(summarizeTimer);
  let st;
  try { st = await (await fetch(`${cfg.daemonHttp}/v1/timeline/state`)).json(); } catch { return; }
  if (!st || (st.pending_frames || 0) <= 0) return;
  fetch(`${cfg.daemonHttp}/v1/timeline/summarize`, { method: 'POST' }).catch(() => {});
  let tries = 0;
  const tick = async () => {
    if (mode === 'wrapped' || els.search.value.trim()) return;
    await loadRange(mode, { silent: true });
    let s;
    try { s = await (await fetch(`${cfg.daemonHttp}/v1/timeline/state`)).json(); } catch { s = {}; }
    tries += 1;
    if (tries < 10 && (s.pending_frames || 0) > 0) summarizeTimer = setTimeout(tick, 20000);
  };
  summarizeTimer = setTimeout(tick, 20000);
}

// ─── proportional calendar rendering ─────────────────────────────────────

// One activity block, positioned + sized by time. `originTs` is the 4 AM anchor
// of its column; `hourPx` is the vertical scale. Returns {el, top} or null when
// the event falls outside the window.
function calCard(ev, originTs, hourPx) {
  const ppm = hourPx / 60;
  let startMin = (ev.start_ts - originTs) / 60;
  const durMin = Math.max(0, (ev.end_ts - ev.start_ts) / 60);
  if (startMin > HOURS * 60) return null;
  if (startMin < 0) startMin = 0;
  const top = startMin * ppm + 1;
  const height = Math.max(MIN_CARD_PX, durMin * ppm - 2);
  const compact = height < 34 || durMin < COMPACT_MIN;

  const card = document.createElement('button');
  card.className = 'tl-card tl-cal-card' + (compact ? ' compact' : '') + (ev.id === selected ? ' sel' : '');
  card.style.top = top + 'px';
  card.style.height = height + 'px';
  card.style.setProperty('--cat', colorFor(ev.type));
  card.dataset.id = ev.id;

  const headRow = document.createElement('span');
  headRow.className = 'tl-cc-head';
  const fav = faviconEl(ev.app_primary);
  if (fav) headRow.appendChild(fav);
  const title = document.createElement('span');
  title.className = 'tl-cc-title';
  title.textContent = ev.title || labelFor(ev.type);
  headRow.appendChild(title);
  card.appendChild(headRow);
  if (!compact) {
    const meta = document.createElement('span');
    meta.className = 'tl-cc-meta mono';
    meta.textContent = `${clock(ev.start_ts)} – ${clock(ev.end_ts)}`;
    card.appendChild(meta);
  }
  card.addEventListener('click', () => openDetail(ev));
  return { el: card, top };
}

// A small favicon for the card's primary app/site domain (Dayflow uses these).
// Falls back to nothing on error — never blocks the card.
function faviconEl(domain) {
  const host = (domain || '').trim().toLowerCase();
  if (!host || !host.includes('.') || host === 'terminal') return null;
  const img = document.createElement('img');
  img.className = 'tl-fav';
  img.loading = 'lazy';
  img.src = `https://www.google.com/s2/favicons?domain=${encodeURIComponent(host)}&sz=64`;
  img.addEventListener('error', () => img.remove());
  return img;
}

function hourLabels(hourPx, into) {
  for (let h = 0; h < HOURS; h++) {
    const lbl = document.createElement('div');
    lbl.className = 'tl-hourlabel mono';
    lbl.style.top = (h * hourPx) + 'px';
    lbl.textContent = formatHour((DAY_START_HOUR + h) % 24);
    into.appendChild(lbl);
  }
}
function hourLines(hourPx, into, inset) {
  for (let h = 0; h <= HOURS; h++) {
    const line = document.createElement('div');
    line.className = 'tl-hourline';
    line.style.top = (h * hourPx) + 'px';
    if (inset) line.style.left = '0';
    into.appendChild(line);
  }
}

function renderDay(list, win) {
  els.empty.hidden = true;
  const main = els.main; main.innerHTML = '';
  const cal = document.createElement('div');
  cal.className = 'tl-cal';
  cal.style.height = (HOURS * HOUR_PX) + 'px';
  hourLabels(HOUR_PX, cal);
  hourLines(HOUR_PX, cal);
  let firstTop = Infinity;
  for (const ev of list) {
    const block = calCard(ev, win.origin, HOUR_PX);
    if (block) { cal.appendChild(block.el); firstTop = Math.min(firstTop, block.top); }
  }
  main.appendChild(cal);
  if (firstTop < Infinity) main.scrollTop = Math.max(0, firstTop - 56);
}

function renderWeek(list, win) {
  els.empty.hidden = true;
  const main = els.main; main.innerHTML = '';
  const week = document.createElement('div');
  week.className = 'tl-week';

  const headrow = document.createElement('div');
  headrow.className = 'tl-week-headrow';
  const sp = document.createElement('div'); sp.className = 'tl-week-sp'; headrow.appendChild(sp);
  for (let i = 0; i < 7; i++) {
    const h = document.createElement('div'); h.className = 'tl-week-head';
    h.textContent = shortDayTs(win.origin + i * 86400 + 12 * 3600);
    headrow.appendChild(h);
  }
  week.appendChild(headrow);

  const body = document.createElement('div');
  body.className = 'tl-week-body';
  const axis = document.createElement('div');
  axis.className = 'tl-week-axis';
  axis.style.height = (HOURS * WEEK_HOUR_PX) + 'px';
  hourLabels(WEEK_HOUR_PX, axis);
  body.appendChild(axis);

  const grid = document.createElement('div'); grid.className = 'tl-week-grid';
  const lanes = [];
  for (let i = 0; i < 7; i++) {
    const col = document.createElement('div'); col.className = 'tl-week-col';
    const lane = document.createElement('div'); lane.className = 'tl-week-lane';
    lane.style.height = (HOURS * WEEK_HOUR_PX) + 'px';
    hourLines(WEEK_HOUR_PX, lane, true);
    col.appendChild(lane);
    grid.appendChild(col);
    lanes.push({ origin: win.origin + i * 86400, lane });
  }
  body.appendChild(grid);
  week.appendChild(body);

  for (const ev of list) {
    const i = Math.floor((ev.start_ts - win.origin) / 86400);
    if (i < 0 || i > 6) continue;
    const block = calCard(ev, lanes[i].origin, WEEK_HOUR_PX);
    if (block) lanes[i].lane.appendChild(block.el);
  }
  main.appendChild(week);
  main.scrollTop = Math.max(0, (8 - DAY_START_HOUR) * WEEK_HOUR_PX);   // ~8 AM into view
}

// Search spans arbitrary time, so it stays a compact list rather than a calendar.
function renderList(list, banner) {
  els.detail.hidden = true; els.empty.hidden = true;
  const main = els.main; main.innerHTML = '';
  if (banner) {
    const b = document.createElement('div'); b.className = 'tl-banner'; b.textContent = banner; main.appendChild(b);
  }
  const wrap = document.createElement('div'); wrap.className = 'tl-list';
  for (const ev of list) {
    const card = document.createElement('button');
    card.className = 'tl-card tl-list-card' + (ev.id === selected ? ' sel' : '');
    card.style.setProperty('--cat', colorFor(ev.type));
    card.dataset.id = ev.id;
    card.innerHTML = `<div class="tl-lc-when mono"></div><div class="tl-lc-body"><div class="tl-lc-title"></div><div class="tl-lc-meta"></div></div>`;
    card.querySelector('.tl-lc-when').textContent = `${dayShortTs(ev.start_ts)} ${clock(ev.start_ts)}`;
    card.querySelector('.tl-lc-title').textContent = ev.title || labelFor(ev.type);
    card.querySelector('.tl-lc-meta').textContent = `${clock(ev.start_ts)} – ${clock(ev.end_ts)} · ${labelFor(ev.type)}`;
    card.addEventListener('click', () => openDetail(ev));
    wrap.appendChild(card);
  }
  main.appendChild(wrap);
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
  if (ev.detailed_summary && ev.detailed_summary !== ev.summary) {
    const p = document.createElement('p');
    p.className = 'tl-d-detail';
    p.textContent = ev.detailed_summary;
    d.appendChild(p);
  }

  const chips = [];
  if (ev.app_primary) chips.push(ev.app_primary);
  if (ev.app_secondary) chips.push(ev.app_secondary);
  (ev.projects || []).forEach((x) => chips.push('#' + x));
  if (chips.length) {
    const wrap = document.createElement('div');
    wrap.className = 'tl-chips';
    chips.slice(0, 8).forEach((c) => {
      const s = document.createElement('span'); s.className = 'tl-chip'; s.textContent = c; wrap.appendChild(s);
    });
    d.appendChild(wrap);
  }

  // Play-by-play — the observations overlapping this card's time span.
  const scenesWrap = document.createElement('div');
  d.appendChild(scenesWrap);
  loadPlayByPlay(ev, scenesWrap);

  // Distractions — brief interruptions the model logged inside the card.
  if ((ev.distractions || []).length) {
    const h = document.createElement('h3'); h.className = 'tl-d-h'; h.textContent = 'Distractions';
    d.appendChild(h);
    const ul = document.createElement('div'); ul.className = 'tl-scenes';
    ev.distractions.forEach((x) => {
      const row = document.createElement('div'); row.className = 'tl-distraction';
      const t = document.createElement('div'); t.className = 'tl-dx-title'; t.textContent = x.title || '';
      const s = document.createElement('div'); s.className = 'tl-dx-sub'; s.textContent = x.summary || '';
      row.appendChild(t); if (x.summary) row.appendChild(s);
      ul.appendChild(row);
    });
    d.appendChild(ul);
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

async function loadPlayByPlay(ev, wrap) {
  const h = document.createElement('h3'); h.className = 'tl-d-h'; h.textContent = 'Play-by-play';
  wrap.appendChild(h);
  const body = document.createElement('div');
  body.className = 'tl-scenes';
  body.innerHTML = `<div class="tl-d-pending">Reconstructing this session…</div>`;
  wrap.appendChild(body);
  try {
    const res = await fetch(
      `${cfg.daemonHttp}/v1/timeline/observations?from_ts=${ev.start_ts}&to_ts=${ev.end_ts}`);
    const data = await res.json();
    const obs = (data.observations || []).slice().sort((a, b) => a.start_ts - b.start_ts);
    body.innerHTML = '';
    if (!obs.length) {
      body.innerHTML = ev.summarized
        ? `<div class="tl-d-pending">No moment-by-moment detail for this card.</div>`
        : `<div class="tl-d-pending">Reconstructing this session…</div>`;
      return;
    }
    obs.forEach((o) => {
      const row = document.createElement('div');
      row.className = 'tl-scene';
      row.innerHTML = `<span class="tl-scene-t mono"></span><span class="tl-scene-x"></span>`;
      row.querySelector('.tl-scene-t').textContent =
        `${clock(o.start_ts)}${o.end_ts && o.end_ts !== o.start_ts ? '–' + clock(o.end_ts) : ''}`;
      row.querySelector('.tl-scene-x').textContent = o.text || '';
      body.appendChild(row);
    });
  } catch { body.innerHTML = `<div class="tl-d-pending">Couldn’t load the play-by-play.</div>`; }
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
function formatHour(h) {
  const period = h >= 12 ? 'PM' : 'AM';
  const hr = h % 12 === 0 ? 12 : h % 12;
  return `${hr} ${period}`;
}
function shortDayTs(ts) {   // "Mon 5" — week column header
  const d = new Date(ts * 1000);
  return d.toLocaleDateString([], { weekday: 'short', day: 'numeric' });
}
function dayShortTs(ts) {   // "Mon Jul 5" — search-result timestamp
  return new Date(ts * 1000).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
}
function shortDay(ymdStr) {
  const [Y, M, D] = ymdStr.split('-').map(Number);
  return new Date(Y, M - 1, D).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
}
function debounce(fn, ms) {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
