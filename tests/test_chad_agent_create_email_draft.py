"""Tests for chad_agent's create_email_draft tool.

Added 2026-05-11 to close a gap surfaced during a live iOS demo:
Chad asked the agent to draft an email and the agent had no way to
create one.

Per Patton AI's outbound-comms rule (CLAUDE.md): drafts only — never
auto-sends. The tool wraps Gmail's drafts().create() endpoint using
the already-granted gmail.compose OAuth scope.

All Gmail API touches are mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from home_builder_agent.agents import chad_agent


# ---------------------------------------------------------------------------
# TOOLS registry
# ---------------------------------------------------------------------------


def test_create_email_draft_registered_in_tools():
    names = [t["name"] for t in chad_agent.TOOLS]
    assert "create_email_draft" in names


def test_create_email_draft_schema_to_subject_body_required_cc_optional():
    tool = next(t for t in chad_agent.TOOLS if t["name"] == "create_email_draft")
    required = set(tool["input_schema"]["required"])
    assert required == {"to", "subject", "body"}
    props = tool["input_schema"]["properties"]
    assert "cc" in props
    assert "cc" not in required


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("to,subject,body,cc,expected_msg", [
    ("not-an-email", "subj", "body", None, "'to' must be a valid email"),
    ("", "subj", "body", None, "'to' must be a valid email"),
    ("a@b.com", "", "body", None, "subject is required"),
    ("a@b.com", "subj", "", None, "body is required"),
    ("a@b.com", "subj", "body", "bad-cc", "'cc' must be a valid email"),
])
def test_create_email_draft_validation(to, subject, body, cc, expected_msg):
    out, cost = chad_agent._tool_create_email_draft(to, subject, body, cc)
    assert expected_msg in out
    assert cost == 0.0


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_create_email_draft_happy_path_returns_gmail_url():
    """Verify the Gmail API is called and the result formats as a draft
    URL Chad can click."""
    fake_creds = MagicMock()
    fake_service = MagicMock()
    fake_service.users().drafts().create().execute.return_value = {
        "id": "draft_id_xyz",
        "message": {"id": "msg_id_abc", "threadId": "thread_123"},
    }

    with patch("home_builder_agent.core.auth.get_credentials", return_value=fake_creds), \
         patch(
             "home_builder_agent.integrations.gmail.gmail_service",
             return_value=fake_service,
         ):
        out, cost = chad_agent._tool_create_email_draft(
            to="chad@palmettocustomhomes.com",
            subject="Whitfield update — week of 5/10",
            body="Quick check-in on framing progress.",
        )

    assert "Draft created" in out
    assert "chad@palmettocustomhomes.com" in out
    assert "Whitfield update" in out
    assert "mail.google.com" in out
    assert "drafts" in out.lower()
    assert "msg_id_abc" in out  # message_id surfaces in the URL
    assert cost == 0.0


def test_create_email_draft_attaches_cc_when_provided():
    """CC header must be set on the MIME message when cc is provided."""
    fake_creds = MagicMock()
    fake_service = MagicMock()
    fake_service.users().drafts().create().execute.return_value = {
        "id": "draft_id_xyz",
        "message": {"id": "msg_id_abc"},
    }

    with patch("home_builder_agent.core.auth.get_credentials", return_value=fake_creds), \
         patch(
             "home_builder_agent.integrations.gmail.gmail_service",
             return_value=fake_service,
         ):
        out, cost = chad_agent._tool_create_email_draft(
            to="primary@x.com",
            subject="S",
            body="B",
            cc="cc@x.com",
        )

    assert "CC:" in out
    assert "cc@x.com" in out


def test_create_email_draft_surfaces_gmail_api_error():
    """Gmail API exception → clean error string, no crash."""
    fake_creds = MagicMock()
    fake_service = MagicMock()
    fake_service.users().drafts().create().execute.side_effect = (
        RuntimeError("invalid_grant: token expired")
    )

    with patch("home_builder_agent.core.auth.get_credentials", return_value=fake_creds), \
         patch(
             "home_builder_agent.integrations.gmail.gmail_service",
             return_value=fake_service,
         ):
        out, cost = chad_agent._tool_create_email_draft(
            "a@b.com", "subj", "body"
        )

    assert "Gmail API call failed" in out
    assert "invalid_grant" in out
    assert cost == 0.0


def test_create_email_draft_does_not_call_send():
    """Defensive: confirm the implementation hits drafts().create() —
    NOT messages().send(). Drafts-only is a binding Patton AI rule."""
    fake_creds = MagicMock()
    fake_service = MagicMock()
    fake_service.users().drafts().create().execute.return_value = {
        "id": "x", "message": {"id": "m"},
    }

    with patch("home_builder_agent.core.auth.get_credentials", return_value=fake_creds), \
         patch(
             "home_builder_agent.integrations.gmail.gmail_service",
             return_value=fake_service,
         ):
        chad_agent._tool_create_email_draft(
            "to@x.com", "subj", "body"
        )

    # drafts().create() should have been called (it's the .execute on
    # the chain we mocked above). messages().send() should NOT.
    fake_service.users().drafts().create.assert_called()
    fake_service.users().messages().send.assert_not_called()
