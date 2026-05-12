# SMS Hub MVP

> One-line summary: a per-tenant Twilio inbound webhook + outbound helper that becomes the universal interface for every non-builder human in the construction-turtle stack (subs, homeowners, supers, inspectors). Six audience-side Phase-2 features become config rows once this exists.

**Status:** Spec — locked next concrete build per ADR 2026-05-11 (SMS hub is highest-leverage next build).
**Phase:** Originally Phase-1 Week-2; now the canonical next build after the Today screen.
**Owner:** CP.
**Last updated:** 2026-05-11.

**Why this matters:** ADR-POS-001 forbids us from building a portal for any non-builder human. SMS is the only channel that respects that constraint while still letting subs, owners, supers, and inspectors interact with the system. Six downstream features (Owner #2, Owner #5, Super #6, Super #7, Super #8, Office #12) become hours-of-work config rows once this lands. No other piece of remaining infrastructure has comparable leverage.

## Overview

A sub texts a photo to Chad's Twilio number. Within 5 seconds:
- The webhook routes the message to a per-command handler.
- The handler resolves the sender's identity + project (via known-phone lookup, magic-link token, or fall-through to "which project?").
- The photo lands in the right project's Drive folder + a candidate site-log entry.
- An AI Field Card reply confirms ("Got it — photo from Tony, tagged to Whitfield framing. I'll loop Chad in tomorrow.").
- A `sms.inbound` and `sms.outbound` event lands in `platform.event` with per-tenant cost.

Same plumbing serves:
- **Owner texts "is everything OK?"** → AI synthesizes a Field Card status reply (Owner #2).
- **Owner texts "what's my budget?"** → AI synthesizes spend vs contract reply (Owner #5).
- **Sub gets magic-link to sign a lien waiver** (Office #12).
- **Super gets the morning packet at 5 AM** (Super #6).
- **Inspector replies to scheduling SMS** → AI logs the inspection date (Super #9).

One pipe. Many consumers. Same Field Card primitive per ADR-UI-001.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ Twilio (per-tenant phone number)                                │
│                                                                 │
│   Inbound SMS/MMS → webhook POST                                │
│                                                                 │
└──────────┬──────────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────────┐
│ FastAPI (patton-ai-ios/backend)                                 │
│                                                                 │
│ POST /v1/turtles/{turtle_id}/sms/inbound                        │
│                                                                 │
│   1. validate Twilio signature                                  │
│   2. parse body + MMS media + sender phone                      │
│   3. STOP / HELP / START — A2P 10DLC compliance                 │
│   4. resolve_sender_identity(phone, tenant)                     │
│        → known sub / owner / super / inspector / unknown        │
│   5. resolve_project_context(identity, body, magic_link)        │
│        → exact project_id or "ask which one?"                   │
│   6. match_command(body) → handler_agent + config               │
│   7. dispatch handler → builds Field Card reply                 │
│   8. emit_event("sms.inbound", ...) + outbound TwiML reply      │
│                                                                 │
└──────────┬──────────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────────┐
│ Handler agents (home-builder-agent + patton-os)                 │
│                                                                 │
│ - field_card_synthesizer (status, budget, etc.)                 │
│ - photo_intake_agent (Drive upload + site log candidate)        │
│ - lien_waiver_handler (when magic-link present)                 │
│ - default_fallback_agent (unknown command → ask Chad)           │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## Backend changes

### Schema migration: `015_sms_hub.sql`

```sql
-- 015_sms_hub.sql — per ADR 2026-05-11 (SMS hub is the next build).
-- Six audience-side features become config rows once this lands.

CREATE SCHEMA IF NOT EXISTS sms;

-- ─── sms.identity ────────────────────────────────────────────────────────
-- One row per known phone number, scoped to a tenant. Auto-populated from
-- the inbox watcher's outbound mail seen across the system.

CREATE TABLE IF NOT EXISTS sms.identity (
    phone_number       TEXT NOT NULL,            -- E.164 format: '+12515551234'
    tenant_id          TEXT NOT NULL,            -- 'chad_homes' (mirrors platform.event)
    display_name       TEXT,                     -- 'Tony Garcia'
    role               TEXT NOT NULL,            -- 'sub' | 'owner' | 'super' | 'inspector' | 'other'
    known_projects     UUID[] NOT NULL DEFAULT '{}',
    opted_out_at       TIMESTAMPTZ,              -- non-null = STOP received; never message again
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (phone_number, tenant_id),
    CONSTRAINT identity_role_valid CHECK (role IN ('sub', 'owner', 'super', 'inspector', 'other')),
    CONSTRAINT identity_phone_e164 CHECK (phone_number ~ '^\+[1-9][0-9]{6,14}$')
);

CREATE INDEX IF NOT EXISTS identity_tenant_idx ON sms.identity (tenant_id);

-- ─── sms.command_config ──────────────────────────────────────────────────
-- Per-tenant intent routing. Each row is a regex → handler binding.
-- New SMS commands ship as config rows, not new code (per the SMS hub ADR).

CREATE TABLE IF NOT EXISTS sms.command_config (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          TEXT NOT NULL,
    intent_id          TEXT NOT NULL,            -- 'status' | 'budget' | 'help' | etc.
    pattern_regex      TEXT NOT NULL,            -- case-insensitive match against body
    handler_agent      TEXT NOT NULL,            -- 'field_card_synthesizer' | 'photo_intake' | 'lien_waiver' etc.
    audience_filter    TEXT[],                   -- ['owner', 'super'] = only match for these identity roles. NULL = all.
    priority           INTEGER NOT NULL DEFAULT 100,
    enabled            BOOLEAN NOT NULL DEFAULT TRUE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, intent_id)
);

CREATE INDEX IF NOT EXISTS command_tenant_priority_idx
    ON sms.command_config (tenant_id, enabled, priority);

-- ─── sms.magic_link_token ────────────────────────────────────────────────
-- Opaque single-use tokens for non-builder humans to perform a specific
-- action (sign a lien waiver, view a project status page, etc.) without
-- logging in. URL pattern:
--   https://<tenant>.chad.pattonai.app/sub/{project_id}/{token}

CREATE TABLE IF NOT EXISTS sms.magic_link_token (
    token              TEXT PRIMARY KEY,         -- 32-byte url-safe random string
    tenant_id          TEXT NOT NULL,
    project_id         UUID,                     -- may be null for cross-project actions
    recipient_phone    TEXT NOT NULL,
    purpose            TEXT NOT NULL,            -- 'lien_waiver_sign' | 'status_view' | etc.
    metadata           JSONB NOT NULL DEFAULT '{}'::jsonb,
    expires_at         TIMESTAMPTZ NOT NULL,
    used_at            TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS magic_link_phone_idx ON sms.magic_link_token (recipient_phone, used_at);
CREATE INDEX IF NOT EXISTS magic_link_expiry_idx ON sms.magic_link_token (expires_at) WHERE used_at IS NULL;

-- ─── sms.outbound_log ────────────────────────────────────────────────────
-- Every outbound message, for rate limiting + cost telemetry + audit.
-- Inbound messages also emit to platform.event but their full body lives
-- here (in case we ever need to retrieve the exchange for a customer
-- dispute).

CREATE TABLE IF NOT EXISTS sms.outbound_log (
    id                 BIGSERIAL PRIMARY KEY,
    tenant_id          TEXT NOT NULL,
    recipient_phone    TEXT NOT NULL,
    body               TEXT NOT NULL,
    segments           INTEGER NOT NULL DEFAULT 1,    -- 160 chars per segment for billing
    is_mms             BOOLEAN NOT NULL DEFAULT FALSE,
    cost_usd           NUMERIC(10,5) NOT NULL DEFAULT 0,
    twilio_message_sid TEXT,                          -- once delivered
    delivery_status    TEXT NOT NULL DEFAULT 'queued', -- 'queued' | 'sent' | 'delivered' | 'failed'
    error_message      TEXT,
    idempotency_key    TEXT,                          -- prevents duplicate sends on retry
    sent_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS outbound_recipient_time_idx
    ON sms.outbound_log (recipient_phone, sent_at DESC);

-- ─── sms.tenant_config ───────────────────────────────────────────────────
-- Per-tenant operational config — phone number, timezone, quiet hours,
-- rate limits.

CREATE TABLE IF NOT EXISTS sms.tenant_config (
    tenant_id              TEXT PRIMARY KEY,
    twilio_phone_number    TEXT NOT NULL,        -- the +1xxx number Twilio assigned
    timezone               TEXT NOT NULL DEFAULT 'America/Chicago',
    quiet_hours_start      TIME NOT NULL DEFAULT '21:00',  -- 9pm
    quiet_hours_end        TIME NOT NULL DEFAULT '07:00',  -- 7am
    rate_limit_per_hour    INTEGER NOT NULL DEFAULT 10,    -- outbound per recipient per hour
    enabled                BOOLEAN NOT NULL DEFAULT TRUE,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### New event types in `platform.event` taxonomy

No schema change — `event_type` is free-text with a format CHECK. Just adds new conventional names:
- `sms.inbound` — webhook received an SMS. Metadata: sender_phone, body_length, has_media, intent_matched.
- `sms.outbound` — outbound send. Metadata: recipient_phone, segments, cost_usd, idempotency_key.
- `sms.command_matched` — command router matched an intent. Metadata: intent_id, handler_agent.
- `sms.identity_resolved` — sender resolved to known identity. Metadata: role, was_known.
- `sms.magic_link_issued` — new magic-link token created. Metadata: purpose, expires_at.
- `sms.magic_link_used` — magic-link consumed. Metadata: token_id, time_since_issue.
- `sms.rate_limited` — outbound blocked by rate limit. Metadata: recipient, recent_count.
- `sms.quiet_hours_deferred` — outbound deferred until business hours. Metadata: scheduled_for.
- `sms.opted_out` — STOP received. Metadata: sender_phone.

### FastAPI routes

```
POST   /v1/turtles/{turtle_id}/sms/inbound          ← Twilio webhook
GET    /v1/turtles/{turtle_id}/sms/magic-link/{token}  ← magic-link landing page (HTML or JSON)
POST   /v1/turtles/{turtle_id}/sms/magic-link/{token}/use  ← magic-link consume + side-effect
```

The inbound webhook MUST:
- Validate Twilio signature (X-Twilio-Signature header) using the auth token from `backend/.env`.
- Return TwiML XML synchronously when a reply is appropriate; return empty TwiML when no reply (e.g. STOP).
- Be idempotent (Twilio retries on 5xx).

### Outbound helper

`backend/app/services/sms.py`:

```python
async def send_sms(
    settings: Settings,
    tenant_id: str,
    to_phone: str,
    body: str,
    *,
    idempotency_key: str | None = None,
    media_urls: list[str] | None = None,
) -> SmsSendResult:
    """Send an SMS or MMS via Twilio.

    Idempotency: if idempotency_key is set and already exists in
    sms.outbound_log for this tenant, returns the prior result without
    re-sending.

    Rate limit: if recipient has received >= rate_limit_per_hour outbound
    in the last 60 minutes, raises SmsRateLimitedError. Caller decides
    whether to defer or drop.

    Quiet hours: if current time in tenant timezone is within
    [quiet_hours_start, quiet_hours_end], raises SmsQuietHoursError.
    Caller decides whether to queue for delivery at next business hour.

    Telemetry: emits sms.outbound event with cost_usd computed from
    Twilio segment pricing ($0.0075/segment SMS, $0.02/MMS).
    """
```

## iOS changes

Minimal. The existing APNs push pipeline already handles "AI thinks Chad should see this." Inbound SMS that needs Chad's attention surfaces as a push notification through the existing `home_builder.event` + notification dispatcher. **No new iOS code in the MVP.**

Future: a "Pending SMS" badge in the Inbox/Activity tab. Deferred.

## Privacy & A2P 10DLC compliance

- **STOP**: any inbound containing "STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT" → mark `sms.identity.opted_out_at = NOW()` → reply with confirmation → never message that phone again. Required by US carrier rules.
- **HELP**: any inbound containing "HELP" or "INFO" → reply with the help text including how to opt out.
- **START**: any inbound containing "START" or "UNSTOP" → clear opted_out_at → reply with welcome.
- Quiet hours default 9 PM – 7 AM tenant-local (mitigates phone-reputation risk for AI-driven sends).
- Outbound rate limit default 10/hour/recipient (same).
- **Twilio registration**: the tenant's phone number must be registered with A2P 10DLC before going live for commercial messaging. Manual step on the Twilio dashboard.

## Failure modes

| Failure | Handling |
|---|---|
| Twilio signature invalid | 403 + log + emit `sms.signature_invalid` event |
| Sender phone unknown | Reply with "I don't have this number on file. Have your contact (Chad) add you, then text again." + log `sms.identity_unknown` |
| Body matches no command | Forward to default_fallback_agent → AI asks "what do you need?" → escalates to Chad if still unclear |
| Handler agent raises | 200 OK to Twilio (so it doesn't retry) + emit `sms.handler_failed` + reply with "I had a problem — Chad will follow up" |
| Twilio API down (outbound) | Mark `outbound_log.delivery_status = 'failed'` + retry queue (deferred to v2; v1 just logs) |
| Magic-link expired | 410 Gone with a Field Card-shaped "this link expired, ask Chad for a new one" |
| Magic-link already used | Same — single-use semantics |

## Tenant onboarding

For each new tenant:
1. Provision Twilio number on Twilio dashboard (manual).
2. Set webhook URL to `https://<tenant>.api.pattonai.app/v1/turtles/<tenant_id>/sms/inbound`.
3. Insert row into `sms.tenant_config`.
4. Insert starter command rows into `sms.command_config` (the universal HELP / STOP / START + tenant-specific defaults).
5. Test: send "HELP" from the builder's phone → expect HELP reply.

Time to onboard tenant #2 (Newton RFID, RPM Filter, etc.): ~15 minutes once the MVP is live.

## Acceptance criteria

v1 done when:

1. Migration 015 applies cleanly + tables exist.
2. Twilio number provisioned for `chad_homes` tenant + webhook URL pointed at our backend.
3. Send "HELP" from any phone → receive Help reply within 5s.
4. Send "STOP" → opted_out_at is set, future sends are blocked, confirmation reply lands.
5. Chad's phone (in `sms.identity`) sends a regular message → routed to the default handler, AI replies with a Field Card.
6. `platform.event` shows `sms.inbound` + `sms.outbound` events with non-zero cost.
7. Send 11 outbound messages in an hour to the same recipient → 11th raises `SmsRateLimitedError` → caller can decide (drop or queue).
8. During 9 PM–7 AM, automated send raises `SmsQuietHoursError` → caller can decide (queue for morning or skip).
9. Unit tests cover: identity resolution, command matching, STOP/HELP/START, rate limit, quiet hours, signature validation.

## v2 deferments (out of scope for this MVP)

- Voice (call answering)
- Group messages / broadcasts
- Rich media beyond MMS photos (PDF attachments, video)
- SMS-based new-user enrollment workflows
- Retry queue for failed outbound
- Per-recipient cost dashboards
- Multi-number per tenant (e.g. one number per super)
- Image OCR on inbound MMS (defer to Phase-2 #2 photo-as-universal-verb)

## Cross-references

- ADR: `~/Projects/patton-os/data/decisions.md` — SMS hub next-build ADR (2026-05-11)
- ADR: `~/Projects/patton-os/data/decisions.md` — Phase-2 backlog (2026-05-11)
- ADR: `~/Projects/patton-os/data/decisions.md` — ADR-UI-001 Field Card primitive (2026-05-11)
- ADR: `~/Projects/patton-os/data/decisions.md` — ADR-POS-001 construction-turtle positioning
- Codex handoff brief: `~/Projects/patton-ai-ios/docs/CODEX_HANDOFFS/VERTICAL_HANDOFF__sms-hub.md`
- Roadmap: `~/Projects/home-builder-agent/docs/ROADMAP-Phase-2-2026-05-11.md` § Week 9 (extensions sit on top of this MVP)
- Existing watcher pattern reference: `home_builder_agent/scheduling/notification_triggers.py` + heartbeat helper.
- Existing telemetry: `backend/app/services/telemetry.py` + `backend/migrations/009_platform_event.sql`.
