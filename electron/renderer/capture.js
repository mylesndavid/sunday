// Ambient observer capture loop. Runs in a hidden Sunday window so the mic
// grant is attributed to Sunday (not a python child). Records ~30s chunks via
// MediaRecorder and hands each to main, which transcribes + ticks.

const CHUNK_MS = 30000;   // 30s windows, matching the tick cadence

let stream = null;
let capturing = false;

async function start() {
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    window.capture.reportState({ active: false, error: `getusermedia-failed: ${err && err.name}` });
    return;
  }
  capturing = true;
  window.capture.reportState({ active: true, error: null });
  cycle();
}

// One record→stop→ship cycle, then immediately start the next. Using
// discrete MediaRecorder lifecycles (rather than a timeslice) gives us a
// clean, standalone webm per chunk that Whisper accepts directly.
function cycle() {
  if (!capturing || !stream) return;
  let mr;
  try {
    mr = new MediaRecorder(stream, { mimeType: 'audio/webm' });
  } catch {
    // Some hosts don't accept the explicit mimeType — let the UA choose.
    mr = new MediaRecorder(stream);
  }
  const parts = [];
  mr.ondataavailable = (e) => { if (e.data && e.data.size) parts.push(e.data); };
  mr.onstop = async () => {
    if (parts.length) {
      try {
        const blob = new Blob(parts, { type: mr.mimeType || 'audio/webm' });
        const buf = await blob.arrayBuffer();
        await window.capture.sendChunk(new Uint8Array(buf));
      } catch { /* drop this chunk; keep going */ }
    }
    if (capturing) cycle();   // next window
  };
  mr.start();
  setTimeout(() => { try { mr.state !== 'inactive' && mr.stop(); } catch {} }, CHUNK_MS);
}

start();
