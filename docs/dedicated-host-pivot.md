# RFC: Sunday on a dedicated host — Server / Satellite topology

Status: Draft for review
Date: 2026-06-16

## Summary

Make "Sunday lives on its own always-on Mac (a Mac mini) and you reach it from
the machines you actually sit at" a first-class, blessed topology — instead of
something you can only assemble by hand with environment variables and a VPS.

Two roles, set per machine:

- **Server** — this Mac *is* the brain. The daemon runs always-on, owns the one
  chat, and is reachable over Tailscale. A laptop can also flip into this role
  later via "convert to server"; a headless mini uses `sunday serve`.
- **Satellite** — the brain lives elsewhere. This machine runs the desktop UI
  pointed at the server's tailnet address, *and* a device satellite so the brain
  can still drive its iMessage / screen / shell.

The connection runs over Tailscale: **Serve** for tailnet-only client traffic
(desktop UI + satellites), **Funnel** for exactly one public path — the Sendblue
webhook — so texting is webhook-fast instead of falling back to the 30-second
poller. No VPS, no DNS, no manual cert.

## Design doctrine

Sunday is intentionally opinionated software for a Mac mini that belongs to one
person — not a portable, multi-tenant app. Every decision below should be read
through this lens, and so should every decision after it:

- **One owner, one brain, one chat.** No multi-user, no accounts, no roles. The
  single bearer token is the whole auth model.
- **The mini is always-on and canonical.** Satellites — including your phone over
  Sendblue — are disposable views. Config lives on the brain.
- **The tailnet is the perimeter.** Expose exactly one public path (the secret
  webhook via Funnel) and never broaden it "just in case."
- **Mac-native is the point.** Lean into iMessage, Codex, screen, local tools.
  The Linux/VPS path is legacy/secondary, not a co-equal target.
- **Zero-config by conviction.** Sunday reads its own Tailscale identity and mints
  its own secrets. The user never types an IP, a port, or hand-edits config.
- **Texting is first-class, often primary** — the owner carries a phone, not the
  mini.

When a choice arises, prefer the opinionated default for a single-owner mini over
a flexible knob.

## Why

Today there are two documented ways to host the brain, and neither is the thing
most people want:

1. **Local mode** — daemon on the Mac you're using (`electron/main.js:401`).
   Great tools (iMessage, Codex, screen, local shell), but it's not always-on,
   and it has no public URL — so inbound texts depend on the Sendblue **poller**,
   not the webhook.
2. **VPS mode** — daemon on a public Linux box with Caddy + DNS + a cert
   (`README.md:125`). Texting is fast (real webhook), but a Linux VPS can't send
   iMessage, run Codex, or drive a Mac natively. You bolt Mac satellites back on
   to recover what you gave up.

A **dedicated Mac mini at home** is the best of both — every Mac-native tool,
always on — except for one thing: it sits behind your home network / tailnet, so
it has no public URL, and texting silently degrades to the poller. This pivot
exists to fix that one gap and make the topology easy to choose.

## Where we are today

Grounded in the current code so the spec extends reality rather than reinventing
it.

- **Run mode** is already a local↔cloud toggle. "Cloud" means "point the app at a
  remote daemon by URL + token," with the cloud URL remembered so you can flip
  back (`electron/main.js:401-438`). This is the seed of Server/Satellite.
- **Satellites already dial home** to the brain over WebSocket and expose
  shell / screen / CDP / iMessage; the server URL can be any host — tailnet IP,
  hostname, public URL (`src/sunday/devices/satellite.py`,
  `electron/satellite.js`). A client driving a remote brain is a solved pattern.
- **The daemon can bind beyond localhost** via `SUNDAY_HOST` / `SUNDAY_PORT`
  (`src/sunday/config.py:66`, default `127.0.0.1:8765`).
- **Sendblue inbound** has both a webhook (`src/sunday/channels/sendblue.py:224`)
  and a 30-second poller backstop (`:260`, `POLL_INTERVAL_SECONDS = 30`). The
  webhook path is **auth-exempt** (`daemon.py:136`, `_AUTH_EXEMPT_PREFIXES`) and
  performs **no signature verification** — it trusts any JSON it receives.
- **prefs.json** already carries `daemonHttp/Ws/Token`, the saved
  `cloudDaemonHttp/Ws/Token`, and an `onboarded` flag; an embedded satellite is
  gated by `prefs.embeddedSatellite` (`electron/main.js`).
- The README's own philosophy: *"Webhooks are webhooks. No polling. If your
  inbound channel drops a message, that's a bug in the channel."*
  (`README.md:263`) — which the current Mac-local story quietly violates.

## The core problem

> A private Mac brain has every tool but no public webhook, so inbound texts fall
> back to a 0–30s poll.

That latency and unreliability is the felt problem ("text Sunday reliably… it
needs to be fast"). Everything else in the pivot is packaging. The fix is to give
the private box exactly one public ingress — the Sendblue webhook — and nothing
else.

## Design

### 1. Roles

A new explicit `role` in prefs: `"server" | "satellite"`. It supersedes the
implicit local/cloud check (`isLocalDaemon()`), which stays as the underlying
mechanism.

**Server role**
- Embedded daemon runs locally and **always-on** (launchd `KeepAlive`), not just
  while the app window is open.
- Tailscale Serve + Funnel configured (see Networking).
- Owns the one chat — the single source of truth. No other machine holds history.
- The desktop UI is optional on this box (you can sit at the mini, or never).
- Headless equivalent: `sunday serve` — same daemon + networking, no Electron.

**Satellite role**
- `daemonHttp/Ws` point at the server's tailnet address
  (`https://mini.<tailnet>.ts.net`, `wss://…/v1/ws`); token is the server's.
- The embedded device satellite runs, pointed at the same server's
  `/v1/devices/ws`, so the brain can drive this machine.
- Holds no chat history of its own; it's a window onto the server's one chat.

Mapping to today: Server ≈ local mode, but always-on + exposed + canonical.
Satellite ≈ cloud mode, but the URL is a tailnet name and the embedded satellite
is on by default.

**Config ownership.** The server owns everything that *is* Sunday: model /
provider, credentials, memory, the one chat, networking. A satellite owns only
"how I reach her" — connection (server URL + token) plus local UI prefs (theme,
HUD, login item). There is no model picker on a satellite; changing the brain is
a server-side act. The daemon enforces this by construction — model, network,
and channel config are auth-gated `/v1` routes on the brain, so only the box
that *is* the server can change them. This is the concrete reason "convert to
server" matters: it's the act of taking ownership of config.

**A satellite need not be a computer.** Your phone over Sendblue is a satellite —
a surface you reach Sunday through, with zero install. Someone may run *no*
satellite computer at all and interact entirely by text. "Install a satellite on
each Mac" is one option, not a requirement; texting is a complete way to use her.

### 2. Networking — Tailscale Serve + Funnel

The daemon **stays bound to `127.0.0.1:8765`**. Tailscale proxies to it; we never
bind `0.0.0.0`. Two layers:

```
                         public internet
                               │
                               │  POST /webhooks/sendblue   (Funnel: this path only)
                               ▼
   Sendblue ──────►  mini.<tailnet>.ts.net  ──► 127.0.0.1:8765/webhooks/sendblue
                               ▲
                               │  https/wss, all paths   (Serve: tailnet-only)
              ┌────────────────┼────────────────┐
        laptop satellite   phone        other Mac satellite
        (desktop UI +      (Tailscale)  (device satellite)
         device satellite)
```

- **Serve (tailnet-only):** `tailscale serve` proxies the node's HTTPS to
  `localhost:8765`. Satellites and the desktop reach the brain at
  `https://mini.<tailnet>.ts.net/…` and `wss://…/v1/ws`, getting TLS and tailnet
  ACLs for free — no token in the clear on the LAN, no self-signed cert.
- **Funnel (public, one path):** `tailscale funnel` exposes **only**
  `/webhooks/sendblue` to the public internet, with an automatic Let's Encrypt
  cert on the `ts.net` name. Sendblue's inbound webhook points there. Every other
  path 404s publicly while still working over the tailnet.
- **Poller demoted:** with a reliable public webhook, the 30s poller drops to a
  slow backstop (e.g. every few minutes) purely to catch a missed webhook —
  realigning with the "webhooks are webhooks" philosophy.

Should Sunday *run* the Tailscale commands or just *instruct*? Recommended:
detect the `tailscale` CLI and Funnel capability; if present and permitted, run
`serve`/`funnel` itself and surface the resulting webhook URL; otherwise show the
exact commands + the URL to paste into Sendblue. Tailscale is the dependency the
user already trusts for this ("tailscale has a solution for this").

### 3. Texting reliability

- Inbound: Sendblue → Funnel → webhook handler, p50 well under one second vs.
  today's 0–30s. Poller becomes backstop only.
- Outbound is unchanged (daemon → Sendblue API), already retried with backoff.
- Target: a text to Sunday is acknowledged (typing indicator) within ~1s of send
  under normal conditions.

### 4. Security model

Funnel makes the `ts.net` hostname publicly resolvable, so the webhook path is
now reachable by anyone who learns it. The handler currently has **no
verification** (`sendblue.py:224`). Before exposing it we must add at least one,
ideally layered:

1. **Unguessable path segment.** Mount the webhook at
   `/webhooks/sendblue/<long-random-secret>` and Funnel only that. The secret
   lives in `~/.sunday/credentials.env` and in the Sendblue dashboard URL.
   Reject any other path. (Simple, effective, no Sendblue feature needed.)
2. **Sender allowlist.** Optionally reject inbound whose `number` isn't a known
   contact — cheap defense-in-depth.
3. **Signature, if Sendblue offers one.** Confirm whether Sendblue signs
   webhooks (HMAC header); if so, verify it and treat (1) as backup. (Open
   question below.)

Unchanged: all `/v1/*` stays behind the bearer token (`daemon.py:139`); Funnel
exposes only the webhook path, never `/v1`. Tailnet ACLs gate who can reach the
daemon at all over Serve. Rate-limit the public webhook path.

## UX flows

### Onboarding — pick a role
A new first step: "Is this Mac the brain, or a satellite?"
- **Brain (server):** run the server wizard — check Tailscale is up, enable
  Serve + Funnel, show the Sendblue webhook URL to paste, collect credentials.
  Start the always-on daemon.
- **Satellite:** ask for the server's tailnet address + a pairing code (below),
  redeem it for the token, start the embedded satellite, open the chat.

### Convert to server
From a laptop running local or as a satellite, a Settings action
"Convert to server" that: migrates the chat + memory down if needed (reuse the
existing `migrate-to-local` plumbing, `electron/main.js:442`), switches the
daemon to always-on, runs the Tailscale setup, and sets `role = server`.

### Satellite pairing
Avoid pasting the raw bearer token around. The server shows its tailnet address
plus a short-lived **pairing code**; the satellite submits the code to the server
and receives `{ daemonHttp, daemonWs, token }`. (A QR encoding the same is a nice
later add.)

### `sunday serve` (headless)
A CLI entry that runs the daemon in server mode with no Electron — for a mini you
administer entirely from a satellite. It installs the launchd service and runs
the Tailscale setup. Mirrors the existing daemon/satellite dispatch in
`electron/build/daemon-entry.py`.

## Data & migration

- **The server is the single source of truth** for the one chat
  (`sunday.db`) and memories (`memories.db`). Satellites never hold history.
- **Local → server:** data is already on-box; "convert to server" just makes it
  always-on + exposed.
- **Existing local brain → satellite of another server:** this means abandoning
  (or merging) the local history in favor of the server's. Needs an explicit
  choice — see open questions. Default proposal: the server wins; the old local
  chat is archived, not silently dropped.
- Backups (Litestream → R2, `README.md:176`) continue to run on the server only.

## Build plan

Phased so the felt win ships before the larger UX refactor.

- **Phase 1 — texting + networking (the actual problem).**
  Tailscale Serve + Funnel integration (detect or instruct), webhook path secret
  + verification, poller demoted to backstop, Sendblue URL surfaced in the app.
  Shippable on its own; makes texting fast on any tailnet-hosted brain.
- **Phase 2 — roles.** Explicit `role` in prefs, onboarding role pick,
  always-on launchd for server, embedded satellite on by default in satellite
  role, tray/settings reflect role.
- **Phase 3 — convert + pair.** "Convert to server" action, pairing codes,
  `sunday serve` headless entry, history-ownership handling.
- **Phase 4 — polish.** QR pairing, server health surfaced on satellites, docs
  rewrite (replace the VPS-centric hosting section with the mini-first story).

## Open questions

1. **Does Sendblue sign its webhooks?** Determines whether security relies on the
   path secret alone or can also verify an HMAC. Needs a dashboard/docs check.
2. **Sunday-managed vs. instructed Tailscale.** How much of `tailscale up /
   serve / funnel` does the app run itself vs. hand the user to paste? Funnel also
   requires the tailnet admin to have enabled it — detect and message that.
3. **History ownership on convert.** When a machine that already had a local
   brain becomes a satellite, server-wins-and-archive vs. an actual merge.
4. **Pairing transport.** Pairing code redeemed over Serve is clean; confirm the
   redeem endpoint itself is safe (rate-limited, short TTL, one-time).
5. **Mini reachability when Tailscale is down.** If the tailnet drops, the
   satellite can't reach the brain at all. Acceptable? Any LAN fallback?

## Out of scope

Multi-user, daemon-to-daemon federation/replication, non-Mac brains (the Linux
VPS path stays as-is for those who want it), and token rotation/expiry.
