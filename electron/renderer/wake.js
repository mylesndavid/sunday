// "Hey Sunday" wake loop. Runs in a hidden Sunday window so the mic grant is
// attributed to Sunday (not a detached child). Records short ~2.5s windows and
// ships each to main, which transcribes locally (Whisper) and scans for the
// wake phrase. Deliberately separate from the 30s ambient-observer loop: wake
// needs fast turnaround, the observer needs context.

const WINDOW_MS = 2500;   // short windows so "Hey Sunday" is heard quickly

let stream = null;
let listening = false;

async function start() {
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    window.wake.reportState({ active: false, error: `getusermedia-failed: ${err && err.name}` });
    return;
  }
  listening = true;
  window.wake.reportState({ active: true, error: null });
  cycle();
}

// One record→stop→ship cycle, then immediately start the next — back-to-back
// short windows. Discrete MediaRecorder lifecycles give a clean standalone
// webm per window that Whisper accepts directly.
function cycle() {
  if (!listening || !stream) return;
  let mr;
  try {
    mr = new MediaRecorder(stream, { mimeType: 'audio/webm' });
  } catch {
    mr = new MediaRecorder(stream);
  }
  const parts = [];
  mr.ondataavailable = (e) => { if (e.data && e.data.size) parts.push(e.data); };
  mr.onstop = async () => {
    if (parts.length) {
      try {
        const blob = new Blob(parts, { type: mr.mimeType || 'audio/webm' });
        const buf = await blob.arrayBuffer();
        await window.wake.sendChunk(new Uint8Array(buf));
      } catch { /* drop this window; keep going */ }
    }
    if (listening) cycle();   // next window
  };
  mr.start();
  setTimeout(() => { try { mr.state !== 'inactive' && mr.stop(); } catch {} }, WINDOW_MS);
}

start();
