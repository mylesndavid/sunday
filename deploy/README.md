# Deploying Sunday

Sunday's whole server side is one compose stack. A server's only
requirement is Docker — everything else (the agent daemon, Nango for
integrations, its Postgres + Redis) comes up together.

## Quick start

```bash
cd deploy
cp .env.example .env
# fill in OPENROUTER_API_KEY / OPENAI_API_KEY and a NANGO_ENCRYPTION_KEY
#   openssl rand -base64 32
docker compose up -d
```

- Sunday → `http://<host>:8765` (point the desktop app here, or front it
  with your reverse proxy on a hostname).
- Nango → `http://<host>:3003`. Put a public hostname in front of it
  (`NANGO_PUBLIC_URL`) so OAuth providers can redirect back.

## Reverse proxy (Caddy example)

```
sunday.example.com  { reverse_proxy localhost:8765 }   # the app
nango.example.com   { reverse_proxy localhost:3003 }   # Nango API + OAuth callback
connect.example.com { reverse_proxy localhost:3009 }   # Nango Connect UI
```

## Connecting integrations (Gmail, Calendar, …) — all env-driven

No Nango dashboard needed (its self-hosted dashboard is built for Nango
Cloud login). Integrations are provisioned from env on startup:

1. Create the OAuth client **once** in the provider's console. For Google:
   enable the Gmail + Calendar APIs, make an OAuth 2.0 **Web** client, and
   set the redirect URI to `<NANGO_PUBLIC_URL>/oauth/callback`.
2. Put it in `.env` (`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`) and
   `docker compose up -d sunday`. On boot Sunday creates the matching Nango
   integrations automatically (`POST /integrations`).
3. In the Sunday app: Settings → Connections → **Connect** → approve in the
   browser (Nango's Connect UI at `NANGO_CONNECT_URL`).

Sunday holds no provider OAuth secrets — Nango owns the apps and refreshes
tokens; Sunday calls provider APIs through Nango's proxy.

## Data

Per-user state (memory db, sessions, skills, credentials) lives in the
`sunday-data` volume at `/data`. Back it up (or point Litestream at the
sqlite files) and the stack is reproducible anywhere.
