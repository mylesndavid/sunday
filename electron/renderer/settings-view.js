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
}

const CONN_ICON = {
  gmail: '<path d="M4 6h16a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1z"/><path d="m3 7 9 6 9-6"/>',
  calendar: '<rect x="3" y="4" width="18" height="17" rx="2"/><path d="M3 9h18M8 2v4M16 2v4"/>',
  slack: '<path d="M9 12a2 2 0 1 1-2-2h2zM12 9a2 2 0 1 1 2-2v2zM15 12a2 2 0 1 1 2 2h-2zM12 15a2 2 0 1 1-2 2v-2z"/>',
};

async function loadConnections() {
  const ul = document.querySelector('#conn-list');
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/integrations`);
    const d = await res.json();
    document.querySelector('#conn-unconfigured').hidden = !!d.configured;
    ul.innerHTML = (d.providers || []).map((p) => `
      <li class="conn-row" data-id="${p.id}">
        <span class="conn-ico"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${CONN_ICON[p.id] || ''}</svg></span>
        <span class="conn-name">${esc(p.label)}</span>
        ${p.connected
          ? '<span class="conn-on">connected</span>'
          : `<button class="btn conn-connect" data-id="${p.id}" ${d.configured ? '' : 'disabled'}>Connect</button>`}
      </li>`).join('');
    ul.querySelectorAll('.conn-connect').forEach((b) => b.addEventListener('click', () => connectProvider(b.dataset.id, b)));
  } catch {
    ul.innerHTML = '<li class="conn-loading">couldn\'t reach the daemon</li>';
  }
}

async function connectProvider(provider, btn) {
  btn.disabled = true; const orig = btn.textContent; btn.textContent = 'opening…';
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/integrations/connect`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider }) });
    const d = await res.json();
    if (d.connect_url) {
      await window.sunday.openExternal(d.connect_url);
      btn.textContent = 'approve in browser…';
      // poll for the connection landing
      pollConnected(provider, 0);
    } else {
      btn.textContent = orig; btn.disabled = false;
      flashError(d.error ? friendlyConn(d.error) : 'couldn\'t start the connect flow');
    }
  } catch (err) { btn.textContent = orig; btn.disabled = false; flashError(err.message); }
}

function friendlyConn(e) {
  if (/integration does not exist/i.test(e)) return 'That service isn\'t set up on your Nango server yet (see setup notes).';
  return e;
}

function pollConnected(provider, n) {
  if (n > 40) { loadConnections(); return; }
  setTimeout(async () => {
    try {
      const d = await (await fetch(`${DAEMON_HTTP}/v1/integrations`)).json();
      if ((d.providers || []).find((p) => p.id === provider && p.connected)) { loadConnections(); flashSaved(); }
      else pollConnected(provider, n + 1);
    } catch { pollConnected(provider, n + 1); }
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
