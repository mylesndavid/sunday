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
    await refreshRunMode();   // sets the provider dropdown + loads its models via applyProvider
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
  loadBuiltinConnectors();
  try {
    const d = await (await fetch(`${DAEMON_HTTP}/v1/mcp`)).json();
    const cfg = d.config && Object.keys(d.config.mcpServers || {}).length ? d.config : null;
    if (cfg && !$('#mcp-config').value.trim()) $('#mcp-config').value = JSON.stringify(cfg, null, 2);
    renderMcpServers(d.servers || []);
  } catch {}
}

// One-click built-in connectors (e.g. Playwright browser). Toggle wires the
// stdio MCP server into mcp.json + reconnects; setup steps show when enabled.
async function loadBuiltinConnectors() {
  const wrap = $('#mcp-builtin'); if (!wrap) return;
  let d; try { d = await (await fetch(`${DAEMON_HTTP}/v1/mcp/builtin`)).json(); } catch { return; }
  renderBuiltins(wrap, d.connectors || []);
}
function renderBuiltins(wrap, connectors) {
  wrap.innerHTML = connectors.map((c) => `
    <div class="mcp-bi ${c.enabled ? 'on' : ''}">
      <div class="mcp-bi-head">
        <div class="mcp-bi-txt"><div class="mcp-bi-title">${esc(c.title)}</div><div class="mcp-bi-desc">${esc(c.desc)}</div></div>
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
          ${c.setup_url ? `<a href="#" data-href="${esc(c.setup_url)}" class="mcp-bi-link">Where do I get the extension + token? →</a>` : ''}
        </div>` : ''}
      ${c.enabled && (c.setup || []).length ? `<ol class="mcp-bi-setup">${c.setup.map((s) => `<li>${esc(s)}</li>`).join('')}${c.setup_url ? `<li><a href="#" data-href="${esc(c.setup_url)}" class="mcp-bi-link">Get the extension →</a></li>` : ''}</ol>` : ''}
    </div>`).join('');

  const toggle = async (id, enabled, token) => {
    const r = await fetch(`${DAEMON_HTTP}/v1/mcp/builtin`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, enabled, token }) });
    const d = await r.json();
    if (d.connectors) renderBuiltins(wrap, d.connectors);
    loadMcp();
  };
  wrap.querySelectorAll('button[data-act]').forEach((b) => b.addEventListener('click', async () => {
    const { act, id } = b.dataset;
    if (act === 'reveal') {   // show the token input
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

// Providers that need an API key (codex/ollama don't).
const _KEY_FOR = {
  openrouter: 'OPENROUTER_API_KEY', openai: 'OPENAI_API_KEY',
  anthropic: 'ANTHROPIC_API_KEY',
};
// Curated model lists for providers without a live catalog. OpenRouter + Ollama
// are fetched live; these are searchable + you can also type any custom id.
const STATIC_MODELS = {
  codex: [{ id: 'gpt-5.2', name: 'gpt-5.2' }, { id: 'gpt-5.5', name: 'gpt-5.5' }],
  openai: [{ id: 'gpt-4o', name: 'GPT-4o' }, { id: 'gpt-4o-mini', name: 'GPT-4o mini' }, { id: 'o3', name: 'o3' }, { id: 'o4-mini', name: 'o4-mini' }],
  anthropic: [{ id: 'claude-opus-4-1', name: 'Claude Opus 4.1' }, { id: 'claude-sonnet-4-5', name: 'Claude Sonnet 4.5' }, { id: 'claude-3-5-haiku-latest', name: 'Claude Haiku 3.5' }],
};
function fmtSize(bytes) { if (!bytes) return ''; const gb = bytes / 1e9; return gb >= 1 ? `${gb.toFixed(1)} GB` : `${Math.round(bytes / 1e6)} MB`; }

async function refreshRunMode() {
  try {
    const m = await window.sunday.runMode();
    document.querySelectorAll('#set-runmode .brain-card').forEach((b) =>
      b.classList.toggle('active', (b.dataset.mode === 'local') === !!m.local));
    const mig = $('#set-migrate-row');
    if (mig) mig.hidden = !!m.local;          // offer "bring history over" only while on cloud
    // ChatGPT (Codex) only works on a local brain — disable the row on cloud.
    const codexRow = document.querySelector('#set-prov-menu [data-provider="codex"]');
    if (codexRow) {
      codexRow.classList.toggle('dd-disabled', !m.local);
      const slug = codexRow.querySelector('.mr-slug');
      if (slug) slug.textContent = m.local ? 'your subscription · no key' : 'This Mac only';
    }
    await refreshBrainProvider();
  } catch {}
}

// Pull the daemon's active provider + model and reflect them into the UI.
async function refreshBrainProvider() {
  try {
    const c = await (await fetch(`${DAEMON_HTTP}/v1/config`)).json();
    await applyProvider(c.model?.provider || 'openrouter', c.model?.name);
  } catch {}
}

// The model list for a provider — live for OpenRouter/Ollama, curated otherwise.
// Returns an array, or {error} for Ollama when it can't be reached.
async function loadProviderModels(provider) {
  if (provider === 'openrouter') {
    try { const d = await (await fetch(`${DAEMON_HTTP}/v1/models`)).json(); return d.models || []; } catch { return []; }
  }
  if (provider === 'ollama') {
    try {
      const d = await (await fetch(`${DAEMON_HTTP}/v1/ollama/models`)).json();
      if (!d.available) return { error: 'Ollama is not running here. Install it from ollama.com, then run: ollama pull llama3.2' };
      if (!d.models.length) return { error: 'No models pulled yet — run: ollama pull llama3.2' };
      return d.models.map((m) => ({ id: m.name, name: m.name, slug: fmtSize(m.size) }));
    } catch { return { error: 'Could not reach Ollama.' }; }
  }
  return STATIC_MODELS[provider] || [];
}

// Single source of truth for the MODEL section: load the provider's models into
// the one searchable picker, toggle the key field + ChatGPT connect, reflect the
// current model. Same control for every provider.
const PROV_DD_LABEL = {
  codex: 'ChatGPT', openrouter: 'OpenRouter', ollama: 'Ollama', openai: 'OpenAI', anthropic: 'Anthropic',
};
let selectedProvider = 'openrouter';
async function applyProvider(provider, currentModel) {
  selectedProvider = provider;
  const label = $('#set-prov-label'); if (label) label.textContent = PROV_DD_LABEL[provider] || provider;
  const note = $('#set-model-note'); if (note) { note.textContent = ''; note.removeAttribute('data-state'); }
  // key field — only providers that need one
  const keyEl = $('#set-key'), needKey = _KEY_FOR[provider];
  if (keyEl) { keyEl.hidden = !needKey; if (needKey) keyEl.placeholder = `${needKey} — leave blank to keep current`; else keyEl.value = ''; }
  // model list for the picker
  const res = await loadProviderModels(provider);
  if (res && res.error) { allModels = []; if (note) { note.dataset.state = 'wait'; note.textContent = res.error; } }
  else allModels = res;
  $('#set-model').value = currentModel || '';
  $('#set-model-search').value = '';
  $('#set-model-results').hidden = true;
  showCurrentModel();
  // ChatGPT connect / status
  const connect = $('#set-connect');
  if (provider === 'codex') {
    let connected = false, email = '';
    try { const s = await (await fetch(`${DAEMON_HTTP}/v1/codex/status`)).json(); connected = s.connected; email = s.email || ''; } catch {}
    if (connect) connect.hidden = connected;
    setCodexStatus(connected ? (email ? `Connected as ${email}` : 'Connected') : 'Sign in to use your ChatGPT subscription.', connected ? 'ok' : 'wait');
  } else {
    if (connect) connect.hidden = true;
    setCodexStatus('', '');
  }
  // Fully-local (Ollama) status — install/start affordances + Gemma hint
  refreshOllamaRow(provider === 'ollama');
}

let _ollamaRec = null;   // the recommended model from /v1/local/recommend

async function refreshOllamaRow(show) {
  const row = $('#set-ollama-row'); if (!row) return;
  row.hidden = !show;
  $('#set-pullbar').hidden = true;
  if (!show) return;
  const status = $('#set-ollama-status');
  const install = $('#set-ollama-install');
  const start = $('#set-ollama-start');
  const pull = $('#set-ollama-pull');
  let d; try { d = await (await fetch(`${DAEMON_HTTP}/v1/local/recommend`)).json(); } catch { return; }
  const o = d.ollama || {};
  const rec = (d.models || []).find((m) => m.recommended);
  _ollamaRec = rec || null;
  install.hidden = true; start.hidden = true; pull.hidden = true;
  if (o.running) {
    const hasRec = rec && (o.models || []).some((n) => n.split(':latest')[0] === rec.name || n.startsWith(`${rec.name}`));
    if (rec && !hasRec) {
      status.dataset.state = 'wait';
      status.textContent = `Ollama running. This Mac (${d.chip}, ${d.ram_gb}GB) runs ${rec.label} — one download away from fully local.`;
      pull.textContent = `Download ${rec.label}`;
      pull.hidden = false;
    } else {
      status.dataset.state = 'ok';
      status.textContent = 'Ollama running — fully local, nothing leaves this Mac.';
    }
  } else if (o.installed) {
    status.dataset.state = 'wait';
    status.textContent = 'Ollama is installed but not running.';
    start.hidden = false;
  } else {
    status.dataset.state = 'fail';
    status.textContent = `Fully-local needs Ollama (free). This Mac: ${d.chip || ''} · ${d.ram_gb || '?'}GB${rec ? ` — runs ${rec.label}` : ''}.`;
    install.hidden = false;
  }
}
$('#set-ollama-install')?.addEventListener('click', () => window.sunday.openExternal('https://ollama.com/download'));
$('#set-ollama-start')?.addEventListener('click', async () => {
  const status = $('#set-ollama-status');
  if (status) { status.dataset.state = 'wait'; status.textContent = 'Starting Ollama…'; }
  try { await fetch(`${DAEMON_HTTP}/v1/ollama/start`, { method: 'POST' }); } catch {}
  setTimeout(() => refreshOllamaRow(true), 2500);
});
$('#set-ollama-pull')?.addEventListener('click', async () => {
  // One click: pull the recommended Gemma with live progress, then make it
  // the active brain. The whole fully-local path without leaving the GUI.
  const rec = _ollamaRec; if (!rec) return;
  const btn = $('#set-ollama-pull'); const status = $('#set-ollama-status');
  const bar = $('#set-pullbar'); const fill = $('#set-pullbar-fill'); const lab = $('#set-pullbar-label');
  btn.disabled = true; bar.hidden = false; fill.style.width = '0%';
  lab.textContent = `downloading ${rec.label}…`;
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
        } else if (p.status) { lab.textContent = `${rec.label}: ${p.status}`; }
      }
    }
    fill.style.width = '100%'; lab.textContent = `${rec.label}: activating…`;
    const res = await fetch(`${DAEMON_HTTP}/v1/config`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: 'ollama', model_name: rec.name }),
    });
    if (!res.ok) { const dd = await res.json().catch(() => ({})); throw new Error(dd.error || `HTTP ${res.status}`); }
    lab.textContent = `${rec.label}: active — Sunday is fully local.`;
    flashSaved();
    await applyProvider('ollama', rec.name);
  } catch (err) {
    lab.textContent = `✗ ${err.message || err}`;
  } finally {
    btn.disabled = false;
  }
});
function setCodexStatus(text, state) {
  const el = $('#set-codex-status'); if (!el) return;
  el.textContent = text; el.hidden = !text;
  if (state) el.dataset.state = state; else el.removeAttribute('data-state');
}
// Run (or resume) the one-click ChatGPT sign-in. Returns true once connected.
async function connectCodex() {
  setCodexStatus('Starting sign-in…', 'wait');
  const res = await fetch(`${DAEMON_HTTP}/v1/codex/login`, { method: 'POST' });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  if (data.connected) { setCodexStatus(data.email ? `Connected as ${data.email}` : 'Connected', 'ok'); return true; }
  if (!data.auth_url) throw new Error('no sign-in URL returned');
  await window.sunday.openExternal(data.auth_url);
  setCodexStatus('Opening your browser — sign in to ChatGPT, then come back.', 'wait');
  // Poll until the daemon's callback completes (~3 min budget).
  for (let i = 0; i < 120; i++) {
    await new Promise((r) => setTimeout(r, 1500));
    let s; try { s = await (await fetch(`${DAEMON_HTTP}/v1/codex/status`)).json(); } catch { continue; }
    if (s.connected) { setCodexStatus(s.email ? `Connected as ${s.email}` : 'Connected', 'ok'); return true; }
    if (s.error) throw new Error(s.error);
  }
  throw new Error('timed out waiting for sign-in');
}
// ── the one searchable model picker (fed per-provider by applyProvider) ──
let allModels = [];

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
    // The BUTTON reflects the on/off intent (enabled) so it always flips and
    // you can always turn it off. The STATUS text reflects the live state.
    btn.textContent = s.enabled ? 'Turn off' : 'Turn on';
    if (s.error && s.error.startsWith('mic-denied')) {
      statusEl.dataset.state = 'fail';
      statusEl.textContent = 'mic permission denied — enable Sunday in System Settings → Privacy → Microphone';
    } else if (s.error && s.error.startsWith('mic-')) {
      statusEl.dataset.state = 'fail';
      statusEl.textContent = `mic not available (${mic})`;
    } else if (s.error) {
      statusEl.dataset.state = 'fail';
      statusEl.textContent = `couldn't start: ${s.error}`;
    } else if (s.enabled && s.running) {
      statusEl.dataset.state = 'ok';
      statusEl.textContent = 'on — listening';
    } else if (s.enabled) {
      statusEl.dataset.state = '';
      statusEl.textContent = 'on — starting…';
    } else {
      statusEl.dataset.state = '';
      statusEl.textContent = 'off';
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
      // Mic-gate: if it's not granted we'd silently fail; refuse explicitly
      // and bounce the user to the Permissions section.
      const perms = await window.sunday.permissionsStatus();
      if (perms.microphone !== 'granted') {
        const status = $('#set-observer-status');
        status.dataset.state = 'fail';
        status.textContent = 'needs microphone — grant it in Permissions above';
        document.getElementById('sec-permissions')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        // Trigger the prompt while we're here.
        await window.sunday.requestMicrophone();
        return;
      }
      const s = await window.sunday.observerStatus();
      const result = await window.sunday.observerSet(!s.enabled);   // toggle the intent, not live-state
      setObserverUI(result);
      // Always poll after Turn-on: state may take a few ticks to settle
      // (capture window has to load + getUserMedia has to succeed).
      startObserverPolling();
    } finally { btn.disabled = false; }
  });
  refreshObserverUI().then(() => {
    // If it claims to be on at load, keep polling to catch silent death.
    window.sunday.observerStatus().then((s) => { if (s.running) startObserverPolling(); }).catch(() => {});
  });

  // ── Argus analytics toggle (opt-in, "for nerds") ──
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
      if (statusEl) statusEl.textContent = s.enabled ? 'stopping…' : 'starting Argus + reconnecting the brain…';
      const r = await window.sunday.setArgus(!s.enabled);
      if (!r.ok && statusEl) { statusEl.dataset.state = 'fail'; statusEl.textContent = r.error || 'failed'; }
    } finally { btn.disabled = false; await refreshArgusUI(); }
  });
  $('#set-argus-open')?.addEventListener('click', () => window.sunday.openArgus());
  refreshArgusUI();

  // ("Hey Sunday" wake-word toggle removed — superseded by realtime voice mode.)

  // Transcription mode — read-only status. Sunday auto-installs local
  // whisper.cpp on first launch; the OpenAI Whisper fallback runs silently
  // during the window where local isn't ready yet. No buttons.
  let installLines = [];
  window.sunday.onInstallLog((line) => {
    installLines.push(line.line || line);
    if (installLines.length > 40) installLines = installLines.slice(-40);
    refreshTransUI();
  });
  async function refreshTransUI() {
    try {
      const t = await window.sunday.transcriptionStatus();
      const box = $('#set-trans');
      if (!box) return;
      if (t.ready) {
        const m = t.model_name ? ` (${t.model_name})` : '';
        const upgradeNote = t.upgrading
          ? '<div class="set-trans-sub">Upgrading to a better model in the background — current transcription continues uninterrupted.</div>'
          : '';
        box.innerHTML = `<div class="set-trans-on">✓ transcription is fully on-device${m}. Audio never leaves this Mac.</div>${upgradeNote}`;
        return;
      }
      // Setup in progress (or transient fallback) — show a quiet status, no
      // button. If the most recent log line indicates real work, surface it.
      const lastLog = installLines[installLines.length - 1] || '';
      const installing = lastLog && !lastLog.startsWith('✓');
      box.innerHTML = `
        <div class="set-trans-pending">
          <div class="set-trans-head">${installing ? 'Setting up local transcription…' : 'Using OpenAI Whisper as fallback.'}</div>
          ${installing ? `<pre class="set-trans-log">${installLines.slice(-12).join('\n')}</pre>` : ''}
        </div>`;
    } catch {}
  }
  refreshTransUI();
  setInterval(refreshTransUI, 4000);

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
  search.addEventListener('focus', () => renderModelResults(search.value));
  search.addEventListener('input', () => renderModelResults(search.value));
  search.addEventListener('blur', () => setTimeout(() => { $('#set-model-results').hidden = true; }, 160));
  search.addEventListener('keydown', (e) => {
    // Enter accepts a typed id even if it's not in the list (custom models).
    if (e.key === 'Enter') { const v = search.value.trim(); if (v) pickModel(v); }
    if (e.key === 'Escape') { $('#set-model-results').hidden = true; }
  });
  // Save the selected model + (if shown) the provider's API key together.
  $('#set-model-save').addEventListener('click', async () => {
    const provider = selectedProvider;
    const model = $('#set-model').value.trim();
    const keyEl = $('#set-key'); const key = (keyEl && !keyEl.hidden) ? keyEl.value.trim() : '';
    const KEY = _KEY_FOR[provider];
    if (!model && !key) { flashError('pick a model or enter a key'); return; }
    const body = { provider };
    if (model) body.model_name = model;
    if (key && KEY) body.credentials = { [KEY]: key };
    try {
      const res = await fetch(`${DAEMON_HTTP}/v1/config`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.error || `HTTP ${res.status}`); }
      if (keyEl) keyEl.value = '';
      flashSaved();
    } catch (err) { flashError(`save failed: ${err.message}`); }
  });
  // ── run mode (cloud / local) + migrate ──
  document.querySelectorAll('#set-runmode .brain-card').forEach((b) => b.addEventListener('click', async () => {
    const mode = b.dataset.mode;
    if (mode === 'local' && !confirm('Run Sunday on this Mac? If you want your cloud chat + memory to come along, use "Bring my history over" first.')) return;
    b.style.opacity = '0.6';
    const r = await window.sunday.setRunMode(mode);
    b.style.opacity = '';
    // On success the main process reloads the window, which re-inits everything
    // cleanly — running refreshRunMode() here would race that reload and paint a
    // half-switched state. Only refresh when the switch FAILED (no reload).
    if (r && r.error) { alert(`Couldn't switch: ${r.error}`); await refreshRunMode(); }
  }));
  // Styled provider dropdown — button toggles the popover list.
  const provBtn = $('#set-prov-btn'), provMenu = $('#set-prov-menu');
  const closeProvMenu = () => { if (provMenu) provMenu.hidden = true; };
  provBtn?.addEventListener('click', (e) => { e.stopPropagation(); if (provMenu) provMenu.hidden = !provMenu.hidden; });
  document.addEventListener('click', (e) => { if (provMenu && !provMenu.hidden && !$('#set-prov-dd').contains(e.target)) closeProvMenu(); });
  const switchProvider = async (provider) => {
    if (provider === 'codex') {
      try {
        const cfg = await window.sunday.getConfig(); if (cfg.daemonHttp) DAEMON_HTTP = cfg.daemonHttp;
        const m = await window.sunday.runMode();
        await applyProvider('codex');
        if (!m.local) { setCodexStatus('ChatGPT runs on This Mac — switch the brain above first.', 'fail'); return; }
        const s = await (await fetch(`${DAEMON_HTTP}/v1/codex/status`)).json().catch(() => ({}));
        if (s.connected) await fetch(`${DAEMON_HTTP}/v1/config`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider: 'codex' }) });
      } catch (err) { flashError(err.message); }
      return;
    }
    try {
      const res = await fetch(`${DAEMON_HTTP}/v1/config`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider }) });
      if (!res.ok) { const d = await res.json().catch(() => ({})); flashError(d.error || `HTTP ${res.status}`); }
    } catch (err) { flashError(err.message); }
    await applyProvider(provider);
  };
  provMenu?.querySelectorAll('.model-row').forEach((row) => row.addEventListener('click', () => {
    if (row.classList.contains('dd-disabled')) return;
    closeProvMenu();
    switchProvider(row.dataset.provider);
  }));
  // Connect ChatGPT — shown only for ChatGPT when not signed in.
  $('#set-connect')?.addEventListener('click', async () => {
    try {
      const cfg = await window.sunday.getConfig(); if (cfg.daemonHttp) DAEMON_HTTP = cfg.daemonHttp;
      const m = await window.sunday.runMode();
      if (!m.local) throw new Error('ChatGPT runs on This Mac — switch the brain above first.');
      await connectCodex();
      await fetch(`${DAEMON_HTTP}/v1/config`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider: 'codex' }) });
      await applyProvider('codex');
    } catch (err) { setCodexStatus(err.message || 'Sign-in failed', 'fail'); }
  });
  $('#set-migrate')?.addEventListener('click', async () => {
    const note = $('#set-migrate-note'); const btn = $('#set-migrate');
    btn.disabled = true; const prev = note ? note.textContent : '';
    if (note) note.textContent = 'Copying your history + memory down…';
    try {
      const r = await window.sunday.migrateToLocal();
      if (note) note.textContent = r.ok
        ? `Copied ${(r.files || []).length} databases — now running locally on this Mac.`
        : `Failed: ${r.error}`;
      await refreshRunMode();
    } catch (e) { if (note) note.textContent = `Failed: ${e.message}`; }
    finally { btn.disabled = false; }
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
  // Permissions panel — single source of truth, polled from macOS. Each row
  // shows real status; the Allow button hides when granted.
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
      statusEl.dataset.state = 'ok';
      statusEl.textContent = 'allowed';
      btn.hidden = true;
    } else if (status === 'denied') {
      statusEl.dataset.state = 'fail';
      statusEl.textContent = 'denied — open System Settings to enable';
      btn.hidden = false;
    } else {
      statusEl.dataset.state = '';
      statusEl.textContent = 'not granted';
      btn.hidden = false;
    }
  }
  async function refreshPerms() {
    try {
      const m = await window.sunday.permissionsStatus();
      for (const p of PERMS) applyPerm(p, m);
    } catch {}
  }
  for (const p of PERMS) {
    const row = document.getElementById(`perm-${p.id}`);
    row?.querySelector('.perm-grant')?.addEventListener('click', async () => {
      try { await p.request(); } catch {}
      // The system prompt is modal — poll a few times so the UI catches up
      // quickly after the user clicks Allow.
      for (let i = 0; i < 8; i++) { await new Promise((r) => setTimeout(r, 500)); await refreshPerms(); }
    });
  }
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

  refreshPerms();
  setInterval(refreshPerms, 2000);

  // ── Updates panel — mirrors the tray's update state machine. ────────────
  const updVerLine = document.getElementById('upd-version-line');
  const updStateText = document.getElementById('upd-state-text');
  const updBar = document.getElementById('upd-bar');
  const updBarFill = document.getElementById('upd-bar-fill');
  const updCheckBtn = document.getElementById('upd-check-btn');
  const updRestartBtn = document.getElementById('upd-restart-btn');
  function applyUpdState(s) {
    if (!s) return;
    const phase = s.phase || 'idle';
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
  // Initial state + subscribe to live updates.
  window.sunday.updateState().then(applyUpdState).catch(() => {});
  window.sunday.onUpdateState(applyUpdState);
  updCheckBtn?.addEventListener('click', () => window.sunday.updateCheck().catch(() => {}));
  updRestartBtn?.addEventListener('click', () => window.sunday.updateRestart().catch(() => {}));
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
