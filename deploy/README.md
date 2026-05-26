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
sunday.example.com { reverse_proxy localhost:8765 }
nango.example.com  { reverse_proxy localhost:3003 }
```

## Connecting integrations (Gmail, Calendar, …)

1. Open the Nango dashboard at `NANGO_PUBLIC_URL` (login from `.env`).
2. Grab the environment **secret key**, put it in `.env` as
   `NANGO_SECRET_KEY`, and `docker compose up -d sunday` to pick it up.
3. Add each provider as an integration in Nango (e.g. `google-mail`,
   `google-calendar`) with your own Google Cloud OAuth client id/secret.
4. In the Sunday app: Settings → Connections → Connect.

Sunday holds no provider OAuth secrets — Nango owns the apps and refreshes
tokens; Sunday calls provider APIs through Nango's proxy.

## Data

Per-user state (memory db, sessions, skills, credentials) lives in the
`sunday-data` volume at `/data`. Back it up (or point Litestream at the
sqlite files) and the stack is reproducible anywhere.
