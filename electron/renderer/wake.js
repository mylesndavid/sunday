// "Hey Sunday" wake loop. Runs in a hidden Sunday window so the mic grant is
// attributed to Sunday (not a detached child). Records short ~2.5s windows and
// ships each to main, which transcribes locally (Whisper) and scans for the
// wake phrase.
//
// VAD gate: a cheap Web Audio energy meter runs continuously, but a window is
// only shipped to Whisper if it actually contained speech-level audio. When
// the room is quiet (almost always) we spend nothing but the meter — no
// ffmpeg, no Whisper. This is the difference between "runs hot 24/7" and
// "idles for free until you talk."

const WINDOW_MS = 2500;     // short windows so "Hey Sunday" is heard quickly
const RMS_SPEECH = 0.018;   // normalized-RMS threshold; below this = silence, skip Whisper
const SAMPLE_MS = 100;      // how often the meter samples energy

let stream = null;
let listening = false;
let analyser = null, meterData = null, meterTimer = null, windowPeak = 0;

async function start() {
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    window.wake.reportState({ active: false, error: `getusermedia-failed: ${err && err.name}` });
    return;
  }
  // Energy meter — the VAD gate. Negligible cost; lets us skip Whisper on silence.
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const src = ctx.createMediaStreamSource(stream);
    analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    src.connect(analyser);
    meterData = new Uint8Array(analyser.fftSize);
    meterTimer = setInterval(sampleEnergy, SAMPLE_MS);
  } catch { /* no meter → fall back to shipping every window */ }
  listening = true;
  window.wake.reportState({ active: true, error: null });
  cycle();
}

// Track the loudest moment in the current window (normalized RMS, 0..~1).
function sampleEnergy() {
  if (!analyser) return;
  analyser.getByteTimeDomainData(meterData);
  let sum = 0;
  for (let i = 0; i < meterData.length; i++) { const v = (meterData[i] - 128) / 128; sum += v * v; }
  const rms = Math.sqrt(sum / meterData.length);
  if (rms > windowPeak) windowPeak = rms;
}

function cycle() {
  if (!listening || !stream) return;
  windowPeak = 0;   // reset the meter for this window
  let mr;
  try {
    mr = new MediaRecorder(stream, { mimeType: 'audio/webm' });
  } catch {
    mr = new MediaRecorder(stream);
  }
  const parts = [];
  mr.ondataavailable = (e) => { if (e.data && e.data.size) parts.push(e.data); };
  mr.onstop = async () => {
    // VAD gate: only spend Whisper if this window had speech-level audio.
    const hadSpeech = !analyser || windowPeak >= RMS_SPEECH;
    if (hadSpeech && parts.length) {
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
