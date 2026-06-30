# Sunday API

The **thin hosted backbone** for Sunday accounts. One small aiohttp service that:

- **Authenticates** the user via **WorkOS AuthKit** (magic link / Google — WorkOS
  hosts the sign-in UI).
- **Issues identity**: maps a WorkOS user → a stable Sunday account with a stable
  `agent_id` (relay identity), a `relay_token` (relay socket credential), and a
  `sunday_key` (this service's bearer credential).
- **Gateways the free model tier**: proxies `/v1/chat/completions` to OpenRouter
  on Sunday's master key and **meters** usage against a per-account budget.
- **Validates relay agents**: an internal endpoint the relay can call to replace
  trust-on-first-use enrollment with account-gating.

SQLite on a Fly volume at `/data/sunday.db` is the source of truth (`db.py`).
This service is the **outbound** mirror of the relay's **inbound** job: the relay
proxies inbound events *to* your agent; this proxies your agent's model calls
*out* through a Sunday plan. **Self-hosted Sunday never touches this** — it's BYO
relay + BYO model keys. The account/plan is the default, never a dependency.

See `docs/sunday-accounts-plan.md` for the architecture (note: auth is **WorkOS**,
the store is **SQLite on a volume**).

## Files

| File | Role |
| --- | --- |
| `server.py` | aiohttp app: auth handshake, `/account`, model gateway, relay validation |
| `db.py` | SQLite account + usage store (the only persistent state) |
| `Dockerfile`, `requirements.txt`, `fly.toml` | deploy |

## Routes

| Route | Auth | Purpose |
| --- | --- | --- |
| `GET /health` | — | `{"ok": true}` liveness probe |
| `GET /auth/start?cb=<loopback>` | — | 302 → WorkOS AuthKit (packs `cb` into `state`) |
| `GET /auth/callback?code&state` | — | exchange code, upsert account, 302 → `cb` with creds |
| `GET /account` | Bearer `sunday_key` | `{email, agent_id, plan:"free", used, limit}` |
| `POST /v1/chat/completions` | Bearer `sunday_key` | metered OpenRouter proxy (stream or buffered) |
| `POST /internal/validate-agent` | Bearer `INTERNAL_SECRET` | `{agent_id}` → `{ok:true}` or 404 |

## The daemon sign-in handshake (step by step)

This mirrors Sunday's existing Codex OAuth callback flow: the daemon opens a
browser and listens on a localhost callback for the result.

1. **Daemon** starts a tiny local HTTP server, e.g. `http://127.0.0.1:53112/cb`,
   then opens the user's browser at:
   `https://api.sunday.xyz/auth/start?cb=http://127.0.0.1:53112/cb`
2. **`/auth/start`** validates that `cb` is a loopback URL (open-redirect guard —
   a freshly minted `sunday_key` must never bounce off-box), packs `cb` into the
   OAuth `state`, and **302s the browser to WorkOS AuthKit**.
3. **WorkOS** authenticates the human (magic link / Google) and redirects the
   browser back to **this service's** registered `WORKOS_REDIRECT_URI`
   (`/auth/callback`) with `?code=...&state=<the daemon cb>`.
4. **`/auth/callback`** exchanges the `code` with WorkOS for the user (`id`,
   `email`), calls `db.upsert_account(...)` — minting `agent_id` / `relay_token` /
   `sunday_key` on first sign-in, **reusing** them on every later sign-in (this is
   what makes identity stable/recoverable across reinstalls) — and **302s the
   browser back to the daemon's `cb`** with the issued creds as query params:
   `http://127.0.0.1:53112/cb?agent_id=...&relay_token=...&sunday_key=...`
5. **Daemon's** local server reads those params, persists them (as today: into
   `relay.json` / `credentials.env`), and is now signed in: it uses `agent_id` +
   `relay_token` with the relay, and `sunday_key` with this service's gateway.

### WorkOS endpoints used (User Management / AuthKit REST API, plain httpx)

- **Authorize**: `GET https://api.workos.com/user_management/authorize`
  with `client_id`, `redirect_uri`, `response_type=code`, `provider=authkit`,
  `state`.
  ([docs](https://workos.com/docs/reference/user-management/authentication/get-authorization-url))
- **Code → token**: `POST https://api.workos.com/user_management/authenticate`
  with `grant_type=authorization_code`, `client_id`, `client_secret`
  (= `WORKOS_API_KEY`), `code` → response includes the `user` object
  (`id`, `email`, …).
  ([docs](https://workos.com/docs/reference/user-management/authentication/code))

## Model gateway

`POST /v1/chat/completions` with `Authorization: Bearer <sunday_key>`:

1. Resolve the account from the key (401 if unknown).
2. If this month's metered tokens (`tokens_in + tokens_out`) ≥ `FREE_TIER_TOKENS`
   → **`402 {"error":"free tier exhausted", ...}`**.
3. Otherwise proxy the body **verbatim** to
   `https://openrouter.ai/api/v1/chat/completions` with
   `Authorization: Bearer $OPENROUTER_API_KEY`.
4. On a 200, read the response's `usage` block (`prompt_tokens` /
   `completion_tokens`) and add it to the account's budget for the current
   `YYYY-MM` period.

**Streaming** (`stream:true`) is supported: the upstream SSE is passed through
byte-for-byte, and metering reads the `usage` block from the final stream chunk
(best-effort — to get it, ask OpenRouter to include usage, e.g.
`"stream_options":{"include_usage":true}`; without a usage chunk the call simply
isn't metered).

The metering period is the current calendar month (`YYYY-MM`, UTC). This is a
normal hosted service, so `datetime.now()` is the correct source for the window.

## Environment variables

| Var | Default | Purpose |
| --- | --- | --- |
| `WORKOS_API_KEY` | _(required)_ | WorkOS User Management secret; used as the `client_secret` on the token exchange |
| `WORKOS_CLIENT_ID` | _(required)_ | WorkOS environment client ID |
| `WORKOS_REDIRECT_URI` | _(required)_ | **this service's** `/auth/callback` URL; must be registered in the WorkOS dashboard Redirects |
| `OPENROUTER_API_KEY` | _(required)_ | Sunday's master OpenRouter key the free tier draws from |
| `INTERNAL_SECRET` | _(unset)_ | shared secret for `/internal/validate-agent`; unset → route refuses everything (fail closed) |
| `FREE_TIER_TOKENS` | `1000000` | per-account metered-token budget per month |
| `SUNDAY_DB` | `/data/sunday.db` | SQLite path (mount a volume in Docker/Fly) |
| `PORT` | `8080` | listen port (PaaS injects `PORT`) |
| `SUNDAY_HOST` | `0.0.0.0` | bind host |
| `WORKOS_API_BASE` | `https://api.workos.com` | override only for testing |
| `OPENROUTER_URL` | `https://openrouter.ai/api/v1/chat/completions` | upstream override |
| `SUNDAY_UPSTREAM_TIMEOUT` | `120` | upstream read timeout (s) for WorkOS/OpenRouter |

## Run locally

```bash
cd sunday-api
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export WORKOS_API_KEY=sk_test_...
export WORKOS_CLIENT_ID=client_...
export WORKOS_REDIRECT_URI=http://localhost:8080/auth/callback
export OPENROUTER_API_KEY=sk-or-...
export INTERNAL_SECRET=$(openssl rand -hex 32)
export SUNDAY_DB=./sunday.db          # local file instead of the volume path

python server.py                      # listens on :8080 (PORT to override)
```

Add `http://localhost:8080/auth/callback` to the WorkOS dashboard's **Redirects**
list, then drive the handshake by visiting
`http://localhost:8080/auth/start?cb=http://127.0.0.1:53112/cb` (with any
loopback `cb`). Hit the gateway with the issued `sunday_key`:

```bash
curl -s localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer $SUNDAY_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"openai/gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
```

## Deploy on Fly

`fly.toml` is production-shaped: a volume at `/data` for the SQLite DB and
`min_machines_running = 1` (single SQLite file → single machine).

```bash
cd sunday-api
fly launch --copy-config --no-deploy --name sunday-api
fly volumes create sunday_api_data --size 1 --region iad -a sunday-api
fly secrets set \
  WORKOS_API_KEY=sk_... \
  WORKOS_CLIENT_ID=client_... \
  WORKOS_REDIRECT_URI=https://sunday-api.fly.dev/auth/callback \
  OPENROUTER_API_KEY=sk-or-... \
  INTERNAL_SECRET=$(openssl rand -hex 32) \
  -a sunday-api
fly deploy -a sunday-api
```

Then register `https://sunday-api.fly.dev/auth/callback` in the WorkOS dashboard
Redirects list so the callback round-trip is allowed.

## How the daemon integrates

- **Sign in**: open `/auth/start?cb=<localhost callback>` in a browser, receive
  `agent_id` + `relay_token` + `sunday_key` back on the loopback callback,
  persist them. Re-signing in on a new machine returns the **same** creds.
- **Relay**: use `agent_id` + `relay_token` as the relay socket identity (today's
  `relay.json` / `credentials.env`), now account-issued instead of locally minted.
- **Model gateway**: point the "Sunday (free tier)" model provider at this
  service's `/v1/chat/completions`, passing `Authorization: Bearer <sunday_key>`.
  A `402` means the free budget is spent.
- **Account surface**: poll `GET /account` to show email, plan, and `used/limit`.
- **Relay (server side)**: the relay can call `POST /internal/validate-agent`
  (with `INTERNAL_SECRET`) to confirm an `agent_id` belongs to an account before
  enrolling its socket — replacing pure TOFU. Not wired into the relay yet.
