"""Tests for chad_agent's web_fetch tool.

Added 2026-05-11 to close a gap surfaced during a live iOS demo:
Chad asked the agent to pull a FEMA FIRM panel page and the agent
couldn't reach the public web.

Uses stdlib only (urllib + html.parser) — no new dependency. All
network touches are mocked.
"""

from __future__ import annotations

import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from home_builder_agent.agents import chad_agent


# ---------------------------------------------------------------------------
# TOOLS registry
# ---------------------------------------------------------------------------


def test_web_fetch_registered_in_tools():
    names = [t["name"] for t in chad_agent.TOOLS]
    assert "web_fetch" in names


def test_web_fetch_schema_requires_url():
    tool = next(t for t in chad_agent.TOOLS if t["name"] == "web_fetch")
    assert set(tool["input_schema"]["required"]) == {"url"}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url,expected_msg", [
    ("", "url is required"),
    (None, "url is required"),
    ("ftp://example.com", "only http/https"),
    ("not a url", "only http/https"),
])
def test_web_fetch_validation_rejects_bad_urls(url, expected_msg):
    out, cost = chad_agent._tool_web_fetch(url)
    assert expected_msg in out
    assert cost == 0.0


# ---------------------------------------------------------------------------
# Network error handling — never raises
# ---------------------------------------------------------------------------


def test_web_fetch_handles_http_error_cleanly():
    err = urllib.error.HTTPError(
        "https://example.com", 404, "Not Found", {}, None  # type: ignore
    )
    with patch("urllib.request.urlopen", side_effect=err):
        out, cost = chad_agent._tool_web_fetch("https://example.com/missing")
    assert "HTTP 404" in out
    assert cost == 0.0


def test_web_fetch_handles_connection_error_cleanly():
    err = urllib.error.URLError("connection refused")
    with patch("urllib.request.urlopen", side_effect=err):
        out, cost = chad_agent._tool_web_fetch("https://nonexistent.example")
    assert "connection error" in out
    assert "connection refused" in out
    assert cost == 0.0


# ---------------------------------------------------------------------------
# HTML extraction
# ---------------------------------------------------------------------------


def _mock_response(content_type: str, body: bytes):
    resp = MagicMock()
    resp.headers = {"Content-Type": content_type}
    resp.read = MagicMock(return_value=body)
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=None)
    return resp


def test_web_fetch_strips_html_tags_and_skips_scripts():
    """HTML in → readable text out. <script> + <style> contents dropped."""
    html_bytes = (
        b"<html><head><title>T</title>"
        b"<script>alert('xss');</script>"
        b"<style>body{color:red}</style></head>"
        b"<body><h1>Header</h1>"
        b"<p>Paragraph one.</p>"
        b"<p>Paragraph two with <a href='/'>a link</a>.</p>"
        b"<script>console.log('also gone')</script>"
        b"</body></html>"
    )
    resp = _mock_response("text/html; charset=utf-8", html_bytes)

    with patch("urllib.request.urlopen", return_value=resp):
        out, cost = chad_agent._tool_web_fetch("https://example.com/page")

    assert "Header" in out
    assert "Paragraph one" in out
    assert "Paragraph two" in out
    assert "a link" in out
    assert "alert" not in out
    assert "console.log" not in out
    assert "color:red" not in out
    assert cost == 0.0


def test_web_fetch_caps_output_at_50kb():
    big_html = b"<html><body>" + (b"<p>line</p>" * 10_000) + b"</body></html>"
    resp = _mock_response("text/html", big_html)

    with patch("urllib.request.urlopen", return_value=resp):
        out, cost = chad_agent._tool_web_fetch("https://example.com/long")

    assert "[web_fetch:" in out
    assert "output capped at" in out
    body = out.rsplit("[web_fetch:", 1)[0]
    assert len(body) <= 50 * 1024 + 100


def test_web_fetch_rejects_non_text_binary_content():
    resp = _mock_response("application/pdf", b"%PDF-1.4\xde\xad\xbe\xef" * 100)

    with patch("urllib.request.urlopen", return_value=resp):
        out, cost = chad_agent._tool_web_fetch("https://example.com/file.pdf")

    assert "non-text content" in out
    assert "application/pdf" in out
    assert cost == 0.0


def test_web_fetch_handles_plain_text():
    """text/plain → returned as-is (no HTML extraction)."""
    body = b"Just plain text.\nNo HTML here.\n"
    resp = _mock_response("text/plain; charset=utf-8", body)

    with patch("urllib.request.urlopen", return_value=resp):
        out, cost = chad_agent._tool_web_fetch("https://example.com/robots.txt")

    assert "Just plain text" in out
    assert "No HTML here" in out
    assert cost == 0.0
