"""Tests for home_builder_agent.classifiers.supplier_email."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from home_builder_agent.classifiers.supplier_email import (
    SUBJECT_HINTS,
    SUPPLIER_DOMAIN_HINTS,
    _classify_event,
    extract_supplier_data,
    is_supplier_email,
    supplier_payload,
)
from home_builder_agent.scheduling.events import EventSeverity, EventType


# ---------------------------------------------------------------------------
# Heuristic detector — pure function, fast, no API
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("from_email,subject,expected", [
    ("orders@wholesaleplumbing.com", "Order #4471 confirmed", True),
    ("noreply@hammond-supply.com",   "PO 2023 shipping update", True),
    ("sales@andersonwindows.com",    "Your order has been delayed", True),
    ("noreply@homedepot.com",        "Pickup ready for tracking #12345", True),
    ("ceo@palmettocustomhomes.com",  "Lunch tomorrow?", False),
    ("chad@gmail.com",               "Just checking in", False),
])
def test_is_supplier_email_basic_cases(from_email, subject, expected):
    assert is_supplier_email({
        "from_email": from_email,
        "subject": subject,
        "snippet": "",
    }) is expected


def test_is_supplier_email_subject_only_signal():
    """Even from an unknown domain, a clear supplier subject signals."""
    assert is_supplier_email({
        "from_email": "info@example.com",
        "subject": "ETA update for your shipment",
        "snippet": "",
    }) is True


def test_is_supplier_email_po_in_snippet():
    """When neither sender domain nor subject signals, snippet PO# pattern still triggers."""
    assert is_supplier_email({
        "from_email": "info@example.com",
        "subject": "Update",
        "snippet": "Your PO #4471 has shipped via UPS.",
    }) is True


def test_is_supplier_email_friend_email_not_a_match():
    """Negative case — personal email with no supplier signals."""
    assert is_supplier_email({
        "from_email": "mom@example.com",
        "subject": "How was your weekend?",
        "snippet": "Hope all is well — call me back when you get a chance.",
    }) is False


def test_supplier_domain_hints_cover_chads_known_vendors():
    """Sanity — Chad's KB-known vendor name fragments are in the hint list."""
    assert "wholesaleplumbing" in SUPPLIER_DOMAIN_HINTS
    assert "anderson" in SUPPLIER_DOMAIN_HINTS
    assert "hammond" in SUPPLIER_DOMAIN_HINTS


def test_subject_hints_include_eta_and_backorder():
    """Sanity — the high-value supplier signal words are tracked."""
    assert "eta" in SUBJECT_HINTS
    assert "backorder" in SUBJECT_HINTS
    assert "delayed" in SUBJECT_HINTS


# ---------------------------------------------------------------------------
# _classify_event — pure mapping, no API
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action,sev,expected_type", [
    ("eta-update",            "info",     EventType.ETA_CHANGE.value),
    ("shipment-notification", "info",     EventType.ETA_CHANGE.value),
    ("delivery-confirmation", "info",     EventType.ETA_CHANGE.value),
    ("backorder",             "warning",  EventType.BACKORDER_DETECTED.value),
    ("stock-notice",          "info",     EventType.STOCK_CHANGE.value),
    ("price-quote",           "info",     EventType.PRICE_CHANGE.value),
    ("invoice",               "info",     EventType.ETA_CHANGE.value),
    ("other",                 "info",     EventType.ETA_CHANGE.value),
    ("unknown-action",        "warning",  EventType.ETA_CHANGE.value),  # fallback
])
def test_classify_event_type_mapping(action, sev, expected_type):
    out_type, out_sev = _classify_event(action, sev)
    assert out_type == expected_type
    assert out_sev == sev


def test_classify_event_invalid_severity_falls_back_to_info():
    """Defensive — bad severity values map to info rather than crash."""
    _, sev = _classify_event("eta-update", "wat")
    assert sev == "info"


def test_classify_event_blocking_severity_passes_through():
    """blocking is a real severity even though our LLM prompt only says
    info|warning|critical — accept it if it leaks through."""
    _, sev = _classify_event("backorder", "blocking")
    assert sev == "blocking"


# ---------------------------------------------------------------------------
# extract_supplier_data — Haiku-driven, mocked here
# ---------------------------------------------------------------------------

def _mock_response(text: str):
    """Build a fake Anthropic response object with .content[0].text + .usage."""
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    msg.usage = MagicMock(input_tokens=200, output_tokens=80)
    return msg


def _mock_client(response_text: str):
    client = MagicMock()
    client.messages.create.return_value = _mock_response(response_text)
    return client


def test_extract_returns_dict_for_real_supplier():
    client = _mock_client("""
{
  "is_supplier_email": true,
  "vendor_name": "Wholesale Plumbing",
  "vendor_category": "plumbing",
  "action_type": "eta-update",
  "po_or_order_ref": "PO-4471",
  "items_summary": "Kohler bath valves x6",
  "eta_or_ship_date": "2026-05-20",
  "severity": "warning",
  "summary": "Wholesale Plumbing pushed PO-4471 ETA to 2026-05-20 (was 2026-05-12).",
  "confidence": "high"
}
""")
    summary = {"from_email": "orders@wholesaleplumbing.com", "subject": "ETA update", "snippet": ""}
    data = extract_supplier_data(client, summary)
    assert data is not None
    assert data["vendor_name"] == "Wholesale Plumbing"
    assert data["event_type"] == EventType.ETA_CHANGE.value
    assert data["event_severity"] == "warning"
    assert data["action_type"] == "eta-update"


def test_extract_returns_none_when_not_supplier():
    """When LLM judges 'not a supplier email', return None — false-positive
    at the heuristic gate is filtered out here."""
    client = _mock_client('{"is_supplier_email": false}')
    data = extract_supplier_data(client, {"from_email": "x@y.com", "subject": "ok", "snippet": ""})
    assert data is None


def test_extract_handles_markdown_fences():
    """Sonnet sometimes returns JSON wrapped in ```json fences despite our
    instruction. Strip them."""
    client = _mock_client("""```json
{
  "is_supplier_email": true,
  "vendor_name": "Anderson Windows",
  "vendor_category": "windows",
  "action_type": "shipment-notification",
  "po_or_order_ref": "",
  "items_summary": "Andersen 400-series casement",
  "eta_or_ship_date": "",
  "severity": "info",
  "summary": "Andersen confirmed shipment.",
  "confidence": "high"
}
```""")
    data = extract_supplier_data(client, {"from_email": "x@y.com", "subject": "ok", "snippet": ""})
    assert data is not None
    assert data["vendor_name"] == "Anderson Windows"
    assert data["event_type"] == EventType.ETA_CHANGE.value


def test_extract_returns_none_on_invalid_json():
    """Defensive — never crash the watcher loop on a bad LLM response."""
    client = _mock_client("not json at all, the LLM lost its mind")
    data = extract_supplier_data(client, {"from_email": "x@y.com", "subject": "x", "snippet": ""})
    assert data is None


def test_extract_returns_none_on_api_failure():
    """Network/credential failures result in None, not exception bubble-up."""
    client = MagicMock()
    client.messages.create.side_effect = Exception("API down")
    data = extract_supplier_data(client, {"from_email": "x@y.com", "subject": "x", "snippet": ""})
    assert data is None


# ---------------------------------------------------------------------------
# supplier_payload — payload shape for Event
# ---------------------------------------------------------------------------

def test_supplier_payload_round_trip():
    extracted = {
        "vendor_name": "Wholesale Plumbing",
        "vendor_category": "plumbing",
        "po_or_order_ref": "PO-4471",
        "items_summary": "Kohler bath valves x6",
        "eta_or_ship_date": "2026-05-20",
        "action_type": "eta-update",
        "summary": "Wholesale Plumbing ETA update.",
    }
    p = supplier_payload(extracted)
    assert p["vendor_name"] == "Wholesale Plumbing"
    assert p["po_or_order_ref"] == "PO-4471"
    assert p["items_summary"] == "Kohler bath valves x6"
    assert p["from_email"] == ""  # caller fills this in


def test_supplier_payload_handles_missing_fields():
    """A sparse extraction (low confidence) still produces a valid payload."""
    p = supplier_payload({})
    assert p["vendor_name"] == "Unknown supplier"
    assert p["items_summary"] == ""
    assert p["action_type"] == "other"


# ---------------------------------------------------------------------------
# Event-summary integration — supplier payload renders correctly
# ---------------------------------------------------------------------------

def test_eta_change_summary_renders_with_supplier_payload():
    """End-to-end: supplier_payload → make_event → Event.summary uses
    the per-type template."""
    from home_builder_agent.scheduling.events import make_event, EventType, EventSeverity

    extracted = {
        "vendor_name": "Wholesale Plumbing",
        "vendor_category": "plumbing",
        "items_summary": "Kohler bath valves x6",
        "eta_or_ship_date": "2026-05-20",
        "po_or_order_ref": "PO-4471",
        "action_type": "eta-update",
        "summary": "ETA pushed.",
    }
    payload = supplier_payload(extracted)
    e = make_event(
        type=EventType.ETA_CHANGE, severity=EventSeverity.WARNING,
        payload=payload, source="supplier-email-watcher",
    )
    s = e.summary()
    assert "Wholesale Plumbing" in s
    assert "Kohler bath valves" in s
    assert "2026-05-20" in s


# ---------------------------------------------------------------------------
# _normalize_vendor_type — pure function, the V1 watcher's category map
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("category,expected", [
    # Direct matches in the schema's allowed set
    ("plumbing",   "plumbing"),
    ("electrical", "electrical"),
    ("lumber",     "lumber"),
    ("tile",       "tile"),
    ("appliance",  "appliance"),
    ("paint",      "paint"),
    ("hardware",   "hardware"),
    # Known aliases / pluralization
    ("cabinets",   "cabinet"),
    ("cabinet",    "cabinet"),
    # Categories outside the schema CHECK list — fall to 'other'
    ("hvac",       "other"),
    ("windows",    "other"),
    ("doors",      "other"),
    ("flooring",   "other"),
    ("concrete",   "other"),
    ("roofing",    "other"),
    ("insulation", "other"),
    ("general",    "other"),
    ("unknown",    "other"),
    # Defensive cases
    (None,         "other"),
    ("",           "other"),
    ("nonsense",   "other"),
    ("PLUMBING",   "plumbing"),  # case-insensitive
])
def test_normalize_vendor_type(category, expected):
    from home_builder_agent.scheduling.store_postgres import _normalize_vendor_type
    assert _normalize_vendor_type(category) == expected
