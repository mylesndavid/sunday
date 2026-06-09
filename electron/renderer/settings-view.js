// Settings — seven task-based pages (Overview, Model & data path, Tools,
// Voice & capture, Behavior & memory, Privacy, About). Nav buttons switch the
// visible page; every control writes real state. Model/provider changes save
// instantly with honest painting: a selection is never shown active until the
// POST succeeds, and reverts to the authoritative /v1/config state on failure.

const $ = (s) => document.querySelector(s);
let DAEMON_HTTP = '';
let defaultPrompt = '';
let sysTimer = null;

// Authoritative live state, refreshed from /v1/config + runMode(). The
// Overview + Privacy generated sentences read from here.
let live = {
  local: true,
  provider: 'openrouter',
  model: '',
  voiceProvider: 'openai',
  transcriptionReady: false,
  transcriptionModel: '',
  browserConnected: false,
  serviceCount: 0,
  permsGranted: 0,
  permsTotal: 4,
  memoryFacts: null,
};

export function init(daemonHttp) {
  DAEMON_HTTP = daemonHttp;
  wire();
}
export function setDaemon(http) { DAEMON_HTTP = http; }

// Catastrophic-only error toast. Row-level status carries the routine cases.
function flashError(msg) {
  const el = $('#set-error');
  if (!el) return;
  el.hidden = false; el.textContent = msg;
  clearTimeout(el._t); el._t = setTimeout(() => { el.hidden = true; }, 4000);
}

// ── page switching ─────────────────────────────────────────────────────────
function showPage(id) {
  document.querySelectorAll('.set-page').forEach((p) => p.classList.toggle('active', p.id === id));
  document.querySelectorAll('.set-navitem').forEach((b) => {
    const on = b.dataset.page === id;
    b.classList.toggle('active', on);
    if (on) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  });
  const stage = $('#set-stage'); if (stage) stage.scrollTop = 0;
}

export async function loadAll() {
  const cfg = await window.sunday.getConfig();
  DAEMON_HTTP = cfg.daemonHttp || DAEMON_HTTP;
  if ($('#set-http')) $('#set-http').value = cfg.daemonHttp || '';
  if ($('#set-ws')) $('#set-ws').value = cfg.daemonWs || '';
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/config`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const c = await res.json();
    live.provider = c.model?.provider || 'openrouter';
    live.model = c.model?.name || '';
    $('#set-provider').value = live.provider;
    $('#set-model').value = live.model;
    await refreshRunMode();   // sets runtime + loads the active provider's models
    defaultPrompt = c.identity_prompt?.default || '';
    const eff = c.identity_prompt?.effective || '';
    const custom = !!c.identity_prompt?.custom_present;
    loadInstructions(custom, eff);
  } catch (err) {
    flashError(`couldn't load settings: ${err.message}`);
  }
  refreshSystem();
  loadConnections();
  loadMcp();
  loadGmailStatus();
  loadTelegramStatus();
  loadCockpitStatus();
  loadMemorySummary();
  loadSkills();
  updateOverview();
  updatePrivacy();
}

// ── instructions (Behavior & memory) ────────────────────────────────────────
function loadInstructions(custom, eff) {
  const text = custom ? eff : defaultPrompt;
  $('#set-prompt').value = text;
  $('#instr-text-preview').textContent = text;
  $('#set-prompt-status').textContent = custom ? 'Using your custom instructions' : 'Using default instructions';
  updateChars();
  exitInstrEdit();
}
function enterInstrEdit() {
  $('#instr-preview').hidden = true;
  $('#instr-edit').hidden = false;
  $('#instr-reset-confirm').hidden = true;
  $('#set-prompt').focus();
}
function exitInstrEdit() {
  $('#instr-preview').hidden = false;
  $('#instr-edit').hidden = true;
  $('#instr-reset-confirm').hidden = true;
}
function updateChars() { $('#set-prompt-chars').textContent = `${$('#set-prompt').value.length} chars`; }

// ── memory summary (Behavior & memory) ──────────────────────────────────────
async function loadMemorySummary() {
  const el = $('#set-mem-summary'); if (!el) return;
  try {
    const h = await (await fetch(`${DAEMON_HTTP}/v1/admin/health`)).json();
    const m = h.memory || {};
    live.memoryFacts = m.total ?? null;
    el.textContent = m.available
      ? `${m.total ?? 0} fact${m.total === 1 ? '' : 's'} remembered.`
      : 'Memory is not available on this runtime.';
  } catch { el.textContent = 'Couldn\'t reach memory.'; }
}

// ── skills (Behavior & memory) ───────────────────────────────────────────────
// The local library plus a "find online" disclosure. The agent sees these on
// the shelf every turn; this surface lets the user read, edit, add, and delete
// them. Honest painting: list repaints only after a write succeeds, and a row
// that failed to load offers retry inline.
let skillsCache = [];
let skillOpen = null;   // slug of the skill open in the detail card

function skillStatus(msg, tone) {
  const el = $('#skill-status'); if (!el) return;
  if (!msg) { el.hidden = true; el.textContent = ''; el.removeAttribute('data-state'); return; }
  el.hidden = false; el.textContent = msg;
  if (tone) el.setAttribute('data-state', tone); else el.removeAttribute('data-state');
}

async function loadSkills() {
  const ul = $('#skill-list'); if (!ul) return;
  try {
    const d = await (await fetch(`${DAEMON_HTTP}/v1/skills`)).json();
    skillsCache = d.skills || [];
    skillStatus(null);
  } catch (err) {
    skillsCache = [];
    skillStatus('Couldn\'t load skills — retrying won\'t hurt.', 'fail');
  }
  renderSkills();
}

function renderSkills() {
  const ul = $('#skill-list'); if (!ul) return;
  const q = ($('#skill-search')?.value || '').trim().toLowerCase();
  const rows = skillsCache.filter((s) =>
    !q || s.slug.toLowerCase().includes(q) || (s.name || '').toLowerCase().includes(q) || (s.description || '').toLowerCase().includes(q));
  if (!skillsCache.length) {
    ul.innerHTML = '<li class="skill-empty">No skills yet. Add one, or Sunday will write her own as she learns.</li>';
    return;
  }
  if (!rows.length) {
    ul.innerHTML = `<li class="skill-empty">No skills match “${esc(q)}”.</li>`;
    return;
  }
  ul.innerHTML = rows.map((s) => `
    <li class="conn-row skill-row" data-slug="${esc(s.slug)}" tabindex="0" role="button">
      <div class="skill-row-main">
        <span class="conn-name">${esc(s.name || s.slug)}</span>
        ${s.description ? `<span class="skill-row-desc">${esc(s.description)}</span>` : ''}
      </div>
      <span class="skill-row-slug mono">${esc(s.slug)}</span>
    </li>`).join('');
  ul.querySelectorAll('.skill-row').forEach((row) => {
    const open = () => openSkill(row.dataset.slug);
    row.addEventListener('click', open);
    row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
  });
}

function skillCardEditMode(on) {
  $('#skill-view').hidden = on;
  $('#skill-edit-pane').hidden = !on;
  $('#skill-del-confirm').hidden = true;
  $('#skill-edit-status').textContent = '';
}

async function openSkill(slug) {
  const card = $('#skill-card'); if (!card) return;
  skillOpen = slug;
  card.hidden = false;
  $('#skill-card-title').textContent = slug;
  $('#skill-card-meta').textContent = '';
  $('#skill-body-view').textContent = 'Loading…';
  skillCardEditMode(false);
  try {
    const r = await fetch(`${DAEMON_HTTP}/v1/skills/${encodeURIComponent(slug)}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    card._skill = d;
    $('#skill-card-title').textContent = d.name || d.slug;
    $('#skill-card-meta').textContent = d.slug;
    $('#skill-body-view').textContent = d.body || '';
    card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (err) {
    $('#skill-body-view').textContent = `Couldn't open this skill — ${err.message}`;
  }
}

function closeSkillCard() {
  const card = $('#skill-card'); if (card) { card.hidden = true; card._skill = null; }
  skillOpen = null;
}

function enterSkillEdit() {
  const card = $('#skill-card'); const d = card?._skill; if (!d) return;
  $('#skill-name').value = d.name || d.slug;
  $('#skill-body-edit').value = d.body || '';
  skillCardEditMode(true);
  $('#skill-body-edit').focus();
}

function newSkill() {
  const card = $('#skill-card'); if (!card) return;
  skillOpen = null;
  card._skill = null;   // null slug = create
  card.hidden = false;
  $('#skill-card-title').textContent = 'New skill';
  $('#skill-card-meta').textContent = '';
  $('#skill-name').value = '';
  $('#skill-body-edit').value = '';
  skillCardEditMode(true);
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  $('#skill-name').focus();
}

async function saveSkill() {
  const card = $('#skill-card'); if (!card) return;
  const st = $('#skill-edit-status');
  const name = $('#skill-name').value.trim();
  const body = $('#skill-body-edit').value;
  if (!body.trim()) { st.dataset.state = 'fail'; st.textContent = 'Body can\'t be empty.'; return; }
  const payload = { name, body };
  const existing = card._skill?.slug;
  if (existing) payload.slug = existing;   // overwrite = the patch path
  st.dataset.state = 'wait'; st.textContent = 'Saving…';
  try {
    const r = await fetch(`${DAEMON_HTTP}/v1/skills`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    await loadSkills();          // repaint only after the write lands
    await openSkill(d.slug);     // reopen the saved skill in view mode
  } catch (err) {
    st.dataset.state = 'fail'; st.textContent = `Save failed — ${err.message}`;
  }
}

async function deleteSkill() {
  const card = $('#skill-card'); const slug = card?._skill?.slug; if (!slug) return;
  $('#skill-del-confirm').hidden = false;
  $('#skill-del-go').onclick = async () => {
    try {
      const r = await fetch(`${DAEMON_HTTP}/v1/skills/${encodeURIComponent(slug)}`, { method: 'DELETE' });
      if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.error || `HTTP ${r.status}`); }
      closeSkillCard();
      await loadSkills();
    } catch (err) { flashError(`delete failed: ${err.message}`); }
  };
}

// ── find skills online (skills.sh directory) ───
function skillFindStatus(msg, tone) {
  const el = $('#skill-find-status'); if (!el) return;
  if (!msg) { el.hidden = true; el.textContent = ''; el.removeAttribute('data-state'); return; }
  el.hidden = false; el.textContent = msg;
  if (tone) el.setAttribute('data-state', tone); else el.removeAttribute('data-state');
}

async function searchSkillsOnline() {
  const q = ($('#skill-find-q')?.value || '').trim();
  const ul = $('#skill-find-list'); if (!ul) return;
  ul.innerHTML = '';
  if (!q) { skillFindStatus('Type something to search.', null); return; }
  skillFindStatus('Searching skills.sh…', 'wait');
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/skills/search?q=${encodeURIComponent(q)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const d = await res.json();
    const items = d.results || [];
    skillFindStatus(items.length ? null : 'Nothing found in the directory for that.', null);
    ul.innerHTML = items.map((s) => `
      <li class="conn-row skill-find-row" data-id="${esc(s.id)}">
        <div class="skill-row-main">
          <span class="conn-name">${esc(s.name || s.skill_id || s.id)}</span>
          <span class="skill-row-desc">${esc(s.source || '')}${s.installs ? ` · ${s.installs} installs` : ''}</span>
        </div>
        <button type="button" class="btn skill-install" data-id="${esc(s.id)}">Install</button>
      </li>`).join('');
    ul.querySelectorAll('.skill-install').forEach((b) => b.addEventListener('click', () => installSkillOnline(b)));
  } catch (err) {
    skillFindStatus(`Search failed — ${err.message}`, 'fail');
  }
}

async function installSkillOnline(btn) {
  const id = btn.dataset.id;
  btn.disabled = true; const prev = btn.textContent; btn.textContent = 'Installing…';
  try {
    const r = await fetch(`${DAEMON_HTTP}/v1/skills/install`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ slug: id }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    btn.textContent = 'Installed';
    await loadSkills();
  } catch (err) {
    btn.disabled = false; btn.textContent = prev;
    skillFindStatus(`Install failed — ${err.message}`, 'fail');
  }
}

// ── custom servers (Tools) ──────────────────────────────────────────────────
async function loadMcp() {
  loadBuiltinConnectors();
  try {
    const d = await (await fetch(`${DAEMON_HTTP}/v1/mcp`)).json();
    const cfg = d.config && Object.keys(d.config.mcpServers || {}).length ? d.config : null;
    if (cfg && !$('#mcp-config').value.trim()) $('#mcp-config').value = JSON.stringify(cfg, null, 2);
    renderMcpServers(d.servers || []);
  } catch {}
}

// Built-in connectors (Playwright browser) — the first-class Browser card.
async function loadBuiltinConnectors() {
  const wrap = $('#mcp-builtin'); if (!wrap) return;
  let d; try { d = await (await fetch(`${DAEMON_HTTP}/v1/mcp/builtin`)).json(); } catch { return; }
  renderBuiltins(wrap, d.connectors || []);
  const pw = (d.connectors || []).find((c) => c.id === 'playwright');
  live.browserConnected = !!(pw && pw.enabled);
  updateOverview(); updatePrivacy();
}
function renderBuiltins(wrap, connectors) {
  wrap.innerHTML = connectors.map((c) => {
    let state, stateTone;
    if (c.enabled) { state = 'Connected'; stateTone = 'ok'; }
    else if (!c.ready) { state = 'Node.js required'; stateTone = 'fail'; }
    else if (c.needs_token) { state = 'Needs token'; stateTone = 'wait'; }
    else { state = 'Extension not installed'; stateTone = 'wait'; }
    return `
    <div class="mcp-bi ${c.enabled ? 'on' : ''}">
      <div class="mcp-bi-head">
        <div class="mcp-bi-txt">
          <div class="mcp-bi-title">${esc(c.title)}</div>
          <div class="mcp-bi-desc">${esc(c.desc)}</div>
        </div>
        <span class="set-verify mcp-bi-state" data-state="${stateTone}">${esc(state)}</span>
        <button class="btn ${c.enabled ? '' : 'btn-primary'}" data-act="${c.enabled ? 'disable' : (c.needs_token ? 'reveal' : 'enable')}" data-id="${esc(c.id)}" ${c.ready ? '' : 'disabled'}>${c.enabled ? 'Disconnect' : 'Connect'}</button>
      </div>
      ${!c.ready ? `<div class="mcp-bi-warn">Needs Node.js 18+ on this Mac — install it (nodejs.org) and reopen.</div>` : ''}
      ${c.needs_token && !c.enabled ? `
        <div class="mcp-bi-token" id="bi-token-${esc(c.id)}" hidden>
          <div class="mcp-bi-tokhint">${esc(c.token_label || 'Paste the connection token.')}</div>
          <div class="mcp-bi-tokrow">
            <input type="text" class="field" id="bi-tokin-${esc(c.id)}" placeholder="paste token" autocomplete="off" spellcheck="false">
            <button class="btn btn-primary" data-act="enable" data-id="${esc(c.id)}">Connect</button>
          </div>
          ${c.setup_url ? `<a href="#" data-href="${esc(c.setup_url)}" class="mcp-bi-link">Where do I get the extension and token?</a>` : ''}
        </div>` : ''}
      ${c.enabled && (c.setup || []).length ? `<ol class="mcp-bi-setup">${c.setup.map((s) => `<li>${esc(s)}</li>`).join('')}${c.setup_url ? `<li><a href="#" data-href="${esc(c.setup_url)}" class="mcp-bi-link">Get the extension</a></li>` : ''}</ol>` : ''}
    </div>`;
  }).join('');

  const toggle = async (id, enabled, token) => {
    const r = await fetch(`${DAEMON_HTTP}/v1/mcp/builtin`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, enabled, token }) });
    const d = await r.json();
    if (d.connectors) {
      renderBuiltins(wrap, d.connectors);
      const pw = (d.connectors || []).find((c) => c.id === 'playwright');
      live.browserConnected = !!(pw && pw.enabled);
      updateOverview(); updatePrivacy();
    }
    loadMcp();
  };
  wrap.querySelectorAll('button[data-act]').forEach((b) => b.addEventListener('click', async () => {
    const { act, id } = b.dataset;
    if (act === 'reveal') {
      const box = $(`#bi-token-${id}`); if (box) { box.hidden = false; $(`#bi-tokin-${id}`)?.focus(); }
      return;
    }
    const token = act === 'enable' ? ($(`#bi-tokin-${id}`)?.value.trim() || undefined) : undefined;
    b.disabled = true; b.textContent = act === 'disable' ? 'Disconnecting…' : 'Connecting…';
    try { await toggle(id, act !== 'disable', token); }
    catch { b.disabled = false; b.textContent = act === 'disable' ? 'Disconnect' : 'Connect'; }
  }));
  wrap.querySelectorAll('.mcp-bi-link').forEach((a) => a.addEventListener('click', (e) => { e.preventDefault(); window.sunday.openExternal(a.dataset.href); }));
}

// Gmail (direct IMAP/SMTP via an app password). Saves GMAIL_ADDRESS +
// GMAIL_APP_PASSWORD through the standard saveBrain({credentials}) path, then
// reflects the daemon's quick login probe.
async function loadGmailStatus() {
  const line = $('#gmail-status'); if (!line) return;
  let d;
  try { d = await (await fetch(`${DAEMON_HTTP}/v1/gmail/status`)).json(); }
  catch { line.dataset.state = ''; line.textContent = ''; return; }
  if (!d.configured) {
    line.dataset.state = ''; line.textContent = 'Not connected';
    const addr = $('#gmail-address'); if (addr && !addr.value) addr.value = '';
    return;
  }
  const addr = $('#gmail-address');
  if (addr && !addr.value && d.address) addr.value = d.address;
  if (d.ok) { line.dataset.state = 'ok'; line.textContent = `connected as ${d.address}`; }
  else { line.dataset.state = 'fail'; line.textContent = `saved, but couldn't log in as ${d.address} — check the app password`; }
}

async function saveGmailCreds() {
  const address = $('#gmail-address')?.value.trim() || '';
  const password = $('#gmail-password')?.value.trim() || '';
  const line = $('#gmail-status');
  if (!address || !password) {
    if (line) { line.dataset.state = 'wait'; line.textContent = 'Enter both an address and an app password.'; }
    return;
  }
  if (line) { line.dataset.state = 'wait'; line.textContent = 'saving…'; }
  try {
    await saveBrain({ credentials: { GMAIL_ADDRESS: address, GMAIL_APP_PASSWORD: password } });
    if ($('#gmail-password')) $('#gmail-password').value = '';   // don't keep the secret on screen
    await loadGmailStatus();
  } catch (err) {
    if (line) { line.dataset.state = 'fail'; line.textContent = `failed — ${err.message}`; }
  }
}

// Telegram (bot API). Saves TELEGRAM_BOT_TOKEN (+ optional allowlist) through
// the same saveBrain({credentials}) path; status reflects a getMe probe. The
// poller picks up the new token on the next daemon restart.
async function loadTelegramStatus() {
  const line = $('#telegram-status'); if (!line) return;
  let d;
  try { d = await (await fetch(`${DAEMON_HTTP}/v1/telegram/status`)).json(); }
  catch { line.dataset.state = ''; line.textContent = ''; return; }
  if (!d.configured) { line.dataset.state = ''; line.textContent = 'Not connected'; return; }
  if (d.ok) { line.dataset.state = 'ok'; line.textContent = d.username ? `connected as @${d.username} — DM your bot to start` : 'connected — DM your bot to start'; }
  else { line.dataset.state = 'fail'; line.textContent = "saved, but the token didn't work — re-check it with @BotFather"; }
}

async function saveTelegramCreds() {
  const token = $('#telegram-token')?.value.trim() || '';
  const allowed = $('#telegram-allowed')?.value.trim() || '';
  const line = $('#telegram-status');
  if (!token) {
    if (line) { line.dataset.state = 'wait'; line.textContent = 'Paste the bot token from @BotFather.'; }
    return;
  }
  if (line) { line.dataset.state = 'wait'; line.textContent = 'saving…'; }
  try {
    await saveBrain({ credentials: { TELEGRAM_BOT_TOKEN: token, TELEGRAM_ALLOWED_CHAT_IDS: allowed } });
    if ($('#telegram-token')) $('#telegram-token').value = '';   // don't keep the secret on screen
    await loadTelegramStatus();
    if (line) line.textContent += ' (restart Sunday to start listening)';
  } catch (err) {
    if (line) { line.dataset.state = 'fail'; line.textContent = `failed — ${err.message}`; }
  }
}

// Cockpit (the user's real logged-in Chrome via the extension). Saves the
// COCKPIT_TOKEN credential through the standard saveBrain({credentials}) path;
// status reflects whether the daemon has the token and a live extension socket.
// While the settings view is open we poll so "Connected" appears the moment
// the extension dials in — pairing has no other feedback channel.
let cockpitPollTimer = null;
async function loadCockpitStatus() {
  const line = $('#cockpit-status'); if (!line) return;
  if (!cockpitPollTimer) cockpitPollTimer = setInterval(() => {
    if (document.hidden) return;
    loadCockpitStatus();
  }, 4000);
  let d;
  try { d = await (await fetch(`${DAEMON_HTTP}/v1/cockpit/status`)).json(); }
  catch { line.dataset.state = ''; line.textContent = ''; return; }
  if (d.connected) { line.dataset.state = 'ok'; line.textContent = 'Connected — Sunday has hands in your browser'; return; }
  // The daemon saw a wrong-token handshake just now: the extension's token has
  // changed (reinstall/"New token" regenerates it) — the fix is a re-copy, and
  // without this hint the mismatch is silent forever.
  if (d.token_mismatch) {
    line.dataset.state = 'fail';
    line.textContent = 'The extension is dialing with a different token — copy the token from its popup again and Connect.';
    return;
  }
  if (!d.paired) { line.dataset.state = ''; line.textContent = 'Not paired — extension installed? Paste its token above.'; return; }
  line.dataset.state = 'wait'; line.textContent = 'Token saved — waiting for the extension to dial in…';
}

async function saveCockpitToken() {
  const token = $('#cockpit-token')?.value.trim() || '';
  const line = $('#cockpit-status');
  if (!token) {
    if (line) { line.dataset.state = 'wait'; line.textContent = 'Paste the token the extension shows.'; }
    return;
  }
  if (line) { line.dataset.state = 'wait'; line.textContent = 'saving…'; }
  try {
    await saveBrain({ credentials: { COCKPIT_TOKEN: token } });
    if ($('#cockpit-token')) $('#cockpit-token').value = '';   // don't keep the secret on screen
    await loadCockpitStatus();
  } catch (err) {
    if (line) { line.dataset.state = 'fail'; line.textContent = `failed — ${err.message}`; }
  }
}

function renderMcpServers(servers) {
  const ul = $('#mcp-servers');
  if (!servers.length) { ul.innerHTML = ''; return; }
  ul.innerHTML = servers.map((s) => {
    let badge;
    if (s.connected) badge = `<span class="conn-on">Connected · ${(s.tools || []).length} tools</span>`;
    else if (s.starting) badge = `<span class="set-verify" data-state="wait" style="flex:0">Starting</span>`;
    else badge = `<span class="set-verify" data-state="fail" style="flex:0">Failed · ${esc((s.error || '').slice(0, 50))}</span>`;
    return `
    <li class="conn-row">
      <span class="conn-ico"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M4 12h16M4 17h10"/></svg></span>
      <span class="conn-name">${esc(s.name)}</span>
      ${badge}
    </li>`;
  }).join('');
}

// ── connected services + catalog (Tools) ────────────────────────────────────
const CONN_CATS = ['popular', 'productivity', 'communication', 'dev-tools'];
const CONN_LIST_MAX = 12;
let connState = { providers: [], cat: 'popular', q: '', connected: new Set() };
let connSearchTimer = null;
let connSeq = 0;   // sequence guard for catalog loads

async function loadConnections() {
  renderConnCats();
  $('#conn-search').oninput = (e) => {
    connState.q = e.target.value.trim().toLowerCase();
    clearTimeout(connSearchTimer);
    connSearchTimer = setTimeout(refreshConnList, 120);
  };
  await loadPinnedConnectors();
  refreshConnList();
}

async function loadPinnedConnectors() {
  const ul = $('#conn-pinned');
  try {
    const d = await (await fetch(`${DAEMON_HTTP}/v1/connectors`)).json();
    const conns = d.connectors || [];
    document.querySelector('#conn-unconfigured').hidden = true;
    connState.connected = new Set(conns.map((c) => c.provider));
    live.serviceCount = conns.filter((c) => c.has_tools).length;
    updateOverview();
    if (!conns.length) {
      ul.innerHTML = '<li class="conn-pinned-empty">No services connected yet. Add one below to start.</li>';
      return;
    }
    ul.innerHTML = conns.map((c) => {
      const label = PROVIDER_LABEL[c.provider] || c.provider;
      if (!c.has_tools) {
        return `
          <li class="conn-pinned-row">
            <span class="conn-name">${esc(label)}</span>
            <span class="conn-pinned-note">connected — tools coming soon</span>
          </li>`;
      }
      return `
        <li class="conn-pinned-row" data-provider="${c.provider}">
          <span class="conn-name">${esc(label)}</span>
          <span class="conn-pinned-hint">${c.enabled ? 'always available' : 'Sunday finds it when needed'}</span>
          <label class="toggle">
            <span class="sr-only">Always available</span>
            <input type="checkbox" class="conn-toggle" data-provider="${c.provider}" ${c.enabled ? 'checked' : ''}>
            <span class="toggle-track"><span class="toggle-thumb"></span></span>
          </label>
        </li>`;
    }).join('');
    ul.querySelectorAll('.conn-toggle').forEach((cb) => {
      cb.addEventListener('change', () => toggleConnector(cb.dataset.provider, cb.checked, cb));
    });
  } catch (err) {
    ul.innerHTML = `<li class="conn-pinned-empty">couldn't reach the daemon: ${esc(err.message)}</li>`;
    document.querySelector('#conn-unconfigured').hidden = false;
  }
}

const PROVIDER_LABEL = {
  'google-mail':     'Gmail',
  'google-calendar': 'Google Calendar',
  'fireflies':       'Fireflies',
};

async function toggleConnector(provider, on, cb) {
  const row = cb.closest('.conn-pinned-row');
  const hint = row?.querySelector('.conn-pinned-hint');
  if (hint) hint.textContent = on ? 'always available' : 'Sunday finds it when needed';
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/connectors/toggle`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider, on }),
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      throw new Error(d.error || `HTTP ${res.status}`);
    }
  } catch (err) {
    cb.checked = !on;
    if (hint) hint.textContent = !on ? 'always available' : 'Sunday finds it when needed';
    flashError(err.message);
  }
}

function renderConnCats() {
  const wrap = $('#conn-cats');
  wrap.innerHTML = CONN_CATS.map((c) => `
    <button type="button" class="conn-cat ${c === connState.cat ? 'on' : ''}" data-cat="${c}">${c}</button>
  `).join('');
  wrap.querySelectorAll('.conn-cat').forEach((b) => b.addEventListener('click', () => {
    connState.cat = b.dataset.cat;
    renderConnCats();
    refreshConnList();
  }));
}

async function refreshConnList() {
  const ul = $('#conn-list');
  const seq = ++connSeq;
  ul.innerHTML = '<li class="conn-loading">loading…</li>';
  const params = new URLSearchParams();
  if (connState.q) params.set('q', connState.q);
  else if (connState.cat) params.set('category', connState.cat);
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/integrations/catalog?${params}`);
    const d = await res.json();
    if (seq !== connSeq) return;   // a newer search superseded this one
    const items = (d.providers || []).slice(0, CONN_LIST_MAX);
    if (!items.length) {
      ul.innerHTML = connState.q
        ? '<li class="conn-loading">no matches — the sign-in catalog may not be available on this runtime. The browser above and custom servers below always work.</li>'
        : '<li class="conn-loading">No sign-in catalog on this runtime — it runs on the remote daemon. The browser above and custom servers below work everywhere.</li>';
      return;
    }
    ul.innerHTML = items.map((p) => {
      const connected = connState.connected.has(p.name);
      return `
        <li class="conn-row" data-name="${p.name}">
          <span class="conn-name">${esc(p.display_name)}</span>
          <span class="conn-mode">${esc(p.auth_mode || '')}</span>
          ${connected
            ? '<span class="conn-on">connected</span>'
            : `<button type="button" class="btn conn-pick" data-name="${p.name}">Set up</button>`}
        </li>`;
    }).join('');
    ul.querySelectorAll('.conn-pick').forEach((b) => b.addEventListener('click', () => openConnCard(b.dataset.name)));
  } catch (err) {
    if (seq !== connSeq) return;
    ul.innerHTML = `<li class="conn-loading">couldn't reach the daemon: ${esc(err.message)}</li>`;
  }
}

// ── dynamic setup card (Tools) ──────────────────────────────────────────────
async function openConnCard(name) {
  const card = $('#conn-card');
  card.hidden = false;
  card.innerHTML = '<div class="conn-card-loading">loading setup…</div>';
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  let spec;
  try {
    spec = await (await fetch(`${DAEMON_HTTP}/v1/integrations/setup/${encodeURIComponent(name)}`)).json();
  } catch (err) {
    card.innerHTML = `<div class="conn-card-err">couldn't load: ${esc(err.message)}</div>`;
    return;
  }
  if (spec.error) {
    card.innerHTML = `<div class="conn-card-err">${esc(spec.error)}</div>`;
    return;
  }
  card.innerHTML = renderConnCard(spec);

  card.querySelector('.conn-card-close')?.addEventListener('click', () => { card.hidden = true; });
  card.querySelectorAll('.conn-copy').forEach((el) => el.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(el.dataset.copy); el.classList.add('copied'); setTimeout(() => el.classList.remove('copied'), 1200); }
    catch {}
  }));
  card.querySelectorAll('.conn-link').forEach((el) => el.addEventListener('click', (e) => {
    e.preventDefault();
    window.sunday.openExternal(el.dataset.href);
  }));
  card.querySelector('.conn-submit')?.addEventListener('click', () => submitConnCard(spec, card));
}

function renderConnCard(spec) {
  const oauth = (spec.auth_mode || '').startsWith('OAUTH');
  const setupLink = spec.setup_guide_url || spec.docs;

  const fields = [];
  if (oauth) {
    fields.push({ name: 'client_id',     title: 'Client ID',     description: `Your OAuth client ID from ${spec.display_name}.`,     secret: false, group: 'credentials' });
    fields.push({ name: 'client_secret', title: 'Client Secret', description: `Your OAuth client secret from ${spec.display_name}.`, secret: true,  group: 'credentials' });
  } else if (spec.credentials) {
    for (const [k, v] of Object.entries(spec.credentials)) {
      fields.push({ name: k, title: v.title || k, description: v.description, secret: v.secret || /key|secret|token|password/i.test(k), example: v.example, group: 'credentials' });
    }
  }
  if (spec.connection_config) {
    for (const [k, v] of Object.entries(spec.connection_config)) {
      fields.push({ name: k, title: v.title || k, description: v.description, secret: false, example: v.example, group: 'connection_config' });
    }
  }

  const fieldHtml = fields.map((f) => `
    <label class="conn-field">
      <span class="conn-field-title">${esc(f.title)}</span>
      ${f.description ? `<span class="conn-field-desc">${esc(f.description)}</span>` : ''}
      <input type="${f.secret ? 'password' : 'text'}"
             class="set-input conn-input"
             data-name="${esc(f.name)}"
             data-group="${esc(f.group)}"
             ${f.example ? `placeholder="${esc(f.example)}"` : ''}
             autocomplete="off" spellcheck="false">
    </label>`).join('');

  return `
    <div class="conn-card-head">
      <h3>${esc(spec.display_name)}</h3>
      <span class="conn-mode">${esc(spec.auth_mode || '')}</span>
      <button type="button" class="btn conn-card-close" aria-label="close">×</button>
    </div>

    ${oauth ? `
      <ol class="conn-steps">
        ${setupLink ? `<li>Open <a class="conn-link" data-href="${esc(setupLink)}" href="#">${esc(spec.display_name)}'s setup guide</a> and create an OAuth client.</li>` : ''}
        ${spec.redirect_uri ? `<li>When asked for an authorized redirect URI, paste <span class="conn-copy" data-copy="${esc(spec.redirect_uri)}" title="click to copy"><code>${esc(spec.redirect_uri)}</code></span></li>` : ''}
        <li>Copy the resulting <strong>Client ID</strong> and <strong>Client Secret</strong> into the fields below.</li>
      </ol>` : ''}
    ${(!oauth && setupLink) ? `<p class="conn-card-help">See the <a class="conn-link" data-href="${esc(setupLink)}" href="#">${esc(spec.display_name)} setup guide</a> for where to find these values.</p>` : ''}

    <div class="conn-fields">${fieldHtml || '<p class="conn-card-help">No fields needed — click Connect to start the sign-in flow.</p>'}</div>

    <div class="conn-card-actions">
      <button type="button" class="btn conn-submit">Save and connect</button>
      <span class="conn-card-status" id="conn-card-status" aria-live="polite"></span>
    </div>
  `;
}

async function submitConnCard(spec, card) {
  const inputs = card.querySelectorAll('.conn-input');
  const credentials = {};
  const connection_config = {};
  for (const inp of inputs) {
    const val = inp.value.trim();
    if (!val) continue;
    if (inp.dataset.group === 'connection_config') connection_config[inp.dataset.name] = val;
    else credentials[inp.dataset.name] = val;
  }
  if ((spec.auth_mode || '').startsWith('OAUTH') && Array.isArray(spec.default_scopes)) {
    credentials.scopes = spec.default_scopes.join(',');
  }

  const status = card.querySelector('#conn-card-status');
  const btn = card.querySelector('.conn-submit');
  btn.disabled = true; const orig = btn.textContent; btn.textContent = 'connecting…';
  status.textContent = ''; delete status.dataset.state;

  try {
    const body = {
      provider: spec.name,
      unique_key: spec.name,
      auth_mode: spec.auth_mode,
      credentials,
      connection_config,
    };
    const res = await fetch(`${DAEMON_HTTP}/v1/integrations/provision_one`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await res.json();

    if (d.flow === 'direct' && d.connected) {
      connState.connected.add(spec.name);
      $('#conn-card').hidden = true;
      loadPinnedConnectors();
      refreshConnList();
      return;
    }
    if (d.connect_url) {
      await window.sunday.openExternal(d.connect_url);
      renderOauthWaiting(spec, card, status);
      pollProviderConnected(spec.name, 0, card);
      return;
    }
    btn.disabled = false; btn.textContent = orig;
    status.textContent = d.error || d.session_error || d.connection_error || 'something went wrong';
    status.dataset.state = 'fail';
  } catch (err) {
    btn.disabled = false; btn.textContent = orig;
    status.textContent = err.message; status.dataset.state = 'fail';
  }
}

// OAuth waiting state — visible recovery controls so a slow/abandoned approval
// never leaves a dead, disabled button.
function renderOauthWaiting(spec, card, status) {
  const actions = card.querySelector('.conn-card-actions');
  if (!actions) return;
  card._oauthCancelled = false;
  actions.innerHTML = `
    <span class="conn-card-status" id="conn-card-status" aria-live="polite">Waiting for you to approve in the browser…</span>
    <div class="conn-card-recover">
      <button type="button" class="btn btn-primary" data-act="recheck">I approved — check again</button>
      <button type="button" class="btn" data-act="retry">Try again</button>
      <button type="button" class="btn" data-act="cancel">Cancel</button>
    </div>`;
  actions.querySelector('[data-act="recheck"]').addEventListener('click', () => checkProviderOnce(spec.name, card));
  actions.querySelector('[data-act="retry"]').addEventListener('click', () => openConnCard(spec.name));
  actions.querySelector('[data-act="cancel"]').addEventListener('click', () => { card._oauthCancelled = true; card.hidden = true; });
}

async function checkProviderOnce(name, card) {
  const status = card.querySelector('#conn-card-status');
  if (status) status.textContent = 'Checking…';
  try {
    const d = await (await fetch(`${DAEMON_HTTP}/v1/integrations`)).json();
    const hit = (d.providers || []).find((p) => p.id === name && p.connected);
    if (hit) {
      connState.connected.add(name);
      $('#conn-card').hidden = true;
      loadPinnedConnectors();
      refreshConnList();
      return true;
    }
    if (status) { status.textContent = 'Not connected yet — finish approving in the browser, then check again.'; status.dataset.state = ''; }
  } catch {
    if (status) status.textContent = 'Couldn\'t reach the daemon — try again in a moment.';
  }
  return false;
}

function pollProviderConnected(name, n, card) {
  if (card && card._oauthCancelled) return;
  if (n > 60) {
    // Out of automatic patience — leave the visible recovery buttons in place.
    const status = card?.querySelector('#conn-card-status');
    if (status) { status.dataset.state = ''; status.textContent = 'Still waiting — approve in the browser, then use "I approved — check again".'; }
    return;
  }
  setTimeout(async () => {
    if (card && card._oauthCancelled) return;
    try {
      const d = await (await fetch(`${DAEMON_HTTP}/v1/integrations`)).json();
      const hit = (d.providers || []).find((p) => p.id === name && p.connected);
      if (hit) {
        connState.connected.add(name);
        $('#conn-card').hidden = true;
        loadPinnedConnectors();
        refreshConnList();
      } else {
        pollProviderConnected(name, n + 1, card);
      }
    } catch { pollProviderConnected(name, n + 1, card); }
  }, 1500);
}

// ── provider + model (Model & data path) ────────────────────────────────────
const _KEY_FOR = {
  openrouter: 'OPENROUTER_API_KEY', openai: 'OPENAI_API_KEY',
  anthropic: 'ANTHROPIC_API_KEY',
};
const STATIC_MODELS = {
  codex: [{ id: 'gpt-5.2', name: 'gpt-5.2' }, { id: 'gpt-5.5', name: 'gpt-5.5' }],
  openai: [{ id: 'gpt-4o', name: 'GPT-4o' }, { id: 'gpt-4o-mini', name: 'GPT-4o mini' }, { id: 'o3', name: 'o3' }, { id: 'o4-mini', name: 'o4-mini' }],
  anthropic: [{ id: 'claude-opus-4-1', name: 'Claude Opus 4.1' }, { id: 'claude-sonnet-4-5', name: 'Claude Sonnet 4.5' }, { id: 'claude-3-5-haiku-latest', name: 'Claude Haiku 3.5' }],
};
function fmtSize(bytes) { if (!bytes) return ''; const gb = bytes / 1e9; return gb >= 1 ? `${gb.toFixed(1)} GB` : `${Math.round(bytes / 1e6)} MB`; }

// localStorage key for the "key saved" hint per provider (keys can't be read back).
function keySavedFlag(provider) { return localStorage.getItem(`keySaved:${provider}`) === '1'; }
function setKeySaved(provider) { localStorage.setItem(`keySaved:${provider}`, '1'); }

async function refreshRunMode() {
  try {
    const m = await window.sunday.runMode();
    live.local = !!m.local;
    paintRuntime();
    // ChatGPT (Codex) only works on This Mac — reflect that in the provider row.
    const codexRow = document.querySelector('#set-prov-list [data-provider="codex"]');
    if (codexRow) {
      codexRow.classList.toggle('prov-row-disabled', !m.local);
      const sub = $('#prov-sub-codex');
      if (sub) sub.textContent = m.local ? 'your subscription · no key' : 'This Mac only';
    }
    await refreshBrainProvider();
  } catch {}
}

function paintRuntime() {
  document.querySelectorAll('#set-runmode .seg-btn').forEach((b) => {
    const on = (b.dataset.mode === 'local') === live.local;
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
  const copy = $('#set-runtime-copy');
  if (copy) copy.textContent = live.local
    ? 'The daemon, memory, and local tools run inside this app.'
    : 'Sunday runs on a daemon you host elsewhere; this app is the window into it.';
  const form = $('#set-remote-form');
  if (form) form.hidden = live.local;
}

async function refreshBrainProvider() {
  try {
    const c = await (await fetch(`${DAEMON_HTTP}/v1/config`)).json();
    live.provider = c.model?.provider || 'openrouter';
    live.model = c.model?.name;
    await applyProvider(live.provider, live.model);
  } catch {}
}

let modelSeq = 0;   // sequence guard for model-list loads

async function loadProviderModels(provider) {
  if (provider === 'openrouter') {
    try { const d = await (await fetch(`${DAEMON_HTTP}/v1/models`)).json(); return d.models || []; } catch { return []; }
  }
  if (provider === 'ollama') {
    try {
      const d = await (await fetch(`${DAEMON_HTTP}/v1/ollama/models`)).json();
      if (!d.available) return { error: 'Ollama is not running here — use the steps below to set it up.' };
      const chat = d.models.filter((m) => !/embed|minilm|bge|e5-/i.test(m.name));
      if (!chat.length) return { error: 'No chat models downloaded yet — use the wizard below.' };
      return chat.map((m) => ({ id: m.name, name: m.name, slug: fmtSize(m.size) }));
    } catch { return { error: 'Could not reach Ollama.' }; }
  }
  return STATIC_MODELS[provider] || [];
}

let selectedProvider = 'openrouter';

// Paint the active provider row. Honest: only the authoritative provider gets
// the active class. A row mid-switch shows "switching…"; on failure "failed —
// retry" and the active selection stays where /v1/config says it is.
function paintProviderRows() {
  document.querySelectorAll('#set-prov-list .prov-row').forEach((row) => {
    const p = row.dataset.provider;
    const on = p === selectedProvider;
    row.classList.toggle('prov-row-active', on);
    row.setAttribute('aria-checked', on ? 'true' : 'false');
    // key hint
    const keyEl = $(`#prov-key-${p}`);
    if (keyEl) {
      if (!_KEY_FOR[p]) keyEl.textContent = '';
      else keyEl.textContent = keySavedFlag(p) ? 'key saved — replace' : 'no key saved';
    }
  });
}

function setProvStatus(provider, text, tone) {
  const el = $(`#prov-status-${provider}`);
  if (!el) return;
  el.textContent = text || '';
  if (tone) el.dataset.state = tone; else el.removeAttribute('data-state');
}

async function applyProvider(provider, currentModel) {
  selectedProvider = provider;
  paintProviderRows();
  const note = $('#set-model-note'); if (note) { note.textContent = ''; note.removeAttribute('data-state'); }
  // key field — only providers that need one
  const keyField = $('#set-key-field'), keyEl = $('#set-key'), needKey = _KEY_FOR[provider];
  if (keyField) keyField.hidden = !needKey;
  if (keyEl && needKey) { keyEl.placeholder = keySavedFlag(provider) ? 'paste a new key to replace the saved one' : `paste your key (saves on Enter)`; keyEl.value = ''; }
  // model list for the picker
  const seq = ++modelSeq;
  const res = await loadProviderModels(provider);
  if (seq !== modelSeq) return;   // a newer provider switch superseded this load
  if (res && res.error) { allModels = []; if (note) { note.dataset.state = 'wait'; note.textContent = res.error; } }
  else allModels = res;
  $('#set-model').value = currentModel || '';
  $('#set-model-search').value = '';
  hideModelResults();
  showCurrentModel();
  // ChatGPT connect / status
  const connect = $('#set-connect'); const connectRow = $('#set-connect-row');
  if (provider === 'codex') {
    let connected = false, email = '';
    try { const s = await (await fetch(`${DAEMON_HTTP}/v1/codex/status`)).json(); connected = s.connected; email = s.email || ''; } catch {}
    if (connectRow) connectRow.hidden = false;
    if (connect) connect.hidden = connected;
    setCodexStatus(connected ? (email ? `Connected as ${email}` : 'Connected') : 'Sign in to use your ChatGPT subscription.', connected ? 'ok' : 'wait');
  } else {
    if (connectRow) connectRow.hidden = true;
    if (connect) connect.hidden = true;
    setCodexStatus('', '');
  }
  // Fully-local (Ollama) wizard
  refreshOllamaRow(provider === 'ollama');
  updateImpact(); updateOverview(); updatePrivacy();
}

let _ollamaRec = null;

async function refreshOllamaRow(show) {
  const row = $('#set-ollama-row'); if (!row) return;
  const pb = $('#set-pullbar'); if (pb) pb.hidden = true;
  row.hidden = !show;
  if (!show) return;
  const status = $('#set-ollama-status');
  const install = $('#set-ollama-install');
  const start = $('#set-ollama-start');
  const pull = $('#set-ollama-pull');
  status.dataset.state = 'wait'; status.textContent = 'Checking hardware…';
  let d; try { d = await (await fetch(`${DAEMON_HTTP}/v1/local/recommend`)).json(); } catch { row.hidden = true; return; }
  if (!d || !d.ollama) { row.hidden = true; return; }
  const o = d.ollama || {};
  const rec = (d.models || []).find((m) => m.recommended);
  _ollamaRec = rec || null;
  install.hidden = true; start.hidden = true; pull.hidden = true;
  if (o.running) {
    const hasRec = rec && (o.models || []).some((n) => n.split(':latest')[0] === rec.name || n.startsWith(`${rec.name}`));
    if (rec && !hasRec) {
      status.dataset.state = 'wait';
      status.textContent = `Choose model — this Mac (${d.chip}, ${d.ram_gb}GB) runs ${rec.label}. One download away from fully local.`;
      pull.textContent = `Download ${rec.label}`;
      pull.hidden = false;
    } else {
      status.dataset.state = 'ok';
      status.textContent = `Fully local. ${live.model || (rec && rec.label) || 'your model'} is active — nothing leaves this Mac.`;
    }
  } else if (o.installed) {
    status.dataset.state = 'wait';
    status.textContent = 'Ollama is installed but not running.';
    start.hidden = false;
  } else {
    status.dataset.state = 'fail';
    status.textContent = `Ollama not installed. This Mac: ${d.chip || ''} · ${d.ram_gb || '?'}GB${rec ? ` — runs ${rec.label}` : ''}.`;
    install.hidden = false;
  }
}

function setCodexStatus(text, state) {
  const el = $('#set-codex-status'); if (!el) return;
  el.textContent = text; el.hidden = !text;
  if (state) el.dataset.state = state; else el.removeAttribute('data-state');
}

async function connectCodex() {
  setCodexStatus('Starting sign-in…', 'wait');
  const res = await fetch(`${DAEMON_HTTP}/v1/codex/login`, { method: 'POST' });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  if (data.connected) { setCodexStatus(data.email ? `Connected as ${data.email}` : 'Connected', 'ok'); return true; }
  if (!data.auth_url) throw new Error('no sign-in URL returned');
  await window.sunday.openExternal(data.auth_url);
  setCodexStatus('Opening your browser — sign in to ChatGPT, then come back.', 'wait');
  for (let i = 0; i < 120; i++) {
    await new Promise((r) => setTimeout(r, 1500));
    let s; try { s = await (await fetch(`${DAEMON_HTTP}/v1/codex/status`)).json(); } catch { continue; }
    if (s.connected) { setCodexStatus(s.email ? `Connected as ${s.email}` : 'Connected', 'ok'); return true; }
    if (s.error) throw new Error(s.error);
  }
  throw new Error('timed out waiting for sign-in');
}

// ── the one searchable model combobox ───────────────────────────────────────
let allModels = [];
let modelActiveIdx = -1;

function showCurrentModel() {
  const slug = $('#set-model').value.trim();
  const cur = $('#set-model-current');
  if (!slug) { cur.hidden = true; return; }
  const m = allModels.find((x) => x.id === slug);
  const vision = m?.vision;
  cur.hidden = false;
  cur.innerHTML = `<span class="mc-label">Current</span>
    <span class="mc-name">${esc(m?.name || slug)}</span>
    ${vision ? '<span class="mc-vision">sees images</span>' : '<span class="mc-text">text only</span>'}
    <span class="mc-slug">${esc(slug)}</span>`;
}

function hideModelResults() {
  const box = $('#set-model-results');
  box.hidden = true;
  modelActiveIdx = -1;
  $('#set-model-search')?.setAttribute('aria-expanded', 'false');
}

function renderModelResults(q) {
  const box = $('#set-model-results');
  const query = q.trim().toLowerCase();
  let list = allModels;
  if (query) list = allModels.filter((m) => m.id.toLowerCase().includes(query) || (m.name || '').toLowerCase().includes(query));
  list = list.slice(0, 60);
  if (!list.length) { hideModelResults(); return; }
  box.innerHTML = list.map((m, i) => `
    <button type="button" class="model-row" role="option" id="model-opt-${i}" data-id="${esc(m.id)}" aria-selected="false">
      <span class="mr-name">${esc(m.name || m.id)}</span>
      ${m.vision ? '<span class="mr-vision">sees images</span>' : ''}
      <span class="mr-slug">${esc(m.id)}</span>
    </button>`).join('');
  box.hidden = false;
  modelActiveIdx = -1;
  $('#set-model-search')?.setAttribute('aria-expanded', 'true');
  box.querySelectorAll('.model-row').forEach((b) => b.addEventListener('mousedown', (e) => {
    e.preventDefault();
    pickModel(b.dataset.id);
  }));
}

function moveModelActive(delta) {
  const box = $('#set-model-results');
  const rows = [...box.querySelectorAll('.model-row')];
  if (!rows.length) return;
  rows.forEach((r) => { r.classList.remove('active'); r.setAttribute('aria-selected', 'false'); });
  modelActiveIdx = (modelActiveIdx + delta + rows.length) % rows.length;
  const row = rows[modelActiveIdx];
  row.classList.add('active'); row.setAttribute('aria-selected', 'true');
  row.scrollIntoView({ block: 'nearest' });
  $('#set-model-search')?.setAttribute('aria-activedescendant', row.id);
}

// Instant-save a partial config ({provider, model_name, credentials}). On
// failure, revert by re-fetching the authoritative config.
async function saveBrain(body) {
  const res = await fetch(`${DAEMON_HTTP}/v1/config`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.error || `HTTP ${res.status}`); }
  return res;
}

async function pickModel(id) {
  const prev = $('#set-model').value;
  $('#set-model').value = id;
  $('#set-model-search').value = '';
  hideModelResults();
  showCurrentModel();
  setProvStatus(selectedProvider, 'switching…', 'wait');
  try {
    await saveBrain({ provider: selectedProvider, model_name: id });
    live.model = id;
    setProvStatus(selectedProvider, '', '');
    updateImpact(); updateOverview();
  } catch (err) {
    $('#set-model').value = prev;
    showCurrentModel();
    setProvStatus(selectedProvider, 'failed — retry', 'fail');
    await refreshBrainProvider();   // re-sync to authoritative state
  }
}

// ── system + memory status (About → diagnostics) ────────────────────────────
async function refreshSystem() {
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/admin/health`);
    if (!res.ok) return;
    const h = await res.json();
    const d = h.daemon || {};
    if ($('#sys-grid')) $('#sys-grid').innerHTML = `
      <span class="k">version</span><span class="v">${esc(d.version || '—')}</span>
      <span class="k">model</span><span class="v">${esc((d.model || '—').split('/').slice(-1)[0])}</span>
      <span class="k">messages</span><span class="v">${d.messages ?? '—'}</span>
      <span class="k">tools</span><span class="v">${d.tools_count ?? '—'}</span>
      <span class="k">uptime</span><span class="v">${d.uptime_s ? uptime(d.uptime_s) : '—'}</span>`;
    const devs = h.devices || [];
    if ($('#sys-devices')) $('#sys-devices').innerHTML = devs.length
      ? devs.map((x) => `<li><strong>${esc(x.device_id)}</strong><div>${(x.capabilities || []).map((c) => `<span class="cap">${esc(c)}</span>`).join('')}</div></li>`).join('')
      : '<li class="empty">no devices connected</li>';
    const m = h.memory || {};
    if ($('#sys-memory')) $('#sys-memory').innerHTML = `
      <span class="k">available</span><span class="v">${m.available ? 'yes' : 'no'}</span>
      <span class="k">facts</span><span class="v">${m.total ?? '—'}</span>`;
  } catch {}
}

export function startSystemPolling() { refreshSystem(); if (!sysTimer) sysTimer = setInterval(refreshSystem, 5000); }
export function stopSystemPolling() { if (sysTimer) { clearInterval(sysTimer); sysTimer = null; } }

// ── generated sentences: Privacy impact, Overview, Privacy page ──────────────
const PROV_DEST = {
  ollama: null, codex: 'OpenAI', openai: 'OpenAI', openrouter: 'OpenRouter', anthropic: 'Anthropic',
};
function provLabel(p) { return ({ ollama: 'Local (Ollama)', codex: 'ChatGPT', openai: 'OpenAI', openrouter: 'OpenRouter', anthropic: 'Anthropic' })[p] || p; }
function chatDestSentence() {
  const dest = PROV_DEST[live.provider];
  return dest ? `Prompts are sent to ${dest}.` : 'Prompts stay on this Mac.';
}

function updateImpact() {
  const el = $('#set-impact'); if (!el) return;
  const runtime = live.local ? 'The daemon runs on this Mac.' : 'The daemon runs on your remote host.';
  const memory = live.local ? 'Memory is stored on this Mac.' : 'Memory is stored on your remote daemon.';
  el.textContent = `${runtime} ${chatDestSentence()} ${memory}`;
}

function voiceConfigured() {
  const vp = live.voiceProvider;
  return keySavedFlag(vp === 'gemini' ? 'voice-gemini' : 'voice-openai');
}
function voiceSentence() {
  const vp = live.voiceProvider === 'gemini' ? 'Gemini' : 'OpenAI';
  return voiceConfigured() ? `${vp} (configured)` : 'not configured';
}

function updateOverview() {
  if (!$('#ov-runtime')) return;
  $('#ov-runtime').textContent = live.local ? 'This Mac' : (DAEMON_HTTP || 'remote daemon');
  $('#ov-thinking').textContent = `${provLabel(live.provider)}${live.model ? ` · ${live.model.split('/').slice(-1)[0]}` : ''}`;
  $('#ov-datapath').textContent = chatDestSentence();
  $('#ov-voice').textContent = voiceSentence();
  const tools = [];
  tools.push(live.browserConnected ? 'Browser connected' : 'Browser not connected');
  tools.push(`${live.serviceCount} service${live.serviceCount === 1 ? '' : 's'} always available`);
  $('#ov-tools').textContent = tools.join(' · ');
  $('#ov-perms').textContent = `${live.permsGranted} of ${live.permsTotal} allowed`;
  renderAttention();
}

// "Needs attention" — only renders when something is actually wrong.
let _daemonReachable = true;
function renderAttention() {
  const box = $('#ov-attention'); if (!box) return;
  const items = [];
  if (!_daemonReachable) items.push('The daemon is unreachable — check the runtime on Model and data path.');
  if (live.permsGranted < live.permsTotal) items.push(`${live.permsTotal - live.permsGranted} permission${live.permsTotal - live.permsGranted === 1 ? '' : 's'} not granted — some features need them.`);
  if (_updateReady) items.push('An update is ready — restart to apply it.');
  if (!items.length) { box.hidden = true; box.innerHTML = ''; return; }
  box.hidden = false;
  box.innerHTML = `<div class="ov-att-h">Needs attention</div><ul>${items.map((i) => `<li>${esc(i)}</li>`).join('')}</ul>`;
}

function updatePrivacy() {
  if (!$('#df-chat')) return;
  $('#df-chat').textContent = PROV_DEST[live.provider] ? `sent to ${PROV_DEST[live.provider]}` : 'stays on this Mac';
  $('#df-voice').textContent = voiceConfigured() ? `sent to ${live.voiceProvider === 'gemini' ? 'Google (Gemini)' : 'OpenAI'}` : 'not configured';
  $('#df-bg').textContent = live.transcriptionReady ? 'stays on this Mac' : 'OpenAI fallback until on-device transcription is ready';
  $('#df-browser').textContent = 'runs on this Mac';
  $('#df-memory').textContent = live.local ? 'stays on this Mac' : 'on your remote daemon';
}

// ── update state (About) — tracked for the Overview attention block ──────────
let _updateReady = false;

function wire() {
  // ── nav ──
  document.querySelectorAll('.set-navitem').forEach((b) => b.addEventListener('click', () => showPage(b.dataset.page)));
  document.querySelectorAll('.ov-action[data-goto]').forEach((b) => b.addEventListener('click', () => showPage(b.dataset.goto)));

  // ── runtime segmented control + migrate ──
  document.querySelectorAll('#set-runmode .seg-btn').forEach((b) => b.addEventListener('click', async () => {
    const mode = b.dataset.mode;
    const m = await window.sunday.runMode().catch(() => ({}));
    if (mode === 'local' && !m.local) { openMigrateModal(); return; }
    if (mode === 'cloud' && m.local) {
      // Switching This Mac → remote: just flip; main process reloads on success.
      const r = await window.sunday.setRunMode('cloud');
      if (r && r.error) { flashError(`Couldn't switch: ${r.error}`); await refreshRunMode(); }
      return;
    }
    // No-op (already in that mode) — re-sync the paint.
    paintRuntime();
  }));

  // Remote-daemon form
  $('#set-conn-test')?.addEventListener('click', async () => {
    const url = $('#set-http').value.trim().replace(/\/+$/, '');
    const v = $('#set-conn-verify'); v.dataset.state = ''; v.textContent = `→ ${url}/v1/status …`;
    try {
      const res = await fetch(`${url}/v1/status`); if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const d = await res.json();
      v.dataset.state = 'ok';
      v.textContent = `${d.version} · ${(d.model || '').split('/').slice(-1)[0]} · ${d.messages} msgs · ${(d.devices || []).length} device`;
    } catch (err) { v.dataset.state = 'fail'; v.textContent = err.message; }
  });
  $('#set-conn-save')?.addEventListener('click', async () => {
    const http = $('#set-http').value.trim().replace(/\/+$/, '');
    const ws = $('#set-ws').value.trim() || (http.replace(/^http/, 'ws') + '/v1/ws');
    await window.sunday.saveConnection({ daemonHttp: http, daemonWs: ws });
    const v = $('#set-conn-verify'); if (v) { v.dataset.state = 'ok'; v.textContent = 'connection saved'; }
  });

  // ── provider rows ──
  document.querySelectorAll('#set-prov-list .prov-row').forEach((row) => {
    const activate = () => { if (row.classList.contains('prov-row-disabled')) return; switchProvider(row.dataset.provider); };
    row.addEventListener('click', activate);
    row.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(); } });
  });

  // ── model combobox ──
  const search = $('#set-model-search');
  search.addEventListener('focus', () => renderModelResults(search.value));
  search.addEventListener('input', () => renderModelResults(search.value));
  search.addEventListener('blur', () => setTimeout(hideModelResults, 160));
  search.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); if ($('#set-model-results').hidden) renderModelResults(search.value); else moveModelActive(1); return; }
    if (e.key === 'ArrowUp') { e.preventDefault(); moveModelActive(-1); return; }
    if (e.key === 'Enter') {
      e.preventDefault();
      const rows = [...$('#set-model-results').querySelectorAll('.model-row')];
      if (modelActiveIdx >= 0 && rows[modelActiveIdx]) { pickModel(rows[modelActiveIdx].dataset.id); return; }
      const v = search.value.trim(); if (v) pickModel(v);
      return;
    }
    if (e.key === 'Escape') { hideModelResults(); }
  });
  $('#set-model-current')?.addEventListener('click', () => { search.focus(); renderModelResults(search.value); });

  // API key — saves on Enter / blur
  const keyEl = $('#set-key');
  const saveKey = async () => {
    const key = keyEl.value.trim();
    const KEY = _KEY_FOR[selectedProvider];
    if (!key || !KEY) return;
    setProvStatus(selectedProvider, 'saving key…', 'wait');
    try {
      await saveBrain({ provider: selectedProvider, credentials: { [KEY]: key } });
      setKeySaved(selectedProvider);
      paintProviderRows();
      keyEl.value = '';
      keyEl.placeholder = 'paste a new key to replace the saved one';
      setProvStatus(selectedProvider, '', '');
    } catch (err) { setProvStatus(selectedProvider, `key failed — ${err.message}`, 'fail'); }
  };
  keyEl?.addEventListener('keydown', (e) => { if (e.key === 'Enter') saveKey(); });
  keyEl?.addEventListener('blur', saveKey);

  // Connect ChatGPT
  $('#set-connect')?.addEventListener('click', async () => {
    try {
      const cfg = await window.sunday.getConfig(); if (cfg.daemonHttp) DAEMON_HTTP = cfg.daemonHttp;
      const m = await window.sunday.runMode();
      if (!m.local) throw new Error('ChatGPT runs on This Mac — switch the runtime above first.');
      await connectCodex();
      await saveBrain({ provider: 'codex' });
      await applyProvider('codex');
    } catch (err) { setCodexStatus(err.message || 'Sign-in failed', 'fail'); }
  });

  // Gmail (app password) card — Save button (Enter still works); open Google's
  // app-password page; status reflects the daemon's quick IMAP login probe.
  $('#gmail-apppw-link')?.addEventListener('click', (e) => { e.preventDefault(); window.sunday.openExternal('https://myaccount.google.com/apppasswords'); });
  $('#gmail-save')?.addEventListener('click', () => saveGmailCreds());
  ['#gmail-address', '#gmail-password'].forEach((sel) => {
    $(sel)?.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); saveGmailCreds(); } });
  });

  // Telegram card — open @BotFather, save the token (Enter works too).
  $('#telegram-botfather-link')?.addEventListener('click', (e) => { e.preventDefault(); window.sunday.openExternal('https://t.me/BotFather'); });
  $('#telegram-save')?.addEventListener('click', () => saveTelegramCreds());
  ['#telegram-token', '#telegram-allowed'].forEach((sel) => {
    $(sel)?.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); saveTelegramCreds(); } });
  });

  // Cockpit (browser extension) card — the full pairing flow lives here:
  // reveal the bundled extension folder, deep-link chrome://extensions (via
  // AppleScript in the main process — Chrome refuses chrome:// from `open`),
  // then Connect saves the pasted token (Enter still works too).
  $('#cockpit-reveal')?.addEventListener('click', () => window.sunday.revealExtension());
  $('#cockpit-open-chrome')?.addEventListener('click', async () => {
    const r = await window.sunday.openChromeExtensions().catch(() => ({ ok: false }));
    if (!r?.ok) {
      const line = $('#cockpit-status');
      if (line) { line.dataset.state = 'wait'; line.textContent = 'Couldn’t drive Chrome — open chrome://extensions yourself.'; }
    }
  });
  $('#cockpit-connect')?.addEventListener('click', () => saveCockpitToken());
  $('#cockpit-token')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); saveCockpitToken(); } });

  // Ollama wizard buttons
  $('#set-ollama-install')?.addEventListener('click', () => window.sunday.openExternal('https://ollama.com/download'));
  $('#set-ollama-start')?.addEventListener('click', async () => {
    const status = $('#set-ollama-status');
    if (status) { status.dataset.state = 'wait'; status.textContent = 'Starting Ollama…'; }
    try { await fetch(`${DAEMON_HTTP}/v1/ollama/start`, { method: 'POST' }); } catch {}
    setTimeout(() => refreshOllamaRow(true), 2500);
  });
  $('#set-ollama-pull')?.addEventListener('click', onOllamaPull);

  // ── instructions editor ──
  $('#instr-customize')?.addEventListener('click', enterInstrEdit);
  $('#instr-discard')?.addEventListener('click', () => { loadAll(); });
  $('#set-prompt').addEventListener('input', updateChars);
  $('#set-prompt-reset')?.addEventListener('click', () => { $('#instr-reset-confirm').hidden = false; });
  $('#instr-reset-cancel')?.addEventListener('click', () => { $('#instr-reset-confirm').hidden = true; });
  $('#instr-reset-go')?.addEventListener('click', async () => {
    try {
      await saveBrain({ identity_prompt: null });
      await loadAll();
    } catch (err) { flashError(`reset failed: ${err.message}`); }
  });
  $('#set-prompt-save')?.addEventListener('click', async () => {
    try {
      const text = $('#set-prompt').value;
      const same = !text.trim() || text.trim() === defaultPrompt.trim();
      await saveBrain({ identity_prompt: same ? null : text });
      await loadAll();
    } catch (err) { flashError(`save failed: ${err.message}`); }
  });

  // ── memory: Open Memory tab ──
  $('#set-mem-open')?.addEventListener('click', () => document.querySelector('.tab[data-view="memory"]')?.click());

  // ── skills ──
  $('#skill-search')?.addEventListener('input', renderSkills);
  $('#skill-new')?.addEventListener('click', newSkill);
  $('#skill-card-close')?.addEventListener('click', closeSkillCard);
  $('#skill-edit')?.addEventListener('click', enterSkillEdit);
  $('#skill-edit-cancel')?.addEventListener('click', () => {
    const card = $('#skill-card');
    if (card?._skill) skillCardEditMode(false);   // back to view
    else closeSkillCard();                          // was a new-skill draft
  });
  $('#skill-save')?.addEventListener('click', saveSkill);
  $('#skill-delete')?.addEventListener('click', deleteSkill);
  $('#skill-del-cancel')?.addEventListener('click', () => { $('#skill-del-confirm').hidden = true; });
  $('#skill-find-go')?.addEventListener('click', searchSkillsOnline);
  $('#skill-find-q')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); searchSkillsOnline(); } });

  // ── custom servers: add form + raw config ──
  document.querySelectorAll('#mcp-add-type .seg-btn').forEach((b) => b.addEventListener('click', () => {
    document.querySelectorAll('#mcp-add-type .seg-btn').forEach((x) => { x.classList.toggle('active', x === b); x.setAttribute('aria-pressed', x === b ? 'true' : 'false'); });
    const remote = b.dataset.type === 'remote';
    $('#mcp-add-target-l').textContent = remote ? 'URL' : 'Command';
    $('#mcp-add-target').placeholder = remote ? 'https://example.com/mcp' : 'npx -y some-mcp-server';
    $('#mcp-add-headers').closest('.set-field').hidden = !remote;
  }));
  $('#mcp-add-form')?.addEventListener('submit', onMcpAdd);
  $('#mcp-save')?.addEventListener('click', onMcpSaveRaw);

  // ── voice provider + key (Voice & capture) ──
  function paintVoiceProvider() {
    const cur = localStorage.getItem('voiceProvider') || 'openai';
    live.voiceProvider = cur;
    document.querySelectorAll('#set-voice-provider .seg-btn').forEach((b) => {
      const on = b.dataset.vp === cur;
      b.classList.toggle('active', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    const k = $('#set-voice-key');
    if (k) {
      const saved = keySavedFlag(cur === 'gemini' ? 'voice-gemini' : 'voice-openai');
      k.placeholder = saved
        ? 'key saved — paste a new one to replace it'
        : (cur === 'gemini'
          ? 'Gemini API key — saves on Enter'
          : 'OpenAI platform key (sk-…) — saves on Enter; the ChatGPT login does not cover realtime');
    }
    updateOverview(); updatePrivacy();
  }
  document.querySelectorAll('#set-voice-provider .seg-btn').forEach((b) => b.addEventListener('click', () => {
    localStorage.setItem('voiceProvider', b.dataset.vp);
    paintVoiceProvider();
  }));
  $('#set-voice-key')?.addEventListener('keydown', async (e) => {
    if (e.key !== 'Enter') return;
    const v = e.target.value.trim(); if (!v) return;
    const cur = localStorage.getItem('voiceProvider') || 'openai';
    const note = $('#set-voice-note');
    if (note) { note.dataset.state = 'wait'; note.textContent = 'saving key…'; }
    try {
      await saveBrain({ credentials: { [cur === 'gemini' ? 'GEMINI_API_KEY' : 'OPENAI_API_KEY']: v } });
      setKeySaved(cur === 'gemini' ? 'voice-gemini' : 'voice-openai');
      e.target.value = ''; e.target.placeholder = 'key saved — paste a new one to replace it';
      if (note) { note.dataset.state = 'ok'; note.textContent = 'key saved'; }
      updateOverview(); updatePrivacy();
    } catch (err) { if (note) { note.dataset.state = 'fail'; note.textContent = `key failed — ${err.message}`; } }
  });
  paintVoiceProvider();

  // ── background listening (observer) ──
  wireObserver();

  // ── transcription status ──
  wireTranscription();

  // ── developer diagnostics (Argus) ──
  wireArgus();

  // ── permissions ──
  wirePermissions();

  // ── updates ──
  wireUpdates();

  // Default landing page.
  showPage('page-overview');
}

// ── Ollama pull (fully-local download with live progress) ───────────────────
async function onOllamaPull() {
  const rec = _ollamaRec; if (!rec) return;
  const btn = $('#set-ollama-pull'); const status = $('#set-ollama-status');
  const bar = $('#set-pullbar'); const fill = $('#set-pullbar-fill'); const lab = $('#set-pullbar-label');
  btn.disabled = true; bar.hidden = false; fill.style.width = '0%';
  lab.textContent = `Downloading ${rec.label}…`;
  if (status) { status.dataset.state = 'wait'; status.textContent = `Downloading ${rec.label}…`; }
  try {
    const r = await fetch(`${DAEMON_HTTP}/v1/ollama/pull`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: rec.name }),
    });
    if (!r.ok || !r.body) throw new Error(`pull failed (${r.status})`);
    const reader = r.body.getReader(); const dec = new TextDecoder();
    let buf = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split('\n'); buf = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        let p; try { p = JSON.parse(line); } catch { continue; }
        if (p.error) throw new Error(p.error);
        if (p.total && p.completed != null) {
          const pct = Math.round((100 * p.completed) / p.total);
          fill.style.width = `${pct}%`;
          lab.textContent = `${rec.label}: ${pct}%  (${(p.completed / 1e9).toFixed(1)} / ${(p.total / 1e9).toFixed(1)} GB)`;
          if (status) status.textContent = `Downloading ${pct}%`;
        } else if (p.status) { lab.textContent = `${rec.label}: ${p.status}`; }
      }
    }
    fill.style.width = '100%'; lab.textContent = `${rec.label}: activating…`;
    await saveBrain({ provider: 'ollama', model_name: rec.name });
    live.model = rec.name; live.provider = 'ollama';
    lab.textContent = `${rec.label}: active`;
    if (status) { status.dataset.state = 'ok'; status.textContent = `Fully local. ${rec.label} is active.`; }
    await applyProvider('ollama', rec.name);
  } catch (err) {
    const msg = String(err.message || err);
    if (/newer version of Ollama/i.test(msg)) {
      lab.textContent = 'Ollama is too old for this model — Install Ollama gets you the update, then retry.';
      $('#set-ollama-install').hidden = false;
    } else {
      lab.textContent = msg;
      if (status) { status.dataset.state = 'fail'; status.textContent = msg; }
    }
  } finally {
    btn.disabled = false;
  }
}

// ── provider switch ─────────────────────────────────────────────────────────
async function switchProvider(provider) {
  const prev = selectedProvider;
  if (provider === 'codex') {
    try {
      const cfg = await window.sunday.getConfig(); if (cfg.daemonHttp) DAEMON_HTTP = cfg.daemonHttp;
      const m = await window.sunday.runMode();
      await applyProvider('codex');
      if (!m.local) { setCodexStatus('ChatGPT runs on This Mac — switch the runtime above first.', 'fail'); return; }
      const s = await (await fetch(`${DAEMON_HTTP}/v1/codex/status`)).json().catch(() => ({}));
      if (s.connected) { await saveBrain({ provider: 'codex' }); live.provider = 'codex'; updateImpact(); updateOverview(); updatePrivacy(); }
    } catch (err) { setProvStatus('codex', `failed — ${err.message}`, 'fail'); }
    return;
  }
  setProvStatus(provider, 'switching…', 'wait');
  try {
    await saveBrain({ provider });
    live.provider = provider;
    setProvStatus(provider, '', '');
    await applyProvider(provider);
  } catch (err) {
    setProvStatus(provider, `failed — ${err.message}`, 'fail');
    await refreshBrainProvider();   // revert visual selection to authoritative state
  }
}

// ── migrate modal (cloud/remote → This Mac) ─────────────────────────────────
function openMigrateModal() {
  let modal = $('#set-migrate-modal');
  if (modal) modal.remove();
  modal = document.createElement('div');
  modal.id = 'set-migrate-modal';
  modal.className = 'set-modal-backdrop';
  modal.innerHTML = `
    <div class="set-modal" role="dialog" aria-modal="true" aria-labelledby="set-migrate-title">
      <h3 id="set-migrate-title">Move Sunday to this Mac?</h3>
      <p class="set-modal-body" id="set-migrate-note">Your chat and memory can come along, or you can start clean. The daemon will run inside this app afterwards.</p>
      <div class="set-modal-actions">
        <button type="button" class="btn" id="set-migrate-cancel">Cancel</button>
        <button type="button" class="btn" id="set-migrate-fresh">Start clean</button>
        <button type="button" class="btn btn-primary" id="set-migrate">Copy chat and memory</button>
      </div>
    </div>`;
  document.body.appendChild(modal);
  const close = () => { modal.remove(); paintRuntime(); };
  $('#set-migrate-cancel').addEventListener('click', close);
  modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
  document.addEventListener('keydown', function esc2(e) { if (e.key === 'Escape') { close(); document.removeEventListener('keydown', esc2); } });

  $('#set-migrate-fresh').addEventListener('click', async () => {
    const note = $('#set-migrate-note'); note.textContent = 'Switching to this Mac…';
    disableModalButtons(modal);
    const r = await window.sunday.setRunMode('local');
    if (r && r.error) { note.textContent = `Couldn't switch: ${r.error}`; enableModalButtons(modal); await refreshRunMode(); }
    // On success the main process reloads the window.
  });
  $('#set-migrate').addEventListener('click', async () => {
    const note = $('#set-migrate-note'); note.textContent = 'Copying your chat and memory down…';
    disableModalButtons(modal);
    try {
      const r = await window.sunday.migrateToLocal();
      note.textContent = r.ok
        ? `Copied ${(r.files || []).length} databases — now running on this Mac.`
        : `Failed: ${r.error}`;
      if (!r.ok) { enableModalButtons(modal); }
      await refreshRunMode();
      if (r.ok) setTimeout(close, 1200);
    } catch (e) { note.textContent = `Failed: ${e.message}`; enableModalButtons(modal); }
  });
}
function disableModalButtons(modal) { modal.querySelectorAll('button').forEach((b) => b.disabled = true); }
function enableModalButtons(modal) { modal.querySelectorAll('button').forEach((b) => b.disabled = false); }

// ── custom servers: add a server via form ───────────────────────────────────
async function onMcpAdd(e) {
  e.preventDefault();
  const status = $('#mcp-add-status'); status.dataset.state = ''; status.textContent = '';
  const name = $('#mcp-add-name').value.trim();
  const type = document.querySelector('#mcp-add-type .seg-btn.active')?.dataset.type || 'remote';
  const target = $('#mcp-add-target').value.trim();
  if (!name || !target) { status.dataset.state = 'fail'; status.textContent = 'Name and ' + (type === 'remote' ? 'URL' : 'command') + ' are required.'; return; }
  let headers;
  if (type === 'remote') {
    const raw = $('#mcp-add-headers').value.trim();
    if (raw) { try { headers = JSON.parse(raw); } catch { status.dataset.state = 'fail'; status.textContent = 'Headers must be valid JSON.'; return; } }
  }
  // Merge into the existing config, then save through the same /v1/mcp flow.
  let cfg;
  try { cfg = $('#mcp-config').value.trim() ? JSON.parse($('#mcp-config').value) : { mcpServers: {} }; }
  catch { cfg = { mcpServers: {} }; }
  cfg.mcpServers = cfg.mcpServers || {};
  if (type === 'remote') {
    const entry = { url: target }; if (headers) entry.headers = headers;
    cfg.mcpServers[name] = entry;
  } else {
    const parts = target.split(/\s+/);
    cfg.mcpServers[name] = { command: parts[0], args: parts.slice(1) };
  }
  status.dataset.state = 'wait'; status.textContent = 'connecting…';
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/mcp`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ config: cfg }) });
    const d = await res.json();
    if (d.error) { status.dataset.state = 'fail'; status.textContent = d.error.slice(0, 80); return; }
    $('#mcp-config').value = JSON.stringify(d.config || cfg, null, 2);
    renderMcpServers(d.servers || []);
    status.dataset.state = 'ok'; status.textContent = `added ${name}`;
    $('#mcp-add-name').value = ''; $('#mcp-add-target').value = ''; $('#mcp-add-headers').value = '';
  } catch (err) { status.dataset.state = 'fail'; status.textContent = err.message; }
}

async function onMcpSaveRaw() {
  const s = $('#mcp-status'); s.dataset.state = ''; s.textContent = 'connecting…';
  let cfg;
  try { cfg = JSON.parse($('#mcp-config').value); }
  catch (e) { s.dataset.state = 'fail'; s.textContent = 'invalid JSON'; return; }
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/mcp`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ config: cfg }) });
    const d = await res.json();
    if (d.error) { s.dataset.state = 'fail'; s.textContent = d.error.slice(0, 80); return; }
    const ok = (d.servers || []).filter((x) => x.connected).length;
    const tools = (d.servers || []).reduce((a, x) => a + (x.tools || []).length, 0);
    s.dataset.state = 'ok'; s.textContent = `connected ${ok} server(s), ${tools} tools`;
    renderMcpServers(d.servers || []);
  } catch (err) { s.dataset.state = 'fail'; s.textContent = err.message; }
}

// ── background listening (observer) ─────────────────────────────────────────
function wireObserver() {
  let observerPoll = null;
  function setObserverUI(s) {
    const statusEl = $('#set-observer-status');
    const btn = $('#set-observer-toggle');
    const mic = s.mic || 'unknown';
    btn.textContent = s.enabled ? 'Turn off' : 'Turn on';
    if (s.error && s.error.startsWith('mic-denied')) {
      statusEl.dataset.state = 'fail';
      statusEl.textContent = 'Blocked by microphone permission — enable Sunday in System Settings → Privacy → Microphone';
    } else if (s.error && s.error.startsWith('mic-')) {
      statusEl.dataset.state = 'fail';
      statusEl.textContent = `Microphone not available (${mic})`;
    } else if (s.error) {
      statusEl.dataset.state = 'fail';
      statusEl.textContent = `failed — ${s.error}`;
    } else if (s.enabled && s.running) {
      statusEl.dataset.state = 'ok';
      statusEl.textContent = 'Listening';
    } else if (s.enabled) {
      statusEl.dataset.state = '';
      statusEl.textContent = 'Starting…';
    } else {
      statusEl.dataset.state = '';
      statusEl.textContent = 'Off';
    }
  }
  async function refreshObserverUI() {
    try { setObserverUI(await window.sunday.observerStatus()); } catch {}
  }
  function startObserverPolling() {
    if (observerPoll) return;
    observerPoll = setInterval(refreshObserverUI, 4000);
  }
  $('#set-observer-toggle').addEventListener('click', async () => {
    const btn = $('#set-observer-toggle'); btn.disabled = true;
    try {
      const perms = await window.sunday.permissionsStatus();
      if (perms.microphone !== 'granted') {
        const status = $('#set-observer-status');
        status.dataset.state = 'fail';
        status.textContent = 'Blocked by microphone permission — grant it on the Privacy page';
        await window.sunday.requestMicrophone();
        return;
      }
      const s = await window.sunday.observerStatus();
      const result = await window.sunday.observerSet(!s.enabled);
      setObserverUI(result);
      startObserverPolling();
    } finally { btn.disabled = false; }
  });
  refreshObserverUI().then(() => {
    window.sunday.observerStatus().then((s) => { if (s.running) startObserverPolling(); }).catch(() => {});
  });
}

// ── transcription status ────────────────────────────────────────────────────
function wireTranscription() {
  let installLines = [];
  window.sunday.onInstallLog((line) => {
    installLines.push(line.line || line);
    if (installLines.length > 40) installLines = installLines.slice(-40);
    refreshTransUI();
  });
  async function refreshTransUI() {
    try {
      const t = await window.sunday.transcriptionStatus();
      live.transcriptionReady = !!t.ready;
      live.transcriptionModel = t.model_name || '';
      updatePrivacy();
      const box = $('#set-trans');
      if (!box) return;
      if (t.ready) {
        const m = t.model_name ? ` (${t.model_name})` : '';
        const upgradeNote = t.upgrading
          ? '<div class="set-trans-sub">Upgrading to a better model in the background — current transcription continues uninterrupted.</div>'
          : '';
        box.innerHTML = `<div class="set-trans-on">On-device transcription ready${m}. Audio never leaves this Mac.</div>${upgradeNote}`;
        return;
      }
      const lastLog = installLines[installLines.length - 1] || '';
      const installing = lastLog && !lastLog.startsWith('✓');
      box.innerHTML = `
        <div class="set-trans-pending">
          <div class="set-trans-head">${installing ? 'Setting up on-device transcription…' : 'Remote transcription fallback active — audio goes to OpenAI until the local model is ready.'}</div>
          ${installing ? `<pre class="set-trans-log">${esc(installLines.slice(-12).join('\n'))}</pre>` : ''}
        </div>`;
    } catch {}
  }
  refreshTransUI();
  setInterval(refreshTransUI, 4000);
}

// ── developer diagnostics (Argus) ───────────────────────────────────────────
function wireArgus() {
  async function refreshArgusUI() {
    const statusEl = $('#set-argus-status');
    const btn = $('#set-argus-toggle');
    const openBtn = $('#set-argus-open');
    if (!statusEl || !btn) return;
    let s; try { s = await window.sunday.argusStatus(); } catch { return; }
    btn.textContent = s.enabled ? 'Turn off' : 'Turn on';
    btn.disabled = !s.available;
    if (openBtn) openBtn.hidden = !s.enabled;
    if (!s.available) { statusEl.dataset.state = 'fail'; statusEl.textContent = 'not bundled in this build'; }
    else if (!s.nodeOk && !s.enabled) { statusEl.dataset.state = 'fail'; statusEl.textContent = 'needs Node 22+ on your PATH'; }
    else if (s.enabled && s.running) { statusEl.dataset.state = 'ok'; statusEl.textContent = `on — ${s.url}`; }
    else if (s.enabled) { statusEl.dataset.state = ''; statusEl.textContent = 'on — starting…'; }
    else { statusEl.dataset.state = ''; statusEl.textContent = 'off'; }
  }
  $('#set-argus-toggle')?.addEventListener('click', async () => {
    const btn = $('#set-argus-toggle'); btn.disabled = true;
    const statusEl = $('#set-argus-status');
    try {
      const s = await window.sunday.argusStatus();
      if (statusEl) statusEl.textContent = s.enabled ? 'stopping…' : 'starting up and reconnecting the brain…';
      const r = await window.sunday.setArgus(!s.enabled);
      if (!r.ok && statusEl) { statusEl.dataset.state = 'fail'; statusEl.textContent = r.error || 'failed'; }
    } finally { btn.disabled = false; await refreshArgusUI(); }
  });
  $('#set-argus-open')?.addEventListener('click', () => window.sunday.openArgus());
  refreshArgusUI();

  // Start-at-login toggle (mirrors the BTM login-item state both ways).
  async function refreshLoginUI() {
    const statusEl = $('#set-login-status');
    const btn = $('#set-login-toggle');
    if (!statusEl || !btn) return;
    let s; try { s = await window.sunday.loginItemGet(); } catch { return; }
    if (!s.ok) { statusEl.dataset.state = 'fail'; statusEl.textContent = s.error || 'unavailable'; btn.disabled = true; return; }
    btn.disabled = false;
    btn.textContent = s.openAtLogin ? 'Turn off' : 'Turn on';
    statusEl.dataset.state = s.openAtLogin ? 'ok' : '';
    statusEl.textContent = s.openAtLogin ? 'on — opens when you log in' : 'off';
  }
  $('#set-login-toggle')?.addEventListener('click', async () => {
    const btn = $('#set-login-toggle'); btn.disabled = true;
    try {
      const cur = await window.sunday.loginItemGet();
      await window.sunday.loginItemSet(!cur.openAtLogin);
    } finally { await refreshLoginUI(); }
  });
  refreshLoginUI();
}

// ── permissions ─────────────────────────────────────────────────────────────
function wirePermissions() {
  const PERMS = [
    { id: 'microphone', key: 'microphone', request: () => window.sunday.requestMicrophone() },
    { id: 'screen',     key: 'screen',     request: () => window.sunday.requestScreen()     },
    { id: 'control',    key: 'control',    request: () => window.sunday.requestControl()    },
    { id: 'fulldisk',   key: 'fullDisk',   request: () => window.sunday.requestFullDisk()   },
  ];
  function applyPerm(p, statusMap) {
    const row = document.getElementById(`perm-${p.id}`);
    if (!row) return;
    const status = statusMap[p.key] || 'not-determined';
    const statusEl = row.querySelector('.perm-status');
    const btn      = row.querySelector('.perm-grant');
    if (status === 'granted') {
      statusEl.dataset.state = 'ok'; statusEl.textContent = 'allowed'; btn.hidden = true;
    } else if (status === 'denied') {
      statusEl.dataset.state = 'fail'; statusEl.textContent = 'denied — open System Settings to enable'; btn.hidden = false;
    } else {
      statusEl.dataset.state = ''; statusEl.textContent = 'not granted'; btn.hidden = false;
    }
  }
  async function refreshPerms() {
    try {
      const m = await window.sunday.permissionsStatus();
      let granted = 0;
      for (const p of PERMS) { applyPerm(p, m); if ((m[p.key] || '') === 'granted') granted++; }
      live.permsGranted = granted; live.permsTotal = PERMS.length;
      updateOverview();
    } catch {}
  }
  for (const p of PERMS) {
    const row = document.getElementById(`perm-${p.id}`);
    row?.querySelector('.perm-grant')?.addEventListener('click', async () => {
      try { await p.request(); } catch {}
      for (let i = 0; i < 8; i++) { await new Promise((r) => setTimeout(r, 500)); await refreshPerms(); }
    });
  }
  refreshPerms();
  setInterval(refreshPerms, 2000);
}

// ── updates ─────────────────────────────────────────────────────────────────
function wireUpdates() {
  const updVerLine = document.getElementById('upd-version-line');
  const updStateText = document.getElementById('upd-state-text');
  const updBar = document.getElementById('upd-bar');
  const updBarFill = document.getElementById('upd-bar-fill');
  const updCheckBtn = document.getElementById('upd-check-btn');
  const updRestartBtn = document.getElementById('upd-restart-btn');
  function applyUpdState(s) {
    if (!s) return;
    const phase = s.phase || 'idle';
    _updateReady = phase === 'downloaded';
    renderAttention();
    if (updVerLine) updVerLine.textContent = s.current ? `You're on Sunday ${s.current}.` : '';
    updBar.hidden = !(phase === 'downloading' || phase === 'available');
    updBarFill.style.width = `${Math.max(0, Math.min(100, s.percent || 0))}%`;
    updRestartBtn.hidden = phase !== 'downloaded';
    updCheckBtn.disabled = (phase === 'checking' || phase === 'available' || phase === 'downloading');
    const text = {
      idle:        'Tap Check for updates to ping the feed.',
      checking:    'Checking the update feed…',
      available:   `New version ${s.version || ''} found — downloading…`,
      downloading: `Downloading ${s.version || ''}… ${Math.round(s.percent || 0)}%`,
      downloaded:  `Sunday ${s.version || ''} is ready — restart to apply.`,
      none:        'You\'re running the latest version.',
      error:       `Update check failed${s.message ? ': ' + s.message : ''}.`,
    }[phase] || '';
    updStateText.textContent = text;
    updStateText.dataset.state = (phase === 'downloaded' || phase === 'none') ? 'ok' : (phase === 'error' ? 'fail' : '');
  }
  window.sunday.updateState().then(applyUpdState).catch(() => {});
  window.sunday.onUpdateState(applyUpdState);
  updCheckBtn?.addEventListener('click', () => window.sunday.updateCheck().catch(() => {}));
  updRestartBtn?.addEventListener('click', () => window.sunday.updateRestart().catch(() => {}));
}

function uptime(s) { if (s < 60) return `${Math.round(s)}s`; if (s < 3600) return `${Math.round(s / 60)}m`; if (s < 86400) return `${Math.round(s / 3600)}h`; return `${Math.round(s / 86400)}d`; }
function esc(s) { return String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
