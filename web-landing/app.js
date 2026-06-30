/* =========================================================================
   Sunday — a calm solar system you move through.

   A real Three.js cosmos: a deep starfield, a genuine procedural sun (animated
   plasma surface via fbm of 3D simplex noise, limb darkening, an additive
   corona and a few prominences licking off the limb), and 2–4 planets on slow
   tilted elliptical orbits. Lenis drives smooth scroll; scroll progress 0->1
   dollies the camera inward through the system toward the star.

   Robustness:
   - prefers-reduced-motion -> static (sun rendered, no churn/orbits/camera).
   - no WebGL              -> CSS deep-space fallback (class on <html>), no canvas.
   - mobile                -> fewer stars, simpler, no mouse parallax.

   Performance: one rAF loop, no per-frame allocations, capped DPR, modest sun
   geometry, fbm kept to 5 octaves on the surface / cheaper on the corona.
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
   Returns snoise(vec3) in roughly [-1, 1]. Shared by the sun surface, corona
   and prominence shaders. */
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

/* A soft radial-gradient glow sprite texture — white-hot center fading to warm
   transparent. Used for the sun's corona/halo: a billboard with this reads as a
   real glow, unlike a back-side fresnel sphere which only lights its silhouette
   (a hard hoop). */
function makeGlowTexture(THREE) {
  const s = 256;
  const cv = document.createElement('canvas');
  cv.width = cv.height = s;
  const ctx = cv.getContext('2d');
  const g = ctx.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2);
  g.addColorStop(0.00, 'rgba(255,247,225,1.0)');
  g.addColorStop(0.14, 'rgba(255,190,104,0.82)');
  g.addColorStop(0.40, 'rgba(240,122,40,0.30)');
  g.addColorStop(1.00, 'rgba(240,122,40,0.0)');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, s, s);
  return new THREE.CanvasTexture(cv);
}

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
  scene.fog = new THREE.FogExp2(0x05060b, 0.010);

  const camera = new THREE.PerspectiveCamera(
    58, window.innerWidth / window.innerHeight, 0.1, 3000
  );
  const CAM_START_Z = 130;
  const CAM_END_Z = 27;
  camera.position.set(0, 0, CAM_START_Z);

  const world = new THREE.Group();
  scene.add(world);

  const SUN_POS = new THREE.Vector3(0, 0, -30);
  const SUN_RADIUS = 7.2;

  /* ------------------------------------------------------- the starfield */
  const STAR_COUNT = isMobile ? 1800 : (prefersReduced ? 3200 : 4800);

  const positions = new Float32Array(STAR_COUNT * 3);
  const colors = new Float32Array(STAR_COUNT * 3);
  const sizes = new Float32Array(STAR_COUNT);
  const seeds = new Float32Array(STAR_COUNT);

  const cWhite = new THREE.Color(0xeef0f6);
  const cViolet = new THREE.Color(0x8e9bd6);
  const cAmber = new THREE.Color(0xf1b24a);

  for (let i = 0; i < STAR_COUNT; i++) {
    const i3 = i * 3;
    positions[i3] = (Math.random() - 0.5) * 340;
    positions[i3 + 1] = (Math.random() - 0.5) * 240;
    positions[i3 + 2] = -Math.random() * 720 + 70;

    const roll = Math.random();
    const c = roll > 0.92 ? cAmber : (roll > 0.6 ? cViolet : cWhite);
    const b = 0.55 + Math.random() * 0.45;
    colors[i3] = c.r * b; colors[i3 + 1] = c.g * b; colors[i3 + 2] = c.b * b;

    sizes[i] = 0.6 + Math.pow(Math.random(), 2) * 2.6;
    seeds[i] = Math.random() * Math.PI * 2;
  }

  const starGeo = new THREE.BufferGeometry();
  starGeo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  starGeo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
  starGeo.setAttribute('aSize', new THREE.BufferAttribute(sizes, 1));
  starGeo.setAttribute('aSeed', new THREE.BufferAttribute(seeds, 1));

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

  const stars = new THREE.Points(starGeo, starMat);
  world.add(stars);

  /* ================================================================ THE SUN */
  // The sun is its own group: a plasma sphere (ShaderMaterial), an additive
  // corona shell, and a thin band of prominences/flares at the limb.
  const sun = new THREE.Group();
  sun.position.copy(SUN_POS);
  world.add(sun);

  /* --- 1. Plasma surface ---------------------------------------------------
     fbm(4–5 oct) of 3D simplex sampled over the sphere surface (vPos*freq),
     with a slow time flow + a SECOND domain-warped layer so it churns/convects
     like granulation. The fbm value drives a solar color ramp (ember -> red-
     orange -> orange -> near-white-hot). Front-facing fresnel adds limb
     darkening so it reads spherical. A gentle pulse brightens the hot regions. */
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

  /* --- 2. Corona ------------------------------------------------------------
     A back-side sphere a bit larger than the sun, additive, warm, with a radial
     falloff toward the limb (fresnel) so it reads as a controlled glow rather
     than a giant fuzzy ball. Faint noise gives it living texture. */
  const coronaMat = new THREE.SpriteMaterial({
    map: makeGlowTexture(THREE),
    color: 0xffd9a0,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    opacity: 1.0,
  });
  const corona = new THREE.Sprite(coronaMat);
  corona.scale.set(SUN_RADIUS * 5.4, SUN_RADIUS * 5.4, 1);
  sun.add(corona);

  /* --- 3. Prominences / flares ---------------------------------------------
     A thin shell hugging the limb. The fragment shader keeps only the rim (a
     fresnel band) and modulates it with animated noise so warm wisps lick off
     the limb. Additive, sparse — driven by the same noise vocabulary. */
  const flareMat = new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    side: THREE.FrontSide,
    uniforms: {
      uTime: { value: 0 },
      uIntensity: { value: 1.0 },
      uChurn: { value: prefersReduced ? 0.0 : 1.0 },
    },
    vertexShader: /* glsl */`
      varying vec3 vNormal;
      varying vec3 vView;
      varying vec3 vPos;
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
      varying vec3 vNormal;
      varying vec3 vView;
      varying vec3 vPos;
      uniform float uTime;
      uniform float uIntensity;
      uniform float uChurn;

      ${SNOISE3}
      ${FBM3}

      void main() {
        // Keep only a band near the silhouette edge.
        float rim = 1.0 - abs(dot(vNormal, vView));
        float band = smoothstep(0.45, 0.95, rim);

        // Sparse, animated wisps: thresholded fbm so only a few prominences
        // appear at once and they wave over time.
        vec3 sp = normalize(vPos);
        float t = uTime * 0.18 * uChurn;
        float n = fbm(sp * 4.5 + vec3(t, t * 0.6, 0.0));
        float wisp = smoothstep(0.30, 0.85, n * 0.5 + 0.5);

        float a = band * wisp * uIntensity;
        vec3 flare = vec3(1.0, 0.45, 0.12) + vec3(0.0, 0.18, 0.0) * wisp;
        gl_FragColor = vec4(flare, a * 0.9);
      }
    `,
  });
  const flares = new THREE.Mesh(
    new THREE.SphereGeometry(SUN_RADIUS * 1.12, 48, 48),
    flareMat
  );
  sun.add(flares);

  /* A point light at the sun so the planets get a real light/dark terminator. */
  const sunLight = new THREE.PointLight(0xffd9a0, 2.4, 0, 2.0);
  sunLight.position.copy(SUN_POS);
  scene.add(sunLight);
  scene.add(new THREE.AmbientLight(0x223047, 0.35)); // faint fill so dark sides aren't pure black

  /* ============================================================== PLANETS */
  // 4 planets on slow, tilted elliptical orbits — distant, peaceful. Each is a
  // small lit sphere; one carries a thin ring. Faint orbit-path lines, very dim.
  const planets = [];
  const orbitGroup = new THREE.Group();
  orbitGroup.position.copy(SUN_POS);
  world.add(orbitGroup);

  const planetDefs = [
    { a: 16, ecc: 0.10, radius: 0.9, color: 0x9fb6d6, tilt: 0.18, speed: 0.085, phase: 0.4, ring: false }, // pale blue
    { a: 24, ecc: 0.16, radius: 1.3, color: 0xc97a55, tilt: -0.30, speed: 0.055, phase: 2.1, ring: true },  // rust + ring
    { a: 34, ecc: 0.08, radius: 0.7, color: 0xbfc3cb, tilt: 0.42, speed: 0.038, phase: 4.0, ring: false }, // soft grey
    { a: 44, ecc: 0.20, radius: 1.1, color: 0xc98b5a, tilt: -0.12, speed: 0.026, phase: 5.2, ring: false }, // warm rust
  ];

  for (const def of planetDefs) {
    const mesh = new THREE.Mesh(
      new THREE.SphereGeometry(def.radius, 32, 32),
      new THREE.MeshStandardMaterial({
        color: def.color,
        roughness: 0.95,
        metalness: 0.0,
      })
    );

    // Each planet rides its own tilted orbit plane (a group rotated on X+Z).
    const orbit = new THREE.Group();
    orbit.rotation.x = def.tilt;
    orbit.rotation.z = def.phase * 0.12;
    orbit.add(mesh);

    // Faint elliptical orbit-path line in the orbit plane (XZ).
    const segs = 96;
    const pts = new Float32Array((segs + 1) * 3);
    const b = def.a * Math.sqrt(1 - def.ecc * def.ecc); // semi-minor
    for (let s = 0; s <= segs; s++) {
      const th = (s / segs) * Math.PI * 2;
      pts[s * 3] = Math.cos(th) * def.a;
      pts[s * 3 + 1] = 0;
      pts[s * 3 + 2] = Math.sin(th) * b;
    }
    const lineGeo = new THREE.BufferGeometry();
    lineGeo.setAttribute('position', new THREE.BufferAttribute(pts, 3));
    const line = new THREE.Line(lineGeo, new THREE.LineBasicMaterial({
      color: 0x6a6f88, transparent: true, opacity: 0.10, depthWrite: false,
    }));
    orbit.add(line);

    // Optional thin ring (slightly tilted relative to its planet).
    if (def.ring) {
      const ring = new THREE.Mesh(
        new THREE.RingGeometry(def.radius * 1.5, def.radius * 2.3, 48),
        new THREE.MeshBasicMaterial({
          color: 0xd8c39a, transparent: true, opacity: 0.35,
          side: THREE.DoubleSide, depthWrite: false,
        })
      );
      ring.rotation.x = Math.PI / 2 - 0.4;
      mesh.add(ring);
    }

    orbitGroup.add(orbit);
    planets.push({ mesh, def, b, angle: def.phase });
  }

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

  /* ------------------------------------------- the quiet floating line reveal */
  // One short line floats in near the end of the journey (handled in CSS via
  // opacity tied to a class). It fades in around the close.
  const closeLine = document.querySelector('.scene-line');

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

  /* ===================================================== the render loop */
  let camZ = CAM_START_Z;
  const EASE = (t) => t * t * (3 - 2 * t);

  // Reduced motion: render one static, still-beautiful frame and stop.
  if (prefersReduced) {
    camera.position.z = CAM_START_Z * 0.62;
    camera.lookAt(0, 0, SUN_POS.z + 10);
    // Advance planets to a pleasing static arrangement.
    for (const pl of planets) {
      const a = pl.def.a, b = pl.b;
      pl.mesh.position.set(Math.cos(pl.angle) * a, 0, Math.sin(pl.angle) * b);
    }
    if (closeLine) closeLine.classList.add('is-in');
    sunSurfaceMat.uniforms.uTime.value = 12.0; // a frozen, detailed surface
    coronaMat.opacity = 1.4;
    flareMat.uniforms.uTime.value = 12.0;
    renderer.render(scene, camera);
    window.addEventListener('resize', () => {
      camera.lookAt(0, 0, SUN_POS.z + 10);
      renderer.render(scene, camera);
    });
    return;
  }

  let last = performance.now();
  function frame(now) {
    const dt = Math.min((now - last) / 1000, 0.05);
    last = now;
    if (lenis) lenis.raf(now);

    const p = scrollProgress;
    const tSec = now * 0.001;

    // --- camera dolly inward toward/around the sun (eased) ---
    const targetZ = CAM_START_Z + (CAM_END_Z - CAM_START_Z) * EASE(p);
    camZ += (targetZ - camZ) * Math.min(dt * 4, 1);

    pointer.x += (pointer.tx - pointer.x) * Math.min(dt * 3, 1);
    pointer.y += (pointer.ty - pointer.y) * Math.min(dt * 3, 1);

    // Drift the camera laterally as we descend so we arc around the system.
    const arc = Math.sin(p * Math.PI) * 10.0;
    camera.position.x = pointer.x * 8 + arc;
    camera.position.y = -pointer.y * 6 + Math.sin(p * Math.PI) * 4.0;
    camera.position.z = camZ;
    camera.lookAt(pointer.x * 3, -pointer.y * 2, SUN_POS.z + 8);

    // Slow ambient rotation of the star field.
    stars.rotation.y += dt * 0.012;
    stars.rotation.x = Math.sin(now * 0.00005) * 0.04;

    // --- sun: animate the shaders + a faint global rotation + brightness ramp.
    sunSurfaceMat.uniforms.uTime.value = tSec;
    flareMat.uniforms.uTime.value = tSec;
    sun.rotation.y += dt * 0.02; // very slow spin

    const arrive = EASE(Math.max(0, (p - 0.5) / 0.5));
    coronaMat.opacity = 0.9 + arrive * 0.85; // sprite glow ramps as we arrive
    corona.scale.setScalar(SUN_RADIUS * (5.2 + Math.sin(tSec * 0.5) * 0.45)); // gentle breath
    flareMat.uniforms.uIntensity.value = 0.7 + arrive * 0.6;
    sunLight.intensity = 2.0 + arrive * 1.4;

    // --- planets: advance along their tilted elliptical orbits.
    for (const pl of planets) {
      pl.angle += dt * pl.def.speed;
      const a = pl.def.a, b = pl.b;
      // Ellipse centered (good enough for a calm look); foci offset omitted.
      pl.mesh.position.set(Math.cos(pl.angle) * a, 0, Math.sin(pl.angle) * b);
      pl.mesh.rotation.y += dt * 0.2;
    }

    // World tilt for parallax depth.
    world.rotation.y = pointer.x * 0.04;
    world.rotation.x = pointer.y * 0.03;

    // Quiet floating line fades in near the end.
    if (closeLine) {
      if (p > 0.78) closeLine.classList.add('is-in');
      else closeLine.classList.remove('is-in');
    }

    starMat.uniforms.uTime.value = tSec;

    renderer.render(scene, camera);
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}
