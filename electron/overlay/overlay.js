// Sunday HUD — ambient, notch-integrated. Idle = invisible + click-through.
// When sub-agents are running it grows out of the notch with the count;
// click to expand a glass card. Auto-collapses back to idle a moment after
// the work finishes.

const $ = (s) => document.querySelector(s);
const notch = $('#notch');
let DAEMON_HTTP = 'http://127.0.0.1:8765';
let mode = 'idle';            // idle | active | expanded
let pinnedExpanded = false;   // user clicked to open the card
let idleTimer = null;

async function boot() {
  if (window.sunday?.getConfig) {
    try { const c = await window.sunday.getConfig(); if (c.daemonHttp) DAEMON_HTTP = c.daemonHttp; } catch {}
  }
  if (window.sunday?.notchMetrics) {
    try { const m = await window.sunday.notchMetrics(); if (m?.notchHeight) document.documentElement.style.setProperty('--notch-h', m.notchHeight + 'px'); } catch {}
  }
  setMode('idle');
  tick();
  setInterval(tick, 1500);
}

function setMode(next) {
  if (next === mode) return;
  mode = next;
  notch.dataset.mode = next;
  window.sunday?.notchMode?.(next);     // main resizes + toggles click-through
}

// Click anywhere in the overlay window toggles the card. In idle the bar is
// invisible but the window (notch + a small lip below) still catches the
// click, so "click the notch" opens the HUD. Clicks inside the open card are
// ignored so interacting with it doesn't collapse it.
document.addEventListener('click', (e) => {
  if (mode === 'expanded') {
    if (e.target.closest('#hud')) return;
    pinnedExpanded = false; setMode('idle');
  } else {
    pinnedExpanded = true; setMode('expanded');
  }
});

let active = false;

async function tick() {
  let d = null, conn = 'offline';
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/status`);
    if (res.ok) { d = await res.json(); conn = 'online'; }
  } catch {}
  render(d, conn);

  const agents = (d && d.agents) || [];
  active = agents.length > 0;

  // The live agent count now lives in the MENU BAR (the tray status item), so
  // the notch overlay no longer auto-floats a bar over the screen. It stays
  // invisible (idle) and only opens the glass card when the user clicks it.
  if (pinnedExpanded) { setMode('expanded'); return; }
  if (mode !== 'idle') setMode('idle');
}

function render(d, conn) {
  $('#hud-conn').textContent = conn === 'online' ? (d?.model ? d.model.split('/').slice(-1)[0] : 'online') : 'offline';
  const agents = (d && d.agents) || [];
  $('#agents-count').textContent = String(agents.length);

  const empty = $('#hud-empty'), list = $('#hud-agent-list');
  if (agents.length) {
    empty.hidden = true;
    list.innerHTML = agents.map((a) => `<li><span class="dot"></span><span>${esc(a.task || 'working…')}</span></li>`).join('');
  } else { empty.hidden = false; list.innerHTML = ''; }

  if (d) $('#hud-stats').innerHTML = `
    <div class="row"><span class="k">agents</span><span class="v">${agents.length}</span></div>
    <div class="row"><span class="k">messages</span><span class="v">${d.messages ?? '—'}</span></div>
    <div class="row"><span class="k">devices</span><span class="v">${(d.devices || []).length}</span></div>
    <div class="row"><span class="k">tools</span><span class="v">${(d.tools || []).length}</span></div>`;
}

function esc(s) { return String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
boot();
