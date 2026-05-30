// Ambient observer capture loop. Runs in a hidden Sunday window so the mic
// grant is attributed to Sunday (not a python child). Records ~30s chunks via
// MediaRecorder and hands each to main, which transcribes + ticks.
//
// VAD gate: a cheap Web Audio energy meter runs the whole time, but a chunk is
// only sent for transcription if it actually contained speech-level audio.
// Silence costs nothing but the meter — no ffmpeg, no Whisper, no tick. Most
// of the day is silence, so this is the difference between idling for free and
// running Whisper every 30s around the clock.

const CHUNK_MS = 30000;     // 30s windows, matching the tick cadence
const RMS_SPEECH = 0.015;   // normalized-RMS threshold; below this = silence, skip
const SAMPLE_MS = 150;      // energy sampling cadence

let stream = null;
let capturing = false;
let analyser = null, meterData = null, meterTimer = null, windowPeak = 0;

async function start() {
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    window.capture.reportState({ active: false, error: `getusermedia-failed: ${err && err.name}` });
    return;
  }
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const src = ctx.createMediaStreamSource(stream);
    analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    src.connect(analyser);
    meterData = new Uint8Array(analyser.fftSize);
    meterTimer = setInterval(sampleEnergy, SAMPLE_MS);
  } catch { /* no meter → fall back to transcribing every chunk */ }
  capturing = true;
  window.capture.reportState({ active: true, error: null });
  cycle();
}

// Loudest moment in the current window (normalized RMS, 0..~1).
function sampleEnergy() {
  if (!analyser) return;
  analyser.getByteTimeDomainData(meterData);
  let sum = 0;
  for (let i = 0; i < meterData.length; i++) { const v = (meterData[i] - 128) / 128; sum += v * v; }
  const rms = Math.sqrt(sum / meterData.length);
  if (rms > windowPeak) windowPeak = rms;
}

// One record→stop→ship cycle, then immediately start the next. Using
// discrete MediaRecorder lifecycles (rather than a timeslice) gives us a
// clean, standalone webm per chunk that Whisper accepts directly.
function cycle() {
  if (!capturing || !stream) return;
  windowPeak = 0;   // reset the meter for this window
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
    // VAD gate: only transcribe (and tick) when the window had speech.
    const hadSpeech = !analyser || windowPeak >= RMS_SPEECH;
    if (hadSpeech && parts.length) {
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
