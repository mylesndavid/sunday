/* =========================================================================
   Sunday — a calm cosmos you travel through, arriving at a sunrise.

   A real Three.js scene: a deep, multi-layer starfield (2–3 depth layers that
   parallax at different rates), a genuine procedural sun (animated plasma
   surface via fbm of 3D simplex noise, limb darkening), and an atmospheric
   horizon glow that bleeds warmth upward from the low sun. Lenis drives smooth
   scroll; scroll progress 0->1 carries the camera along a gentle curved path
   through the field and settles into a wide, still hero frame where the sun
   rests LOW on the horizon and the big "Sunday" wordmark + mission overlay it.

   No planets, no fuzzy corona sprite. Stars + sun + horizon glow only.

   Robustness:
   - prefers-reduced-motion -> render the FINAL composed frame, no camera motion.
   - no WebGL              -> CSS deep-space fallback (class on <html>), no canvas.
   - mobile                -> fewer stars, simpler path; the finale still composes.

   Performance: one rAF loop, no per-frame allocations, capped DPR, modest sun
   geometry, fbm kept to 5 octaves on the surface. Starfield keeps vertexColors.
   ========================================================================= */

const html = document.documentElement;
html.classList.add('js');

const prefersReduced =
  window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const isMobile = window.matchMedia('(max-width: 820px), (pointer: coarse)').matches;

/* ----------------------------------------------------- WebGL capability check */
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
} else {
  boot();
}

/* ------------------------------------------------------------------ GLSL bits */

/* Stefan Gustavson / Ashima "Simplex 3D Noise" — known-good, compiles cleanly.
   Returns snoise(vec3) in roughly [-1, 1]. Used by the sun surface shader. */
const SNOISE3 = /* glsl */`
vec4 permute(vec4 x){return mod(((x*34.0)+1.0)*x, 289.0);}
vec4 taylorInvSqrt(vec4 r){return 1.79284291400159 - 0.85373472095314 * r;}

float snoise(vec3 v){
  const vec2  C = vec2(1.0/6.0, 1.0/3.0);
  const vec4  D = vec4(0.0, 0.5, 1.0, 2.0);
  vec3 i  = floor(v + dot(v, C.yyy));
  vec3 x0 = v - i + dot(i, C.xxx);
  vec3 g = step(x0.yzx, x0.xyz);
  vec3 l = 1.0 - g;
  vec3 i1 = min(g.xyz, l.zxy);
  vec3 i2 = max(g.xyz, l.zxy);
  vec3 x1 = x0 - i1 + 1.0 * C.xxx;
  vec3 x2 = x0 - i2 + 2.0 * C.xxx;
  vec3 x3 = x0 - 1.0 + 3.0 * C.xxx;
  i = mod(i, 289.0);
  vec4 p = permute(permute(permute(
             i.z + vec4(0.0, i1.z, i2.z, 1.0))
           + i.y + vec4(0.0, i1.y, i2.y, 1.0))
           + i.x + vec4(0.0, i1.x, i2.x, 1.0));
  float n_ = 1.0/7.0;
  vec3  ns = n_ * D.wyz - D.xzx;
  vec4 j = p - 49.0 * floor(p * ns.z *ns.z);
  vec4 x_ = floor(j * ns.z);
  vec4 y_ = floor(j - 7.0 * x_);
  vec4 x = x_ *ns.x + ns.yyyy;
  vec4 y = y_ *ns.x + ns.yyyy;
  vec4 h = 1.0 - abs(x) - abs(y);
  vec4 b0 = vec4(x.xy, y.xy);
  vec4 b1 = vec4(x.zw, y.zw);
  vec4 s0 = floor(b0)*2.0 + 1.0;
  vec4 s1 = floor(b1)*2.0 + 1.0;
  vec4 sh = -step(h, vec4(0.0));
  vec4 a0 = b0.xzyw + s0.xzyw*sh.xxyy;
  vec4 a1 = b1.xzyw + s1.xzyw*sh.zzww;
  vec3 p0 = vec3(a0.xy, h.x);
  vec3 p1 = vec3(a0.zw, h.y);
  vec3 p2 = vec3(a1.xy, h.z);
  vec3 p3 = vec3(a1.zw, h.w);
  vec4 norm = taylorInvSqrt(vec4(dot(p0,p0), dot(p1,p1), dot(p2,p2), dot(p3,p3)));
  p0 *= norm.x; p1 *= norm.y; p2 *= norm.z; p3 *= norm.w;
  vec4 m = max(0.6 - vec4(dot(x0,x0), dot(x1,x1), dot(x2,x2), dot(x3,x3)), 0.0);
  m = m * m;
  return 42.0 * dot(m*m, vec4(dot(p0,x0), dot(p1,x1), dot(p2,x2), dot(p3,x3)));
}`;

/* fbm: 5 octaves of snoise. Cheap, stable. */
const FBM3 = /* glsl */`
float fbm(vec3 p){
  float total = 0.0;
  float amp = 0.5;
  float freq = 1.0;
  for (int i = 0; i < 5; i++){
    total += snoise(p * freq) * amp;
    freq *= 2.02;
    amp *= 0.5;
  }
  return total;
}`;

/* ======================================================================== */
async function boot() {
  const THREE = await import('three');

  /* ----------------------------------------------------------- core setup */
  const canvas = document.getElementById('cosmos');
  const renderer = new THREE.WebGLRenderer({
    canvas,
    antialias: !isMobile,
    alpha: true,
    powerPreference: 'high-performance',
  });
  renderer.setClearColor(0x000000, 0);
  const DPR = Math.min(window.devicePixelRatio || 1, isMobile ? 1.5 : 2);
  renderer.setPixelRatio(DPR);

  const scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x05060b, 0.0065);

  const camera = new THREE.PerspectiveCamera(
    58, window.innerWidth / window.innerHeight, 0.1, 3000
  );

  const world = new THREE.Group();
  scene.add(world);

  // The sun lives at the world origin. The camera composes it LOW on screen at
  // the finale by looking ABOVE the sun (lookAt y > sun.y), so the sun falls to
  // the bottom of the frame and its glow bleeds upward into the dark.
  const SUN_POS = new THREE.Vector3(0, 0, 0);
  const SUN_RADIUS = 7.2;

  /* ------------------------------------------------- the starfield (layers) */
  // 2–3 depth layers. Each layer is its own Points object placed in its own
  // Group so we can parallax-rotate them at different rates. Near layers move
  // more, far layers barely drift — depth you feel as you travel.
  const cWhite = new THREE.Color(0xeef0f6);
  const cViolet = new THREE.Color(0x8e9bd6);
  const cAmber = new THREE.Color(0xf1b24a);

  // [count, spreadXY, depthNear, depthFar, sizeScale, parallaxRate]
  const baseCount = isMobile ? 1100 : (prefersReduced ? 2600 : 4200);
  const layerDefs = isMobile
    ? [
        { n: Math.round(baseCount * 0.55), sx: 260, sy: 200, near: -40,  far: -240, size: 1.7, rate: 0.020 },
        { n: Math.round(baseCount * 0.45), sx: 420, sy: 300, near: -240, far: -620, size: 1.1, rate: 0.008 },
      ]
    : [
        { n: Math.round(baseCount * 0.34), sx: 220, sy: 170, near: -20,  far: -180, size: 2.1, rate: 0.026 },
        { n: Math.round(baseCount * 0.36), sx: 340, sy: 250, near: -180, far: -420, size: 1.4, rate: 0.013 },
        { n: Math.round(baseCount * 0.30), sx: 520, sy: 380, near: -420, far: -820, size: 0.9, rate: 0.005 },
      ];

  const starMat = new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    vertexColors: true,
    uniforms: {
      uTime: { value: 0 },
      uPixelRatio: { value: DPR },
      uTwinkle: { value: prefersReduced ? 0.0 : 1.0 },
    },
    vertexShader: /* glsl */`
      attribute float aSize;
      attribute float aSeed;
      uniform float uTime;
      uniform float uPixelRatio;
      uniform float uTwinkle;
      varying vec3 vColor;
      varying float vTw;
      void main() {
        vColor = color;
        float tw = 0.7 + 0.3 * sin(uTime * 0.8 + aSeed) * uTwinkle;
        vTw = tw;
        vec4 mv = modelViewMatrix * vec4(position, 1.0);
        gl_Position = projectionMatrix * mv;
        gl_PointSize = aSize * tw * 90.0 * uPixelRatio / -mv.z;
      }
    `,
    fragmentShader: /* glsl */`
      varying vec3 vColor;
      varying float vTw;
      void main() {
        vec2 d = gl_PointCoord - vec2(0.5);
        float dist = dot(d, d);
        float alpha = smoothstep(0.25, 0.0, dist);
        gl_FragColor = vec4(vColor * (0.8 + vTw * 0.4), alpha);
      }
    `,
  });

  const starLayers = [];
  for (const def of layerDefs) {
    const positions = new Float32Array(def.n * 3);
    const colors = new Float32Array(def.n * 3);
    const sizes = new Float32Array(def.n);
    const seeds = new Float32Array(def.n);

    for (let i = 0; i < def.n; i++) {
      const i3 = i * 3;
      positions[i3] = (Math.random() - 0.5) * def.sx;
      positions[i3 + 1] = (Math.random() - 0.5) * def.sy;
      positions[i3 + 2] = def.near + Math.random() * (def.far - def.near);

      const roll = Math.random();
      const c = roll > 0.92 ? cAmber : (roll > 0.6 ? cViolet : cWhite);
      const b = 0.55 + Math.random() * 0.45;
      colors[i3] = c.r * b; colors[i3 + 1] = c.g * b; colors[i3 + 2] = c.b * b;

      sizes[i] = (0.6 + Math.pow(Math.random(), 2) * 2.6) * def.size;
      seeds[i] = Math.random() * Math.PI * 2;
    }

    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
    geo.setAttribute('aSize', new THREE.BufferAttribute(sizes, 1));
    geo.setAttribute('aSeed', new THREE.BufferAttribute(seeds, 1));

    const grp = new THREE.Group();
    const pts = new THREE.Points(geo, starMat);
    grp.add(pts);
    world.add(grp);
    starLayers.push({ grp, rate: def.rate });
  }

  /* ================================================================ THE SUN */
  // The sun is its own group holding the plasma sphere and an atmospheric
  // horizon glow plane. No PointLight / AmbientLight — the surface is emissive.
  const sun = new THREE.Group();
  sun.position.copy(SUN_POS);
  world.add(sun);

  /* --- 1. Plasma surface (KEPT) --------------------------------------------
     fbm(5 oct) of 3D simplex sampled over the sphere surface, with a slow time
     flow + a domain-warped layer so it churns like granulation. Drives a solar
     color ramp; fresnel adds limb darkening so it reads spherical. */
  const sunSurfaceMat = new THREE.ShaderMaterial({
    uniforms: {
      uTime: { value: 0 },
      uChurn: { value: prefersReduced ? 0.0 : 1.0 }, // 0 = frozen but detailed
    },
    vertexShader: /* glsl */`
      varying vec3 vPos;       // object-space position (sphere surface)
      varying vec3 vNormal;    // view-space normal (for limb darkening)
      varying vec3 vView;      // view direction
      void main() {
        vPos = position;
        vNormal = normalize(normalMatrix * normal);
        vec4 mv = modelViewMatrix * vec4(position, 1.0);
        vView = normalize(-mv.xyz);
        gl_Position = projectionMatrix * mv;
      }
    `,
    fragmentShader: /* glsl */`
      precision highp float;
      varying vec3 vPos;
      varying vec3 vNormal;
      varying vec3 vView;
      uniform float uTime;
      uniform float uChurn;

      ${SNOISE3}
      ${FBM3}

      void main() {
        // Normalised surface point, scaled into noise space.
        vec3 sp = normalize(vPos);
        float t = uTime * 0.06 * uChurn;       // slow flow
        vec3 flow = vec3(0.0, t, t * 0.5);

        // Base granulation field.
        vec3 q = sp * 2.6 + flow;
        // Second, domain-warped layer: warp q by its own fbm so cells convect.
        vec3 warp = vec3(
          fbm(q + vec3(0.0, 1.7, 0.0)),
          fbm(q + vec3(3.2, 0.0, 1.1)),
          fbm(q + vec3(1.4, 2.9, 0.0))
        );
        float n = fbm(q + warp * 0.9 + flow * 0.7);
        // Remap roughly to 0..1 and push contrast so filaments pop.
        float v = clamp(n * 0.5 + 0.5, 0.0, 1.0);
        v = pow(v, 1.4);

        // Solar color ramp: ember -> deep red-orange -> orange -> white-hot.
        vec3 ember   = vec3(0.165, 0.024, 0.0);   // #2a0600
        vec3 deepRed = vec3(0.55,  0.10,  0.01);
        vec3 orange  = vec3(1.0,   0.478, 0.094);  // #ff7a18
        vec3 hot     = vec3(1.0,   0.906, 0.69);   // #ffe7b0
        vec3 col = mix(ember, deepRed, smoothstep(0.0, 0.35, v));
        col = mix(col, orange,        smoothstep(0.30, 0.62, v));
        col = mix(col, hot,           smoothstep(0.66, 0.95, v));

        // Gentle brightness pulse on the hottest regions.
        float pulse = 0.85 + 0.15 * sin(uTime * 0.8 + v * 6.2831);
        col *= mix(1.0, pulse, smoothstep(0.6, 1.0, v));

        // Hot filaments glow a touch hotter.
        col += hot * smoothstep(0.82, 1.0, v) * 0.6;

        // Limb darkening: front-facing brighter, edge falls to ember.
        float facing = clamp(dot(vNormal, vView), 0.0, 1.0);
        float limb = pow(facing, 0.55);
        col *= mix(0.45, 1.15, limb);

        gl_FragColor = vec4(col, 1.0);
      }
    `,
  });
  const sunSurface = new THREE.Mesh(
    new THREE.SphereGeometry(SUN_RADIUS, isMobile ? 48 : 64, isMobile ? 48 : 64),
    sunSurfaceMat
  );
  sun.add(sunSurface);

  /* --- 2. Horizon glow (NEW; replaces the corona sprite) -------------------
     A large, vertically-stretched additive plane sitting just behind the sun.
     Its shader paints a soft warm gradient that is strongest at the sun's
     centre and bleeds UPWARD (and a little outward), fading to nothing — an
     atmosphere/sunrise seen from space. Not a ring around a disc: an integrated
     vertical wash of warmth rising from where the sun rests on the horizon.

     uGlow ramps 0->1 toward the finale so the warmth "rises" as we arrive. */
  const glowMat = new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    depthTest: false,
    blending: THREE.AdditiveBlending,
    uniforms: {
      uTime: { value: 0 },
      uGlow: { value: 0.0 },   // 0..1 arrival ramp
    },
    vertexShader: /* glsl */`
      varying vec2 vUv;
      void main() {
        vUv = uv;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }
    `,
    fragmentShader: /* glsl */`
      precision highp float;
      varying vec2 vUv;
      uniform float uTime;
      uniform float uGlow;

      void main() {
        // Centre the plane: x,y in roughly [-1,1] with the sun at origin.
        vec2 p = (vUv - 0.5) * 2.0;

        // Horizontal falloff — wide but soft, a broad warm band.
        float hx = exp(-p.x * p.x * 1.1);

        // Vertical profile: warmth concentrated at/just above the sun and
        // bleeding UPWARD. Below the sun it fades fast (it's the horizon).
        float up = p.y;                                  // +y is up
        float rise = exp(-max(up, 0.0) * 3.0);           // keep the warmth LOW near the horizon
        float below = exp(-max(-up, 0.0) * 5.0);         // quick fade downward
        float vy = mix(below, rise, step(0.0, up));

        // Core warmth right around the sun's seat (slightly above centre).
        float core = exp(-dot(p - vec2(0.0, 0.06), p - vec2(0.0, 0.06)) * 2.4);

        // A barely-there shimmer so the atmosphere feels alive, not a decal.
        float shimmer = 0.94 + 0.06 * sin(uTime * 0.4 + p.y * 3.0);

        float a = (vy * hx * 0.40 + core * 0.58) * shimmer;
        a *= uGlow;

        // Warm gradient: hot cream near the core -> amber -> deep orange edges.
        vec3 hot    = vec3(1.0, 0.93, 0.74);
        vec3 amber  = vec3(1.0, 0.66, 0.28);
        vec3 deep   = vec3(0.85, 0.34, 0.10);
        float m = clamp(core * 0.9 + vy * 0.3, 0.0, 1.0);
        vec3 col = mix(deep, amber, smoothstep(0.0, 0.5, m));
        col = mix(col, hot, smoothstep(0.5, 1.0, m));

        gl_FragColor = vec4(col, a);
      }
    `,
  });
  // Tall, wide plane behind the sun. Stretched vertically so the upward bleed
  // has room to climb into the dark above the horizon.
  const glow = new THREE.Mesh(
    new THREE.PlaneGeometry(SUN_RADIUS * 22, SUN_RADIUS * 26),
    glowMat
  );
  glow.position.set(0, SUN_RADIUS * 2.0, -2.5); // seated low, climbing up, behind sun
  sun.add(glow);

  /* ----------------------------------------------------- camera curve path */
  // A gentle Catmull-Rom curve through space: lateral drift + depth + a touch
  // of vertical. Scroll progress 0->1 maps to position along the curve. The
  // FINAL control point places the camera so the sun lands LOW & centred.
  //
  // Composition trick for "sun low on screen": at the finale the camera sits a
  // bit ABOVE the sun and looks at a point ABOVE the sun, so the sun disc falls
  // to the bottom of the frame. lookAt targets follow their own eased path.
  const camCurve = new THREE.CatmullRomCurve3([
    new THREE.Vector3(-26,  10,  150),  // start: off to the side, high, far
    new THREE.Vector3( 22,  -6,  120),  // drift across and down
    new THREE.Vector3(-14,  12,   92),  // back across, lift
    new THREE.Vector3( 16,   2,   66),  // settle toward centre
    new THREE.Vector3(  0,  20,   48),  // FINAL: centred, raised so sun sits low
  ], false, 'catmullrom', 0.5);

  // lookAt path: ends ABOVE the sun so the sun seats at the bottom of frame.
  const lookCurve = new THREE.CatmullRomCurve3([
    new THREE.Vector3( 0, 2,  20),
    new THREE.Vector3( 0, 3,  10),
    new THREE.Vector3( 0, 5,   2),
    new THREE.Vector3( 0, 8,  -4),
    new THREE.Vector3( 0, 16, -8),   // FINAL: looking well above the sun
  ], false, 'catmullrom', 0.5);

  const _camPos = new THREE.Vector3();
  const _lookPos = new THREE.Vector3();

  /* ----------------------------------------------------- scroll via Lenis */
  let scrollProgress = 0;
  let lenis = null;

  if (!prefersReduced) {
    try {
      const Lenis = (await import('lenis')).default;
      lenis = new Lenis({ lerp: 0.09, wheelMultiplier: 1, smoothWheel: true });
      lenis.on('scroll', ({ scroll, limit }) => {
        scrollProgress = limit > 0 ? Math.min(scroll / limit, 1) : 0;
      });
    } catch (e) {
      lenis = null;
    }
  }

  function nativeProgress() {
    const max = document.documentElement.scrollHeight - window.innerHeight;
    return max > 0 ? Math.min(window.scrollY / max, 1) : 0;
  }
  if (!lenis) {
    scrollProgress = nativeProgress();
    window.addEventListener('scroll', () => { scrollProgress = nativeProgress(); }, { passive: true });
  }

  /* ------------------------------------------------ the hero overlay reveal */
  // The big "Sunday" + mission live in the DOM (.hero). They fade/rise in as
  // the camera arrives at the finale. Real, focusable text over the canvas.
  const hero = document.querySelector('.hero');

  /* ----------------------------------------------------- mouse parallax */
  const pointer = { x: 0, y: 0, tx: 0, ty: 0 };
  if (!isMobile && !prefersReduced) {
    window.addEventListener('pointermove', (e) => {
      pointer.tx = (e.clientX / window.innerWidth - 0.5);
      pointer.ty = (e.clientY / window.innerHeight - 0.5);
    }, { passive: true });
  }

  /* ---------------------------------------------------------- resize */
  function resize() {
    const w = window.innerWidth, h = window.innerHeight;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h, false);
  }
  resize();
  window.addEventListener('resize', resize);

  const EASE = (t) => t * t * (3 - 2 * t);

  /* ------------------------------------------ place camera at a path point */
  // Shared by the live loop and the static (reduced-motion) render.
  function placeCamera(p, px, py) {
    const e = EASE(p);
    camCurve.getPoint(e, _camPos);
    lookCurve.getPoint(e, _lookPos);
    // Mouse parallax is a tiny additive sway, calm and never disorienting.
    camera.position.set(_camPos.x + px * 6, _camPos.y - py * 4, _camPos.z);
    // A slow roll that eases out to level by the finale (never disorienting).
    camera.up.set(Math.sin((1.0 - e) * 0.5) * 0.10, 1.0, 0.0).normalize();
    camera.lookAt(_lookPos.x + px * 2, _lookPos.y - py * 1.5, _lookPos.z);
  }

  /* ===================================================== reduced motion */
  // Render the FINAL composed frame statically: sun low, glow risen, big
  // Sunday + mission shown (via .hero), calm starfield. No camera motion.
  if (prefersReduced) {
    placeCamera(1, 0, 0);
    sunSurfaceMat.uniforms.uTime.value = 12.0; // a frozen, detailed surface
    glowMat.uniforms.uTime.value = 12.0;
    glowMat.uniforms.uGlow.value = 0.46;
    starMat.uniforms.uTime.value = 12.0;
    if (hero) hero.classList.add('is-in');
    renderer.render(scene, camera);
    window.addEventListener('resize', () => {
      placeCamera(1, 0, 0);
      renderer.render(scene, camera);
    });
    return;
  }

  /* ===================================================== the render loop */
  let last = performance.now();
  function frame(now) {
    const dt = Math.min((now - last) / 1000, 0.05);
    last = now;
    if (lenis) lenis.raf(now);

    const p = scrollProgress;
    const tSec = now * 0.001;

    // Smooth the pointer for a calm, lagging parallax.
    pointer.x += (pointer.tx - pointer.x) * Math.min(dt * 3, 1);
    pointer.y += (pointer.ty - pointer.y) * Math.min(dt * 3, 1);

    // Camera travels the curved path; even at p=0 the field drifts (alive).
    placeCamera(p, pointer.x, pointer.y);

    // Multi-layer parallax: each star layer drifts at its own rate. Near layers
    // move more, far layers barely — depth you feel as you travel.
    for (const L of starLayers) {
      L.grp.rotation.y += dt * L.rate;
      L.grp.rotation.x = Math.sin(now * 0.00004) * 0.03 * (L.rate * 20.0);
    }

    // Sun: animate the plasma + a very slow spin.
    sunSurfaceMat.uniforms.uTime.value = tSec;
    sun.rotation.y += dt * 0.02;

    // Horizon glow rises as we arrive at the finale.
    const arrive = EASE(Math.max(0, (p - 0.45) / 0.55));
    glowMat.uniforms.uTime.value = tSec;
    glowMat.uniforms.uGlow.value = 0.16 + arrive * 0.30; // restrained — deep space stays dark

    // The big "Sunday" + mission fade in near the close.
    if (hero) {
      if (p > 0.82) hero.classList.add('is-in');
      else hero.classList.remove('is-in');
    }

    starMat.uniforms.uTime.value = tSec;

    renderer.render(scene, camera);
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}
