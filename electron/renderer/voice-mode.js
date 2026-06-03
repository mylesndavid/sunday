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
// v1: OpenAI Realtime (gpt-realtime-2) over WebRTC. Gemini Live is the next
// provider. The avatar lip-syncs off the live audio amplitude; full viseme
// lip-sync via TalkingHead's audio worklet is a follow-up.

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
    // 1) ephemeral realtime session from the daemon (prompt + tools live there)
    const sres = await fetch(`${ctx.daemonHttp}/v1/voice/session`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...(ctx.daemonToken ? { Authorization: `Bearer ${ctx.daemonToken}` } : {}) },
    });
    const sdata = await sres.json().catch(() => ({}));
    if (!sres.ok) throw new Error(sdata.error || `session ${sres.status}`);
    const ephemeral = sdata?.client_secret?.value;
    const model = sdata?.model || 'gpt-realtime';
    if (!ephemeral) throw new Error('no ephemeral token in session response');

    // 2) the avatar (best-effort — if TalkingHead/WebGL fails, voice still works)
    setStatus('Loading face…', 'wait');
    try { await loadAvatar(ctx.avatarMount); } catch (e) { console.warn('avatar load failed; audio-only', e); }

    // 3) WebRTC to OpenAI Realtime
    setStatus('Connecting…', 'wait');
    await connectOpenAI(ephemeral, model, ctx);
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
  const r = await fetch(`https://api.openai.com/v1/realtime?model=${encodeURIComponent(model)}`, {
    method: 'POST', body: offer.sdp,
    headers: { Authorization: `Bearer ${ephemeral}`, 'Content-Type': 'application/sdp' },
  });
  if (!r.ok) throw new Error(`realtime connect ${r.status}: ${(await r.text()).slice(0, 200)}`);
  await pc.setRemoteDescription({ type: 'answer', sdp: await r.text() });
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
  try { s.pc?.close(); } catch {}
  try { s.head?.stop?.(); } catch {}
  try { if (s.ctx.avatarMount) s.ctx.avatarMount.innerHTML = ''; } catch {}
  try { s.ctx.overlay.classList.remove('open'); s.ctx.overlay.hidden = true; } catch {}
  try { s.ctx.onClose?.(); } catch {}
}
