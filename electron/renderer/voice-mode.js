// Voice mode — realtime, full-duplex Sunday with a TalkingHead avatar.
//
// Lazy-loaded ONLY when the user opens voice mode, so none of this heavy stuff
// (Three.js / TalkingHead / WebRTC) is in the main app's load path — if any of
// it throws, the overlay shows an error and the rest of Sunday is untouched.
//
// Flow: daemon mints an ephemeral realtime session (Sunday's prompt + tools) →
// browser connects WebRTC to the provider → mic streams up, audio streams down
// and drives the avatar's mouth → tool calls bridge back to the daemon.
//
// Two providers, same avatar + same Sunday tools:
//   • OpenAI Realtime (gpt-realtime) over WebRTC — mic/playback handled by the
//     peer connection; tools bridge over the "oai-events" data channel.
//   • Gemini Live over a WebSocket — we capture mic PCM16/16k and stream it up,
//     decode PCM16/24k coming down into a Web Audio playback queue, and bridge
//     toolCall→toolResponse.
// The daemon picks the provider (Settings → Voice) and returns {provider:...}.
// The avatar lip-syncs off live audio amplitude; full viseme lip-sync via
// TalkingHead's audio worklet is a follow-up.

const TALKINGHEAD_URL = 'https://cdn.jsdelivr.net/gh/met4citizen/TalkingHead@1.5/modules/talkinghead.mjs';
const DEFAULT_AVATAR = 'https://models.readyplayer.me/64bfa15f0e72c63d7c3934a6.glb?morphTargets=ARKit,Oculus+Visemes&textureAtlas=1024';

let session = null;   // active session object so we can tear it down

export function isOpen() { return !!session; }

export async function open(ctx) {
  // ctx = { daemonHttp, daemonToken, overlay, avatarMount, status, onClose }
  if (session) return;
  const setStatus = (t, state) => { if (ctx.status) { ctx.status.textContent = t; ctx.status.dataset.state = state || ''; } };
  ctx.overlay.hidden = false;
  ctx.overlay.classList.add('open');
  session = { ctx, pc: null, head: null, audioEl: null, micStream: null, raf: 0, closed: false };

  try {
    setStatus('Starting voice mode…', 'wait');
    // 1) ephemeral realtime session from the daemon (prompt + tools live there).
    // Provider comes from Settings → Voice (the daemon defaults if unset).
    const vprov = (() => { try { return localStorage.getItem('voiceProvider') || ''; } catch { return ''; } })();
    const sres = await fetch(`${ctx.daemonHttp}/v1/voice/session${vprov ? `?provider=${encodeURIComponent(vprov)}` : ''}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...(ctx.daemonToken ? { Authorization: `Bearer ${ctx.daemonToken}` } : {}) },
    });
    const sdata = await sres.json().catch(() => ({}));
    if (!sres.ok) throw new Error(sdata.error || `session ${sres.status}`);
    const provider = sdata?.provider || 'openai';

    // 2) the avatar (best-effort — if TalkingHead/WebGL fails, voice still works)
    setStatus('Loading face…', 'wait');
    try { await loadAvatar(ctx.avatarMount); } catch (e) { console.warn('avatar load failed; audio-only', e); }

    // 3) connect to the chosen provider
    setStatus('Connecting…', 'wait');
    if (provider === 'gemini') {
      if (!sdata.ws_url) throw new Error('no ws_url in gemini session response');
      await connectGemini(sdata, ctx);
    } else {
      // GA: ephemeral token is top-level `value` (beta nested it under client_secret)
      const ephemeral = sdata?.value || sdata?.client_secret?.value;
      const model = sdata?.model || 'gpt-realtime';
      if (!ephemeral) throw new Error('no ephemeral token in session response');
      await connectOpenAI(ephemeral, model, ctx);
    }
    setStatus('Listening — just talk.', 'ok');
  } catch (err) {
    setStatus(err.message || 'Voice mode failed to start', 'fail');
    console.error('voice mode open failed', err);
  }
}

async function loadAvatar(mount) {
  const { TalkingHead } = await import(TALKINGHEAD_URL);
  const head = new TalkingHead(mount, {
    ttsEndpoint: null, lipsyncModules: [], cameraView: 'upper',
    modelFps: 30, avatarMood: 'neutral',
  });
  await head.showAvatar({ url: DEFAULT_AVATAR, body: 'F', avatarMood: 'neutral' });
  session.head = head;
}

async function connectOpenAI(ephemeral, model, ctx) {
  const pc = new RTCPeerConnection();
  session.pc = pc;

  // remote audio → play + drive the avatar's mouth off amplitude
  const audioEl = new Audio(); audioEl.autoplay = true; session.audioEl = audioEl;
  pc.ontrack = (e) => { audioEl.srcObject = e.streams[0]; driveMouthFromStream(e.streams[0]); };

  // mic up
  const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
  session.micStream = mic;
  mic.getTracks().forEach((t) => pc.addTrack(t, mic));

  // events + tool calls over the data channel
  const dc = pc.createDataChannel('oai-events');
  session.dc = dc;
  dc.addEventListener('message', (e) => handleEvent(JSON.parse(e.data), ctx));

  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  // GA WebRTC: POST the SDP offer to /realtime/calls (model is bound to the
  // ephemeral token server-side, so it isn't a query param anymore).
  const r = await fetch('https://api.openai.com/v1/realtime/calls', {
    method: 'POST', body: offer.sdp,
    headers: { Authorization: `Bearer ${ephemeral}`, 'Content-Type': 'application/sdp' },
  });
  if (!r.ok) throw new Error(`realtime connect ${r.status}: ${(await r.text()).slice(0, 200)}`);
  await pc.setRemoteDescription({ type: 'answer', sdp: await r.text() });
}

// ── Gemini Live (WebSocket) ────────────────────────────────────────────────
// Browsers can't set WS headers, so the daemon hands us a ws_url with the
// ephemeral token already on it (?access_token=). We send `setup` (built by the
// daemon: model + Sunday's prompt + tools), stream mic as PCM16/16k base64, and
// decode PCM16/24k audio back into a Web Audio queue. Tools bridge through the
// daemon exactly like the OpenAI path.
async function connectGemini(sdata, ctx) {
  const ws = new WebSocket(sdata.ws_url);
  ws.binaryType = 'arraybuffer';
  session.ws = ws;

  // playback queue @ 24k, scheduled back-to-back so speech is gapless
  const playCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
  session.playCtx = playCtx;
  let playHead = 0;
  const playPcm = (b64) => {
    const f32 = pcm16ToFloat32(b64);
    if (!f32.length) return;
    const buf = playCtx.createBuffer(1, f32.length, 24000);
    buf.copyToChannel(f32, 0);
    const node = playCtx.createBufferSource(); node.buffer = buf; node.connect(playCtx.destination);
    const now = playCtx.currentTime;
    playHead = Math.max(playHead, now);
    node.start(playHead);
    // drive the mouth across this chunk's lifetime off its RMS
    driveMouthForChunk(f32, playHead - now, buf.duration);
    playHead += buf.duration;
  };

  await new Promise((resolve, reject) => {
    ws.addEventListener('open', () => { try { ws.send(JSON.stringify(sdata.setup)); } catch (e) { reject(e); } resolve(); });
    ws.addEventListener('error', (e) => reject(new Error('gemini ws error')));
  });

  ws.addEventListener('message', async (e) => {
    const msg = await parseGeminiFrame(e.data);
    if (!msg) return;
    const parts = msg.serverContent?.modelTurn?.parts || [];
    for (const p of parts) {
      const d = p.inlineData?.data;
      if (d && (p.inlineData.mimeType || '').includes('audio')) playPcm(d);
    }
    if (msg.toolCall?.functionCalls?.length) {
      for (const fc of msg.toolCall.functionCalls) await runGeminiTool(fc, ctx);
    }
  });
  ws.addEventListener('close', () => { if (!session?.closed) session.ctx && (session.ctx.status.dataset.state = 'fail'); });

  // mic → PCM16/16k → realtimeInput.mediaChunks
  const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
  session.micStream = mic;
  const micCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
  session.micCtx = micCtx;
  const src = micCtx.createMediaStreamSource(mic);
  const proc = micCtx.createScriptProcessor(4096, 1, 1);
  session.proc = proc;
  const sink = micCtx.createGain(); sink.gain.value = 0;          // keep the node pulling without echo
  src.connect(proc); proc.connect(sink); sink.connect(micCtx.destination);
  proc.onaudioprocess = (ev) => {
    if (!session || session.closed || ws.readyState !== 1) return;
    const b64 = float32ToPcm16Base64(ev.inputBuffer.getChannelData(0));
    try { ws.send(JSON.stringify({ realtimeInput: { mediaChunks: [{ mimeType: 'audio/pcm;rate=16000', data: b64 }] } })); } catch {}
  };
}

async function runGeminiTool(fc, ctx) {
  let output;
  try {
    const r = await fetch(`${ctx.daemonHttp}/v1/voice/tool`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...(ctx.daemonToken ? { Authorization: `Bearer ${ctx.daemonToken}` } : {}) },
      body: JSON.stringify({ name: fc.name, arguments: fc.args || {} }),
    });
    output = await r.json();
  } catch (e) { output = { error: String(e) }; }
  const ws = session?.ws; if (!ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({ toolResponse: { functionResponses: [
    { id: fc.id, name: fc.name, response: { result: output } } ] } }));
}

// Gemini frames are JSON; some transports deliver them as Blob/ArrayBuffer.
async function parseGeminiFrame(data) {
  try {
    let text = data;
    if (data instanceof Blob) text = await data.text();
    else if (data instanceof ArrayBuffer) text = new TextDecoder().decode(data);
    return JSON.parse(text);
  } catch { return null; }
}

// base64 PCM16-LE → Float32 [-1,1]
function pcm16ToFloat32(b64) {
  const bin = atob(b64);
  const n = bin.length >> 1;
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const lo = bin.charCodeAt(i * 2), hi = bin.charCodeAt(i * 2 + 1);
    let v = (hi << 8) | lo; if (v >= 0x8000) v -= 0x10000;
    out[i] = v / 0x8000;
  }
  return out;
}

// Float32 [-1,1] → base64 PCM16-LE (chunked so big buffers don't blow the stack)
function float32ToPcm16Base64(f32) {
  const buf = new Uint8Array(f32.length * 2);
  for (let i = 0; i < f32.length; i++) {
    let s = Math.max(-1, Math.min(1, f32[i]));
    s = s < 0 ? s * 0x8000 : s * 0x7fff;
    buf[i * 2] = s & 0xff; buf[i * 2 + 1] = (s >> 8) & 0xff;
  }
  let bin = '';
  for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
  return btoa(bin);
}

// Hold the mouth open proportional to a chunk's loudness for its duration.
function driveMouthForChunk(f32, delaySec, durSec) {
  let sum = 0; for (let i = 0; i < f32.length; i++) sum += f32[i] * f32[i];
  const open = Math.min(1, Math.sqrt(sum / f32.length) * 3.5);
  const apply = () => {
    if (session?.closed) return;
    session.ctx.overlay.style.setProperty('--talk', open.toFixed(2));
    try { session.head?.setValue?.('mouthOpen', open); } catch {}
  };
  setTimeout(apply, Math.max(0, delaySec * 1000));
  setTimeout(() => { if (!session?.closed) { session.ctx.overlay.style.setProperty('--talk', '0'); try { session.head?.setValue?.('mouthOpen', 0); } catch {} } },
    Math.max(0, (delaySec + durSec) * 1000));
}

// Amplitude → jaw/scale so the avatar (or the orb fallback) visibly "talks".
function driveMouthFromStream(stream) {
  try {
    const actx = new (window.AudioContext || window.webkitAudioContext)();
    const src = actx.createMediaStreamSource(stream);
    const an = actx.createAnalyser(); an.fftSize = 256; src.connect(an);
    const buf = new Uint8Array(an.frequencyBinCount);
    const tick = () => {
      if (session?.closed) { try { actx.close(); } catch {} return; }
      an.getByteTimeDomainData(buf);
      let sum = 0; for (let i = 0; i < buf.length; i++) { const v = (buf[i] - 128) / 128; sum += v * v; }
      const rms = Math.sqrt(sum / buf.length);            // 0..~0.5
      const open = Math.min(1, rms * 3.5);
      session.ctx.overlay.style.setProperty('--talk', open.toFixed(2));
      try { session.head?.setValue?.('mouthOpen', open); } catch {}
      session.raf = requestAnimationFrame(tick);
    };
    tick();
  } catch (e) { console.warn('mouth drive failed', e); }
}

async function handleEvent(ev, ctx) {
  // The realtime model asked to call a Sunday tool → run it on the daemon and
  // feed the result back so the model can keep talking.
  if (ev.type === 'response.function_call_arguments.done') {
    let args = {}; try { args = JSON.parse(ev.arguments || '{}'); } catch {}
    let output;
    try {
      const r = await fetch(`${ctx.daemonHttp}/v1/voice/tool`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...(ctx.daemonToken ? { Authorization: `Bearer ${ctx.daemonToken}` } : {}) },
        body: JSON.stringify({ name: ev.name, arguments: args }),
      });
      output = await r.json();
    } catch (e) { output = { error: String(e) }; }
    const dc = session?.dc; if (!dc || dc.readyState !== 'open') return;
    dc.send(JSON.stringify({ type: 'conversation.item.create', item: {
      type: 'function_call_output', call_id: ev.call_id, output: JSON.stringify(output) } }));
    dc.send(JSON.stringify({ type: 'response.create' }));
  }
}

export function close() {
  if (!session) return;
  const s = session; session = null; s.closed = true;
  try { cancelAnimationFrame(s.raf); } catch {}
  try { s.micStream?.getTracks().forEach((t) => t.stop()); } catch {}
  try { s.proc && (s.proc.onaudioprocess = null); } catch {}
  try { s.ws?.close(); } catch {}
  try { s.micCtx?.close(); } catch {}
  try { s.playCtx?.close(); } catch {}
  try { s.pc?.close(); } catch {}
  try { s.head?.stop?.(); } catch {}
  try { if (s.ctx.avatarMount) s.ctx.avatarMount.innerHTML = ''; } catch {}
  try { s.ctx.overlay.classList.remove('open'); s.ctx.overlay.hidden = true; } catch {}
  try { s.ctx.onClose?.(); } catch {}
}
