# Sunday — landing page

The marketing site for **Sunday**, a personal AI with its own phone number, its
own voice, a unified inbox — that runs on **your own machine, not a company's cloud.**

It's an experiential, single-page site built on one idea: **a calm solar system you
move through.** The whole viewport is a fixed full-screen **WebGL canvas** rendering a
real **Three.js** star field over a jewel-dark void. Spare type floats over it, and
**scroll drives the camera and the particles**: as you scroll top→bottom the camera
dollies inward through the stars toward a warm glowing sun, and at the final beats the
scattered stars **swirl and coalesce into the sun** — arriving somewhere peaceful.
The sun is Sunday.

Static site — plain HTML, CSS, and a vanilla ES-module. **No build step, no framework.**
Three.js and Lenis load from a CDN via an import map; nginx just serves the files.

```
web-landing/
├── index.html     # semantic copy beats, the fixed canvas, fonts + import map
├── styles.css     # jewel-dark palette, type, beat reveals, fallbacks
├── app.js         # Three.js cosmos + Lenis smooth scroll + scroll choreography
├── Dockerfile     # nginx:alpine serving the static files
├── fly.toml       # Fly.io deploy (app: sunday-web)
└── README.md
```

## The scene (Three.js)

- **Starfield:** ~5,200 stars (2,000 on mobile) as a single `THREE.Points` over one
  `BufferGeometry`. A small `ShaderMaterial` draws each star as a soft round sprite
  (no texture fetch) with per-star size variation and a slow GPU twinkle. Cool whites,
  faint violets, a sprinkle of warm amber. Slow ambient rotation/drift; `FogExp2` for
  depth.
- **The sun:** a warm core `SphereGeometry` plus an **additive-blended fresnel glow
  shell** (back-side sphere, limb-brightened in the fragment shader) and a broad
  additive billboard `Sprite` haze (radial-gradient canvas texture) — a tasteful
  bloom-like glow with no postprocessing.
- **Scroll choreography:** **Lenis** drives smooth scroll and emits a 0→1 progress.
  That progress eases the **camera dolly** (z 120 → 26, inward through the stars) and,
  past ~55%, the **gather**: each star lerps from its scattered home position toward a
  point on a thin disc around the sun, with a per-star swirl so it spirals in. The sun
  glow + haze brighten as you arrive. The sun gently pulses. Only buffer writes and
  transforms per frame — buffers are pre-allocated, one `BufferGeometry`, one rAF loop,
  no per-frame allocations.
- **Mouse parallax:** the pointer gently offsets the camera and tilts the world group
  (smoothed) for a premium sense of depth. Disabled on mobile / reduced motion.

## Type & palette

- **Display:** [Space Grotesk](https://fonts.google.com/specimen/Space+Grotesk) — clean
  geometric grotesk for headlines.
- **Body / UI / labels:** [Inter](https://fonts.google.com/specimen/Inter).
- **Palette:** space near-black (`#05060b`) deepening to indigo/violet in the void
  (`#141632`), soft cream ink (`#eef0f6`), and **one** warm accent — an amber/gold sun
  (`#f1b24a`), used for the sun and sparingly (a hover, the CTA, a hairline).

Both families load from Google Fonts via one `<link>`. Three.js + Lenis load from
jsDelivr via the import map in `index.html`.

## The copy (in order)

1. Eyebrow: *A personal AI* · **A personal AI that's actually yours.** · *Not an
   assistant you rent by the seat. One that lives with you.*
2. **It has its own number. You text it like a person.** · *No app to open. No prompt
   box. Just a thread, the way you talk to anyone.*
3. **Its own voice, too. It can pick up the phone.** · *When something needs a real
   call, it makes one — in a voice that's its own.*
4. **Everything lands in one inbox.** · *Messages, mail, calls — gathered into a single
   place that's quiet by default.*
5. **It runs on your machine — not our cloud.** · *Yours stays yours. The whole thing
   lives on the computer in front of you.*
6. **Sunday.** · *Yours. On your machine. In its own voice.* + **Download for Mac** /
   *Sign in*

## Robustness

- **Reduced motion** (`prefers-reduced-motion`): renders a single **static starfield**
  frame — no camera motion, no gather, no twinkle — with all copy visible and legible.
- **No WebGL:** a capability check adds `.no-webgl` to `<html>`; the canvas is hidden
  and a tasteful CSS deep-space fallback (radial dark + a few CSS stars + a soft sun)
  shows behind the fully-visible copy.
- **Mobile:** fewer particles (~2,000), capped pixel ratio, no mouse parallax, no
  antialias — still feels like space, holds frame rate.
- **No JS / CDN failure:** `<html>` only gets `.js` once the module runs, so without it
  every line is visible; if Lenis fails to load, the scene falls back to native
  scroll-progress.
- **Accessible:** semantic landmarks, real DOM text + CTAs (canvas is `aria-hidden`),
  `:focus-visible`, high contrast. The canvas warms up after the text paints.

## Run locally

```bash
cd web-landing
python3 -m http.server 8080
# → http://localhost:8080
```

Or with Docker, exactly as it ships:

```bash
cd web-landing
docker build -t sunday-web .
docker run --rm -p 8080:80 sunday-web
# → http://localhost:8080
```

## Deploy to Fly

App name is **`sunday-web`** (set in `fly.toml`). nginx listens on port 80 and Fly
forces HTTPS at the edge.

```bash
cd web-landing
fly deploy
```

## CTAs

- **Download for Mac** → `https://github.com/mylesndavid/sunday/releases/latest`
- **Sign in** → `https://smooth-light-56-staging.authkit.app`
  (placeholder WorkOS AuthKit **staging** URL — swap for the production dashboard URL
  when it's ready; see the comments in `index.html`).
