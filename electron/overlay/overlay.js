// Sunday HUD — polls the daemon for live state, renders the notch bar
// (agent count + connection) and, when expanded, a glass card of what's
// running. Click the bar to expand/collapse; the main process resizes the
// window to match.

const $ = (s) => document.querySelector(s);
const notch = $('#notch');
let DAEMON_HTTP = 'http://127.0.0.1:8765';
let mode = 'compact';

async function boot() {
  if (window.sunday?.getConfig) {
    try { const c = await window.sunday.getConfig(); if (c.daemonHttp) DAEMON_HTTP = c.daemonHttp; } catch {}
  }
  if (window.sunday?.notchMetrics) {
    try { const m = await window.sunday.notchMetrics(); if (m?.notchHeight) document.documentElement.style.setProperty('--notch-h', m.notchHeight + 'px'); } catch {}
  }
  tick();
  setInterval(tick, 2000);
}

$('#bar').addEventListener('click', () => {
  mode = mode === 'compact' ? 'expanded' : 'compact';
  notch.dataset.mode = mode;
  window.sunday?.notchResize?.(mode);
  if (mode === 'expanded') tick();
});

async function tick() {
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/status`);
    if (!res.ok) throw new Error();
    render(await res.json(), 'online');
  } catch {
    render(null, 'offline');
  }
}

function render(d, conn) {
  $('#conn').dataset.state = conn;
  $('#hud-conn').textContent = conn === 'online'
    ? (d?.model ? d.model.split('/').slice(-1)[0] : 'online')
    : 'offline';

  const agents = (d && d.agents) || [];
  const badge = $('#agents-badge');
  if (agents.length) { badge.hidden = false; $('#agents-count').textContent = String(agents.length); }
  else { badge.hidden = true; }

  const empty = $('#hud-empty');
  const list = $('#hud-agent-list');
  if (agents.length) {
    empty.hidden = true;
    list.innerHTML = agents.map((a) => `<li><span class="dot"></span><span>${esc(a.task || 'working…')}</span></li>`).join('');
  } else {
    empty.hidden = false;
    list.innerHTML = '';
  }
  if (d) {
    $('#hud-stats').innerHTML = `
      <div class="row"><span class="k">agents</span><span class="v">${agents.length}</span></div>
      <div class="row"><span class="k">messages</span><span class="v">${d.messages ?? '—'}</span></div>
      <div class="row"><span class="k">devices</span><span class="v">${(d.devices || []).length}</span></div>
      <div class="row"><span class="k">tools</span><span class="v">${(d.tools || []).length}</span></div>`;
  }
}

function esc(s) { return String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

window.sunday?.onOverlayState?.((state) => {
  if (state?.connection) $('#conn').dataset.state = state.connection;
});

boot();
