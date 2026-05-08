"""events.py — Event + Notification entities for the Scheduling Engine.

Per canonical-data-model.md §§ 13 (Notification) and 17 (Event).

The model: an Event is what happened; a Notification is a specific delivery
of that Event to a specific surface. One Event → many Notifications. The
engine OWNS the canonical Event store; emitters from other layers (Vendor
Intelligence, supplier-email watcher, weather monitor, permit ingestion)
call into the engine's Event API to record them.

Six event types ship in V1 (extensible; add a type rather than overload
an existing payload):

  selection-deadline    drop-dead order date approaching
  weather-delay         forecast threshold breached on a phase
  material-no-show      delivery past scheduled date with no confirmation
  sub-no-show           sub didn't check in by 9am on a scheduled day
  inspection-failure    inspection result was Fail or Reinspect
  schedule-slip         phase moved enough to slip estimated completion

Severity drives default channel routing (per § Event + notification model):

  info       → in-app feed only
  warning    → in-app + push
  critical   → in-app + push + email
  blocking   → all channels + dashboard banner

This module is pure-Python (engine-side data model + helpers). Persistence
adapter lives in store_postgres.py. View-model projection lives in
view_models.py. Reconcile dispatch for acknowledge/resolve lives in
reconcile.py. The Notification dispatcher itself (channel routing logic
that decides "fire push vs email vs SMS") is its own module — built when
APNs + email-send infrastructure is wired.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Enums (mirror schemas.py, kept here for engine-side use)
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    # Scheduling-engine emitted (V1 core)
    SELECTION_DEADLINE = "selection-deadline"
    WEATHER_DELAY = "weather-delay"
    MATERIAL_NO_SHOW = "material-no-show"
    SUB_NO_SHOW = "sub-no-show"
    INSPECTION_FAILURE = "inspection-failure"
    SCHEDULE_SLIP = "schedule-slip"
    # Vendor-emitter side (Phase 3 Vendor Intelligence + Phase 2 #11
    # supplier-email watcher). Per canonical-data-model.md § 17 — emitter
    # taxonomy. The DB does not enforce these via CHECK; the engine
    # validates payload contracts at insert time.
    ETA_CHANGE = "eta-change"
    BACKORDER_DETECTED = "backorder-detected"
    STOCK_CHANGE = "stock-change"
    PRICE_CHANGE = "price-change"
    LEAD_TIME_CHANGE = "lead-time-change"


class EventSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    BLOCKING = "blocking"


class EventStatus(str, Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class NotificationChannel(str, Enum):
    IN_APP = "in-app"
    PUSH = "push"
    EMAIL = "email"
    SMS = "sms"


class NotificationSurface(str, Enum):
    DAILY_VIEW = "daily-view"
    WEEKLY_VIEW = "weekly-view"
    MASTER_SCHEDULE = "master-schedule"
    NOTIFICATION_FEED = "notification-feed"
    BANNER = "banner"


# Severity → default channels per canonical-data-model.md § Common Event structure
DEFAULT_CHANNELS_BY_SEVERITY: dict[EventSeverity, list[NotificationChannel]] = {
    EventSeverity.INFO: [
        NotificationChannel.IN_APP,
    ],
    EventSeverity.WARNING: [
        NotificationChannel.IN_APP,
        NotificationChannel.PUSH,
    ],
    EventSeverity.CRITICAL: [
        NotificationChannel.IN_APP,
        NotificationChannel.PUSH,
        NotificationChannel.EMAIL,
    ],
    EventSeverity.BLOCKING: [
        NotificationChannel.IN_APP,
        NotificationChannel.PUSH,
        NotificationChannel.EMAIL,
        NotificationChannel.SMS,
    ],
}

# Default per-type human-readable summary template. The dispatcher fills in
# payload values via .format(). When the payload is missing fields, a more
# generic fallback is used (so the feed never shows raw JSON).
_DEFAULT_SUMMARY_BY_TYPE: dict[str, str] = {
    EventType.SELECTION_DEADLINE.value:
        "Drop-dead {category} order date approaching ({drop_dead_date}, lead time {lead_time_days}d)",
    EventType.WEATHER_DELAY.value:
        "Weather risk for {affected_activity} ({forecast_window_start} → {forecast_window_end})",
    EventType.MATERIAL_NO_SHOW.value:
        "Material delivery overdue (scheduled {scheduled_date}, {days_overdue}d late)",
    EventType.SUB_NO_SHOW.value:
        "Sub no-show at {expected_check_in_time} on {scheduled_date}",
    EventType.INSPECTION_FAILURE.value:
        "{inspection_type} inspection failed — reinspect {reinspect_date}",
    EventType.SCHEDULE_SLIP.value:
        "Schedule slipped {slip_days}d — completion now {new_estimated_completion_date}",
    EventType.ETA_CHANGE.value:
        "{vendor_name}: ETA updated to {eta_or_ship_date} ({items_summary})",
    EventType.BACKORDER_DETECTED.value:
        "{vendor_name}: backorder ({items_summary})",
    EventType.STOCK_CHANGE.value:
        "{vendor_name}: stock change ({items_summary})",
    EventType.PRICE_CHANGE.value:
        "{vendor_name}: price change ({items_summary})",
    EventType.LEAD_TIME_CHANGE.value:
        "{vendor_name}: lead-time change ({items_summary})",
}


# ---------------------------------------------------------------------------
# Entities (canonical-data-model.md § 13 + § 17)
# ---------------------------------------------------------------------------

@dataclass
class Event:
    """Engine-side Event record. Mirrors entity 17."""

    id: str
    type: str                              # EventType value or extensible string
    severity: str                          # EventSeverity value
    status: str                            # EventStatus value
    created_at: datetime

    project_id: str | None = None
    phase_id: str | None = None
    task_id: str | None = None
    vendor_id: str | None = None
    sku_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = "scheduling-engine"

    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None
    acknowledgement_actor: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status == EventStatus.OPEN.value

    def age_seconds(self, now: datetime | None = None) -> int:
        """Seconds since created_at (UTC). For prompt-feed sorting + escalation."""
        ref = now or datetime.now(timezone.utc)
        delta = ref - self.created_at
        return max(0, int(delta.total_seconds()))

    def summary(self) -> str:
        """One-line human-readable summary using the per-type template."""
        template = _DEFAULT_SUMMARY_BY_TYPE.get(self.type)
        if template is None:
            return f"{self.type} event"
        try:
            return template.format(**self.payload)
        except (KeyError, IndexError):
            # Payload missing required field — return a safe fallback rather
            # than crash the feed render.
            return f"{self.type} event (incomplete payload)"


@dataclass
class Notification:
    """Engine-side Notification record. Mirrors entity 13.

    A Notification is an Event delivered to a specific (surface, channel)
    pair. One Event can produce many Notifications.
    """

    id: str
    event_id: str
    channel: str                       # NotificationChannel value
    surface_target: str                # NotificationSurface value
    push_id: str | None = None
    delivered_at: datetime | None = None
    viewed_at: datetime | None = None
    dismissed_at: datetime | None = None
    click_action: str | None = None


# ---------------------------------------------------------------------------
# Builders + helpers
# ---------------------------------------------------------------------------

def make_event(
    *,
    type: EventType | str,
    severity: EventSeverity | str,
    project_id: str | None = None,
    phase_id: str | None = None,
    task_id: str | None = None,
    vendor_id: str | None = None,
    sku_id: str | None = None,
    payload: dict | None = None,
    source: str = "scheduling-engine",
    event_id: str | None = None,
    created_at: datetime | None = None,
) -> Event:
    """Build a fresh Event in `open` status. Caller persists separately
    via store_postgres.insert_event(). Pure helper; no I/O."""
    type_str = type.value if isinstance(type, EventType) else type
    sev_str = severity.value if isinstance(severity, EventSeverity) else severity

    return Event(
        id=event_id or uuid.uuid4().hex,
        type=type_str,
        severity=sev_str,
        status=EventStatus.OPEN.value,
        created_at=created_at or datetime.now(timezone.utc),
        project_id=project_id,
        phase_id=phase_id,
        task_id=task_id,
        vendor_id=vendor_id,
        sku_id=sku_id,
        payload=payload or {},
        source=source,
    )


def default_channels_for(severity: EventSeverity | str) -> list[NotificationChannel]:
    """Severity → default channels (per canonical-data-model.md spec)."""
    sev = severity if isinstance(severity, EventSeverity) else EventSeverity(severity)
    return DEFAULT_CHANNELS_BY_SEVERITY[sev]


def click_action_for(event: Event) -> str | None:
    """Compose a deep-link target for a Notification.click_action.

    Convention: `<entity-type>:<entity-id>`. The renderer parses this and
    routes to the appropriate detail view.
    """
    if event.phase_id:
        return f"phase:{event.phase_id}"
    if event.task_id:
        return f"task:{event.task_id}"
    if event.sku_id:
        return f"sku:{event.sku_id}"
    if event.vendor_id:
        return f"vendor:{event.vendor_id}"
    if event.project_id:
        return f"project:{event.project_id}"
    return None
