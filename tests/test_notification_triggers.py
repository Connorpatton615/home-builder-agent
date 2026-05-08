"""Tests for home_builder_agent.scheduling.notification_triggers."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from home_builder_agent.scheduling.notification_triggers import (
    FireResult,
    _BAND_TO_SEVERITY,
    fire_selection_deadlines_for_project,
)
from home_builder_agent.scheduling.events import EventSeverity, EventType


# ---------------------------------------------------------------------------
# Band → severity mapping
# ---------------------------------------------------------------------------

def test_overdue_and_today_are_critical():
    """OVERDUE + ORDER NOW (today) both fire critical events."""
    assert _BAND_TO_SEVERITY["OVERDUE"] == EventSeverity.CRITICAL
    assert _BAND_TO_SEVERITY["ORDER NOW"] == EventSeverity.CRITICAL


def test_this_week_is_warning():
    assert _BAND_TO_SEVERITY["THIS WEEK"] == EventSeverity.WARNING


def test_upcoming_is_info():
    assert _BAND_TO_SEVERITY["UPCOMING"] == EventSeverity.INFO


def test_later_band_does_not_map():
    """LATER band → no Event fires (filtered out — too far away)."""
    assert "LATER" not in _BAND_TO_SEVERITY


# ---------------------------------------------------------------------------
# fire_selection_deadlines_for_project — mock the DB layer
# ---------------------------------------------------------------------------

_MOCK_ALERTS = {
    "project_id": "proj-1",
    "project_name": "Test Project",
    "today": "2026-05-07",
    "upcoming_window_days": 14,
    "totals": {"OVERDUE": 0, "ORDER NOW": 1, "THIS WEEK": 1, "UPCOMING": 1},
    "alerts": [
        {
            "band": "ORDER NOW",
            "material_category": "window",
            "lead_time_days": 80,
            "install_phase_name": "Roofing",
            "install_date": "2026-08-01",
            "drop_dead_date": "2026-05-07",
            "days_until_drop_dead": 0,
        },
        {
            "band": "THIS WEEK",
            "material_category": "cabinet",
            "lead_time_days": 70,
            "install_phase_name": "Drywall",
            "install_date": "2026-07-20",
            "drop_dead_date": "2026-05-12",
            "days_until_drop_dead": 5,
        },
        {
            "band": "UPCOMING",
            "material_category": "tile",
            "lead_time_days": 21,
            "install_phase_name": "Flooring",
            "install_date": "2026-06-15",
            "drop_dead_date": "2026-05-19",
            "days_until_drop_dead": 12,
        },
    ],
}


def test_fires_three_events_when_no_dedupe():
    inserted: list[dict] = []

    def fake_insert(event, *, conn=None, create_default_notification=True):
        inserted.append({"type": event.type, "severity": event.severity, "payload": event.payload})
        return event.id

    with patch(
        "home_builder_agent.scheduling.notification_triggers.compute_live_procurement_alerts",
        return_value=_MOCK_ALERTS,
    ), patch(
        "home_builder_agent.scheduling.notification_triggers._existing_open_categories",
        return_value=set(),
    ), patch(
        "home_builder_agent.scheduling.store_postgres.insert_event",
        side_effect=fake_insert,
    ):
        result = fire_selection_deadlines_for_project("proj-1")

    assert result.fired == 3
    assert result.skipped_existing == 0
    assert result.alerts_total == 3
    assert set(result.fired_categories) == {"window", "cabinet", "tile"}
    assert len(inserted) == 3
    # Every event is type=selection-deadline
    assert all(i["type"] == EventType.SELECTION_DEADLINE.value for i in inserted)


def test_dedupe_skips_existing_categories():
    """Categories with open Events already are skipped."""
    inserted: list[dict] = []

    def fake_insert(event, *, conn=None, create_default_notification=True):
        inserted.append(event.payload.get("category"))
        return event.id

    with patch(
        "home_builder_agent.scheduling.notification_triggers.compute_live_procurement_alerts",
        return_value=_MOCK_ALERTS,
    ), patch(
        "home_builder_agent.scheduling.notification_triggers._existing_open_categories",
        return_value={"window", "tile"},  # already have Events for these
    ), patch(
        "home_builder_agent.scheduling.store_postgres.insert_event",
        side_effect=fake_insert,
    ):
        result = fire_selection_deadlines_for_project("proj-1")

    assert result.fired == 1                      # only "cabinet" fires
    assert result.skipped_existing == 2
    assert result.fired_categories == ["cabinet"]
    assert set(result.skipped_categories) == {"window", "tile"}
    assert inserted == ["cabinet"]


def test_force_mode_bypasses_dedupe():
    """skip_existing=False emits regardless of pre-existing Events."""
    inserted: list[str] = []

    def fake_insert(event, *, conn=None, create_default_notification=True):
        inserted.append(event.payload.get("category"))
        return event.id

    with patch(
        "home_builder_agent.scheduling.notification_triggers.compute_live_procurement_alerts",
        return_value=_MOCK_ALERTS,
    ), patch(
        "home_builder_agent.scheduling.store_postgres.insert_event",
        side_effect=fake_insert,
    ):
        result = fire_selection_deadlines_for_project("proj-1", skip_existing=False)

    assert result.fired == 3
    assert result.skipped_existing == 0
    assert len(inserted) == 3


def test_no_schedule_returns_error_field_not_raise():
    """When the project has no schedule in Postgres, FireResult carries
    an error field rather than raising."""
    with patch(
        "home_builder_agent.scheduling.notification_triggers.compute_live_procurement_alerts",
        return_value=None,
    ):
        result = fire_selection_deadlines_for_project("proj-missing")

    assert result.fired == 0
    assert result.error is not None
    assert "no schedule" in result.error.lower()


def test_severity_assignment_per_alert():
    """Each band fires with the right severity."""
    inserted: list[tuple[str, str]] = []

    def fake_insert(event, *, conn=None, create_default_notification=True):
        inserted.append((event.payload.get("category"), event.severity))
        return event.id

    with patch(
        "home_builder_agent.scheduling.notification_triggers.compute_live_procurement_alerts",
        return_value=_MOCK_ALERTS,
    ), patch(
        "home_builder_agent.scheduling.notification_triggers._existing_open_categories",
        return_value=set(),
    ), patch(
        "home_builder_agent.scheduling.store_postgres.insert_event",
        side_effect=fake_insert,
    ):
        fire_selection_deadlines_for_project("proj-1")

    by_cat = dict(inserted)
    assert by_cat["window"] == "critical"   # ORDER NOW
    assert by_cat["cabinet"] == "warning"   # THIS WEEK
    assert by_cat["tile"] == "info"         # UPCOMING


def test_compute_alerts_failure_captured_in_error():
    """Exceptions in the lead_times computation surface as FireResult.error."""
    with patch(
        "home_builder_agent.scheduling.notification_triggers.compute_live_procurement_alerts",
        side_effect=RuntimeError("schedule loader exploded"),
    ):
        result = fire_selection_deadlines_for_project("proj-1")
    assert result.fired == 0
    assert result.error is not None
    assert "schedule loader exploded" in result.error


def test_payload_carries_full_context():
    """Verify the Event payload passes drop_dead_date, install_phase, lead_time."""
    inserted_payloads: list[dict] = []

    def fake_insert(event, *, conn=None, create_default_notification=True):
        inserted_payloads.append(event.payload)
        return event.id

    with patch(
        "home_builder_agent.scheduling.notification_triggers.compute_live_procurement_alerts",
        return_value=_MOCK_ALERTS,
    ), patch(
        "home_builder_agent.scheduling.notification_triggers._existing_open_categories",
        return_value=set(),
    ), patch(
        "home_builder_agent.scheduling.store_postgres.insert_event",
        side_effect=fake_insert,
    ):
        fire_selection_deadlines_for_project("proj-1")

    p = next(p for p in inserted_payloads if p["category"] == "window")
    assert p["drop_dead_date"] == "2026-05-07"
    assert p["lead_time_days"] == 80
    assert p["install_phase_name"] == "Roofing"
    assert p["install_date"] == "2026-08-01"
    assert p["band"] == "ORDER NOW"
    assert p["lead_time_source"] == "category-default"


def test_fire_result_to_dict_round_trip():
    r = FireResult(
        project_id="p1", project_name="P1", fired=3, skipped_existing=2,
        skipped_no_band=1, alerts_total=6,
        fired_categories=["window"], skipped_categories=["cabinet"],
    )
    d = r.to_dict()
    assert d["project_id"] == "p1"
    assert d["fired"] == 3
    assert d["fired_categories"] == ["window"]
    assert d["error"] is None
