# Sunday — landing page

The marketing site for **Sunday**, your personal AI with its own number, inbox, and voice.

Static site — plain HTML, CSS, and a little vanilla JS. **No build step, no framework.**

```
web-landing/
├── index.html     # the page
├── styles.css     # all styling
├── app.js         # nav state + scroll reveals (IntersectionObserver)
├── Dockerfile     # nginx:alpine serving the static files
├── fly.toml       # Fly.io deploy (app: sunday-web)
└── README.md
```

## Run locally

It's just static files, so any static server works:

```bash
# Python (no install needed)
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

You can also just open `index.html` directly in a browser — the only network
dependency is the Inter font from Google Fonts.

## Deploy to Fly

App name is **`sunday-web`** (set in `fly.toml`). nginx listens on port 80 and
Fly forces HTTPS at the edge (`force_https = true`).

```bash
cd web-landing
fly launch --no-deploy   # first time only, if the app doesn't exist yet
fly deploy
```

## CTAs

- **Download for Mac** → `https://github.com/mylesndavid/sunday/releases/latest`
- **Sign in** → `https://smooth-light-56-staging.authkit.app`
  (placeholder WorkOS AuthKit **staging** URL — swap for the production
  dashboard / prod WorkOS URL when it's ready; see comments in `index.html`).
