# Sunday Relay

A thin, **stateless, dumb socket-broker** that gives every Sunday daemon a
public URL for inbound webhooks — without DNS, Tailscale Funnel, Caddy, or
port-forwarding. The daemon dials *out* over one persistent WebSocket; the relay
forwards inbound HTTP down that socket and relays the daemon's reply back to the
provider. (See `docs/relay-and-inbox-spec.md` §2/§3 for the full design.)

The relay is the **only** hosted component in Sunday and deliberately the least
powerful one:

- **Stateless** — no agent state, no brain, no memory, no message store.
- **Payload-blind** — inbound bodies are forwarded *opaquely*, never parsed.
- **Capability-poor** — a relay breach leaks routing, not capability.

It is also **BYO-able**: self-hosting it is "deploy this, set a URL," not "stand
up a copy of Sunday." Sunday's hosted relay is the default, never a dependency.

## Files

| File | Role |
| --- | --- |
| `server.py` | aiohttp app: `/ws`, `/u/{agent_id}/{slug}`, `/health`, `/admin/register` |
| `registry.py` | `agent_id -> {token, per-slug auth}` — the only persistent state |
| `agents.example.json` | example registry; copy to `agents.json` |
| `Dockerfile`, `requirements.txt` | deploy |

## Wire protocol (must match the daemon's relay client exactly)

The daemon dials in to `GET /ws` and speaks JSON frames:

```jsonc
// daemon -> relay, first frame on connect (authenticated):
{ "type": "hello", "agent_id": "<id>", "token": "<socket-token>", "version": 1 }

// relay -> daemon, on hello accept:
{ "type": "welcome", "agent_id": "<id>" }

// relay -> daemon, forwarding a public webhook (body is OPAQUE):
{ "type": "webhook", "id": "<req-id>", "slug": "<slug>", "method": "POST",
  "path": "/webhooks/<slug>", "headers": { ... }, "body": "<raw string>" }

// daemon -> relay, ack (carries the loopback response):
{ "type": "ack", "id": "<req-id>", "status": 200, "body": "<raw string>" }

// liveness, either direction, ~every 20s:
{ "type": "ping" }   { "type": "pong" }
```

Routes:

- **`GET /ws`** — daemon WebSocket. First frame must be a valid `hello`; the
  socket is unmapped (and can receive nothing) until it authenticates.
- **`POST /u/{agent_id}/{slug}`** — the public webhook URL providers POST to.
  `{slug}` may be multi-segment (e.g. `hook/cold-email-replies`). Returns the
  daemon's real status + body, so a relay-delivered webhook is indistinguishable
  from a Funnel-delivered one.
- **`GET /health`** — `{"ok": true}`.
- **`POST /admin/register`** — `ADMIN_TOKEN`-gated daemon self-registration.

Public URL form: `https://relay.sunday.xyz/u/<agent-id>/<slug>`

## Auth & registration

**Two separate credentials** (spec §3.2), and keeping them separate is the whole
security story:

1. **Socket token** (`token`) — authenticates the daemon's WebSocket on `hello`
   ("I am agent X's daemon"). Verified constant-time against the registry.
2. **Webhook authorization** — the unguessable `agent_id` in the public URL is
   the coarse gate; an **optional per-slug token** (`?token=` or `X-Relay-Token`)
   and an **optional HMAC** (for providers that sign — Stripe/AgentMail) are the
   fine gate. Compromising a public URL never grants the socket, and vice-versa.

**The daemon mints both credentials** — a 256-bit `agent_id` and a 256-bit
`token` — on first relay-enable, and persists them (`agent_id` in
`~/.sunday/relay.json`, `token` in `~/.sunday/credentials.env`). The relay never
mints secrets — that keeps secret generation on the user's machine.

**Enrollment is trust-on-first-use (the default — nothing to configure).** The
daemon just *connects*. On the first `hello` for an `agent_id` the relay has
never seen, the relay enrolls it (binds that `agent_id` → `token`) and accepts.
Every later connect must present the same bound `token`. This is what makes both
Sunday's hosted relay **and** a BYO relay work by *pointing a URL and toggling
on* — no admin token on the daemon, no registration call, no `agents.json` edit.

It's safe because `agent_id` is an unguessable 256-bit value: you can't claim an
id you can't guess, and the bound token gates every subsequent connect. The only
residual abuse vector — enrollment *spam* from one source — is rate-limited
(`RELAY_ENROLL_MAX_PER_IP` new enrollments per `RELAY_ENROLL_WINDOW`, and a
global `RELAY_ENROLL_MAX_AGENTS` cap). Reconnects of known agents are never
limited. When Sunday grows an account system, enrollment can be tied to identity;
until then, TOFU + rate-limit is the pragmatic, standard posture.

**Optional explicit registration** (for management, pre-provisioning, or
per-slug auth) still exists:

- `POST /admin/register` with `Authorization: Bearer <ADMIN_TOKEN>` and body
  `{"agent_id": "...", "token": "...", "slugs": {...}}`. Idempotent. Returns 404
  when `RELAY_ADMIN_TOKEN` is unset.
- Hand-edit `agents.json` (see `agents.example.json`) — e.g. to attach per-slug
  tokens/HMAC to an already-TOFU-enrolled agent.

Per-slug authorization in `agents.json`:

```jsonc
"slugs": {
  "coldhook": { "token": "per_hook_rotatable_secret" },     // shared-token gate
  "stripe":   { "hmac_secret": "whsec_...", "provider": "stripe" } // HMAC gate
}
```

No per-slug config -> the unguessable path is the gate (allow). HMAC
canonicalization for non-trivial providers (Stripe's `t=...,v1=...` shape) is a
clearly-marked hook in `registry._verify_hmac`.

## Run locally

```bash
cd relay-service
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Optional: enable self-registration.
export RELAY_ADMIN_TOKEN=dev-admin-token

python server.py            # listens on :8787 (PORT or RELAY_PORT to override)
```

Register an agent and drive a webhook end-to-end:

```bash
# 1) register (or hand-edit agents.json instead):
curl -s -X POST localhost:8787/admin/register \
  -H "Authorization: Bearer dev-admin-token" \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"abc123","token":"socket-secret"}'

# 2) (the daemon's relay client connects ws://localhost:8787/ws and sends hello)

# 3) a provider POSTs the public URL; the relay forwards it down the socket:
curl -s -X POST "localhost:8787/u/abc123/agentmail" \
  -H "Content-Type: application/json" \
  -d '{"event":"inbound","msg":"hi"}'
# -> returns whatever the daemon's /webhooks/agentmail handler replied
# -> 503 if the daemon isn't connected; 504 if it doesn't ack in time
```

## Environment variables

| Var | Default | Purpose |
| --- | --- | --- |
| `PORT` / `RELAY_PORT` | `8787` | listen port (PaaS injects `PORT`) |
| `RELAY_HOST` | `0.0.0.0` | bind host |
| `RELAY_AGENTS_FILE` | `agents.json` | registry path (mount a volume in Docker) |
| `RELAY_ADMIN_TOKEN` | _(unset)_ | gates `/admin/register`; unset = route 404 |
| `RELAY_ACK_TIMEOUT` | `25` | seconds to wait for the daemon's ack -> 504 |
| `RELAY_PING_INTERVAL` | `20` | liveness ping cadence (s) |
| `RELAY_MISSED_PONGS` | `2` | dropped after this many missed pongs |
| `RELAY_RATE_BURST` | `60` | per-agent token-bucket burst |
| `RELAY_RATE_REFILL` | `10` | per-agent refill (tokens/sec) |
| `RELAY_ENROLL_MAX_PER_IP` | `20` | max NEW (TOFU) enrollments per IP per window |
| `RELAY_ENROLL_WINDOW` | `3600` | enrollment rate-limit window (s) |
| `RELAY_ENROLL_MAX_AGENTS` | `10000` | global cap on enrolled agents |
| `RELAY_BUFFER_FRAMES` | `32` | reconnect ring depth (per agent) |
| `RELAY_BUFFER_TTL` | `30` | reconnect ring TTL (s) before poller backstop |
| `RELAY_MAX_BODY` | `2097152` | max inbound body bytes -> 413 |

## Deploy

Anywhere that runs a container or a Python process:

```bash
docker build -t sunday-relay .
docker run -p 8787:8787 \
  -e RELAY_ADMIN_TOKEN=$ADMIN \
  -v $(pwd)/data:/data \
  sunday-relay
```

Put TLS in front (the daemon dials `wss://`, providers POST `https://`) via your
platform's edge or a reverse proxy. The volume at `/data` persists `agents.json`
across restarts — without it, daemons just reconnect-and-reregister, but
hand-edited registries would be lost.

## BYO relay (spec §9)

The relay endpoint is a config value on the daemon (`RelayConfig.url`). Sunday's
hosted relay is the default; running your own is a URL change, not a fork:

| Mode | How |
| --- | --- |
| Relay (Sunday-hosted) | in Settings → Channels → Relay, pick **Sunday's relay** — zero setup |
| Relay (BYO) | deploy this, then pick **My own relay** and paste `wss://my-relay…` |
| Funnel | turn the relay off, `sunday net configure` (Tailscale) |
| Direct | turn the relay off, reverse-proxy `/webhooks/*` (VPS + Caddy) |

Because the relay is dumb and stateless, BYO is "deploy a small socket-broker,
set a URL" — and TOFU means there's no registration step: your daemon enrolls
itself the moment it connects.

### Deploy your own on Fly (one walkthrough)

`fly.toml` here is already production-shaped — note `auto_stop_machines = false`
and `min_machines_running = 1`: the relay holds long-lived WebSockets, so it must
**never** be suspended, or every daemon's socket drops.

```bash
cd relay-service
fly launch --copy-config --no-deploy --name my-sunday-relay   # reuse fly.toml
fly volumes create relay_data --size 1 --region <your-region> -a my-sunday-relay
fly deploy -a my-sunday-relay
# (optional) only if you want explicit registration / per-slug auth:
fly secrets set RELAY_ADMIN_TOKEN=$(openssl rand -hex 32) -a my-sunday-relay
```

Then in Sunday: **Settings → Channels → Relay → My own relay**, paste
`wss://my-sunday-relay.fly.dev`, and toggle on. Your daemon connects, TOFU-enrolls
itself, and the per-channel public URLs appear for you to paste into providers.

The same pattern works on Railway/Render or any container host — the only hard
requirements are: holds a persistent WebSocket (no idle suspend) and has a volume
at `/data` so the registry survives restarts.

## Reconnect buffer (why a 2s blip doesn't lose an event)

Each agent has a bounded ring (`RELAY_BUFFER_FRAMES`, `RELAY_BUFFER_TTL`) of
sent-but-unacked `webhook` frames. On reconnect the relay replays the ones still
within TTL; an ack drops a frame from the ring. Anything older than the TTL falls
through to the **daemon's poller backstop** — that's by design (spec §2/§8), not
a gap: the relay aims for "no loss on a fast reconnect," and the poller is the
real delivery guarantee.
