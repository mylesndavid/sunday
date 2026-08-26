// Memory tab — "What Sunday Knows". Facts-first: the primary pane is the flat
// fact store (everything Sunday remembers about you), with Sources (the
// conversations she derived them from) as a secondary subtab.
//
// (The old force-directed memory map was removed when memory moved to flat
// facts + hybrid recall; a map of extracted entities no longer reflected how
// Sunday actually remembers.)

let cfg = null;          // { daemonHttp }
let els = null;          // dom refs

export function init(config, refs) {
  cfg = config; els = refs;
  wireSubtabs();
  wireFacts();
  refreshFactCount();
}

// Refresh whatever pane is active — called when the Memory tab is opened.
export function refresh() {
  const active = document.querySelector('.mem-subtab.active');
  switchSubtab(active ? active.dataset.mempane : 'facts');
}

// ── Memory sub-tabs (Facts / Sources) ─────────────────────────────────────
function wireSubtabs() {
  const tabs = document.querySelectorAll('.mem-subtab');
  tabs.forEach((t) => t.addEventListener('click', () => switchSubtab(t.dataset.mempane)));
}
function switchSubtab(name) {
  const pane = ['facts', 'sources'].includes(name) ? name : 'facts';
  document.querySelectorAll('.mem-subtab').forEach((t) => {
    const selected = t.dataset.mempane === pane;
    t.classList.toggle('active', selected);
    t.setAttribute('aria-selected', selected ? 'true' : 'false');
  });

  const panes = {
    facts: document.getElementById('mem-pane-facts'),
    sources: document.getElementById('mem-pane-sources'),
  };
  Object.values(panes).forEach((el) => { if (el) el.hidden = true; });
  if (pane !== 'facts') closeFactDetail();

  panes[pane].hidden = false;
  if (pane === 'sources') loadSources();
  else loadFacts();
}

// ── FACTS — the primary "what Sunday knows" pane ──────────────────────────
let _facts = [];          // last-fetched [{id, content, source, created_at}]
let _factFilter = '';
let _factDetailId = null;

function wireFacts() {
  const search = document.getElementById('facts-search');
  if (search) search.addEventListener('input', () => { _factFilter = search.value.trim().toLowerCase(); renderFacts(); });

  // Add-fact form
  const addBtn = document.getElementById('facts-add-btn');
  const addBox = document.getElementById('facts-add');
  const addText = document.getElementById('facts-add-text');
  const addStatus = document.getElementById('facts-add-status');
  if (addBtn) addBtn.addEventListener('click', () => {
    const opening = addBox.hidden;
    addBox.hidden = !opening;
    if (opening) { addStatus.textContent = ''; addText.value = ''; addText.focus(); }
  });
  document.getElementById('facts-add-cancel')?.addEventListener('click', () => { addBox.hidden = true; });
  document.getElementById('facts-add-save')?.addEventListener('click', async () => {
    const content = addText.value.trim();
    if (!content) { addStatus.textContent = 'Type a fact first.'; return; }
    addStatus.textContent = 'Saving…';
    try {
      const r = await fetch(`${cfg.daemonHttp}/v1/memory/facts`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, source: 'chat' }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
      addText.value = ''; addBox.hidden = true; addStatus.textContent = '';
      await loadFacts(); refreshFactCount();
    } catch (e) { addStatus.textContent = `Couldn't save: ${e.message}`; }
  });

  // Fact detail — back / edit / forget
  document.getElementById('fact-detail-exit')?.addEventListener('click', closeFactDetail);
  document.getElementById('fact-edit')?.addEventListener('click', () => setFactEditing(true));
  document.getElementById('fact-edit-cancel')?.addEventListener('click', () => setFactEditing(false));
  document.getElementById('fact-save')?.addEventListener('click', saveFactEdit);
  document.getElementById('fact-forget')?.addEventListener('click', () => toggleForgetConfirm(true));
  document.getElementById('fact-del-cancel')?.addEventListener('click', () => toggleForgetConfirm(false));
  document.getElementById('fact-del-go')?.addEventListener('click', forgetFact);
}

async function loadFacts() {
  const ul = document.getElementById('facts-list');
  const empty = document.getElementById('facts-empty');
  try {
    const r = await fetch(`${cfg.daemonHttp}/v1/memory/facts?limit=1000`);
    const d = await r.json();
    _facts = d.facts || [];
    renderFacts();
  } catch (err) {
    if (empty) empty.hidden = true;   // don't let a stale empty state overlap the error row
    ul.innerHTML = `<li class="fact-row"><span class="fact-row-text" style="color:var(--error)">couldn't load facts: ${esc(err.message)}</span></li>`;
  }
}

function renderFacts() {
  const ul = document.getElementById('facts-list');
  const empty = document.getElementById('facts-empty');
  const countEl = document.getElementById('facts-count');
  const f = _factFilter;
  const items = f ? _facts.filter((x) => (x.content || '').toLowerCase().includes(f)) : _facts;
  // Count reflects the total store, not the filtered view.
  if (countEl) countEl.textContent = _facts.length ? `${_facts.length} fact${_facts.length > 1 ? 's' : ''}` : '';
  if (!_facts.length) { empty.hidden = false; ul.innerHTML = ''; return; }
  empty.hidden = true;
  if (!items.length) {
    ul.innerHTML = `<li class="fact-row fact-row-none"><span class="fact-row-text">No facts match “${esc(_factFilter)}”.</span></li>`;
    return;
  }
  ul.innerHTML = items.map((x) => `
    <li class="fact-row" data-id="${x.id}">
      <span class="fact-row-text">${esc(x.content || '')}</span>
      <span class="fact-row-meta">
        <span class="fact-src" data-src="${esc(srcKind(x.source))}">${esc(srcLabel(x.source))}</span>
        <span class="fact-age">${esc(ago(x.created_at))}</span>
      </span>
    </li>`).join('');
  ul.querySelectorAll('.fact-row[data-id]').forEach((row) => {
    row.addEventListener('click', () => openFactDetail(parseInt(row.dataset.id, 10)));
  });
}

// Normalize the stored source string into one of the known chips.
function srcKind(source) {
  const s = (source || '').toLowerCase();
  if (s === 'chat') return 'chat';
  if (s === 'tool') return 'tool';
  return 'auto';   // 'auto', 'observer', anything else
}
function srcLabel(source) {
  return { chat: 'chat', auto: 'auto', tool: 'tool' }[srcKind(source)];
}

function openFactDetail(id) {
  const fact = _facts.find((x) => x.id === id);
  if (!fact) return;
  _factDetailId = id;
  document.getElementById('fact-detail-text').textContent = fact.content || '';
  document.getElementById('fact-detail-meta').textContent =
    `${srcLabel(fact.source)} · ${ago(fact.created_at)}`;
  document.getElementById('fact-edit-text').value = fact.content || '';
  document.getElementById('fact-edit-status').textContent = '';
  setFactEditing(false);
  toggleForgetConfirm(false);
  document.getElementById('fact-detail').hidden = false;
}
function closeFactDetail() {
  const detail = document.getElementById('fact-detail');
  if (detail) detail.hidden = true;
  _factDetailId = null;
}
function setFactEditing(on) {
  document.getElementById('fact-view').hidden = on;
  document.getElementById('fact-edit-pane').hidden = !on;
  if (on) { toggleForgetConfirm(false); document.getElementById('fact-edit-text').focus(); }
}
function toggleForgetConfirm(on) {
  const c = document.getElementById('fact-del-confirm');
  if (c) c.hidden = !on;
}
async function saveFactEdit() {
  if (_factDetailId == null) return;
  const content = document.getElementById('fact-edit-text').value.trim();
  const status = document.getElementById('fact-edit-status');
  if (!content) { status.textContent = 'A fact can\'t be empty.'; return; }
  status.textContent = 'Saving…';
  try {
    const r = await fetch(`${cfg.daemonHttp}/v1/memory/facts/${_factDetailId}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    const fact = _facts.find((x) => x.id === _factDetailId);
    if (fact) fact.content = content;
    document.getElementById('fact-detail-text').textContent = content;
    setFactEditing(false);
    renderFacts();
  } catch (e) { status.textContent = `Couldn't save: ${e.message}`; }
}
async function forgetFact() {
  if (_factDetailId == null) return;
  const id = _factDetailId;
  try {
    const r = await fetch(`${cfg.daemonHttp}/v1/memory/facts/${id}`, { method: 'DELETE' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    _facts = _facts.filter((x) => x.id !== id);
    closeFactDetail();
    renderFacts(); refreshFactCount();
  } catch (e) {
    document.getElementById('fact-edit-status').textContent = `Couldn't forget: ${e.message}`;
    toggleForgetConfirm(false);
  }
}

async function refreshFactCount() {
  try {
    const r = await fetch(`${cfg.daemonHttp}/v1/memory/facts?limit=1`);
    const d = await r.json();
    const s = document.getElementById('mem-facts-count');
    const n = d.total ?? 0;
    if (s) { if (n > 0) { s.textContent = n; s.hidden = false; } else s.hidden = true; }
  } catch {}
}

// ── Sources feed — chronological list of captured conversations ───────────
let _srcShowAll = false;
async function loadSources() {
  const list = document.getElementById('src-feed');
  const empty = document.getElementById('src-empty');
  list.innerHTML = '';
  try {
    // Observer-captured conversations, noise hidden by default.
    const convParam = _srcShowAll ? '&min_value=all' : '';
    const convRes = await fetch(`${cfg.daemonHttp}/v1/conversations?limit=200${convParam}`).then((r) => r.json());
    const hiddenLow = (convRes.hidden && convRes.hidden.low) || 0;
    const rows = (convRes.conversations || [])
      .slice()
      .sort((a, b) => (b.started_at || 0) - (a.started_at || 0));

    if (!rows.length) { empty.hidden = false; return; }
    empty.hidden = true;

    // "Show N hidden" disclosure when the conversation noise filter is active
    // and there's actual junk being suppressed.
    let headerHtml = '';
    if (!_srcShowAll && hiddenLow > 0) {
      headerHtml = `<li class="conv-toggle">
        <button class="conv-toggle-btn" id="src-show-all">Show ${hiddenLow} low-value (TikToks, ambient noise, fragments)</button>
      </li>`;
    } else if (_srcShowAll) {
      headerHtml = `<li class="conv-toggle">
        <button class="conv-toggle-btn" id="src-show-all">Hide low-value</button>
      </li>`;
    }

    list.innerHTML = headerHtml + rows.map((c) => srcRowHtml(c)).join('');

    document.getElementById('src-show-all')?.addEventListener('click', () => {
      _srcShowAll = !_srcShowAll;
      loadSources();
    });
    // Conversation rows keep their lazy transcript disclosure.
    list.querySelectorAll('.conv-card[data-kind="conversation"]').forEach((card) => {
      const d = card.querySelector('details');
      if (!d) return;
      const pre = d.querySelector('pre');
      d.addEventListener('toggle', async () => {
        if (!d.open || pre.dataset.loaded === 'true') return;
        pre.dataset.loaded = 'true';
        try {
          const r = await fetch(`${cfg.daemonHttp}/v1/conversations/${card.dataset.cid}`);
          const cc = await r.json();
          pre.textContent = cc.transcript || '(no transcript)';
        } catch (err) { pre.textContent = `(error: ${err.message})`; }
      });
    });
  } catch (e) {
    if (empty) empty.hidden = true;   // don't let a stale empty state overlap the error row
    list.innerHTML = `<li class="conv-card"><div class="conv-summary" style="color:var(--error)">couldn't load: ${esc(e.message)}</div></li>`;
  }
}

// One row in the feed: a conversation chip, category/people meta, and the
// lazy-loaded transcript disclosure.
function srcRowHtml(c) {
  const people = (c.participants || []).join(', ') || '—';
  const valueDot = c.value ? `<span class="conv-value" data-v="${esc(c.value)}" title="${esc(c.value)}"></span>` : '';
  return `
    <li class="conv-card" data-cid="${c.id}" data-kind="conversation" data-value="${esc(c.value || '')}">
      <div class="conv-head">
        <span class="src-chip" data-kind="conversation">conversation</span>
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
