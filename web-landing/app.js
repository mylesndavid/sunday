/* =========================================================================
   Sunday — a calm solar system you move through.

   A real Three.js cosmos: ~5,000 stars in a deep volume, a warm glowing sun
   with an additive glow halo, and a scroll choreography that dollies the camera
   inward through the stars while the scattered stars swirl + coalesce into the
   sun. Lenis drives smooth scroll; scroll progress 0->1 maps to camera + gather.

   Robustness:
   - prefers-reduced-motion -> static starfield, no camera motion, no gather.
   - no WebGL              -> CSS deep-space fallback (class on <html>), no canvas.
   - mobile                -> fewer particles, no mouse parallax.
   ========================================================================= */

const html = document.documentElement;
html.classList.add('js'); // enables JS-driven entrances + reveals

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

/* If no WebGL: show the CSS fallback sky, still reveal all copy, and stop. */
if (!webglOK()) {
  html.classList.add('no-webgl');
  revealAllBeats();
} else {
  boot();
}

/* All copy visible immediately (reduced-motion / fallback path uses this). */
function revealAllBeats() {
  document.querySelectorAll('.beat-inner').forEach((el) => el.classList.add('is-in'));
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
  renderer.setClearColor(0x000000, 0); // CSS gradient shows through
  const DPR = Math.min(window.devicePixelRatio || 1, isMobile ? 1.5 : 2);
  renderer.setPixelRatio(DPR);

  const scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x05060b, 0.012);

  const camera = new THREE.PerspectiveCamera(
    60, window.innerWidth / window.innerHeight, 0.1, 2000
  );
  // Camera starts far back among the stars; scroll dollies it inward.
  const CAM_START_Z = 120;
  const CAM_END_Z = 26;
  camera.position.set(0, 0, CAM_START_Z);

  /* A wrapper group we tilt for mouse parallax (cheaper than moving camera). */
  const world = new THREE.Group();
  scene.add(world);

  /* ------------------------------------------------------- the starfield */
  const STAR_COUNT = isMobile ? 2000 : (prefersReduced ? 3500 : 5200);

  // Two parallel buffers: the star's scattered "home" position, and the sun's
  // pull target. We lerp live positions between them by the gather amount.
  const homePos = new Float32Array(STAR_COUNT * 3);   // scattered positions
  const targetPos = new Float32Array(STAR_COUNT * 3); // swirl-into-sun targets
  const livePos = new Float32Array(STAR_COUNT * 3);   // what we render
  const colors = new Float32Array(STAR_COUNT * 3);
  const sizes = new Float32Array(STAR_COUNT);
  const seeds = new Float32Array(STAR_COUNT);         // per-star phase for twinkle

  const SUN_POS = new THREE.Vector3(0, 0, -30); // the sun sits ahead of camera

  // Palette for stars: cool whites and faint violets, a few warm amber ones.
  const cWhite = new THREE.Color(0xeef0f6);
  const cViolet = new THREE.Color(0x8e9bd6);
  const cAmber = new THREE.Color(0xf1b24a);

  const tmp = new THREE.Vector3();
  for (let i = 0; i < STAR_COUNT; i++) {
    const i3 = i * 3;

    // Scatter in a deep box volume, denser toward the middle depth.
    const x = (Math.random() - 0.5) * 320;
    const y = (Math.random() - 0.5) * 220;
    const z = -Math.random() * 700 + 60; // spread from near to deep
    homePos[i3] = x; homePos[i3 + 1] = y; homePos[i3 + 2] = z;
    livePos[i3] = x; livePos[i3 + 1] = y; livePos[i3 + 2] = z;

    // Gather target: a point on a thin shell/disc around the sun. Stars spiral
    // into a glowing cloud near the sun rather than collapsing to one point.
    const a = Math.random() * Math.PI * 2;
    const r = 4 + Math.pow(Math.random(), 0.6) * 22;
    tmp.set(
      SUN_POS.x + Math.cos(a) * r,
      SUN_POS.y + Math.sin(a) * r * 0.7,
      SUN_POS.z + (Math.random() - 0.5) * 14
    );
    targetPos[i3] = tmp.x; targetPos[i3 + 1] = tmp.y; targetPos[i3 + 2] = tmp.z;

    // Colour: mostly white, some violet, a sprinkle warm.
    const roll = Math.random();
    const c = roll > 0.92 ? cAmber : (roll > 0.6 ? cViolet : cWhite);
    // Slight per-star brightness variation.
    const b = 0.55 + Math.random() * 0.45;
    colors[i3] = c.r * b; colors[i3 + 1] = c.g * b; colors[i3 + 2] = c.b * b;

    sizes[i] = 0.6 + Math.pow(Math.random(), 2) * 2.6;
    seeds[i] = Math.random() * Math.PI * 2;
  }

  const starGeo = new THREE.BufferGeometry();
  starGeo.setAttribute('position', new THREE.BufferAttribute(livePos, 3));
  starGeo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
  starGeo.setAttribute('aSize', new THREE.BufferAttribute(sizes, 1));
  starGeo.setAttribute('aSeed', new THREE.BufferAttribute(seeds, 1));

  // Soft round star sprite drawn in the fragment shader (no texture fetch).
  const starMat = new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    vertexColors: true, // makes Three inject `attribute vec3 color;` — without
                        // this the vertex shader's `vColor = color;` fails to
                        // compile and the entire starfield silently disappears.
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
        // Slow twinkle: vary size/brightness on a per-star phase.
        float tw = 0.7 + 0.3 * sin(uTime * 0.8 + aSeed) * uTwinkle;
        vTw = tw;
        vec4 mv = modelViewMatrix * vec4(position, 1.0);
        gl_Position = projectionMatrix * mv;
        // Perspective-correct point size, scaled by twinkle.
        gl_PointSize = aSize * tw * 90.0 * uPixelRatio / -mv.z;
      }
    `,
    fragmentShader: /* glsl */`
      varying vec3 vColor;
      varying float vTw;
      void main() {
        // Soft round falloff.
        vec2 d = gl_PointCoord - vec2(0.5);
        float dist = dot(d, d);
        float alpha = smoothstep(0.25, 0.0, dist);
        gl_FragColor = vec4(vColor * (0.8 + vTw * 0.4), alpha);
      }
    `,
  });

  const stars = new THREE.Points(starGeo, starMat);
  world.add(stars);

  /* --------------------------------------------------------------- the sun */
  // Warm core sphere.
  const sunCore = new THREE.Mesh(
    new THREE.SphereGeometry(5.2, 48, 48),
    new THREE.MeshBasicMaterial({ color: 0xfff1d6 }) // hot white-amber, crisp
  );
  sunCore.position.copy(SUN_POS);
  world.add(sunCore);

  // Additive glow shell — a slightly larger sphere with a fresnel-ish falloff,
  // additive blended so it reads as bloom without postprocessing.
  const sunGlow = new THREE.Mesh(
    new THREE.SphereGeometry(8.6, 48, 48),
    new THREE.ShaderMaterial({
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      side: THREE.BackSide,
      uniforms: {
        uColor: { value: new THREE.Color(0xf1b24a) },
        uIntensity: { value: 1.0 },
      },
      vertexShader: /* glsl */`
        varying vec3 vNormal;
        varying vec3 vView;
        void main() {
          vNormal = normalize(normalMatrix * normal);
          vec4 mv = modelViewMatrix * vec4(position, 1.0);
          vView = normalize(-mv.xyz);
          gl_Position = projectionMatrix * mv;
        }
      `,
      fragmentShader: /* glsl */`
        varying vec3 vNormal;
        varying vec3 vView;
        uniform vec3 uColor;
        uniform float uIntensity;
        void main() {
          // Stronger toward the limb -> halo glow.
          float f = pow(1.0 - abs(dot(vNormal, vView)), 1.7);
          gl_FragColor = vec4(uColor, f * uIntensity);
        }
      `,
    })
  );
  sunGlow.position.copy(SUN_POS);
  world.add(sunGlow);

  // A big soft billboard sprite behind everything for the broad outer haze.
  const haze = new THREE.Sprite(new THREE.SpriteMaterial({
    map: makeGlowTexture(THREE),
    color: 0xf1b24a,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    opacity: 0.0, // fades in as we approach
  }));
  haze.scale.set(62, 62, 1);
  haze.position.copy(SUN_POS);
  world.add(haze);

  /* ----------------------------------------------------- scroll via Lenis */
  let scrollProgress = 0; // 0 top -> 1 bottom
  let lenis = null;

  if (!prefersReduced) {
    try {
      const Lenis = (await import('lenis')).default;
      lenis = new Lenis({
        lerp: 0.09,
        wheelMultiplier: 1,
        smoothWheel: true,
      });
      lenis.on('scroll', ({ scroll, limit }) => {
        scrollProgress = limit > 0 ? Math.min(scroll / limit, 1) : 0;
      });
    } catch (e) {
      lenis = null; // fall back to native scroll progress below
    }
  }

  // Native progress fallback (reduced motion, or Lenis failed to load).
  function nativeProgress() {
    const max = document.documentElement.scrollHeight - window.innerHeight;
    return max > 0 ? Math.min(window.scrollY / max, 1) : 0;
  }
  if (!lenis) {
    scrollProgress = nativeProgress();
    window.addEventListener('scroll', () => { scrollProgress = nativeProgress(); }, { passive: true });
  }

  /* -------------------------------------------------- beat reveal (IO) */
  const beats = document.querySelectorAll('.beat-inner');
  if (prefersReduced) {
    revealAllBeats();
  } else {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((en) => { if (en.isIntersecting) en.target.classList.add('is-in'); });
    }, { threshold: 0.35 });
    beats.forEach((b) => io.observe(b));
    // Hero in immediately so first paint isn't blank.
    if (beats[0]) beats[0].classList.add('is-in');
  }

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
  const posAttr = starGeo.getAttribute('position');
  let gather = 0;            // smoothed gather amount 0->1
  let camZ = CAM_START_Z;    // smoothed camera depth
  const EASE = (t) => t * t * (3 - 2 * t); // smoothstep

  // Reduced motion: render one static frame and stop (no camera/gather motion).
  if (prefersReduced) {
    camera.position.z = CAM_START_Z * 0.7;
    haze.material.opacity = 0.12;
    renderer.render(scene, camera);
    // Keep a tiny static render on resize so it stays crisp.
    window.addEventListener('resize', () => renderer.render(scene, camera));
    return;
  }

  let last = performance.now();
  function frame(now) {
    const dt = Math.min((now - last) / 1000, 0.05);
    last = now;
    if (lenis) lenis.raf(now);

    const p = scrollProgress;

    // --- camera dolly inward through the stars (eased) ---
    const targetZ = CAM_START_Z + (CAM_END_Z - CAM_START_Z) * EASE(p);
    camZ += (targetZ - camZ) * Math.min(dt * 4, 1);

    // Mouse parallax: smooth the pointer + gently offset the camera / tilt world.
    pointer.x += (pointer.tx - pointer.x) * Math.min(dt * 3, 1);
    pointer.y += (pointer.ty - pointer.y) * Math.min(dt * 3, 1);
    camera.position.x = pointer.x * 8;
    camera.position.y = -pointer.y * 6;
    camera.position.z = camZ;
    camera.lookAt(pointer.x * 3, -pointer.y * 2, SUN_POS.z + 10);

    // --- the gather: stars swirl + coalesce into the sun over the last beats ---
    // Begins ~55% scroll, completes by the end. Eased.
    const gTarget = EASE(Math.max(0, (p - 0.55) / 0.45));
    gather += (gTarget - gather) * Math.min(dt * 2.2, 1);

    // Slow ambient rotation of the whole field, plus extra swirl as we gather.
    stars.rotation.y += dt * (0.012 + gather * 0.10);
    stars.rotation.x = Math.sin(now * 0.00005) * 0.04;

    // Lerp each star between its home and its sun-target by `gather`, with a
    // little per-star swirl so it spirals in rather than sliding straight.
    if (gather > 0.001) {
      const arr = posAttr.array;
      const swirl = gather * 0.9;
      for (let i = 0; i < STAR_COUNT; i++) {
        const i3 = i * 3;
        const g = Math.min(gather * (0.7 + seeds[i] * 0.05 / Math.PI), 1);
        const hx = homePos[i3], hy = homePos[i3 + 1], hz = homePos[i3 + 2];
        const tx = targetPos[i3], ty = targetPos[i3 + 1], tz = targetPos[i3 + 2];
        // base interpolation
        let x = hx + (tx - hx) * g;
        let y = hy + (ty - hy) * g;
        let z = hz + (tz - hz) * g;
        // add swirl around the sun axis as they near it
        const ang = seeds[i] + now * 0.0006 * swirl;
        const rad = swirl * 6.0 * g;
        x += Math.cos(ang) * rad;
        y += Math.sin(ang) * rad;
        arr[i3] = x; arr[i3 + 1] = y; arr[i3 + 2] = z;
      }
      posAttr.needsUpdate = true;
    } else if (posAttr._dirty) {
      // restore home positions when scrolling back up
      posAttr.array.set(homePos);
      posAttr.needsUpdate = true;
      posAttr._dirty = false;
    }
    if (gather > 0.001) posAttr._dirty = true;

    // Sun grows brighter / haze blooms as we arrive.
    const arrive = EASE(Math.max(0, (p - 0.6) / 0.4));
    sunGlow.material.uniforms.uIntensity.value = 0.7 + arrive * 0.9 + gather * 0.5;
    haze.material.opacity = 0.05 + arrive * 0.35 + gather * 0.2;
    const sunPulse = 1 + Math.sin(now * 0.0009) * 0.015;
    sunCore.scale.setScalar(sunPulse);
    sunGlow.scale.setScalar(sunPulse);

    // World tilt for parallax depth.
    world.rotation.y = pointer.x * 0.04;
    world.rotation.x = pointer.y * 0.03;

    starMat.uniforms.uTime.value = now * 0.001;

    renderer.render(scene, camera);
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}

/* Small radial-gradient glow texture for the outer haze sprite. */
function makeGlowTexture(THREE) {
  const size = 128;
  const c = document.createElement('canvas');
  c.width = c.height = size;
  const ctx = c.getContext('2d');
  const g = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  g.addColorStop(0.0, 'rgba(255,255,255,1)');
  g.addColorStop(0.2, 'rgba(246,200,115,0.8)');
  g.addColorStop(0.5, 'rgba(241,178,74,0.25)');
  g.addColorStop(1.0, 'rgba(241,178,74,0)');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, size, size);
  const tex = new THREE.CanvasTexture(c);
  tex.minFilter = THREE.LinearFilter;
  return tex;
}
