// Memory map — an interactive force-directed graph of who/what Sunday
// knows about you. Pure canvas, no libraries. Drag the background to pan,
// drag a node to move it, scroll to zoom, click a node to see the plain
// facts behind it. No graph jargon surfaces — categories read as People,
// Pets, Places, etc.

const KIND = {
  person:       { label: 'People',  varName: '--g-person' },
  pet:          { label: 'Pets',    varName: '--g-pet' },
  place:        { label: 'Places',  varName: '--g-place' },
  organization: { label: 'Work',    varName: '--g-org' },
  project:      { label: 'Projects',varName: '--g-project' },
  thing:        { label: 'Things',  varName: '--g-thing' },
  event:        { label: 'Events',  varName: '--g-event' },
  topic:        { label: 'Topics',  varName: '--g-topic' },
};

let cfg = null;          // { daemonHttp }
let els = null;          // dom refs
let canvas, ctx, dpr = 1;
let nodes = [], links = [];
let selected = null, hover = null;
let view = { x: 0, y: 0, scale: 1 };
let drag = null;         // { node } | { pan:true, sx, sy, ox, oy }
let raf = null, alpha = 0;
let colorCache = {};
let loaded = false;

function cssColor(varName) {
  if (colorCache[varName]) return colorCache[varName];
  const c = getComputedStyle(document.documentElement).getPropertyValue(varName).trim() || '#999';
  colorCache[varName] = c;
  return c;
}
function kindColor(kind) { return cssColor((KIND[kind] || KIND.thing).varName); }

export function init(config, refs) {
  cfg = config; els = refs;
  canvas = refs.canvas; ctx = canvas.getContext('2d');
  bindEvents();
  buildLegend();
}

export function setDaemon(http) { cfg.daemonHttp = http; }

function buildLegend() {
  els.legend.innerHTML = Object.values(KIND).map((k) =>
    `<span class="lg"><span class="sw" style="background:${cssColor(k.varName)}"></span>${k.label}</span>`
  ).join('');
}

export async function load(force) {
  els.refresh.classList.add('spin');
  try {
    const url = `${cfg.daemonHttp}/v1/memory/graph${force ? '/rebuild' : ''}`;
    const res = await fetch(url, { method: force ? 'POST' : 'GET' });
    const data = await res.json();
    ingest(data);
  } catch (err) {
    console.warn('memory graph load failed', err);
  } finally {
    els.refresh.classList.remove('spin');
  }
}

function ingest(data) {
  const incoming = data.nodes || [];
  els.empty.hidden = incoming.length > 0;
  // preserve positions of nodes we already have
  const prev = new Map(nodes.map((n) => [n.id, n]));
  const rect = canvas.getBoundingClientRect();
  const cx = rect.width / 2, cy = rect.height / 2;
  nodes = incoming.map((n) => {
    const p = prev.get(n.id);
    return {
      ...n,
      x: p ? p.x : cx + (Math.random() - 0.5) * 240,
      y: p ? p.y : cy + (Math.random() - 0.5) * 240,
      vx: 0, vy: 0,
      r: 13 + Math.min(n.degree || 0, 8) * 2.6,
    };
  });
  const byId = new Map(nodes.map((n) => [n.id, n]));
  links = (data.links || []).filter((l) => byId.has(l.source) && byId.has(l.target))
    .map((l) => ({ ...l, a: byId.get(l.source), b: byId.get(l.target) }));
  loaded = true;
  alpha = 1;
  resize();
  kick();
}

// ── physics ──
function step() {
  const rect = canvas.getBoundingClientRect();
  const cx = rect.width / 2, cy = rect.height / 2;
  const REPULSE = 5200, SPRING = 0.012, REST = 118, GRAV = 0.015, DAMP = 0.86;
  for (let i = 0; i < nodes.length; i++) {
    const a = nodes[i];
    if (drag && drag.node === a) continue;
    for (let j = i + 1; j < nodes.length; j++) {
      const b = nodes[j];
      let dx = a.x - b.x, dy = a.y - b.y;
      let d2 = dx * dx + dy * dy || 0.01;
      const f = REPULSE / d2;
      const d = Math.sqrt(d2);
      const fx = (dx / d) * f, fy = (dy / d) * f;
      a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
    }
    a.vx += (cx - a.x) * GRAV; a.vy += (cy - a.y) * GRAV;
  }
  for (const l of links) {
    let dx = l.b.x - l.a.x, dy = l.b.y - l.a.y;
    const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
    const f = (d - REST) * SPRING;
    const fx = (dx / d) * f, fy = (dy / d) * f;
    if (!(drag && drag.node === l.a)) { l.a.vx += fx; l.a.vy += fy; }
    if (!(drag && drag.node === l.b)) { l.b.vx -= fx; l.b.vy -= fy; }
  }
  for (const n of nodes) {
    if (drag && drag.node === n) continue;
    n.vx *= DAMP; n.vy *= DAMP;
    n.x += n.vx * alpha; n.y += n.vy * alpha;
  }
  alpha *= 0.992;
  if (alpha < 0.02) alpha = 0.02;
}

function draw() {
  const rect = canvas.getBoundingClientRect();
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, rect.width, rect.height);
  ctx.save();
  ctx.translate(view.x, view.y);
  ctx.scale(view.scale, view.scale);

  // links
  ctx.lineWidth = 1.4;
  for (const l of links) {
    const active = selected && (l.a.id === selected.id || l.b.id === selected.id);
    ctx.strokeStyle = active ? cssColor('--accent') : 'rgba(120,110,90,0.28)';
    ctx.beginPath(); ctx.moveTo(l.a.x, l.a.y); ctx.lineTo(l.b.x, l.b.y); ctx.stroke();
    if (active && l.label) {
      const mx = (l.a.x + l.b.x) / 2, my = (l.a.y + l.b.y) / 2;
      ctx.font = '11px ' + cssColor('--font-mono');
      ctx.fillStyle = cssColor('--text-3');
      ctx.textAlign = 'center';
      ctx.fillText(l.label, mx, my - 4);
    }
  }
  // nodes
  for (const n of nodes) {
    const col = kindColor(n.kind);
    const isSel = selected && n.id === selected.id;
    const isHov = hover && n.id === hover.id;
    const dim = selected && !isSel && !links.some((l) => (l.a.id === selected.id && l.b.id === n.id) || (l.b.id === selected.id && l.a.id === n.id));
    ctx.globalAlpha = dim ? 0.35 : 1;
    if (isSel || isHov) { ctx.beginPath(); ctx.arc(n.x, n.y, n.r + 5, 0, Math.PI * 2); ctx.fillStyle = col + '33'; ctx.fill(); }
    ctx.beginPath(); ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
    ctx.fillStyle = col; ctx.fill();
    ctx.lineWidth = 2.5; ctx.strokeStyle = cssColor('--surface'); ctx.stroke();
    // label
    ctx.globalAlpha = dim ? 0.4 : 1;
    ctx.font = (isSel ? '600 13px ' : '500 12px ') + cssColor('--font-sans');
    ctx.fillStyle = cssColor('--text');
    ctx.textAlign = 'center';
    ctx.fillText(n.name, n.x, n.y + n.r + 14);
    ctx.globalAlpha = 1;
  }
  ctx.restore();
}

function tick() {
  step(); draw();
  raf = requestAnimationFrame(tick);
}
function kick() { if (!raf) tick(); }

export function resize() {
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  dpr = window.devicePixelRatio || 1;
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  if (!raf) draw();
}

// ── interaction ──
function toWorld(e) {
  const rect = canvas.getBoundingClientRect();
  return { x: (e.clientX - rect.left - view.x) / view.scale, y: (e.clientY - rect.top - view.y) / view.scale };
}
function nodeAt(p) {
  for (let i = nodes.length - 1; i >= 0; i--) {
    const n = nodes[i];
    if ((p.x - n.x) ** 2 + (p.y - n.y) ** 2 <= (n.r + 4) ** 2) return n;
  }
  return null;
}
function bindEvents() {
  let downPos = null, moved = false;
  canvas.addEventListener('mousedown', (e) => {
    const p = toWorld(e); const n = nodeAt(p); downPos = { x: e.clientX, y: e.clientY }; moved = false;
    if (n) drag = { node: n }; else drag = { pan: true, sx: e.clientX, sy: e.clientY, ox: view.x, oy: view.y };
  });
  window.addEventListener('mousemove', (e) => {
    if (downPos && (Math.abs(e.clientX - downPos.x) > 3 || Math.abs(e.clientY - downPos.y) > 3)) moved = true;
    if (drag?.node) { const p = toWorld(e); drag.node.x = p.x; drag.node.y = p.y; drag.node.vx = 0; drag.node.vy = 0; alpha = Math.max(alpha, 0.4); }
    else if (drag?.pan) { view.x = drag.ox + (e.clientX - drag.sx); view.y = drag.oy + (e.clientY - drag.sy); if (!raf) draw(); }
    else { const old = hover; hover = nodeAt(toWorld(e)); if (hover !== old && !raf) draw(); canvas.style.cursor = hover ? 'pointer' : 'grab'; }
  });
  window.addEventListener('mouseup', (e) => {
    if (drag?.node && !moved) selectNode(drag.node);
    else if (drag?.pan && !moved) selectNode(null);
    drag = null; downPos = null;
  });
  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    const factor = e.deltaY < 0 ? 1.1 : 0.9;
    const ns = Math.min(2.4, Math.max(0.4, view.scale * factor));
    view.x = mx - (mx - view.x) * (ns / view.scale);
    view.y = my - (my - view.y) * (ns / view.scale);
    view.scale = ns;
    if (!raf) draw();
  }, { passive: false });
}

function selectNode(n) {
  selected = n;
  // Don't perturb the simulation on click — just redraw the highlight so
  // nodes stay put. (The detail panel is an overlay, so no resize either.)
  if (!raf) draw();
  if (!n) { els.detail.hidden = true; return; }
  els.detail.hidden = false;
  const k = KIND[n.kind] || KIND.thing;
  els.detailKind.textContent = k.label.replace(/s$/, '');
  els.detailKind.style.background = kindColor(n.kind) + '22';
  els.detailKind.style.color = kindColor(n.kind);
  els.detailName.textContent = n.name;
  els.detailFacts.innerHTML = (n.facts && n.facts.length)
    ? n.facts.map((f) => `<li>${escapeHtml(f)}</li>`).join('')
    : '<li style="background:none;color:var(--text-3);padding-left:0">No specific notes yet.</li>';
  const conns = links.filter((l) => l.a.id === n.id || l.b.id === n.id).map((l) => {
    const other = l.a.id === n.id ? l.b : l.a;
    return { other, rel: l.label };
  });
  els.detailConns.innerHTML = conns.length
    ? conns.map((c) => `<li data-id="${c.other.id}"><span class="sw" style="background:${kindColor(c.other.kind)}"></span><span>${escapeHtml(c.other.name)}</span><span class="rel">${escapeHtml(c.rel || '')}</span></li>`).join('')
    : '<li style="cursor:default;color:var(--text-3)">Nothing connected yet.</li>';
  els.detailConns.querySelectorAll('li[data-id]').forEach((li) => {
    li.onclick = () => { const t = nodes.find((x) => x.id == li.dataset.id); if (t) selectNode(t); };
  });
}

function escapeHtml(s) { return String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

export function closeDetail() { selectNode(null); }
export function isLoaded() { return loaded; }
