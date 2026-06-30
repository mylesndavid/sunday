# Sunday Relay + First-Party Inbox — Spec

Status: proposed · Author: Sunday/Myles · Supersedes: `sunday net configure` (Tailscale Funnel) as the default public-ingress path.

## 0. What this is

Two coupled pieces:

1. **The Relay** — a thin, hosted, stateless pipe that gives every Sunday daemon a public URL for inbound events **without** the user touching DNS, Tailscale, Funnel, Caddy, or port-forwarding. The daemon dials *out* to the relay over one persistent socket; the relay forwards inbound HTTP down that socket. This is the tmate model: the thing being reached makes the outbound connection, the relay just brokers.

2. **First-party Inbox** — Sunday gets her own *identity* on each channel and a unified surface for them:
   - her own **number** (Sendblue, exists today),
   - her own **address** (AgentMail, new),
   - her own **voice line** (VAPI, exists today as outbound).

   These three become deep, obvious, first-party — surfaced together under an **Inbox** tab, not buried in Settings → Texting.

### Decisions locked

- The relay carries **both** the blessed first-party channels **and** arbitrary webhooks. Blessed ≠ exclusive. "Send my agent any webhook and it does stuff" stays a first-class feature.
- The relay is a **dumb transport**: stateless, payload-blind where possible, holds no agent state, no brain, no memory. It is the *only* new hosted component, and deliberately the least powerful one. The agent stays local — that preserves Sunday's "everyone's individual, not a SaaS" property.
- Pollers stay as **backup**, never deleted. Same belt-and-suspenders Sendblue already uses (webhook primary + 30s poll), because hosted webhooks drop.
- The relay is **BYO-able via config**. Sunday's hosted relay is the *default*, never a *dependency*. `RelayConfig.url` points at it; override to self-host your own. Empty/disabled → fall back to Funnel/direct ingress. (§9)
- The proxy push populates a **local activity store**; the Inbox reads that store. Provider live-fetch (today's only path) demotes to backfill/reconcile. (§4b)

## 1. Why the relay is mostly *deletion*, not new infra

The ingestion machinery already exists:

| Existing piece | File | Role in relay |
| --- | --- | --- |
| `register_webhook(path, handler)` | `daemon.py:52` | unchanged — channels register handlers exactly as today |
| `_webhook_dispatch` (exact-path lookup) | `daemon.py:2565` | unchanged — relay delivers *into* this |
| `/webhooks/{name:.*}` catch-all route | `daemon.py:2770` | unchanged |
| `_AUTH_EXEMPT_PREFIXES` includes `/webhooks/` | `daemon.py:136` | why loopback delivery needs no token |
| `register_background_task(task)` | `daemon.py:57` | the relay client registers here, beside the Sendblue poller |

So the relay's daemon-side footprint is **one new background task** (the relay client) plus a generic handler for arbitrary webhooks. Nothing in the existing channels changes.

### What `sunday net configure` does today (the thing we're replacing)

`_http_net_configure` (daemon.py) runs `tailscale serve + funnel` to expose exactly one path (`/webhooks/sendblue/<secret>`) and then auto-registers that public URL with Sendblue via `register_receive_webhook`. It works, but it's the DNS/tunnel tax: the user must have Tailscale up, MagicDNS, Funnel enabled. The relay deletes that prerequisite for laptop/local users.

**Segmentation (falls out naturally):**
- **VPS users** (Docker, `0.0.0.0:8765`, Caddy) already have public webhooks → relay optional.
- **Laptop / local users** → relay is the whole point. The "I refuse to run a server" path.

## 2. Relay architecture

```
   Provider (AgentMail / Sendblue / Stripe / arbitrary)
        │  HTTPS POST  https://relay.sunday.xyz/u/<agent-id>/<slug>
        ▼
   ┌─────────────────────────────────────────────┐
   │  RELAY  (hosted, stateless, dumb pipe)       │
   │  • map agent-id → open socket                │
   │  • verify per-endpoint token (+ HMAC opt)    │
   │  • rate-limit per agent                      │
   │  • forward frame down the socket, await ack  │
   └─────────────────────────────────────────────┘
        ▲  persistent WebSocket (daemon dials OUT)
        │  frames: {webhook, ack, ping/pong}
        ▼
   ┌─────────────────────────────────────────────┐
   │  DAEMON (local)                              │
   │  relay client (background task)              │
   │   └─ on webhook frame:                       │
   │        loopback POST 127.0.0.1:8765<path>    │
   │        → existing _webhook_dispatch          │
   │        → existing channel handler            │
   │        → reply status forwarded back as ack  │
   └─────────────────────────────────────────────┘
```

### Why loopback delivery is the right seam

The relay client does **not** import channels, does **not** parse payloads, does **not** know what AgentMail is. On a `webhook` frame it constructs an HTTP POST to `http://127.0.0.1:<port><path>` with the forwarded method/headers/body and returns the response status upstream as an ack. Because `/webhooks/*` is auth-exempt, no token plumbing is needed on the loopback hop. Result: **a relay-delivered webhook is byte-identical to a Funnel-delivered one.** Channels can't tell the difference, so nothing channel-side changes, ever.

### Wire protocol (WebSocket, JSON frames)

Daemon → relay on connect:
```json
{ "type": "hello", "agent_id": "<agent-id>", "token": "<daemon-relay-secret>", "version": 1 }
```
Relay → daemon, forwarding an inbound request:
```json
{ "type": "webhook", "id": "<req-id>", "slug": "agentmail",
  "method": "POST", "path": "/webhooks/agentmail",
  "headers": { ... }, "body": "<raw string>" }
```
Daemon → relay, ack (carries the loopback response so the relay can answer the provider with a real status):
```json
{ "type": "ack", "id": "<req-id>", "status": 200, "body": "{\"ok\":true}" }
```
Liveness: `ping`/`pong` every ~20s; relay drops sockets that miss N pongs. Daemon reconnects with exponential backoff + jitter and **resumes** (relay buffers a small ring of unacked frames per agent so a 2s reconnect doesn't lose an event; anything older falls to the poller backup — by design, not perfection).

`slug → path` mapping lives **on the daemon** (the relay stays dumb): the relay forwards `slug`, the relay client maps it to a local `/webhooks/...` path. Default mapping is identity (`slug` → `/webhooks/<slug>`); blessed channels can override (e.g. `sendblue` → `/webhooks/sendblue/<secret>`).

### Identity & registration

- On first relay enable, daemon mints/loads `RELAY_AGENT_ID` + `RELAY_TOKEN` (256-bit, persisted to `~/.sunday/credentials.env`, same store Sendblue uses via `get_credential`/`set_credential`).
- Daemon registers with the relay (idempotent, mirrors `register_receive_webhook`'s GET-then-POST idempotency).
- Public URL form: `https://relay.sunday.xyz/u/<agent-id>/<slug>`. The `<agent-id>` is unguessable, so it doubles as the coarse auth (same trick as the Sendblue secret path today), with a per-slug token + optional HMAC layered on top.

## 3. Security model

The relay sees inbound traffic for everyone — so it must be incapable of doing anything but routing.

1. **Stateless + payload-blind.** No store of agent data; forwards opaque bodies. A relay breach leaks routing, not capability.
2. **Two separate credentials.** The *socket* is authenticated by `RELAY_TOKEN` (proves "I am agent X's daemon"). The *webhook URL* is authorized by the unguessable `agent-id` + per-slug token (proves "this POST is allowed to reach agent X"). Compromising one URL doesn't grant the socket and vice-versa.
3. **Per-endpoint token + optional HMAC.** Closes the gap that exists *today*: `/webhooks/*` is auth-exempt and the Funnel URL relies entirely on each provider's own signature check. For providers that sign (Stripe, AgentMail), verify HMAC at the relay edge. For those that don't, the unguessable path is the gate.
4. **Per-agent rate limiting** at the relay — the one shared chokepoint. Caps abuse blast radius.
5. **Outbound-only daemon** — no inbound port on the user's machine. This is a security *win* over the tunnel approach, not just convenience.

## 4. Arbitrary webhooks (the general unlock)

A new generic channel, `channels/webhook_inbox.py`, registers a handler that does **not** 404 on unknown slugs. Instead it packages the inbound into an event the brain can act on.

- Public URL: `https://relay.sunday.xyz/u/<agent-id>/hook/<user-slug>`.
- The user creates a named hook in the Inbox UI ("cold-email-replies"), gets a URL + token, pastes it into any tool that can POST.
- On delivery, the handler builds a compact natural-language event ("Webhook `cold-email-replies` fired: <summarized JSON>") and routes it through `respond(...)` with a channel tag `webhook:<slug>`, plus an optional standing instruction the user attached to that hook ("when this fires, draft a reply and text me").
- Hooks are listed/revocable in the Inbox UI; each has its own token so one can be rotated/killed without touching others.

This is the "respond to positive cold-email replies" story end-to-end: paste one URL, attach one instruction, done. No deploy, no poller, no automation framework.

## 4b. Replacing live retrieval with the local activity store

Today every Inbox-style read is **live retrieval**: the Calls view calls `list_calls(limit=50)` straight to VAPI on every open (`daemon.py:2028`, `_http_vapi_calls`) — no local store. Adding Sendblue + AgentMail naively means three live API round-trips per tab open, each with its own latency and failure mode.

The proxy makes a better architecture available: **the daemon's local store becomes the source of truth.**

```
   proxy push (inbound event)  ─┐
   poller backfill ─────────────┼──▶  local activity store (SQLite)  ──▶  Inbox reads here
   provider live-fetch (reconcile) ─┘                                      (fast, unified, offline-tolerant)
```

- **Write side:** the same proxy push that drives inbound message handling also appends to the activity store (calls, texts, emails — one normalized event shape with `channel`, `direction`, `peer`, `ts`, `body`, `thread_id`, `provider_id`). One mechanism feeds both "the brain reacts" and "the Inbox shows it."
- **Read side:** Inbox reads the local store. `/v1/inbox?channel=` serves merged rows from SQLite, not from three provider APIs.
- **Live retrieval demotes, doesn't die.** Provider fetch (`list_calls`, etc.) becomes **backfill/reconcile**: history from before the proxy connected, a freshly-paired device, or catching anything the proxy dropped. Triggered on first boot, on explicit refresh, and on a slow cadence — not on every tab open.

This is the §1 inbound principle (webhook primary, poll backup) applied to **reads**: push keeps the store fresh, provider fetch is the backstop. Net new local infra: one SQLite table, alongside the trace DB the daemon already runs. That table is what makes "replace live retrieval" real rather than just another fetch path.

Detail views (a call's transcript/recording, an email body) can still lazy-fetch the heavy payload live by `provider_id` — the store holds the row, the provider holds the blob. So the store stays small and the recording URL/transcript still comes fresh from VAPI when a row is opened.

## 5. First-party channel model + the Inbox tab

Today the nav (`electron/renderer/index.html:13-17`) is: **Chat · Memory · Calls · Rewind · Settings**. The **Calls** tab already exists and is the seed for Inbox — generalize it, don't bolt on a new one:

- `data-view="calls"` view (`index.html:206`), rendered by `electron/renderer/calls-view.js`.
- A **list/detail activity feed**: list of rows (time · to · purpose · duration · status), click a row → detail (transcript, summary, audio recording).
- Data comes from the daemon, which proxies VAPI and holds the key: `GET /v1/vapi/calls` (list) and `GET /v1/vapi/calls/{id}` (detail). Renderer only ever talks to the local daemon.
- Wired in `app.js`: allowed-views list (`app.js:1084`), `switchView('calls')` → `callsView.load()` (`app.js:1092`), keyboard `Cmd/Ctrl+3` (`app.js:1103`).

**Calls → Inbox is a generalization of an already-correct pattern.** The list/detail-with-transcript shape is exactly what email threads and text threads want too. The work:

| Today (Calls) | Inbox |
| --- | --- |
| tab label "Calls", `data-view="calls"` | "Inbox", `data-view="inbox"` (update allowlist, `switchView`, shortcut, section id) |
| `calls-view.js`, single source `/v1/vapi/calls` | `inbox-view.js`: channel filter (All · Voice · Text · Email), merged feed |
| call rows only | rows from VAPI calls **+** Sendblue threads **+** AgentMail threads |
| one detail renderer (transcript/audio) | per-channel detail: Voice = current call detail; Text/Email = thread view |

| Channel facet | Channel | Sunday's identity | Status |
| --- | --- | --- | --- |
| **Voice** | VAPI | her own line | exists as the Calls tab — becomes the Voice facet |
| **Text** | Sendblue | her own number | exists (Settings → Texting); surface moves into Inbox |
| **Email** | AgentMail | her own address | new (§6) |

Backend: keep `/v1/vapi/calls` as-is; add parallel `GET /v1/sendblue/threads` and `GET /v1/agentmail/threads` (+ `/{id}` detail), or a single merged `GET /v1/inbox?channel=` that the daemon assembles. Each facet panel also shows identity/connected state (via the channel's `account_status()`), the relay public URL (one click to (re)issue), and a per-channel enable toggle. Future first-party channels add a facet the same way — this is the template.

The Settings → "CALLING (outbound via VAPI)" panel (`index.html:608`) and Settings → Texting stay as the *configuration* surfaces; Inbox is the *activity* surface. Config in Settings, activity in Inbox — same split the Calls tab already implies.

**First-party channel contract** (so "any other first-party integration follows suit"):
- A module under `channels/<name>.py` exposing `register(registry, config)` that: registers its `/webhooks/<name>` handler, registers a backup poller via `register_background_task`, registers outbound tool(s), and exposes an `account_status()` for the Inbox panel.
- A `slug → path` entry for the relay client.
- An entry in the Inbox UI registry (label, identity field, status endpoint).

This is exactly the Sendblue shape, generalized. Sendblue and AgentMail are the two reference implementations.

## 6. AgentMail channel — concrete

**Is AgentMail the right pick?** Yes, and for a specific reason. Sunday already has her *own number* (Sendblue), not a borrowed one. The existing Gmail integration (via Nango) is the *user's* inbox that Sunday operates — different thing. AgentMail gives Sunday her *own address* with an API + webhooks, which is the email-side mirror of Sendblue: own-number ↔ own-address. That symmetry is what makes the Inbox framing honest — Sunday has a line, an address, and a voice. Keep Gmail-via-Nango for "act on the user's mail"; AgentMail is "Sunday's own mailbox."

`channels/agentmail.py` mirrors `channels/sendblue.py` almost beat for beat:

| Sendblue piece | AgentMail equivalent |
| --- | --- |
| `_sendblue_headers()` / `SENDBLUE_API_KEY_*` | `_agentmail_headers()` / `AGENTMAIL_API_KEY` |
| `_webhook_handler` (inbound, dedup via `message_handle`) | inbound email webhook, dedup via message id |
| `start_poller` (30s backup, seed-then-tick, SEED_GRACE) | poll AgentMail inbox every N s, same seed-then-tick |
| `_process_inbound` → `respond(...)` → reply | same; reply via AgentMail send instead of Sendblue send |
| `_send_sendblue` (+ retry on transient 5xx) | `_send_agentmail` (send/reply, retry on 5xx) |
| `sendblue_send` tool | `agentmail_send` tool (compose) + reply-in-thread |
| `account_status()` for Texting panel | `account_status()` for Inbox→Email panel |
| `register(registry, config)` | identical shape |

Inbound path options, in shipping order:
1. **Poller first** (this week, zero relay dependency): `register_background_task` loop, exactly the Sendblue seed-then-tick. Proves the integration immediately.
2. **Relay webhook** (with relay): AgentMail → `https://relay.sunday.xyz/u/<agent-id>/agentmail` → loopback → `/webhooks/agentmail`. Poller demotes to backup.

Dedup across webhook + poll uses the provider message id, same as Sendblue's `message_handle == uuid` cross-path dedup (`daemon._sendblue_seen_uids` → `daemon._agentmail_seen_uids`).

## 7. Build order

1. **`channels/agentmail.py` as a poller** — clone Sendblue's structure; outbound `agentmail_send` tool + inbound poll + `respond()` wiring. Ships standalone, no relay. *(Proves first-party email.)*
2. **Calls → Inbox** — generalize the existing Calls tab (`calls-view.js`, `data-view="calls"`) into `inbox-view.js` with a channel filter (Voice/Text/Email); Voice keeps the current VAPI list/detail, Text/Email add thread feeds; per-channel `account_status()` + relay-URL display. Reuses the list/detail pattern that's already there. *(Makes it deep and obvious.)*
3. **Relay client + hosted relay** — background task dialing out; loopback delivery; the dumb stateless relay service (separate deploy). Replaces `sunday net configure` as the default. *(The transport unlock.)*
4. **`channels/webhook_inbox.py`** — arbitrary named hooks + standing instructions, listed/revocable in Inbox. *(Arbitrary acceptance.)*
5. **Flip AgentMail + Sendblue to relay-primary, poll-backup** — once the relay is proven. Keep Funnel as a documented fallback for the paranoid.

Each step is independently valuable and nothing blocks on the grand version.

## 8. Open questions

- Relay hosting shape: single small box vs. managed (Cloudflare Durable Objects / a Pusher-style service) — leaning self-hosted-tiny to keep it the *only* thing Myles operates.
- Relay buffer depth on reconnect (ring size / TTL) before falling back to poller.
- Whether `agent-id` rotation needs a grace window (old + new valid) so re-issuing a URL doesn't drop in-flight providers mid-rotation.
- HMAC posture per blessed provider (which sign, which don't) — determines edge-verify vs. path-secret per slug.

## 9. Configuration — BYO relay

The relay endpoint is a config value, mirroring the existing `ServerConfig` / `VapiConfig` / `CloudflareConfig` dataclasses in `src/sunday/config.py`. Sunday's hosted relay is the default; running your own is a URL change, not a fork.

```python
@dataclass
class RelayConfig:
    """Inbound event relay. Default points at Sunday's hosted relay; set
    `url` to your own self-hosted relay to opt out of the shared one.
    Empty/disabled → no relay; fall back to Tailscale Funnel or direct
    (VPS + Caddy) ingress."""
    enabled: bool = False                       # opt-in; off = today's behavior
    url: str = "wss://relay.sunday.xyz"         # BYO: point at your own
    agent_id: str = ""                          # minted on first enable, persisted
    # token lives in credentials.env (RELAY_TOKEN), not here
```

Ingress becomes a single selectable concern rather than three ad-hoc setups:

| Mode | Who it's for | How |
| --- | --- | --- |
| **Relay (Sunday-hosted)** | laptop/local users | `relay.enabled = true`, default `url` — zero setup |
| **Relay (BYO)** | self-hosters who want no shared dependency | `relay.enabled = true`, `url = wss://my-relay…` |
| **Funnel** | Tailscale users | `relay.enabled = false`, `sunday net configure` (today's path) |
| **Direct** | VPS + Caddy | `relay.enabled = false`, reverse-proxy `/webhooks/*` (today's path) |

The relay being dumb and stateless (§2, §3) is what makes BYO real: a self-hosted relay is "deploy a small socket-broker, set a URL," not "stand up a copy of Sunday." Sunday's relay is the *default*, never a *dependency* — the same property that keeps the agent itself local.
