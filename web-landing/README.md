# Sunday — landing page

The marketing site for **Sunday**, a personal AI with its own phone number, its
own email, its own voice — that runs on **your own machine, not a company's cloud.**

It's an experiential, single-page site built on one idea: **a day with Sunday.**
The sun arcs across the sky as you scroll — low and pre-dawn at the top, risen by
mid-page, set by the bottom — and Sunday's story is told in spare vignettes timed
to it. The background tints subtly through the day (pre-dawn → warm day → dusk →
night). Type and space carry the design; the sun is the only accent.

**The beats, in order:**

1. **Hero (pre-dawn)** — the word *Sunday* in big optical serif, one honest line.
2. **She has her own phone number** — a sparse, type-driven text exchange.
3. **So she makes the call** — a real voice on a real line, while you're away.
4. **It all comes home to one inbox** — a restrained ledger, unread dots.
5. **And she lives on your machine** — the conviction: local, yours, not our cloud.
6. **Close (night)** — the sun sets, one final line, a single CTA + a quiet sign-in.

Static site — plain HTML, CSS, and vanilla JS. **No build step, no framework.**
Motion libraries load from a CDN; nginx just serves the files.

```
web-landing/
├── index.html     # the page (semantic landmarks, the sun, the six beats)
├── styles.css     # all styling — warm near-monochrome, the day phases, the sun
├── app.js         # Lenis smooth scroll + GSAP/ScrollTrigger: the sun's arc,
│                  # the background tint, and quiet section reveals
├── Dockerfile     # nginx:alpine serving the static files
├── fly.toml       # Fly.io deploy (app: sunday-web)
└── README.md
```

## Type & palette

- **Display:** [Fraunces](https://fonts.google.com/specimen/Fraunces) — a warm,
  optical serif that suits the name *Sunday*.
- **Labels / UI / eyebrows:** [JetBrains Mono](https://fonts.google.com/specimen/JetBrains+Mono).
- **Palette:** warm near-monochrome — a warm off-black base, bone/cream ink, and
  **one** accent: a dusty gold/amber sun, used sparingly (the disc, a hover, a rule).

Both font families load from Google Fonts (the only required network dependency for
the look). Motion libraries (Lenis, GSAP, ScrollTrigger) load from jsDelivr.

## The interaction

`app.js` wires **Lenis** (buttery smooth scroll) into **GSAP ScrollTrigger**. A
single scrubbed trigger spanning the whole document drives two things off scroll
progress: the **sun's position** (an inverted-parabola arc — low, high, low — with a
slight scale-up near the horizon) and the **background tint** (interpolated between
the warm day-phase colours). Section content eases in once via `ScrollTrigger.batch`.
Only `transform` and `opacity` are animated, for 60fps.

**Reduced motion:** with `prefers-reduced-motion: reduce` (or if the CDN libs fail
to load), the JS bails early — the sun simply sits low and warm, the background is a
single calm tone, and all content is fully visible. No drift, no reveals, no jank.

**Mobile:** the pinning degrades to a clean vertical sequence — each beat is its own
full-height panel, the sun is smaller, and the ledger reflows to two columns.

## Run locally

It's just static files, so any static server works:

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
