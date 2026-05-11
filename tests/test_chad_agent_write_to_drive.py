"""Tests for chad_agent's write_to_drive tool.

Added 2026-05-11 to close a gap surfaced during a live iOS demo:
when Chad asked the agent to save the chat transcript to the project's
Drive folder, the agent had no way to do it.

All Google API touches are mocked so tests run offline.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from home_builder_agent.agents import chad_agent


# ---------------------------------------------------------------------------
# TOOLS registry — schema shape
# ---------------------------------------------------------------------------


def test_write_to_drive_registered_in_tools():
    names = [t["name"] for t in chad_agent.TOOLS]
    assert "write_to_drive" in names


def test_write_to_drive_schema_requires_three_fields():
    tool = next(t for t in chad_agent.TOOLS if t["name"] == "write_to_drive")
    required = set(tool["input_schema"]["required"])
    assert required == {"folder_id", "file_name", "content"}
    # mime_type is optional with default
    props = tool["input_schema"]["properties"]
    assert "mime_type" in props
    assert "mime_type" not in required


# ---------------------------------------------------------------------------
# Validation fast-paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("folder_id,file_name,content,expected_msg", [
    ("", "x.md", "hi", "folder_id is required"),
    ("f", "", "hi", "file_name is required"),
    ("f", "x.md", "", "content is empty"),
])
def test_write_to_drive_validation_rejects_empty_inputs(
    folder_id, file_name, content, expected_msg,
):
    out, cost = chad_agent._tool_write_to_drive(folder_id, file_name, content)
    assert expected_msg in out
    assert cost == 0.0


def test_write_to_drive_dry_run_skips_drive_call():
    """Dry-run reports the would-be action without invoking auth/upload."""
    out, cost = chad_agent._tool_write_to_drive(
        "folder_abc",
        "test.md",
        "hello world",
        dry_run=True,
    )
    assert "dry-run" in out.lower()
    assert "test.md" in out
    assert cost == 0.0


# ---------------------------------------------------------------------------
# Happy path + error surfacing
# ---------------------------------------------------------------------------


def test_write_to_drive_happy_path_calls_drive_with_right_args():
    """Real path: auth, build service, upload_binary_file with our args,
    return the webViewLink. All three Google touches are mocked."""
    fake_creds = MagicMock()
    fake_service = MagicMock()
    fake_result = {"id": "drive_id_123", "webViewLink": "https://drive.google.com/file/abc"}

    with patch("home_builder_agent.core.auth.get_credentials", return_value=fake_creds), \
         patch("home_builder_agent.integrations.drive.drive_service", return_value=fake_service), \
         patch(
             "home_builder_agent.integrations.drive.upload_binary_file",
             return_value=fake_result,
         ) as mock_upload:
        out, cost = chad_agent._tool_write_to_drive(
            folder_id="folder_abc",
            file_name="chat-export.md",
            content="# Chad chat 2026-05-10\n\nHello world.",
            mime_type="text/markdown",
        )

    assert "Saved" in out
    assert "chat-export.md" in out
    assert "https://drive.google.com/file/abc" in out
    assert cost == 0.0

    # Verify upload was called with bytes-encoded content + our params
    call_kwargs = mock_upload.call_args.kwargs
    assert call_kwargs["file_name"] == "chat-export.md"
    assert call_kwargs["parent_folder_id"] == "folder_abc"
    assert call_kwargs["mime_type"] == "text/markdown"
    assert call_kwargs["file_bytes"] == b"# Chad chat 2026-05-10\n\nHello world."


def test_write_to_drive_surfaces_upload_error_cleanly():
    """If upload_binary_file raises, return a clean error string — don't crash."""
    with patch("home_builder_agent.core.auth.get_credentials", return_value=MagicMock()), \
         patch("home_builder_agent.integrations.drive.drive_service", return_value=MagicMock()), \
         patch(
             "home_builder_agent.integrations.drive.upload_binary_file",
             side_effect=RuntimeError("quota exceeded"),
         ):
        out, cost = chad_agent._tool_write_to_drive(
            "folder_abc", "x.md", "content"
        )

    assert "upload failed" in out
    assert "quota exceeded" in out
    assert cost == 0.0
