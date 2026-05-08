"""supplier_email.py — detect + extract structured data from supplier emails.

Phase 2 item 11 (CLAUDE.md backlog) — V1 feeder for the Vendor Intelligence
System. Until catalog scrapers exist (Phase 3), supplier emails are the
primary live signal on per-order ETAs, backorders, and lead-time updates.

Data flow:
    inbox-watcher (5 min poll)
        ↓ classify_thread (existing Haiku call — urgency/category)
        ↓ ALSO: is_supplier_email() heuristic
        ↓ if supplier: extract_supplier_data() (Haiku — vendor, action, items, ETA, severity)
        ↓ emit Event via insert_event() — type=eta-change | backorder-detected | etc.
        ↓ optionally upsert Vendor row

Until the Vendor Intelligence System is fully live, the watcher emits
Events into home_builder.event with source='supplier-email-watcher'.
The Notification dispatcher's in-app notification on notification-feed
surface lets Chad see the update on his iOS app.

When Vendor Intelligence ships, the same events will additionally drive
upserts into home_builder.vendor + home_builder.vendor_item — but the
Event itself is the load-bearing signal regardless of where vendor
state ends up persisted.

Cost target: Haiku for both detection and extraction. ~$0.003 per email.
At Chad's inbox volume (a few supplier emails per day) this is
~$0.10/month.
"""

from __future__ import annotations

import json
import re
from typing import Any

from home_builder_agent.config import CLASSIFIER_MODEL
from home_builder_agent.scheduling.events import EventSeverity, EventType


# ---------------------------------------------------------------------------
# Heuristic detector — fast, no API call
# ---------------------------------------------------------------------------

# Sender-domain hints that strongly indicate a supplier. Matched as substring
# against from_email so partial-domain matches work
# (e.g., "wholesale-plumbing.example.com" matches "wholesale" + "plumbing").
SUPPLIER_DOMAIN_HINTS = (
    # Specific vendors from Chad's KB / Baldwin County market
    "wholesaleplumbing", "anderson", "hammond", "ferguson",
    "84lumber", "bmc", "homedepot", "lowes",
    # Generic supplier-segment terms
    "lumber", "supply", "building", "windows", "cabinet", "appliance",
    "doors", "hvac", "electric", "plumbing", "tile", "flooring",
    "paint", "hardware", "concrete", "roofing", "insulation",
    "shop", "wholesale", "industrial",
)

# Subject-line signals that the email is about an order or shipment.
SUBJECT_HINTS = (
    "order", "po#", "po ", "purchase order",
    "shipment", "shipping", "shipped", "tracking", "in transit",
    "eta", "delivery", "delayed", "backorder", "back order", "backordered",
    "confirmation", "ack", "acknowledged",
    "in stock", "out of stock", "stock update",
    "quote", "quotation", "estimate",
    "invoice",
)

# PO# / order number pattern in snippet body.
_PO_PATTERN = re.compile(r"\b(po|order|invoice)\s*#?\s*\d{3,}\b", re.IGNORECASE)


def is_supplier_email(summary: dict) -> bool:
    """Heuristic — match sender domain or subject signals.

    `summary` is the dict returned by gmail.get_thread_summary
    (subject, from_name, from_email, snippet). Pure function — no I/O.

    Designed to favor false positives over false negatives at this gate;
    the LLM extraction step downstream (`extract_supplier_data`) returns
    None for things that look like supplier emails to the heuristic but
    aren't actually about an order. Cheap to over-fire here.
    """
    sender = (summary.get("from_email") or "").lower()
    subject = (summary.get("subject") or "").lower()
    snippet = (summary.get("snippet") or "").lower()

    for hint in SUPPLIER_DOMAIN_HINTS:
        if hint in sender:
            return True

    for hint in SUBJECT_HINTS:
        if hint in subject:
            return True

    if _PO_PATTERN.search(snippet):
        return True

    return False


# ---------------------------------------------------------------------------
# LLM extractor — structured payload via Haiku
# ---------------------------------------------------------------------------

EXTRACT_PROMPT = """You are extracting structured data from a supplier email for Palmetto Custom Homes, a luxury custom home builder in Baldwin County, Alabama.

Email:
  From: {from_name} <{from_email}>
  Subject: {subject}
  Snippet: {snippet}

Extract a single JSON object with EXACTLY this shape:

{{
  "is_supplier_email": true | false,
  "vendor_name": "<plain-text supplier name (e.g. 'Wholesale Plumbing Supply'), else empty>",
  "vendor_category": "plumbing" | "electrical" | "hvac" | "lumber" | "windows" | "cabinets" | "appliance" | "tile" | "flooring" | "paint" | "hardware" | "concrete" | "roofing" | "insulation" | "doors" | "general" | "unknown",
  "action_type": "order-acknowledgement" | "shipment-notification" | "eta-update" | "backorder" | "delivery-confirmation" | "stock-notice" | "price-quote" | "invoice" | "other",
  "po_or_order_ref": "<order/PO ref if mentioned, else empty string>",
  "items_summary": "<one-line summary of materials/SKUs mentioned (e.g. 'Pella casement windows ×8'), else empty string>",
  "eta_or_ship_date": "<ISO date YYYY-MM-DD if mentioned, else empty string>",
  "severity": "info" | "warning" | "critical",
  "summary": "<one short sentence describing the email in operator voice — no fluff>",
  "confidence": "high" | "low"
}}

Rules:
- is_supplier_email=false if this is just a marketing newsletter, blog post, or general industry email (not about a specific order).
- vendor_category: pick the closest match; "unknown" only if the email is truly generic.
- severity:
    info     = routine confirmations, ack of placed order, normal ETA
    warning  = ETA delayed by ≤2 weeks, partial backorder, minor stock concern
    critical = ETA delayed >2 weeks, full backorder with no ETA, cancellation, supplier going out of business
- summary should be ONE SENTENCE Chad would read in a notification feed (e.g.,
  "Wholesale Plumbing confirmed PO #4471 ships Monday, ETA Thursday.").

Output ONLY the JSON object — no preamble, no markdown fences."""


# Map (action_type, severity) to (EventType, EventSeverity).
def _classify_event(action_type: str, severity: str) -> tuple[str, str]:
    """Map LLM-extracted action+severity to canonical Event types.

    Returns (event_type, event_severity) as strings. Never raises;
    unknown action_types fall back to ETA_CHANGE.
    """
    sev = severity if severity in ("info", "warning", "critical", "blocking") else "info"

    type_map = {
        "order-acknowledgement":  EventType.ETA_CHANGE.value,
        "shipment-notification":  EventType.ETA_CHANGE.value,
        "eta-update":             EventType.ETA_CHANGE.value,
        "delivery-confirmation":  EventType.ETA_CHANGE.value,
        "backorder":              EventType.BACKORDER_DETECTED.value,
        "stock-notice":           EventType.STOCK_CHANGE.value,
        "price-quote":            EventType.PRICE_CHANGE.value,
        "invoice":                EventType.ETA_CHANGE.value,  # informational
        "other":                  EventType.ETA_CHANGE.value,
    }
    return type_map.get(action_type, EventType.ETA_CHANGE.value), sev


def extract_supplier_data(client, summary: dict, *, model: str = CLASSIFIER_MODEL) -> dict | None:
    """Call Haiku to extract structured supplier data from an email.

    Returns the extracted dict, or None on:
      - is_supplier_email=false in the LLM response (false-positive at heuristic)
      - JSON parse failure
      - API failure (caller decides how to retry; defensive None here)

    The returned dict has all schema fields plus computed `event_type` +
    `event_severity` ready for `make_event`.
    """
    prompt = EXTRACT_PROMPT.format(
        from_name=summary.get("from_name", ""),
        from_email=summary.get("from_email", ""),
        subject=summary.get("subject", ""),
        snippet=(summary.get("snippet") or "")[:1200],  # cap for cost
    )

    try:
        response = client.messages.create(
            model=model,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        return None

    raw = response.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
    raw = re.sub(r"\n?\s*```\s*$", "", raw)

    try:
        data: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if not data.get("is_supplier_email"):
        return None

    # Compute downstream fields the caller needs to emit an Event.
    event_type, event_severity = _classify_event(
        data.get("action_type", "other"),
        data.get("severity", "info"),
    )
    data["event_type"] = event_type
    data["event_severity"] = event_severity

    # Track usage for cost reporting at the call site.
    data["_usage"] = response.usage
    return data


def supplier_payload(extracted: dict) -> dict:
    """Build an Event payload from the extracted dict.

    Mirrors the per-type payload contract loosely — these fields are
    available to the summary template in events.py.
    """
    return {
        "vendor_name": extracted.get("vendor_name") or "Unknown supplier",
        "vendor_category": extracted.get("vendor_category") or "unknown",
        "po_or_order_ref": extracted.get("po_or_order_ref") or "",
        "items_summary": extracted.get("items_summary") or "",
        "eta_or_ship_date": extracted.get("eta_or_ship_date") or "",
        "action_type": extracted.get("action_type", "other"),
        "summary": extracted.get("summary", ""),
        "from_email": "",  # populated by the caller from the email summary
    }
