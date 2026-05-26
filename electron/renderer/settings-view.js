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
    syncModelPreset();
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
}

function updateChars() { $('#set-prompt-chars').textContent = `${$('#set-prompt').value.length} chars`; }
function syncModelPreset() {
  const cur = $('#set-model').value.trim();
  const sel = $('#set-model-preset');
  if (sel) sel.value = [...sel.options].some((o) => o.value === cur) ? cur : '';
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
  $('#set-model-preset').addEventListener('change', (e) => { if (e.target.value) { $('#set-model').value = e.target.value; } });
  $('#set-model').addEventListener('input', syncModelPreset);
  $('#set-model-save').addEventListener('click', async () => {
    const model = $('#set-model').value.trim();
    if (!model) { flashError('model name required'); return; }
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
