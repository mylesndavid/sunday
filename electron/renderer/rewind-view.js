// Rewind — scrub back through a private, on-device timeline of your screen.
// Frames are captured + OCR'd on the Mac (the satellite); this view pulls
// the list from the daemon and loads each frame's image on demand through
// an IPC bridge (the files are local). Drag the slider, step with the
// arrows, or hit play to sweep through time.

let cfg = null, els = null;
let frames = [];      // [{ id, ts, image_path, ocr_text }]
let idx = 0;
let playing = false, playTimer = null;
let loaded = false;
let imgCache = new Map();
let showSeq = 0;      // monotonic token; guards async image loads against fast scrubbing

export function init(config, refs) { cfg = config; els = refs; wire(); }
export function setDaemon(http) { cfg.daemonHttp = http; }
export function isLoaded() { return loaded; }

export async function load() {
  try {
    const res = await fetch(`${cfg.daemonHttp}/v1/rewind/recent?limit=1000`);
    const data = await res.json();
    frames = (data.frames || []).slice().sort((a, b) => a.ts - b.ts);
    loaded = true;
    render();
  } catch (err) {
    console.warn('rewind load failed', err);
    frames = []; render();
  }
}

function render() {
  const has = frames.length > 0;
  els.empty.hidden = has;
  els.controls.hidden = !has;
  els.text.hidden = !has;
  els.img.hidden = !has;
  if (!has) { showEmptyState(); return; }
  els.slider.max = String(frames.length - 1);
  idx = frames.length - 1;        // start at the most recent
  els.slider.value = String(idx);
  showFrame(idx);
}

async function showEmptyState() {
  // reflect actual capture state so the CTA reads right
  try {
    const res = await fetch(`${cfg.daemonHttp}/v1/rewind/state`);
    const s = await res.json();
    if (s.error) {
      els.emptyTitle.textContent = 'Connect your Mac';
      els.emptySub.textContent = 'Open Sunday on the Mac you want a screen timeline for, then turn screen history on here.';
      els.enable.hidden = true;
    } else if (s.running) {
      els.emptyTitle.textContent = 'Capturing — nothing yet';
      els.emptySub.textContent = 'Screen history is on. The first frames will appear here within a few minutes.';
      els.enable.hidden = true;
    } else {
      els.emptyTitle.textContent = 'Screen history is off';
      els.emptySub.textContent = 'Turn it on and Sunday quietly keeps a private, on-device timeline of your screen you can scroll back through. Nothing leaves your Mac.';
      els.enable.hidden = false;
    }
  } catch { els.enable.hidden = false; }
}

async function showFrame(i) {
  const f = frames[i];
  if (!f) return;
  // Sequence guard: fast scrubbing fires many showFrame() calls; an earlier
  // frame's image (loaded over IPC) can resolve AFTER a later one and clobber
  // the picture the user is now looking at. Only the most recent call may
  // write the <img> src. Text/time update synchronously, so they're always current.
  const seq = ++showSeq;
  els.time.textContent = fmt(f.ts);
  els.ocr.textContent = (f.ocr_text || '').trim() || 'No text was on screen.';
  const url = await imageFor(f.image_path);
  if (seq !== showSeq) return;   // a newer frame was selected while we awaited — drop this
  if (url) { els.img.src = url; els.img.hidden = false; }
}

async function imageFor(path) {
  if (!path) return null;
  if (imgCache.has(path)) return imgCache.get(path);
  const url = await window.sunday?.rewindImage(path);
  if (url) imgCache.set(path, url);
  return url;
}

function go(i) {
  idx = Math.max(0, Math.min(frames.length - 1, i));
  els.slider.value = String(idx);
  showFrame(idx);
}

function play() {
  playing = !playing;
  els.play.dataset.active = playing ? 'true' : 'false';
  els.play.innerHTML = playing
    ? `<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><rect x="6" y="5" width="4" height="14"/><rect x="14" y="5" width="4" height="14"/></svg>`
    : `<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>`;
  if (playing) {
    if (idx >= frames.length - 1) go(0);
    playTimer = setInterval(() => { if (idx >= frames.length - 1) { stopPlay(); return; } go(idx + 1); }, 700);
  } else stopPlay();
}
function stopPlay() {
  playing = false; els.play.dataset.active = 'false';
  els.play.innerHTML = `<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>`;
  if (playTimer) { clearInterval(playTimer); playTimer = null; }
}

function wire() {
  els.slider.addEventListener('input', () => { stopPlay(); go(parseInt(els.slider.value, 10)); });
  els.prev.addEventListener('click', () => { stopPlay(); go(idx - 1); });
  els.next.addEventListener('click', () => { stopPlay(); go(idx + 1); });
  els.play.addEventListener('click', play);
  els.enable.addEventListener('click', async () => {
    els.enable.disabled = true; els.enable.textContent = 'turning on…';
    try {
      await fetch(`${cfg.daemonHttp}/v1/rewind/toggle`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ on: true, interval_seconds: 60 }) });
      els.emptyTitle.textContent = 'Capturing — nothing yet';
      els.emptySub.textContent = 'Screen history is on. The first frames will appear here within a couple of minutes. (Allow Screen Recording for Sunday in Settings if prompted.)';
      els.enable.hidden = true;
    } catch { els.enable.textContent = 'Turn on screen history'; els.enable.disabled = false; }
  });
  els.stop.addEventListener('click', async () => {
    stopPlay();
    await fetch(`${cfg.daemonHttp}/v1/rewind/toggle`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ on: false }) }).catch(() => {});
    await load();
  });
}

function fmt(ts) {
  const d = new Date(ts * 1000);
  const day = d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  return `${day} · ${time}`;
}
