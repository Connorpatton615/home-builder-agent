"""Tests for chad_agent's project lifecycle tools.

Per ADR (2026-05-09): `archive_project`, `create_project`, and
`clone_project` ship as three separate top-level tools (replacing the
prior single `manage_project` tool with an action enum). This module
verifies:

  - Each tool's input-validation fast paths
  - Each tool calls its corresponding store_postgres adapter with the
    right shape and surfaces a Chad-voice confirmation string
  - The TOOLS registry exposes all three tools with the spec'd schemas
  - The single shared dispatch path routes each tool name to the right
    handler

We mock at the adapter boundary (store_postgres.* and
project_agent._resolve_project) so tests run offline.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Tool registry shape
# ---------------------------------------------------------------------------


class TestToolsRegistry:
    """ADR contract: three top-level tools, no manage_project."""

    def test_registry_has_three_lifecycle_tools(self):
        from home_builder_agent.agents.chad_agent import TOOLS

        names = [t["name"] for t in TOOLS]
        assert "archive_project" in names
        assert "create_project" in names
        assert "clone_project" in names
        assert "manage_project" not in names

    def test_archive_project_schema(self):
        from home_builder_agent.agents.chad_agent import TOOLS

        tool = next(t for t in TOOLS if t["name"] == "archive_project")
        props = tool["input_schema"]["properties"]
        assert set(props.keys()) == {"project_name", "reason"}
        assert tool["input_schema"]["required"] == ["project_name"]
        # Description must enumerate trigger phrases
        desc = tool["description"].lower()
        assert "archive" in desc
        assert "kill" in desc

    def test_create_project_schema_no_copy_from(self):
        """ADR rationale: keeping create + clone separate means
        create_project must NOT accept a copy_from field — Claude routes
        clone-intent to clone_project, not create_project with a flag.

        Anthropic's tool input_schema does NOT support top-level
        anyOf / oneOf / allOf — including one returns a 400 on the
        whole tools list. The "at least one date" constraint therefore
        lives in the dispatch handler at runtime (see
        _tool_create_project), not in the schema. Tested live on
        2026-05-09; chad-iOS hit a 400 immediately on the first turn.
        """
        from home_builder_agent.agents.chad_agent import TOOLS

        tool = next(t for t in TOOLS if t["name"] == "create_project")
        props = tool["input_schema"]["properties"]
        assert "copy_from" not in props
        assert "project_name" in props
        assert "customer_name" in props
        assert "target_completion_date" in props
        assert "target_framing_start_date" in props
        # Schema must NOT use top-level anyOf / oneOf / allOf —
        # Anthropic 400's on the whole tools list if any tool does.
        assert "anyOf" not in tool["input_schema"]
        assert "oneOf" not in tool["input_schema"]
        assert "allOf" not in tool["input_schema"]
        assert tool["input_schema"]["required"] == ["project_name"]
        # Description must point Chad to clone_project for clone-intent.
        desc = tool["description"].lower()
        assert "clone_project" in desc

    def test_clone_project_schema(self):
        from home_builder_agent.agents.chad_agent import TOOLS

        tool = next(t for t in TOOLS if t["name"] == "clone_project")
        props = tool["input_schema"]["properties"]
        assert "copy_from" in props
        assert "new_name" in props
        assert "customer_name" in props
        # Spec: target dates excluded (clone copies from source as-is)
        assert "target_completion_date" not in props
        assert "target_framing_start_date" not in props
        assert set(tool["input_schema"]["required"]) == {"copy_from", "new_name"}


# ---------------------------------------------------------------------------
# archive_project
# ---------------------------------------------------------------------------


class TestArchiveProject:
    def test_empty_project_name_rejected(self):
        from home_builder_agent.agents.chad_agent import _tool_archive_project

        msg, cost = _tool_archive_project("")
        assert "project_name is required" in msg
        assert cost == 0.0

    def test_no_match_returns_helpful_error(self):
        from home_builder_agent.agents.chad_agent import _tool_archive_project

        with patch(
            "home_builder_agent.agents.project_agent._resolve_project",
            return_value=None,
        ):
            msg, cost = _tool_archive_project("nonexistent")
        assert "no project matched" in msg
        assert "'nonexistent'" in msg
        assert cost == 0.0

    def test_already_archived_is_noop(self):
        from home_builder_agent.agents.chad_agent import _tool_archive_project

        proj = {
            "id": "abc12345-aaaa-bbbb-cccc-ddddeeeeffff",
            "name": "OldTest",
            "status": "archived",
        }
        with patch(
            "home_builder_agent.agents.project_agent._resolve_project",
            return_value=proj,
        ):
            msg, cost = _tool_archive_project("OldTest")
        assert "already archived" in msg
        assert "No-op" in msg
        assert cost == 0.0

    def test_dry_run_does_not_call_adapter(self):
        from home_builder_agent.agents.chad_agent import _tool_archive_project

        proj = {
            "id": "abc12345-aaaa-bbbb-cccc-ddddeeeeffff",
            "name": "Whitfield",
            "status": "active",
        }
        with patch(
            "home_builder_agent.agents.project_agent._resolve_project",
            return_value=proj,
        ), patch(
            "home_builder_agent.scheduling.store_postgres.archive_project_in_db",
        ) as mock_adapter:
            msg, cost = _tool_archive_project(
                "Whitfield", reason="closeout complete", dry_run=True,
            )
        mock_adapter.assert_not_called()
        assert "(dry-run)" in msg
        assert "Whitfield" in msg
        assert "closeout complete" in msg

    def test_happy_path_calls_archive_adapter(self):
        from home_builder_agent.agents.chad_agent import _tool_archive_project

        proj = {
            "id": "abc12345-aaaa-bbbb-cccc-ddddeeeeffff",
            "name": "Whitfield",
            "status": "active",
        }
        with patch(
            "home_builder_agent.agents.project_agent._resolve_project",
            return_value=proj,
        ), patch(
            "home_builder_agent.scheduling.store_postgres.archive_project_in_db",
            return_value=True,
        ) as mock_adapter:
            msg, cost = _tool_archive_project("Whitfield", reason="done")
        mock_adapter.assert_called_once_with(proj["id"], reason="done")
        assert "Archived Whitfield" in msg
        assert "abc12345" in msg
        assert "Reason: done." in msg
        assert cost == 0.0

    def test_adapter_returns_false_surfaces_warning(self):
        from home_builder_agent.agents.chad_agent import _tool_archive_project

        proj = {
            "id": "abc12345-aaaa-bbbb-cccc-ddddeeeeffff",
            "name": "Whitfield",
            "status": "active",
        }
        with patch(
            "home_builder_agent.agents.project_agent._resolve_project",
            return_value=proj,
        ), patch(
            "home_builder_agent.scheduling.store_postgres.archive_project_in_db",
            return_value=False,
        ):
            msg, cost = _tool_archive_project("Whitfield")
        assert "update returned False" in msg
        assert cost == 0.0

    def test_adapter_exception_surfaces(self):
        from home_builder_agent.agents.chad_agent import _tool_archive_project

        proj = {
            "id": "abc12345-aaaa-bbbb-cccc-ddddeeeeffff",
            "name": "Whitfield",
            "status": "active",
        }
        with patch(
            "home_builder_agent.agents.project_agent._resolve_project",
            return_value=proj,
        ), patch(
            "home_builder_agent.scheduling.store_postgres.archive_project_in_db",
            side_effect=RuntimeError("connection refused"),
        ):
            msg, cost = _tool_archive_project("Whitfield")
        assert "DB write failed" in msg
        assert "RuntimeError" in msg
        assert "connection refused" in msg


# ---------------------------------------------------------------------------
# create_project
# ---------------------------------------------------------------------------


class TestCreateProject:
    def test_empty_project_name_rejected(self):
        from home_builder_agent.agents.chad_agent import _tool_create_project

        msg, cost = _tool_create_project("")
        assert "project_name is required" in msg
        assert cost == 0.0

    def test_no_target_dates_rejected(self):
        from home_builder_agent.agents.chad_agent import _tool_create_project

        msg, cost = _tool_create_project("Maple Ridge")
        assert "target_completion_date" in msg
        assert "target_framing_start_date" in msg
        assert cost == 0.0

    def test_invalid_completion_date_rejected(self):
        from home_builder_agent.agents.chad_agent import _tool_create_project

        msg, cost = _tool_create_project(
            "Maple Ridge", target_completion_date="not-a-date",
        )
        assert "target_completion_date" in msg
        assert "not-a-date" in msg
        assert "valid YYYY-MM-DD" in msg

    def test_invalid_framing_date_rejected(self):
        from home_builder_agent.agents.chad_agent import _tool_create_project

        msg, cost = _tool_create_project(
            "Maple Ridge", target_framing_start_date="2026/05/09",
        )
        assert "target_framing_start_date" in msg
        assert "2026/05/09" in msg

    def test_dry_run_does_not_call_adapter(self):
        from home_builder_agent.agents.chad_agent import _tool_create_project

        with patch(
            "home_builder_agent.scheduling.store_postgres.create_project_in_db",
        ) as mock_adapter:
            msg, cost = _tool_create_project(
                "Maple Ridge",
                target_completion_date="2026-12-01",
                dry_run=True,
            )
        mock_adapter.assert_not_called()
        assert "(dry-run)" in msg
        assert "Maple Ridge" in msg

    def test_happy_path_calls_create_adapter_with_tbd_default(self):
        """When customer_name is omitted the adapter receives 'TBD'."""
        from datetime import date
        from home_builder_agent.agents.chad_agent import _tool_create_project

        with patch(
            "home_builder_agent.scheduling.store_postgres.create_project_in_db",
            return_value="11111111-2222-3333-4444-555555555555",
        ) as mock_adapter:
            msg, cost = _tool_create_project(
                "Maple Ridge",
                target_completion_date="2026-12-01",
            )
        mock_adapter.assert_called_once_with(
            name="Maple Ridge",
            customer_name="TBD",
            address=None,
            target_completion_date=date(2026, 12, 1),
            target_framing_start_date=None,
        )
        assert "Created empty project Maple Ridge" in msg
        assert "11111111" in msg
        assert "hb-schedule" in msg

    def test_happy_path_with_explicit_customer_name(self):
        from datetime import date
        from home_builder_agent.agents.chad_agent import _tool_create_project

        with patch(
            "home_builder_agent.scheduling.store_postgres.create_project_in_db",
            return_value="11111111-2222-3333-4444-555555555555",
        ) as mock_adapter:
            msg, cost = _tool_create_project(
                "Maple Ridge",
                customer_name="Smith Family",
                target_framing_start_date="2026-08-15",
            )
        mock_adapter.assert_called_once_with(
            name="Maple Ridge",
            customer_name="Smith Family",
            address=None,
            target_completion_date=None,
            target_framing_start_date=date(2026, 8, 15),
        )
        assert "Maple Ridge" in msg

    def test_adapter_exception_surfaces(self):
        from home_builder_agent.agents.chad_agent import _tool_create_project

        with patch(
            "home_builder_agent.scheduling.store_postgres.create_project_in_db",
            side_effect=ValueError("name already taken"),
        ):
            msg, cost = _tool_create_project(
                "Maple Ridge",
                target_completion_date="2026-12-01",
            )
        assert "DB write failed" in msg
        assert "ValueError" in msg


# ---------------------------------------------------------------------------
# clone_project
# ---------------------------------------------------------------------------


class TestCloneProject:
    def test_empty_copy_from_rejected(self):
        from home_builder_agent.agents.chad_agent import _tool_clone_project

        msg, cost = _tool_clone_project("", "NewProj")
        assert "copy_from is required" in msg

    def test_empty_new_name_rejected(self):
        from home_builder_agent.agents.chad_agent import _tool_clone_project

        msg, cost = _tool_clone_project("Whitfield", "")
        assert "new_name is required" in msg

    def test_unresolved_source_returns_error(self):
        from home_builder_agent.agents.chad_agent import _tool_clone_project

        with patch(
            "home_builder_agent.agents.project_agent._resolve_project",
            return_value=None,
        ):
            msg, cost = _tool_clone_project("Mystery", "NewProj")
        assert "no source project matched" in msg
        assert "'Mystery'" in msg

    def test_dry_run_does_not_call_adapter(self):
        from home_builder_agent.agents.chad_agent import _tool_clone_project

        source = {
            "id": "src12345-aaaa-bbbb-cccc-ddddeeeeffff",
            "name": "Whitfield",
            "status": "active",
        }
        with patch(
            "home_builder_agent.agents.project_agent._resolve_project",
            return_value=source,
        ), patch(
            "home_builder_agent.scheduling.store_postgres.clone_project_in_db",
        ) as mock_adapter:
            msg, cost = _tool_clone_project(
                "Whitfield", "Pelican Point", dry_run=True,
            )
        mock_adapter.assert_not_called()
        assert "(dry-run)" in msg
        assert "Whitfield" in msg
        assert "Pelican Point" in msg

    def test_happy_path_calls_clone_adapter(self):
        from home_builder_agent.agents.chad_agent import _tool_clone_project

        source = {
            "id": "src12345-aaaa-bbbb-cccc-ddddeeeeffff",
            "name": "Whitfield",
            "status": "active",
        }
        new_id = "new12345-aaaa-bbbb-cccc-ddddeeeeffff"
        with patch(
            "home_builder_agent.agents.project_agent._resolve_project",
            return_value=source,
        ), patch(
            "home_builder_agent.scheduling.store_postgres.clone_project_in_db",
            return_value=new_id,
        ) as mock_adapter:
            msg, cost = _tool_clone_project(
                "Whitfield",
                "Pelican Point",
                customer_name="Bradford Family",
            )
        mock_adapter.assert_called_once_with(
            source["id"],
            new_name="Pelican Point",
            customer_name="Bradford Family",
            address=None,
            target_completion_date=None,
            target_framing_start_date=None,
        )
        assert "Cloned Whitfield" in msg
        assert "Pelican Point" in msg
        assert "new12345" in msg

    def test_clone_omits_customer_name_passes_none(self):
        """customer_name not supplied → adapter sees None (the existing
        adapter's contract; it can default downstream).
        """
        from home_builder_agent.agents.chad_agent import _tool_clone_project

        source = {
            "id": "src12345-aaaa-bbbb-cccc-ddddeeeeffff",
            "name": "Whitfield",
            "status": "active",
        }
        with patch(
            "home_builder_agent.agents.project_agent._resolve_project",
            return_value=source,
        ), patch(
            "home_builder_agent.scheduling.store_postgres.clone_project_in_db",
            return_value="new12345-aaaa-bbbb-cccc-ddddeeeeffff",
        ) as mock_adapter:
            _tool_clone_project("Whitfield", "Pelican Point")
        # customer_name kwarg should be None (the adapter's pre-existing default)
        kwargs = mock_adapter.call_args.kwargs
        assert kwargs["customer_name"] is None

    def test_adapter_exception_surfaces(self):
        from home_builder_agent.agents.chad_agent import _tool_clone_project

        source = {
            "id": "src12345-aaaa-bbbb-cccc-ddddeeeeffff",
            "name": "Whitfield",
            "status": "active",
        }
        with patch(
            "home_builder_agent.agents.project_agent._resolve_project",
            return_value=source,
        ), patch(
            "home_builder_agent.scheduling.store_postgres.clone_project_in_db",
            side_effect=RuntimeError("FK violation"),
        ):
            msg, cost = _tool_clone_project("Whitfield", "Pelican Point")
        assert "DB write failed" in msg
        assert "RuntimeError" in msg
