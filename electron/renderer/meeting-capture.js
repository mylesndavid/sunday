// Meeting capture — runs in a hidden Sunday window so BOTH the screen-recording
// grant (system audio) and the mic grant are the app's own. This is why the
// detached Swift recorder failed: ScreenCaptureKit checks the caller's
// authorization, and a spawned helper isn't Sunday. getDisplayMedia here IS
// Sunday.
//
// Two tracks, kept separate so transcription can label You (mic) vs Others
// (system). Chunks stream to main, which appends them to webm files.

const CHUNK_MS = 4000;

let sysStream = null, micStream = null;
let sysRec = null, micRec = null;

async function start() {
  // System audio via getDisplayMedia. main's setDisplayMediaRequestHandler
  // supplies a screen source + 'loopback' audio, so no picker appears and
  // the audio track is the system output.
  let sysOk = false;
  try {
    sysStream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
    // Drop the video track — we only want the system audio.
    sysStream.getVideoTracks().forEach((t) => t.stop());
    if (sysStream.getAudioTracks().length) {
      sysRec = recorderFor(sysStream, 'system');
      sysOk = true;
    }
  } catch (e) {
    window.meetingCapture.report({ track: 'system', ok: false, error: String(e && e.name) });
  }

  let micOk = false;
  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    micRec = recorderFor(micStream, 'mic');
    micOk = true;
  } catch (e) {
    window.meetingCapture.report({ track: 'mic', ok: false, error: String(e && e.name) });
  }

  window.meetingCapture.report({ started: true, system: sysOk, mic: micOk });
}

function recorderFor(stream, label) {
  // Audio-only MediaRecorder. webm/opus is what Chromium gives; ffmpeg on the
  // main side converts to wav for whisper.
  let mr;
  try { mr = new MediaRecorder(stream, { mimeType: 'audio/webm' }); }
  catch { mr = new MediaRecorder(stream); }
  mr.ondataavailable = async (e) => {
    if (!e.data || !e.data.size) return;
    try {
      const buf = await e.data.arrayBuffer();
      await window.meetingCapture.chunk(label, new Uint8Array(buf));
    } catch {}
  };
  mr.start(CHUNK_MS);   // emit a chunk every CHUNK_MS
  return mr;
}

// main asks us to stop + flush.
window.meetingCapture.onStop(() => {
  for (const mr of [sysRec, micRec]) { try { mr && mr.state !== 'inactive' && mr.stop(); } catch {} }
  for (const s of [sysStream, micStream]) { try { s && s.getTracks().forEach((t) => t.stop()); } catch {} }
  window.meetingCapture.stopped();
});

start();
