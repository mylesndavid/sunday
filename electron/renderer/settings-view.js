// Settings tab — connection, model, personality, screen access, and live
// system status (daemon + devices + memory), all in-window. Every control
// writes real state.

const $ = (s) => document.querySelector(s);
let DAEMON_HTTP = '';
let defaultPrompt = '';
let sysTimer = null;

export function init(daemonHttp) {
  DAEMON_HTTP = daemonHttp;
  wire();
}
export function setDaemon(http) { DAEMON_HTTP = http; }

function flashSaved() {
  const el = $('#set-saved');
  el.hidden = false; el.style.color = ''; el.textContent = 'saved';
  clearTimeout(el._t); el._t = setTimeout(() => { el.hidden = true; }, 1500);
}
function flashError(msg) {
  const el = $('#set-saved');
  el.hidden = false; el.style.color = 'var(--error)'; el.textContent = msg;
  clearTimeout(el._t); el._t = setTimeout(() => { el.hidden = true; el.style.color = ''; }, 3000);
}

export async function loadAll() {
  const cfg = await window.sunday.getConfig();
  DAEMON_HTTP = cfg.daemonHttp || DAEMON_HTTP;
  $('#set-http').value = cfg.daemonHttp || '';
  $('#set-ws').value = cfg.daemonWs || '';
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/config`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const c = await res.json();
    $('#set-provider').value = c.model?.provider || '';
    $('#set-model').value = c.model?.name || '';
    await loadModels();
    showCurrentModel();
    defaultPrompt = c.identity_prompt?.default || '';
    const eff = c.identity_prompt?.effective || '';
    const custom = !!c.identity_prompt?.custom_present;
    $('#set-prompt').value = custom ? eff : '';
    $('#set-prompt').placeholder = custom ? 'custom personality saved' : `using the built-in default. Type here to make Sunday your own.`;
    $('#set-prompt-status').textContent = custom ? 'custom — active' : 'using default';
    updateChars();
  } catch (err) {
    flashError(`couldn't load: ${err.message}`);
  }
  refreshSystem();
  loadConnections();
  loadMcp();
}

async function loadMcp() {
  try {
    const d = await (await fetch(`${DAEMON_HTTP}/v1/mcp`)).json();
    const cfg = d.config && Object.keys(d.config.mcpServers || {}).length ? d.config : null;
    if (cfg && !$('#mcp-config').value.trim()) $('#mcp-config').value = JSON.stringify(cfg, null, 2);
    renderMcpServers(d.servers || []);
  } catch {}
}

function renderMcpServers(servers) {
  const ul = $('#mcp-servers');
  if (!servers.length) { ul.innerHTML = ''; return; }
  ul.innerHTML = servers.map((s) => `
    <li class="conn-row">
      <span class="conn-ico"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M4 12h16M4 17h10"/></svg></span>
      <span class="conn-name">${esc(s.name)}</span>
      ${s.connected
        ? `<span class="conn-on">${(s.tools || []).length} tools</span>`
        : `<span class="set-verify" data-state="fail" style="flex:0">${esc((s.error || 'failed').slice(0, 60))}</span>`}
    </li>`).join('');
}

// ── dynamic connections: drive the 800+ Nango catalog directly ────────────

// Curated default category set — Nango carries 30+, that's too many. These
// are the ones a personal-AI user actually reaches for. Anything outside the
// list is reachable via search.
const CONN_CATS = ['popular', 'productivity', 'communication', 'dev-tools'];
const CONN_LIST_MAX = 12;
let connState = { providers: [], cat: 'popular', q: '', connected: new Set() };
let connSearchTimer = null;

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

// Top section: every connected provider gets a row + toggle (when we ship
// tools for it). Driven by /v1/connectors which already joins Nango's
// connection list with our connector toggle state.
async function loadPinnedConnectors() {
  const ul = $('#conn-pinned');
  try {
    const d = await (await fetch(`${DAEMON_HTTP}/v1/connectors`)).json();
    const conns = d.connectors || [];
    document.querySelector('#conn-unconfigured').hidden = true;
    // Track which provider keys are connected, for the catalog list below.
    connState.connected = new Set(conns.map((c) => c.provider));
    if (!conns.length) {
      ul.innerHTML = '<li class="conn-pinned-empty">No connectors yet. Add one below to start.</li>';
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
          <span class="conn-pinned-hint">${c.enabled ? 'pinned to every chat' : 'on demand via find_tools'}</span>
          <label class="toggle">
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

// Small lookup so the pinned row reads "Gmail" not "google-mail". Falls back
// to the raw key if unknown — won't break, just less pretty.
const PROVIDER_LABEL = {
  'google-mail':     'Gmail',
  'google-calendar': 'Google Calendar',
  'fireflies':       'Fireflies',
};

async function toggleConnector(provider, on, cb) {
  // Optimistic UI — flip the hint text immediately, revert on error.
  const row = cb.closest('.conn-pinned-row');
  const hint = row?.querySelector('.conn-pinned-hint');
  if (hint) hint.textContent = on ? 'pinned to every chat' : 'on demand via find_tools';
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/connectors/toggle`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider, on }),
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      throw new Error(d.error || `HTTP ${res.status}`);
    }
    flashSaved();
  } catch (err) {
    cb.checked = !on;
    if (hint) hint.textContent = !on ? 'pinned to every chat' : 'on demand via find_tools';
    flashError(err.message);
  }
}

function renderConnCats() {
  const wrap = $('#conn-cats');
  wrap.innerHTML = CONN_CATS.map((c) => `
    <button class="conn-cat ${c === connState.cat ? 'on' : ''}" data-cat="${c}">${c}</button>
  `).join('');
  wrap.querySelectorAll('.conn-cat').forEach((b) => b.addEventListener('click', () => {
    connState.cat = b.dataset.cat;
    renderConnCats();
    refreshConnList();
  }));
}

async function refreshConnList() {
  const ul = $('#conn-list');
  ul.innerHTML = '<li class="conn-loading">loading…</li>';
  const params = new URLSearchParams();
  // When the user is searching, span everything; otherwise scope to category.
  if (connState.q) params.set('q', connState.q);
  else if (connState.cat) params.set('category', connState.cat);
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/integrations/catalog?${params}`);
    const d = await res.json();
    const items = (d.providers || []).slice(0, CONN_LIST_MAX);
    if (!items.length) { ul.innerHTML = '<li class="conn-loading">no matches.</li>'; return; }
    ul.innerHTML = items.map((p) => {
      const connected = connState.connected.has(p.name);
      return `
        <li class="conn-row" data-name="${p.name}">
          <span class="conn-name">${esc(p.display_name)}</span>
          <span class="conn-mode">${esc(p.auth_mode || '')}</span>
          ${connected
            ? '<span class="conn-on">connected</span>'
            : `<button class="btn conn-pick" data-name="${p.name}">Set up</button>`}
        </li>`;
    }).join('');
    ul.querySelectorAll('.conn-pick').forEach((b) => b.addEventListener('click', () => openConnCard(b.dataset.name)));
  } catch (err) {
    ul.innerHTML = `<li class="conn-loading">couldn't reach the daemon: ${esc(err.message)}</li>`;
  }
}

// ── dynamic setup card ────────────────────────────────────────────────────

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

  // Wire up controls inside the rendered card.
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

  // Resolve the field schema we're going to render.
  const fields = [];
  if (oauth) {
    // Standard OAuth2 needs the user's own OAuth client.
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
      <button class="btn conn-card-close" aria-label="close">×</button>
    </div>

    ${oauth ? `
      <ol class="conn-steps">
        ${setupLink ? `<li>Open <a class="conn-link" data-href="${esc(setupLink)}" href="#">${spec.display_name}'s setup guide</a> and create an OAuth client.</li>` : ''}
        ${spec.redirect_uri ? `<li>When asked for an authorized redirect URI, paste <span class="conn-copy" data-copy="${esc(spec.redirect_uri)}" title="click to copy"><code>${esc(spec.redirect_uri)}</code></span></li>` : ''}
        <li>Copy the resulting <strong>Client ID</strong> and <strong>Client Secret</strong> into the fields below.</li>
      </ol>` : ''}
    ${(!oauth && setupLink) ? `<p class="conn-card-help">See the <a class="conn-link" data-href="${esc(setupLink)}" href="#">${spec.display_name} setup guide</a> for where to find these values.</p>` : ''}

    <div class="conn-fields">${fieldHtml || '<p class="conn-card-help">No fields needed — click Connect to start the OAuth flow.</p>'}</div>

    <div class="conn-card-actions">
      <button class="btn conn-submit">Save &amp; Connect</button>
      <span class="conn-card-status" id="conn-card-status"></span>
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
  // OAuth2: pass the catalog's default_scopes if we have them (Nango fills
  // in defaults otherwise).
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
      // Non-OAuth: connection was created server-side, no browser needed.
      connState.connected.add(spec.name);
      $('#conn-card').hidden = true;
      loadPinnedConnectors();   // surface the new row in the pinned section
      refreshConnList();
      flashSaved();
      return;
    }
    if (d.connect_url) {
      btn.textContent = 'approve in browser…';
      await window.sunday.openExternal(d.connect_url);
      status.textContent = "I'll mark it connected as soon as you approve.";
      pollProviderConnected(spec.name, 0);
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

function pollProviderConnected(name, n) {
  if (n > 60) return;  // ~90s of patience
  setTimeout(async () => {
    try {
      const d = await (await fetch(`${DAEMON_HTTP}/v1/integrations`)).json();
      const hit = (d.providers || []).find((p) => p.id === name && p.connected);
      if (hit) {
        connState.connected.add(name);
        $('#conn-card').hidden = true;
        loadPinnedConnectors();
        refreshConnList();
        flashSaved();
      } else {
        pollProviderConnected(name, n + 1);
      }
    } catch { pollProviderConnected(name, n + 1); }
  }, 1500);
}

function updateChars() { $('#set-prompt-chars').textContent = `${$('#set-prompt').value.length} chars`; }

// ── model picker (searchable OpenRouter catalog) ──
let allModels = [];
let modelsLoaded = false;

async function loadModels() {
  if (modelsLoaded) return;
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/models`);
    const d = await res.json();
    allModels = d.models || [];
    modelsLoaded = allModels.length > 0;
  } catch { allModels = []; }
}

function showCurrentModel() {
  const slug = $('#set-model').value.trim();
  const cur = $('#set-model-current');
  $('#set-model-save').disabled = !slug;
  if (!slug) { cur.hidden = true; return; }
  const m = allModels.find((x) => x.id === slug);
  const vision = m?.vision;
  cur.hidden = false;
  cur.innerHTML = `<span class="mc-label">Current</span>
    <span class="mc-name">${esc(m?.name || slug)}</span>
    ${vision ? '<span class="mc-vision">sees images</span>' : '<span class="mc-text">text only</span>'}
    <span class="mc-slug">${esc(slug)}</span>`;
}

function renderModelResults(q) {
  const box = $('#set-model-results');
  const query = q.trim().toLowerCase();
  let list = allModels;
  if (query) list = allModels.filter((m) => m.id.toLowerCase().includes(query) || (m.name || '').toLowerCase().includes(query));
  list = list.slice(0, 60);
  if (!list.length) { box.hidden = true; return; }
  box.innerHTML = list.map((m) => `
    <button class="model-row" data-id="${esc(m.id)}">
      <span class="mr-name">${esc(m.name || m.id)}</span>
      ${m.vision ? '<span class="mr-vision">sees images</span>' : ''}
      <span class="mr-slug">${esc(m.id)}</span>
    </button>`).join('');
  box.hidden = false;
  box.querySelectorAll('.model-row').forEach((b) => b.addEventListener('mousedown', (e) => {
    e.preventDefault();
    pickModel(b.dataset.id);
  }));
}

function pickModel(id) {
  $('#set-model').value = id;
  $('#set-model-search').value = '';
  $('#set-model-results').hidden = true;
  $('#set-model-save').disabled = false;
  showCurrentModel();
}

async function refreshSystem() {
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/health`);
    if (!res.ok) return;
    const h = await res.json();
    const d = h.daemon || {};
    $('#sys-grid').innerHTML = `
      <span class="k">version</span><span class="v">${esc(d.version || '—')}</span>
      <span class="k">model</span><span class="v">${esc((d.model || '—').split('/').slice(-1)[0])}</span>
      <span class="k">messages</span><span class="v">${d.messages ?? '—'}</span>
      <span class="k">tools</span><span class="v">${d.tools_count ?? '—'}</span>
      <span class="k">uptime</span><span class="v">${d.uptime_s ? uptime(d.uptime_s) : '—'}</span>`;
    const devs = h.devices || [];
    $('#sys-devices').innerHTML = devs.length
      ? devs.map((x) => `<li><strong>${esc(x.device_id)}</strong><div>${(x.capabilities || []).map((c) => `<span class="cap">${esc(c)}</span>`).join('')}</div></li>`).join('')
      : '<li class="empty">no devices connected</li>';
    const m = h.memory || {};
    $('#sys-memory').innerHTML = `
      <span class="k">available</span><span class="v">${m.available ? 'yes' : 'no'}</span>
      <span class="k">facts</span><span class="v">${m.total ?? '—'}</span>`;
  } catch {}
}

export function startSystemPolling() { refreshSystem(); if (!sysTimer) sysTimer = setInterval(refreshSystem, 5000); }
export function stopSystemPolling() { if (sysTimer) { clearInterval(sysTimer); sysTimer = null; } }

function wire() {
  // ── ambient observer toggle ──
  // Honest status: "listening" ONLY when capture is genuinely live. If the
  // mic was denied we say so and point at System Settings, instead of the
  // old green lie. Re-polls while on so a mid-session capture death surfaces.
  let observerPoll = null;
  function setObserverUI(s) {
    const statusEl = $('#set-observer-status');
    const btn = $('#set-observer-toggle');
    const mic = s.mic || 'unknown';
    if (s.running) {
      statusEl.dataset.state = 'ok';
      statusEl.textContent = 'on — listening';
      btn.textContent = 'Turn off';
    } else if (s.error && s.error.startsWith('mic-denied')) {
      statusEl.dataset.state = 'fail';
      statusEl.textContent = 'mic permission denied — enable Sunday in System Settings → Privacy → Microphone';
      btn.textContent = 'Turn on';
    } else if (s.error && s.error.startsWith('mic-')) {
      statusEl.dataset.state = 'fail';
      statusEl.textContent = `mic not available (${mic})`;
      btn.textContent = 'Turn on';
    } else if (s.error) {
      statusEl.dataset.state = 'fail';
      statusEl.textContent = `couldn't start: ${s.error}`;
      btn.textContent = 'Turn on';
    } else {
      statusEl.dataset.state = '';
      statusEl.textContent = 'off';
      btn.textContent = 'Turn on';
    }
  }
  async function refreshObserverUI() {
    try { setObserverUI(await window.sunday.observerStatus()); } catch {}
  }
  function startObserverPolling() {
    if (observerPoll) return;
    observerPoll = setInterval(refreshObserverUI, 4000);
  }
  function stopObserverPolling() {
    if (observerPoll) { clearInterval(observerPoll); observerPoll = null; }
  }
  $('#set-observer-toggle').addEventListener('click', async () => {
    const btn = $('#set-observer-toggle'); btn.disabled = true;
    try {
      const s = await window.sunday.observerStatus();
      const result = await window.sunday.observerSet(!s.running);
      setObserverUI(result);
      if (result.running) startObserverPolling(); else stopObserverPolling();
    } finally { btn.disabled = false; }
  });
  refreshObserverUI().then(() => {
    // If it claims to be on at load, keep polling to catch silent death.
    window.sunday.observerStatus().then((s) => { if (s.running) startObserverPolling(); }).catch(() => {});
  });

  // Transcription status — local first; one-click install when missing.
  let installing = false;
  async function refreshTransUI() {
    try {
      const t = await window.sunday.transcriptionStatus();
      const box = $('#set-trans');
      if (!box) return;
      if (t.ready) {
        box.innerHTML = '<div class="set-trans-on">✓ transcription is fully on-device (whisper.cpp)</div>';
        return;
      }
      if (installing) return;   // don't redraw mid-install
      const missing = [];
      if (!t.bin)    missing.push('whisper-cpp');
      if (!t.ffmpeg) missing.push('ffmpeg');
      if (!t.model)  missing.push('base.en model (~150MB)');
      box.innerHTML = `
        <div class="set-trans-off">
          <div class="set-trans-head">Local transcription not set up — using OpenAI Whisper as fallback (audio leaves this Mac).</div>
          <div class="set-trans-sub">Need: ${missing.join(', ')}.</div>
          <div class="set-trans-actions">
            <button class="btn btn-primary set-trans-install" id="set-trans-install">Install locally</button>
          </div>
          <pre class="set-trans-log" id="set-trans-log" hidden></pre>
        </div>`;
      $('#set-trans-install')?.addEventListener('click', runInstall);
    } catch {}
  }
  async function runInstall() {
    installing = true;
    const logEl = $('#set-trans-log');
    const btn = $('#set-trans-install');
    if (btn) { btn.disabled = true; btn.textContent = 'Installing…'; }
    if (logEl) { logEl.hidden = false; logEl.textContent = ''; }
    window.sunday.onInstallLog((line) => {
      if (!logEl) return;
      logEl.textContent += (line.line || line) + '\n';
      logEl.scrollTop = logEl.scrollHeight;
    });
    const result = await window.sunday.installLocalTranscription();
    installing = false;
    if (result && result.ok) {
      setTimeout(refreshTransUI, 400);
    } else if (btn) {
      btn.disabled = false; btn.textContent = 'Try again';
    }
  }
  refreshTransUI();

  $('#set-conn-test').addEventListener('click', async () => {
    const url = $('#set-http').value.trim().replace(/\/+$/, '');
    const v = $('#set-conn-verify'); v.dataset.state = ''; v.textContent = `→ ${url}/v1/status …`;
    try {
      const res = await fetch(`${url}/v1/status`); if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const d = await res.json();
      v.dataset.state = 'ok';
      v.textContent = `✓ ${d.version} · ${(d.model || '').split('/').slice(-1)[0]} · ${d.messages} msgs · ${(d.devices || []).length} device`;
    } catch (err) { v.dataset.state = 'fail'; v.textContent = `✗ ${err.message}`; }
  });
  $('#set-conn-save').addEventListener('click', async () => {
    const http = $('#set-http').value.trim().replace(/\/+$/, '');
    const ws = $('#set-ws').value.trim() || (http.replace(/^http/, 'ws') + '/v1/ws');
    await window.sunday.saveConnection({ daemonHttp: http, daemonWs: ws });
    flashSaved();
  });
  const search = $('#set-model-search');
  search.addEventListener('focus', async () => { await loadModels(); renderModelResults(search.value); });
  search.addEventListener('input', () => renderModelResults(search.value));
  search.addEventListener('blur', () => setTimeout(() => { $('#set-model-results').hidden = true; }, 160));
  search.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { const v = search.value.trim(); if (v && allModels.some((m) => m.id === v)) pickModel(v); }
    if (e.key === 'Escape') { $('#set-model-results').hidden = true; }
  });
  $('#set-model-save').addEventListener('click', async () => {
    const model = $('#set-model').value.trim();
    if (!model) { flashError('pick a model first'); return; }
    try {
      const res = await fetch(`${DAEMON_HTTP}/v1/config`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model_name: model }) });
      if (!res.ok) throw new Error(`HTTP ${res.status}`); flashSaved();
    } catch (err) { flashError(`save failed: ${err.message}`); }
  });
  $('#set-prompt').addEventListener('input', updateChars);
  $('#set-prompt-show-default').addEventListener('click', () => { $('#set-prompt').value = defaultPrompt; $('#set-prompt-status').textContent = 'showing default — edit and save to override'; updateChars(); });
  $('#set-prompt-reset').addEventListener('click', async () => {
    if (!confirm("Reset Sunday's personality to the default?")) return;
    try {
      const res = await fetch(`${DAEMON_HTTP}/v1/config`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ identity_prompt: null }) });
      if (!res.ok) throw new Error(`HTTP ${res.status}`); await loadAll(); flashSaved();
    } catch (err) { flashError(`reset failed: ${err.message}`); }
  });
  $('#set-prompt-save').addEventListener('click', async () => {
    try {
      const res = await fetch(`${DAEMON_HTTP}/v1/config`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ identity_prompt: $('#set-prompt').value }) });
      if (!res.ok) throw new Error(`HTTP ${res.status}`); await loadAll(); flashSaved();
    } catch (err) { flashError(`save failed: ${err.message}`); }
  });
  $('#set-screen-grant').addEventListener('click', async () => {
    const s = $('#set-screen-status'); s.dataset.state = ''; s.textContent = 'requesting…';
    try {
      const r = await window.sunday.requestScreen();
      if (r.status === 'granted') { s.dataset.state = 'ok'; s.textContent = '✓ already allowed'; }
      else if (r.status === 'prompted') { s.textContent = 'enable “Sunday” in the window that opened, then relaunch'; }
      else { s.dataset.state = 'fail'; s.textContent = `error: ${r.error || 'unknown'}`; }
    } catch (err) { s.dataset.state = 'fail'; s.textContent = `error: ${err.message}`; }
  });
  $('#mcp-save').addEventListener('click', async () => {
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
  });

  $('#set-control-grant').addEventListener('click', async () => {
    const s = $('#set-control-status'); s.dataset.state = ''; s.textContent = 'requesting…';
    try {
      const r = await window.sunday.requestControl();
      if (r.status === 'granted') { s.dataset.state = 'ok'; s.textContent = '✓ already allowed'; }
      else if (r.status === 'prompted') { s.textContent = 'enable “Sunday” in the window that opened, then relaunch'; }
      else { s.dataset.state = 'fail'; s.textContent = `error: ${r.error || 'unknown'}`; }
    } catch (err) { s.dataset.state = 'fail'; s.textContent = `error: ${err.message}`; }
  });
  // section nav
  const links = Array.from(document.querySelectorAll('.set-nav a'));
  const stage = $('#set-stage');
  links.forEach((a) => a.addEventListener('click', (e) => { e.preventDefault(); document.getElementById(a.dataset.target)?.scrollIntoView({ behavior: 'smooth', block: 'start' }); }));
  const obs = new IntersectionObserver((entries) => {
    const vis = entries.filter((en) => en.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
    if (!vis.length) return;
    const id = vis[0].target.id;
    links.forEach((a) => a.classList.toggle('active', a.dataset.target === id));
  }, { root: stage, rootMargin: '-8% 0px -75% 0px', threshold: 0 });
  document.querySelectorAll('.set-block').forEach((s) => obs.observe(s));
}

function uptime(s) { if (s < 60) return `${Math.round(s)}s`; if (s < 3600) return `${Math.round(s / 60)}m`; if (s < 86400) return `${Math.round(s / 3600)}h`; return `${Math.round(s / 86400)}d`; }
function esc(s) { return String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
