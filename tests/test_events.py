"""Tests for home_builder_agent.scheduling.events + notification_feed_view."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from home_builder_agent.scheduling.events import (
    DEFAULT_CHANNELS_BY_SEVERITY,
    Event,
    EventSeverity,
    EventStatus,
    EventType,
    NotificationChannel,
    NotificationSurface,
    click_action_for,
    default_channels_for,
    make_event,
)
from home_builder_agent.scheduling.view_models import notification_feed_view


# ---------------------------------------------------------------------------
# Builders + enums
# ---------------------------------------------------------------------------

def test_make_event_defaults_to_open_now():
    e = make_event(
        type=EventType.SELECTION_DEADLINE,
        severity=EventSeverity.WARNING,
        project_id="proj-1",
        phase_id="phase-3",
        payload={
            "category": "cabinets",
            "drop_dead_date": "2026-06-01",
            "lead_time_days": 70,
        },
    )
    assert e.type == "selection-deadline"
    assert e.severity == "warning"
    assert e.status == "open"
    assert e.is_open
    assert e.id  # uuid
    assert e.project_id == "proj-1"
    assert e.phase_id == "phase-3"
    assert e.payload["category"] == "cabinets"
    assert e.created_at.tzinfo is not None  # tz-aware


def test_make_event_accepts_string_enums():
    """Strings are accepted as well as Enum values — both should produce
    the same record."""
    e1 = make_event(type=EventType.WEATHER_DELAY, severity=EventSeverity.CRITICAL)
    e2 = make_event(type="weather-delay", severity="critical")
    assert e1.type == e2.type
    assert e1.severity == e2.severity


def test_severity_to_default_channels():
    """Channel routing per the canonical spec."""
    assert default_channels_for(EventSeverity.INFO) == [
        NotificationChannel.IN_APP,
    ]
    assert default_channels_for(EventSeverity.WARNING) == [
        NotificationChannel.IN_APP,
        NotificationChannel.PUSH,
    ]
    assert default_channels_for(EventSeverity.CRITICAL) == [
        NotificationChannel.IN_APP,
        NotificationChannel.PUSH,
        NotificationChannel.EMAIL,
    ]
    assert default_channels_for(EventSeverity.BLOCKING) == [
        NotificationChannel.IN_APP,
        NotificationChannel.PUSH,
        NotificationChannel.EMAIL,
        NotificationChannel.SMS,
    ]


def test_severity_string_form_also_works():
    """default_channels_for accepts string severity values too."""
    assert default_channels_for("warning") == default_channels_for(EventSeverity.WARNING)


def test_default_channels_table_covers_every_severity():
    """No severity is missing a channel mapping — the dispatcher would crash."""
    for sev in EventSeverity:
        assert sev in DEFAULT_CHANNELS_BY_SEVERITY


# ---------------------------------------------------------------------------
# Event derived fields
# ---------------------------------------------------------------------------

def test_event_age_seconds():
    now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)
    five_min_ago = now - timedelta(minutes=5)
    e = make_event(
        type=EventType.SUB_NO_SHOW,
        severity=EventSeverity.WARNING,
        created_at=five_min_ago,
    )
    assert e.age_seconds(now) == 300


def test_event_age_clamps_to_zero_for_future_events():
    """Defensive — clock skew shouldn't produce negative ages on the feed."""
    now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)
    one_min_ahead = now + timedelta(minutes=1)
    e = make_event(
        type=EventType.WEATHER_DELAY,
        severity=EventSeverity.INFO,
        created_at=one_min_ahead,
    )
    assert e.age_seconds(now) == 0


def test_summary_renders_per_type():
    """Each event type's default summary template fills from payload."""
    e = make_event(
        type=EventType.SELECTION_DEADLINE,
        severity=EventSeverity.WARNING,
        payload={
            "category": "cabinets",
            "drop_dead_date": "2026-06-01",
            "lead_time_days": 70,
        },
    )
    s = e.summary()
    assert "cabinets" in s
    assert "2026-06-01" in s
    assert "70" in s


def test_summary_falls_back_when_payload_missing():
    """If a required field is missing, summary returns a safe fallback
    rather than crashing — the feed render never errors on bad data."""
    e = make_event(
        type=EventType.SELECTION_DEADLINE,
        severity=EventSeverity.WARNING,
        payload={},  # missing category, drop_dead_date, lead_time_days
    )
    s = e.summary()
    assert "selection-deadline" in s
    assert "incomplete" in s.lower()


def test_summary_for_unknown_type_is_safe():
    """An extensible type without a default template gets a generic summary."""
    e = make_event(type="custom-event-type", severity=EventSeverity.INFO)
    s = e.summary()
    assert "custom-event-type" in s


def test_click_action_prioritizes_phase_over_project():
    """phase > task > sku > vendor > project — most specific wins."""
    e = make_event(
        type=EventType.SCHEDULE_SLIP,
        severity=EventSeverity.WARNING,
        project_id="p1",
        phase_id="ph7",
    )
    assert click_action_for(e) == "phase:ph7"


def test_click_action_falls_back_to_project():
    """When no entity is set except project, deep-link to the project."""
    e = make_event(
        type=EventType.SCHEDULE_SLIP,
        severity=EventSeverity.WARNING,
        project_id="p1",
    )
    assert click_action_for(e) == "project:p1"


def test_click_action_returns_none_when_unbound():
    """Events with no entity refs (rare; system-wide) get no click_action."""
    e = make_event(type=EventType.WEATHER_DELAY, severity=EventSeverity.INFO)
    assert click_action_for(e) is None


# ---------------------------------------------------------------------------
# notification_feed_view projection
# ---------------------------------------------------------------------------

def test_feed_view_empty_when_no_events():
    payload = notification_feed_view([])
    assert payload.items == []


def test_feed_view_projects_event_to_notification_payload():
    """End-to-end: Events list → NotificationItemPayload list."""
    now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

    e = make_event(
        type=EventType.SELECTION_DEADLINE,
        severity=EventSeverity.WARNING,
        project_id="proj-w",
        phase_id="phase-3",
        payload={
            "category": "cabinets",
            "drop_dead_date": "2026-06-01",
            "lead_time_days": 70,
        },
        created_at=now - timedelta(hours=2),
    )

    payload = notification_feed_view([e], now=now)
    assert len(payload.items) == 1
    n = payload.items[0]
    assert n.event_id == e.id
    assert n.notification_id == f"notif:{e.id}"  # synthetic 1:1
    assert n.type == "selection-deadline"
    assert n.severity.value == "warning"
    assert n.status.value == "open"
    assert n.project_id == "proj-w"
    assert n.phase_id == "phase-3"
    assert n.age_seconds == 7200  # 2 hours
    assert "cabinets" in n.summary
    assert n.acknowledge_action == f"event-acknowledge:{e.id}"
    assert n.resolve_action == f"event-resolve:{e.id}"
    assert n.click_action == "phase:phase-3"


def test_feed_view_newest_first():
    """Events are sorted by created_at DESC (newest at top)."""
    now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)
    older = make_event(
        type=EventType.WEATHER_DELAY, severity=EventSeverity.INFO,
        created_at=now - timedelta(hours=10), event_id="old",
    )
    newer = make_event(
        type=EventType.WEATHER_DELAY, severity=EventSeverity.INFO,
        created_at=now - timedelta(hours=1), event_id="new",
    )
    payload = notification_feed_view([older, newer], now=now)
    assert payload.items[0].event_id == "new"
    assert payload.items[1].event_id == "old"


def test_feed_view_uses_real_notification_id_when_supplied():
    """When the persistence layer has a real Notification UUID, the view-
    model uses that instead of the synthetic id."""
    now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)
    e = make_event(
        type=EventType.SUB_NO_SHOW, severity=EventSeverity.CRITICAL,
        event_id="evt-1", created_at=now - timedelta(hours=1),
    )
    payload = notification_feed_view(
        [e], notification_ids_by_event={"evt-1": "notif-real-uuid"}, now=now,
    )
    assert payload.items[0].notification_id == "notif-real-uuid"


# ---------------------------------------------------------------------------
# Reconcile dispatch wiring (structural — no DB roundtrip)
# ---------------------------------------------------------------------------

def test_event_dispatchers_registered():
    """event-acknowledge and event-resolve replaced any prior unknown stubs."""
    from home_builder_agent.scheduling.reconcile import (
        DISPATCHERS,
        _dispatch_event_acknowledge,
        _dispatch_event_resolve,
    )
    assert DISPATCHERS["event-acknowledge"] is _dispatch_event_acknowledge
    assert DISPATCHERS["event-resolve"] is _dispatch_event_resolve


def test_event_acknowledge_rejects_wrong_target_type():
    from home_builder_agent.scheduling.reconcile import (
        DispatchOutcome,
        _dispatch_event_acknowledge,
    )

    action = {
        "id": "act-1",
        "target_entity_type": "phase",  # WRONG
        "target_entity_id": "phase-1",
        "actor_user_id": None,
        "synced_at": None,
        "payload": {},
    }
    result = _dispatch_event_acknowledge(action, conn=None)
    assert result.outcome == DispatchOutcome.SKIPPED
    assert "event" in (result.notes or "")


def test_event_resolve_rejects_wrong_target_type():
    from home_builder_agent.scheduling.reconcile import (
        DispatchOutcome,
        _dispatch_event_resolve,
    )

    action = {
        "id": "act-2",
        "target_entity_type": "checklist-item",  # WRONG
        "target_entity_id": "item-1",
        "actor_user_id": None,
        "synced_at": None,
        "payload": {},
    }
    result = _dispatch_event_resolve(action, conn=None)
    assert result.outcome == DispatchOutcome.SKIPPED


# ---------------------------------------------------------------------------
# Surface enums sanity
# ---------------------------------------------------------------------------

def test_notification_surface_targets_match_spec():
    """Per canonical-data-model.md § 13 — the five valid surface_target values."""
    expected = {"daily-view", "weekly-view", "master-schedule", "notification-feed", "banner"}
    actual = {s.value for s in NotificationSurface}
    assert actual == expected


def test_notification_channels_match_spec():
    """Per canonical-data-model.md § 13 — the four valid channel values."""
    expected = {"in-app", "push", "email", "sms"}
    actual = {c.value for c in NotificationChannel}
    assert actual == expected
