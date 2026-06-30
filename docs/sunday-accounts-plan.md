# Sunday accounts + the hosted plan — build plan

Status: proposed. No billing yet (free tier only). Self-hosted stays fully BYO.

## The frame

The proxy generalizes. Today it proxies **inbound** (webhooks/email/text → your
agent, via the relay). The same lightweight hosted layer proxies **outbound** —
your agent's **model calls** → through a Sunday plan. One account ties both.

The hosted footprint stays small: a managed-auth project (Supabase), the relay
(already deployed: sunday-relay.fly.dev), and a thin model gateway. No heavy
backend.

## Why accounts (the load-bearing reason)

A **free model tier requires accounts.** TOFU was fine for the relay — an
unguessable `agent_id` has nothing worth stealing. Free LLM calls have real
value, so without identity they'd be farmed in a day. So the Sunday-plan
ambition is *what makes accounts necessary* — not the relay.

Accounts also fix a real wart: today `agent_id` is minted per install, so a
reinstall / new machine → new id → new relay URLs → pasted provider webhooks
break. An account gives a **stable, recoverable identity**.

## Architecture

```
  Sunday account (Supabase Auth: magic-link / Google)
     │  issues, per user:
     ├─ a STABLE agent_id + relay token   → the relay identity
     └─ a Sunday API key                  → the model gateway
                                            (+ a free-tier usage budget)
     ▼
  Supabase Postgres = source of truth: accounts, keys, usage counters
     ▲                         ▲
     │ validate key + meter    │ validate agent_id (replaces TOFU enroll)
  ┌───────────────┐      ┌──────────────────┐
  │ MODEL GATEWAY │      │ RELAY (deployed)  │
  │ (Fly, thin)   │      │ sunday-relay.fly  │
  │ key→meter→    │      │ now account-bound │
  │ OpenRouter    │      └──────────────────┘
  └───────────────┘
     ▲ daemon points its provider at the gateway with the Sunday key
  ┌──────────────────────────────────────────┐
  │ DAEMON: "Sign in to Sunday" → gets agent_id + Sunday key → uses both │
  └──────────────────────────────────────────┘
```

Self-hosted = none of this. BYO relay, BYO model keys, no account, no Sunday in
the loop. The account/plan is the *default*, never a dependency.

## Phases

1. **Accounts + stable relay identity.** Supabase Auth (magic-link/Google). On
   sign-in the daemon receives a stable `agent_id` + relay token (persisted to
   `relay.json` as today, but now issued by the account, not minted locally).
   Relay enrollment switches from pure TOFU to "agent_id must belong to a known
   account" (validated against Supabase). Daemon UI: a "Sign in to Sunday"
   surface. *Fixes continuity; gates enrollment.*

2. **Model gateway + free tier.** A Fly service (relay-shaped): authenticates a
   Sunday API key, meters usage against the free-tier budget in Supabase, and
   proxies to OpenRouter (Sunday already routes through OpenRouter). Over budget
   → 402/clear message. Daemon: a provider option "Sunday (free tier)" that
   points the model router at the gateway with the Sunday key — no BYO keys
   needed. *Super-fast onboarding: sign in, you're talking to a model.*

3. **Auto-wire the integrations.** On relay enable, register the relay public
   URL (now using the *stable* account `agent_id`) with each provider that has
   an API: Sendblue (`/account/webhooks` — already have the call) and AgentMail
   (needs webhook-API research; dashboard fallback otherwise). *Makes the relay
   turnkey — no pasting.*

4. **Billing (later).** Stripe on top of the same usage counters. Out of scope
   for now.

## What this needs from the user (the only blockers)

Everything else — all code, all Fly deploys, all testing — is mine.

1. **A Supabase project** (free tier). It's the backbone: auth + the
   accounts/keys/usage DB. I need: the project URL, the `anon` key (client), and
   the `service_role` key (backend). Create at supabase.com → new project.
2. **An OpenRouter API key with some credit** — the gateway's upstream that the
   free tier draws from. (This is "Sunday's" master key; the free tier is a
   budget we meter on top of it.)

## Open questions

- Free-tier size: per-account budget (e.g. $X of model spend / month, or N
  messages). Pick a number that's generous for onboarding but farm-resistant.
- Which model(s) the free tier serves (a cheap-but-good default vs. choice).
- Magic-link vs. Google vs. both for v1 sign-in.
- Abuse: email verification + per-account rate limits + the free-tier cap are
  the v1 defenses; phone verification if farming shows up.
