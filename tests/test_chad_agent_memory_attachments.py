"""Tests for chad_agent's memory + attachments integration.

We mock the Anthropic client at the call boundary so tests run offline.
The goal is to verify the integration: prior turns + summary land in
the request, vision blocks build correctly, and the persistence write
fires after `message_complete`.

Coverage map:
  - vision-block construction from JPEG/PNG bytes
  - vision-block rejects unsupported media_types
  - chad_turn_stream injects rolling_summary into system prompt
  - chad_turn_stream prepends prior turns
  - chad_turn_stream persists user + assistant after end_turn
  - chad_turn (non-streaming) round-trips memory the same way
  - turn 1 → turn 2 sees turn 1 in history (the smoke-test the user
    explicitly called for)
"""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Test fixtures: each test gets an isolated SQLite DB.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_file = tmp_path / "agent_memory.db"
    monkeypatch.setenv("ASK_CONVERSATIONS_DB", str(db_file))
    from home_builder_agent.agents import conversation_store as cs
    if hasattr(cs._thread_local, "conn"):
        try:
            cs._thread_local.conn.close()
        except Exception:
            pass
        del cs._thread_local.conn
    if hasattr(cs._thread_local, "path"):
        del cs._thread_local.path
    yield db_file


# ---------------------------------------------------------------------------
# Vision-block construction
# ---------------------------------------------------------------------------

class TestBuildUserContent:
    def test_no_images_returns_bare_string(self):
        from home_builder_agent.agents.chad_agent import _build_user_content
        out = _build_user_content("hello", None)
        assert out == "hello"

    def test_empty_images_returns_bare_string(self):
        from home_builder_agent.agents.chad_agent import _build_user_content
        out = _build_user_content("hello", [])
        assert out == "hello"

    def test_jpeg_image_builds_vision_block(self):
        from home_builder_agent.agents.chad_agent import (
            ImageInput, _build_user_content,
        )
        # JPEG magic bytes
        jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00fake"
        img = ImageInput(media_type="image/jpeg", data=jpeg_bytes)
        out = _build_user_content("look at this", [img])
        assert isinstance(out, list)
        assert out[0] == {"type": "text", "text": "look at this"}
        assert out[1]["type"] == "image"
        assert out[1]["source"]["type"] == "base64"
        assert out[1]["source"]["media_type"] == "image/jpeg"
        # data is base64 of the bytes
        decoded = base64.b64decode(out[1]["source"]["data"])
        assert decoded == jpeg_bytes

    def test_png_image_builds_vision_block(self):
        from home_builder_agent.agents.chad_agent import (
            ImageInput, _build_user_content,
        )
        png_bytes = b"\x89PNG\r\n\x1a\nfake"
        img = ImageInput(media_type="image/png", data=png_bytes)
        out = _build_user_content("what's wrong with this header?", [img])
        assert out[1]["source"]["media_type"] == "image/png"
        assert base64.b64decode(out[1]["source"]["data"]) == png_bytes

    def test_multiple_images_attach_in_order(self):
        from home_builder_agent.agents.chad_agent import (
            ImageInput, _build_user_content,
        )
        a = ImageInput(media_type="image/jpeg", data=b"first")
        b = ImageInput(media_type="image/png", data=b"second")
        out = _build_user_content("two photos", [a, b])
        assert len(out) == 3  # text + 2 images
        assert out[1]["source"]["media_type"] == "image/jpeg"
        assert out[2]["source"]["media_type"] == "image/png"

    def test_unsupported_media_type_rejected(self):
        from home_builder_agent.agents.chad_agent import (
            ImageInput, _build_user_content,
        )
        bad = ImageInput(media_type="application/pdf", data=b"%PDF-1.4")
        with pytest.raises(ValueError, match="Unsupported image media_type"):
            _build_user_content("nope", [bad])


# ---------------------------------------------------------------------------
# Mock helpers for streaming + non-streaming Anthropic responses
# ---------------------------------------------------------------------------

def _fake_text_block(text: str):
    """Build a stand-in for an Anthropic content block with type='text'."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _fake_final_message(text: str):
    """Build a stand-in for stream.get_final_message() with stop=end_turn."""
    msg = MagicMock()
    msg.stop_reason = "end_turn"
    msg.content = [_fake_text_block(text)]
    msg.usage = MagicMock(input_tokens=100, output_tokens=50)
    return msg


def _fake_stream_context(answer: str):
    """Build a context manager that emulates client.messages.stream(...).

    The body is iterable (yielding zero events — text streams not
    asserted at this layer) and exposes get_final_message().
    """
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=cm)
    cm.__exit__ = MagicMock(return_value=False)
    cm.__iter__ = MagicMock(return_value=iter([]))
    cm.get_final_message = MagicMock(return_value=_fake_final_message(answer))
    return cm


def _fake_response(text: str):
    """Build a stand-in for client.messages.create(...) (non-streaming)."""
    resp = MagicMock()
    resp.stop_reason = "end_turn"
    resp.content = [_fake_text_block(text)]
    resp.usage = MagicMock(input_tokens=100, output_tokens=50)
    return resp


# ---------------------------------------------------------------------------
# Streaming path: memory + attachments
# ---------------------------------------------------------------------------

class TestStreamMemory:
    def _drain(self, gen):
        """Consume a generator and collect (event_type, payload) pairs."""
        events = []
        for _, etype, payload in gen:
            events.append((etype, payload))
        return events

    def test_no_conversation_id_does_not_persist(self):
        from home_builder_agent.agents import chad_agent, conversation_store
        fake_client = MagicMock()
        fake_client.messages.stream = MagicMock(
            return_value=_fake_stream_context("Just an answer."),
        )
        with patch.object(chad_agent, "make_client", return_value=fake_client):
            events = self._drain(chad_agent.chad_turn_stream("ping"))
        assert events[-1][0] == "message_complete"
        # No conversation row created.
        with conversation_store.get_conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM conversations"
            ).fetchone()["n"]
        assert n == 0

    def test_conversation_id_persists_user_and_assistant(self):
        from home_builder_agent.agents import chad_agent, conversation_store
        fake_client = MagicMock()
        fake_client.messages.stream = MagicMock(
            return_value=_fake_stream_context("Three drafts on Whitfield."),
        )
        with patch.object(chad_agent, "make_client", return_value=fake_client):
            events = self._drain(chad_agent.chad_turn_stream(
                "what's pending?", conversation_id="conv-S1",
            ))
        # message_complete fired
        assert events[-1][0] == "message_complete"
        # Both user + assistant rows exist
        rows = conversation_store.load_recent_turns("conv-S1", n=10)
        assert len(rows) == 2
        assert rows[0]["role"] == "user"
        assert rows[0]["content"] == "what's pending?"
        assert rows[1]["role"] == "assistant"
        assert rows[1]["content"] == "Three drafts on Whitfield."

    def test_turn_2_sees_turn_1(self):
        """The smoke test the user prompt explicitly called for."""
        from home_builder_agent.agents import chad_agent, conversation_store
        fake_client = MagicMock()
        fake_client.messages.stream = MagicMock(
            return_value=_fake_stream_context("framing's at 60%."),
        )
        with patch.object(chad_agent, "make_client", return_value=fake_client):
            list(chad_agent.chad_turn_stream(
                "what's the status of Whitfield framing?",
                conversation_id="conv-T",
            ))

            # Capture what's sent on turn 2.
            captured_messages = {}
            captured_system = {}

            def capture_stream(*, model, max_tokens, system, tools, messages):
                captured_system["text"] = system
                captured_messages["list"] = messages
                return _fake_stream_context("It's still on track.")

            fake_client.messages.stream = MagicMock(side_effect=capture_stream)
            list(chad_agent.chad_turn_stream(
                "what about the next phase?",
                conversation_id="conv-T",
            ))

        msgs = captured_messages["list"]
        # turn 1 user + turn 1 assistant + turn 2 user
        assert len(msgs) == 3
        assert msgs[0] == {
            "role": "user",
            "content": "what's the status of Whitfield framing?",
        }
        assert msgs[1] == {"role": "assistant", "content": "framing's at 60%."}
        assert msgs[2]["role"] == "user"
        assert msgs[2]["content"] == "what about the next phase?"

        # And both turns are persisted.
        rows = conversation_store.load_recent_turns("conv-T", n=10)
        assert [r["content"] for r in rows] == [
            "what's the status of Whitfield framing?",
            "framing's at 60%.",
            "what about the next phase?",
            "It's still on track.",
        ]

    def test_rolling_summary_prefixed_into_system(self):
        from home_builder_agent.agents import chad_agent, conversation_store
        # Pre-seed a summary.
        conversation_store.get_or_create("conv-Sum")
        conversation_store.update_rolling_summary(
            "conv-Sum", "Earlier: Chad asked about framing dates."
        )

        captured_system = {}

        def capture_stream(*, system, **kw):
            captured_system["text"] = system
            return _fake_stream_context("done.")

        fake_client = MagicMock()
        fake_client.messages.stream = MagicMock(side_effect=capture_stream)
        with patch.object(chad_agent, "make_client", return_value=fake_client):
            list(chad_agent.chad_turn_stream(
                "hello", conversation_id="conv-Sum",
            ))

        assert "Earlier conversation context" in captured_system["text"]
        assert "framing dates" in captured_system["text"]

    def test_images_attached_to_first_user_message(self):
        from home_builder_agent.agents import chad_agent
        from home_builder_agent.agents.chad_agent import ImageInput

        captured = {}

        def capture_stream(*, messages, **kw):
            captured["messages"] = messages
            return _fake_stream_context("Looks like a header issue.")

        fake_client = MagicMock()
        fake_client.messages.stream = MagicMock(side_effect=capture_stream)
        img = ImageInput(media_type="image/jpeg", data=b"\xff\xd8\xfffake")
        with patch.object(chad_agent, "make_client", return_value=fake_client):
            list(chad_agent.chad_turn_stream(
                "what's wrong with this?",
                images=[img],
                conversation_id="conv-IMG",
            ))

        msgs = captured["messages"]
        assert msgs[0]["role"] == "user"
        content = msgs[0]["content"]
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "what's wrong with this?"}
        assert content[1]["type"] == "image"
        assert content[1]["source"]["media_type"] == "image/jpeg"


# ---------------------------------------------------------------------------
# Non-streaming chad_turn — same memory contract
# ---------------------------------------------------------------------------

class TestChadTurnMemory:
    def test_chad_turn_persists_with_conversation_id(self):
        from home_builder_agent.agents import chad_agent, conversation_store
        fake_client = MagicMock()
        fake_client.messages.create = MagicMock(
            return_value=_fake_response("the answer."),
        )
        with patch.object(chad_agent, "make_client", return_value=fake_client):
            result = chad_agent.chad_turn(
                "non-streaming question",
                conversation_id="conv-NS",
            )
        assert result["answer"] == "the answer."
        rows = conversation_store.load_recent_turns("conv-NS", n=10)
        assert len(rows) == 2
        assert rows[0]["content"] == "non-streaming question"
        assert rows[1]["content"] == "the answer."

    def test_chad_turn_without_conversation_id_does_not_persist(self):
        from home_builder_agent.agents import chad_agent, conversation_store
        fake_client = MagicMock()
        fake_client.messages.create = MagicMock(
            return_value=_fake_response("one-shot answer."),
        )
        with patch.object(chad_agent, "make_client", return_value=fake_client):
            chad_agent.chad_turn("ping")
        with conversation_store.get_conn() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        assert n == 0

    def test_chad_turn_loads_prior_turns(self):
        from home_builder_agent.agents import chad_agent, conversation_store
        # Pre-populate a conversation.
        conversation_store.append_message("conv-NSP", "user", "earlier question")
        conversation_store.append_message("conv-NSP", "assistant", "earlier answer")

        captured = {}

        def capture_create(*, messages, **kw):
            captured["messages"] = messages
            return _fake_response("new answer.")

        fake_client = MagicMock()
        fake_client.messages.create = MagicMock(side_effect=capture_create)
        with patch.object(chad_agent, "make_client", return_value=fake_client):
            chad_agent.chad_turn(
                "follow-up question", conversation_id="conv-NSP",
            )

        msgs = captured["messages"]
        assert len(msgs) == 3
        assert msgs[0]["content"] == "earlier question"
        assert msgs[1]["content"] == "earlier answer"
        assert msgs[2]["content"] == "follow-up question"
