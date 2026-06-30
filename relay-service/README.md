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
`token` — on first relay-enable, and persists them to `~/.sunday/credentials.env`.
The relay never mints secrets (that keeps secret generation on the user's
machine). The daemon then registers `(agent_id, token)` with the relay one of two
ways:

- **Self-register (recommended):** `POST /admin/register` with
  `Authorization: Bearer <ADMIN_TOKEN>` and body
  `{"agent_id": "...", "token": "...", "slugs": {...}}`. Idempotent — safe to
  call on every boot.
- **Hand-edit:** the operator edits `agents.json` directly (see
  `agents.example.json`). Useful for BYO single-box deploys.

If `RELAY_ADMIN_TOKEN` is unset, the `/admin/register` route returns 404 and
hand-editing is the only registration path — a deliberate per-deploy posture
choice.

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
| Relay (Sunday-hosted) | daemon `relay.enabled = true`, default `url` — zero setup |
| Relay (BYO) | deploy this, `relay.url = wss://my-relay…`, register the daemon |
| Funnel | `relay.enabled = false`, `sunday net configure` (Tailscale) |
| Direct | `relay.enabled = false`, reverse-proxy `/webhooks/*` (VPS + Caddy) |

Because the relay is dumb and stateless, BYO is "deploy a small socket-broker,
set a URL" — that's the property that keeps the agent itself local.

## Reconnect buffer (why a 2s blip doesn't lose an event)

Each agent has a bounded ring (`RELAY_BUFFER_FRAMES`, `RELAY_BUFFER_TTL`) of
sent-but-unacked `webhook` frames. On reconnect the relay replays the ones still
within TTL; an ack drops a frame from the ring. Anything older than the TTL falls
through to the **daemon's poller backstop** — that's by design (spec §2/§8), not
a gap: the relay aims for "no loss on a fast reconnect," and the poller is the
real delivery guarantee.
