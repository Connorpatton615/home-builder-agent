"""classifiers/email.py — Haiku-based email thread classifier.

Used by both the gmail_followup agent (batch classification across the
inbox) and the inbox watcher (per-event classification as new threads
arrive).

Lives outside agents/ because it's shared by an agent + a watcher; lives
outside core/ because it carries an Anthropic prompt (core/ is reserved
for prompt-free infrastructure per CLAUDE.md).
"""

import json
import re

from home_builder_agent.config import CLASSIFIER_MODEL


def classify_thread(client, summary):
    """Ask Haiku: does this thread need follow-up from Chad?

    Args:
        client: Anthropic client (from core.claude_client.make_client)
        summary: dict from gmail.get_thread_summary

    Returns:
        (classification_dict, usage) where classification_dict has keys
        {needs_followup: bool, urgency: str, reason: str}.
    """
    prompt = f"""Classify whether this email thread needs a follow-up from Chad (a custom home builder running $1M+ luxury projects in Baldwin County, AL).

THREAD METADATA:
- Subject: {summary['subject']}
- From: {summary['from_name']} <{summary['from_email']}>
- Last message from Chad? {summary['last_from_me']}
- Days since last message: {summary['days_old']}
- Message count in thread: {summary['message_count']}
- Snippet: {summary['snippet']}

Return ONLY a JSON object (no fence, no preamble):
{{
  "needs_followup": <true|false>,
  "urgency": "<high|medium|low|none>",
  "reason": "<one short phrase explaining why>"
}}

Classification rules:
- If last message is FROM Chad and >2 days old with no response → likely needs nudge (medium urgency).
- If last message is TO Chad (he hasn't replied) and >1 day old → he owes a response (high urgency if from a paying client/vendor; medium otherwise).
- Newsletter/marketing/notification → needs_followup: false, urgency: none.
- Automated bookings/confirmations → false unless action needed.
- Internal team CC where someone else is primary → false unless directly addressed.
- Be biased toward FALSE for ambiguous threads — Chad doesn't want a checklist full of noise."""

    response = client.messages.create(
        model=CLASSIFIER_MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        result = {"needs_followup": False, "urgency": "none",
                  "reason": "classifier output unparseable"}

    return result, response.usage
