/* =========================================================================
   Sunday — just stars.

   A deep WebGL star field that's quietly alive: gentle drift toward the
   viewer (floating through space), a slow twinkle, and a soft mouse parallax.
   No sun, no planets, no glow. Stars + the wordmark + the mission, that's it.

   - prefers-reduced-motion -> a single static frame, no animation.
   - no WebGL               -> CSS deep-space wash, text shown plainly.
   - mobile                 -> fewer stars, no parallax.
   ========================================================================= */

const html = document.documentElement;
html.classList.add('js');

const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const mobile = window.matchMedia('(max-width: 820px), (pointer: coarse)').matches;
const hero = document.querySelector('.hero');

function webglOK() {
  try {
    const c = document.createElement('canvas');
    return !!(window.WebGLRenderingContext &&
      (c.getContext('webgl') || c.getContext('experimental-webgl')));
  } catch (e) {
    return false;
  }
}

if (!webglOK()) {
  html.classList.add('no-webgl');
  if (hero) hero.classList.add('is-in');
} else {
  boot();
}

async function boot() {
  const THREE = await import('three');

  const canvas = document.getElementById('stars');
  const renderer = new THREE.WebGLRenderer({
    canvas, alpha: true, antialias: !mobile, powerPreference: 'high-performance',
  });
  renderer.setClearColor(0x000000, 0);
  const DPR = Math.min(window.devicePixelRatio || 1, mobile ? 1.5 : 2);
  renderer.setPixelRatio(DPR);

  const scene = new THREE.Scene();
  // Far stars fade into the dark, so the depth reads and recycled stars never pop.
  scene.fog = new THREE.FogExp2(0x04050a, 0.018);

  const camera = new THREE.PerspectiveCamera(64, window.innerWidth / window.innerHeight, 0.1, 200);
  camera.position.set(0, 0, 0);

  const world = new THREE.Group();
  scene.add(world);

  /* ---- build the field -------------------------------------------------- */
  const COUNT = mobile ? 2200 : (reduced ? 3200 : 5400);
  const DEPTH = 78;            // z spread (stars sit between ~-3 and -DEPTH)
  const SPREAD_X = 130, SPREAD_Y = 96;

  const pos = new Float32Array(COUNT * 3);
  const col = new Float32Array(COUNT * 3);
  const sz = new Float32Array(COUNT);
  const sd = new Float32Array(COUNT);

  const cWhite = new THREE.Color(0xeef1f7);
  const cBlue = new THREE.Color(0x9aa6e4);
  const cWarm = new THREE.Color(0xf2c07a);

  for (let i = 0; i < COUNT; i++) {
    const i3 = i * 3;
    pos[i3] = (Math.random() - 0.5) * SPREAD_X;
    pos[i3 + 1] = (Math.random() - 0.5) * SPREAD_Y;
    pos[i3 + 2] = -3 - Math.random() * DEPTH;

    const r = Math.random();
    const c = r > 0.93 ? cWarm : (r > 0.66 ? cBlue : cWhite);
    const b = 0.5 + Math.random() * 0.5;
    col[i3] = c.r * b; col[i3 + 1] = c.g * b; col[i3 + 2] = c.b * b;

    sz[i] = 0.55 + Math.pow(Math.random(), 2.3) * 2.5;  // a few big, mostly small
    sd[i] = Math.random() * 6.2832;
  }

  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  geo.setAttribute('color', new THREE.BufferAttribute(col, 3));
  geo.setAttribute('aSize', new THREE.BufferAttribute(sz, 1));
  geo.setAttribute('aSeed', new THREE.BufferAttribute(sd, 1));

  const mat = new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    vertexColors: true,               // injects `attribute vec3 color;` — required
    uniforms: {
      uTime: { value: 0 },
      uDpr: { value: DPR },
      uTwinkle: { value: reduced ? 0.0 : 1.0 },
    },
    vertexShader: /* glsl */`
      attribute float aSize;
      attribute float aSeed;
      uniform float uTime;
      uniform float uDpr;
      uniform float uTwinkle;
      varying vec3 vColor;
      varying float vTw;
      void main() {
        vColor = color;
        float tw = 0.62 + 0.38 * sin(uTime * 0.9 + aSeed) * uTwinkle;
        vTw = tw;
        vec4 mv = modelViewMatrix * vec4(position, 1.0);
        gl_Position = projectionMatrix * mv;
        gl_PointSize = aSize * tw * 115.0 * uDpr / -mv.z;
      }
    `,
    fragmentShader: /* glsl */`
      varying vec3 vColor;
      varying float vTw;
      void main() {
        // soft round star
        float d = length(gl_PointCoord - vec2(0.5));
        float a = smoothstep(0.5, 0.0, d);
        a *= a;                         // tighter core, soft edge
        gl_FragColor = vec4(vColor * (0.85 + vTw * 0.5), a);
      }
    `,
  });

  const stars = new THREE.Points(geo, mat);
  world.add(stars);

  /* ---- sizing ----------------------------------------------------------- */
  function resize() {
    renderer.setSize(window.innerWidth, window.innerHeight, false);
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
  }
  resize();
  window.addEventListener('resize', resize);

  /* ---- soft mouse parallax --------------------------------------------- */
  let tx = 0, ty = 0, mx = 0, my = 0;
  if (!mobile && !reduced) {
    window.addEventListener('pointermove', (e) => {
      tx = e.clientX / window.innerWidth - 0.5;
      ty = e.clientY / window.innerHeight - 0.5;
    }, { passive: true });
  }

  // reveal the wordmark + mission on the next frame
  if (hero) requestAnimationFrame(() => hero.classList.add('is-in'));

  /* ---- reduced motion: one still frame ---------------------------------- */
  if (reduced) {
    mat.uniforms.uTime.value = 2.0;
    renderer.render(scene, camera);
    return;
  }

  /* ---- animate ---------------------------------------------------------- */
  const clock = new THREE.Clock();

  function loop() {
    const t = clock.getElapsedTime();
    mat.uniforms.uTime.value = t;

    // Calm and intentional: stars twinkle (in the shader), the field leans
    // gently toward the cursor, and drifts on an almost-imperceptible current.
    // No flying-toward-you motion — that read like a screensaver.
    mx += (tx - mx) * 0.04;
    my += (ty - my) * 0.04;
    world.rotation.y = t * 0.0035 + mx * 0.16;
    world.rotation.x = -my * 0.10;

    renderer.render(scene, camera);
    requestAnimationFrame(loop);
  }
  loop();
}
