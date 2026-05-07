"""chad_voice.py — single source of truth for Chad Lynch's voice.

The persona signature of the home-builder agent suite. Today this powers
the morning brief and the weekly homeowner update; tomorrow it powers
the master agent (`hb-chad`, see docs/specs/chad-agent.md).

Two voice modes:

  NARRATOR — agent speaks TO Chad.
    Used by: hb-brief (morning brief), future status alerts, internal
    summaries. Audience is Chad himself, on his phone, before leaving
    for the job site or between meetings.

  AUTHOR — agent speaks AS Chad.
    Used by: hb-client-update (homeowner emails), future hb-chad outbound
    drafts (subs, vendors, clients). Audience is a homeowner, sub,
    vendor, or other professional reader who is paying or being paid by
    Chad.

Compose a system prompt:
    from home_builder_agent.core.chad_voice import chad_voice_system
    system = chad_voice_system("narrator")
    # or
    system = chad_voice_system("author")

Pull rules separately if you need to mix them with agent-specific output
requirements:
    from home_builder_agent.core.chad_voice import (
        COMPANY_DESCRIPTION, NARRATOR_RULES, CHAD_SIGNATURE_BLOCK,
    )

This module is the foundation step (#1) of the Chad Agent build path —
see docs/specs/chad-agent.md § Build order. Migrating an existing agent
to use it is a no-op semantically (same voice, fewer copies). New
agents should import from here rather than hand-rolling Chad-voice
prompts.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

CUSTOMER_NAME = "Chad Lynch"
COMPANY = "Palmetto Custom Homes"
JURISDICTION = "Baldwin County, Alabama"

COMPANY_DESCRIPTION = (
    f"{COMPANY}, a luxury custom home builder in {JURISDICTION}"
)
# Use COMPANY_DESCRIPTION as a plain noun phrase. The prefix functions below
# handle Chad's role (assistant-to-Chad vs. writing-as-Chad) so the role
# isn't double-stamped.

CHAD_SIGNATURE_BLOCK = f"""{CUSTOMER_NAME}
{COMPANY}
{JURISDICTION}
(251) 555-0100  |  chad@palmettocustomhomes.com"""

# Short signature for less-formal contexts (e.g., change-order approval emails
# where Chad's relationship with the recipient is already established).
CHAD_SHORT_SIGNATURE_BLOCK = f"""Chad
{COMPANY}
{JURISDICTION}"""


# ---------------------------------------------------------------------------
# Voice rules per mode
# ---------------------------------------------------------------------------

NARRATOR_RULES = """- Tight, operator-style prose. No hype, no filler.
- Status-led: most important thing first.
- Bullet points and short sections. No walls of text.
- Mobile context — Chad reads this on his phone.
- Action items must be concrete and immediately actionable.
- No corporate-speak, no AI hedging ("I think", "perhaps"), no apologies."""

AUTHOR_RULES = """- Warm and personal, not corporate.
- Confident and reassuring, not over-promising.
- Brief and scannable — busy readers, often on mobile.
- No jargon, no builder-speak.
- No hollow enthusiasm ("exciting progress!" etc.).
- One short paragraph per section max.
- Sign off with Chad's signature block."""


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

_NARRATOR_PREFIX = (
    f"You are {CUSTOMER_NAME}'s AI project assistant at {COMPANY_DESCRIPTION}."
)

_AUTHOR_PREFIX = (
    f"You write on behalf of {CUSTOMER_NAME}, the owner of "
    f"{COMPANY_DESCRIPTION}."
)


def chad_voice_system(mode: str = "narrator") -> str:
    """Compose a Chad-voice system prompt for a given mode.

    mode:
      "narrator" — agent speaks TO Chad (briefs, alerts, internal summaries)
      "author"   — agent speaks AS Chad (homeowner emails, sub/vendor drafts)

    Raises ValueError on unknown mode.
    """
    if mode == "narrator":
        return f"{_NARRATOR_PREFIX}\n\nVoice rules:\n{NARRATOR_RULES}"
    if mode == "author":
        return f"{_AUTHOR_PREFIX}\n\nVoice rules:\n{AUTHOR_RULES}"
    raise ValueError(
        f"Unknown chad_voice mode: {mode!r}. Valid: 'narrator', 'author'."
    )
