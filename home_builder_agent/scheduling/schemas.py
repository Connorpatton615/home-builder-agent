"""schemas.py — Pydantic models for the wire-format view-model payloads.

These are the canonical wire types the iOS shell and any other renderer
deserializes. The engine projects internal dataclasses (in engine.py /
lead_times.py) into these schemas at the projection boundary in
view_models.py.

Why Pydantic and not dataclasses on the wire:
  - Pydantic v2 validates payload shape on the receiving side
  - Pydantic generates JSON Schema directly via model_json_schema(), which
    feeds Swift Codable generation on iOS
  - Single source of truth in one Python module → one JSON Schema artifact
  - Internal engine compute types stay as @dataclass for speed; Pydantic
    only at the wire boundary

Naming conventions (per canonical-data-model.md § Naming conventions):
  - snake_case field names
  - Entity-reference fields end with `_id`
  - Lifecycle status: lowercase single-word (`queued`, `running`, `complete`)
  - Stateful entity status: lowercase-hyphenated (`in-progress`, `not-started`)
  - Severity: lowercase single-word (`info`, `warning`, `critical`, `blocking`)

Cross-references:
  - canonical-data-model.md § View-model contract
  - scheduling-engine.md § Schedule view-model outputs
  - patton-ai-ios docs/03_build/turtle_contract_v1.md § 4 View-model alignment
"""

from __future__ import annotations

from datetime import date as _date, datetime as _datetime
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class PhaseStatus(str, Enum):
    """Phase lifecycle states. Stateful entity → hyphenated where multi-word."""

    NOT_STARTED = "not-started"
    IN_PROGRESS = "in-progress"
    BLOCKED_ON_CHECKLIST = "blocked-on-checklist"
    COMPLETE = "complete"


class MilestoneStatus(str, Enum):
    PENDING = "pending"
    HIT = "hit"
    MISSED = "missed"
    RESCHEDULED = "rescheduled"


class ViewType(str, Enum):
    """The six view-model types projected by the engine."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    MASTER = "master"
    CHECKLIST_GATES = "checklist-gates"
    NOTIFICATION_FEED = "notification-feed"


class LeadTimeSource(str, Enum):
    """Per canonical-data-model.md § entity 10 LeadTime."""

    SKU_PUBLISHED = "vendor-sku-published"
    VENDOR_DEFAULT = "vendor-default"
    CATEGORY_DEFAULT = "category-default"
    MANUAL_OVERRIDE = "manual-override"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    BLOCKING = "blocking"


# ---------------------------------------------------------------------------
# Shared models
# ---------------------------------------------------------------------------

class _Base(BaseModel):
    """Common config — strict by default; iOS gets predictable shape."""

    model_config = ConfigDict(
        extra="forbid",      # No undocumented fields land in payloads
        frozen=False,
        populate_by_name=True,
    )


class PhasePayload(_Base):
    """A Phase row as it appears in master / monthly view payloads.

    Mirrors canonical-data-model.md § entity 2 Phase. Wire fields only.
    """

    id: str = Field(description="Stable phase id within a project")
    phase_template_id: int = Field(description="1–24 from CHECKLIST_PHASE_NAMES library")
    name: str
    sequence_index: int = Field(ge=1, le=24)
    status: PhaseStatus
    planned_start_date: _date
    planned_end_date: _date
    actual_start_date: _date | None = None
    actual_end_date: _date | None = None
    default_duration_days: int
    duration_days: int = Field(description="Effective duration after overrides")


class MilestonePayload(_Base):
    """Date-anchored event with no duration. Mirrors canonical-data-model.md § entity 4."""

    name: str
    planned_date: _date
    actual_date: _date | None = None
    phase_id: str | None = None
    status: MilestoneStatus = MilestoneStatus.PENDING


class DropDeadDatePayload(_Base):
    """A computed drop-dead order date for a material category on a project."""

    material_category: str = Field(description="e.g. 'window', 'cabinet', 'lumber'")
    lead_time_days: int = Field(ge=0)
    lead_time_source: LeadTimeSource = LeadTimeSource.CATEGORY_DEFAULT
    install_phase_name: str
    install_date: _date
    safety_buffer_days: int = Field(ge=0)
    drop_dead_date: _date


# ---------------------------------------------------------------------------
# View-model: master
# ---------------------------------------------------------------------------

class MasterPhasePayload(PhasePayload):
    """Phase + drop-dead dates aligned to it (master view only)."""

    drop_dead_dates: list[DropDeadDatePayload] = Field(default_factory=list)


class MasterViewPayload(_Base):
    """Full project Gantt-equivalent timeline + drop-dead overlay.

    canonical-data-model.md § Master schedule.
    """

    view_type: Literal[ViewType.MASTER] = ViewType.MASTER
    project_id: str
    project_name: str
    estimated_completion_date: _date
    target_completion_date: _date | None = None
    target_framing_start_date: _date | None = None
    phases: list[MasterPhasePayload]
    milestones: list[MilestonePayload]
    drop_dead_dates: list[DropDeadDatePayload] = Field(
        description="Full drop-dead list; also embedded per-phase above for renderer convenience"
    )


# ---------------------------------------------------------------------------
# View-model: daily
# ---------------------------------------------------------------------------

class DailyItemKind(str, Enum):
    PHASE_ACTIVE = "phase-active"
    DELIVERY = "delivery"
    INSPECTION = "inspection"
    DROP_DEAD = "drop-dead"
    NO_SHOW = "no-show"


class DailyItemPayload(_Base):
    """One activity item in the daily view. Discriminator: `kind`.

    Specific shape varies by kind. Common fields below; per-kind fields are
    optional at the schema level so renderers branch on `kind`.
    """

    kind: DailyItemKind
    tap_action: str | None = Field(default=None, description="Deep-link target")

    # phase-active
    phase_id: str | None = None
    phase_name: str | None = None
    day_n: int | None = None
    of_total: int | None = None

    # drop-dead
    material_category: str | None = None
    install_phase_name: str | None = None
    install_date: _date | None = None
    lead_time_days: int | None = None


class DailyProjectPayload(_Base):
    """Per-project group within the daily view."""

    project_id: str
    project_name: str
    items: list[DailyItemPayload]


class DailyViewPayload(_Base):
    view_type: Literal[ViewType.DAILY] = ViewType.DAILY
    date: _date
    projects: list[DailyProjectPayload]


# ---------------------------------------------------------------------------
# View-model: weekly
# ---------------------------------------------------------------------------

class WeeklyItemKind(str, Enum):
    PHASE = "phase"
    DROP_DEAD = "drop-dead"
    MILESTONE = "milestone"


class WeeklyItemPayload(_Base):
    kind: WeeklyItemKind
    tap_action: str | None = None

    # phase
    phase_id: str | None = None
    phase_name: str | None = None
    planned_start_date: _date | None = None
    planned_end_date: _date | None = None

    # drop-dead
    material_category: str | None = None
    install_phase_name: str | None = None
    drop_dead_date: _date | None = None

    # milestone
    milestone_name: str | None = None
    milestone_date: _date | None = None


class WeeklyProjectPayload(_Base):
    project_id: str
    project_name: str
    items: list[WeeklyItemPayload]


class WeeklyViewPayload(_Base):
    view_type: Literal[ViewType.WEEKLY] = ViewType.WEEKLY
    date_window_start: _date
    date_window_end: _date
    projects: list[WeeklyProjectPayload]


# ---------------------------------------------------------------------------
# View-model: monthly
# ---------------------------------------------------------------------------

class MonthlyPhaseInWindowPayload(_Base):
    phase_id: str
    phase_name: str
    planned_start_date: _date
    planned_end_date: _date
    status: PhaseStatus


class MonthlyProjectPayload(_Base):
    project_id: str
    project_name: str
    pct_complete_vs_plan: float = Field(
        ge=0, le=100, description="Earned-time style — phases complete weighted by duration"
    )
    estimated_completion_date: _date
    next_drop_dead_date: _date | None = None
    next_drop_dead_material: str | None = None
    phases_in_window: list[MonthlyPhaseInWindowPayload]


class MonthlyViewPayload(_Base):
    view_type: Literal[ViewType.MONTHLY] = ViewType.MONTHLY
    date_window_start: _date
    date_window_end: _date
    projects: list[MonthlyProjectPayload]


# ---------------------------------------------------------------------------
# View-model: checklist-gates (V2 — schema-only, no engine projection yet)
# ---------------------------------------------------------------------------

class ChecklistItemPayload(_Base):
    """Mirrors canonical-data-model.md § entity 7 ChecklistItem."""

    id: str
    category: str = Field(
        description="One of 10 for Precon (Client & Contract, Plans & Engineering, ...); smaller set for other phases"
    )
    label: str
    is_complete: bool
    completed_by: str | None = None
    completed_at: _date | None = None
    notes: str | None = None
    tap_action: str | None = None


class ChecklistPayload(_Base):
    """Mirrors canonical-data-model.md § entity 6 Checklist."""

    id: str
    phase_id: str
    template_version: str
    status: Literal["open", "closed"]
    completed_count: int = Field(ge=0)
    total_count: int = Field(ge=0)
    items_by_category: dict[str, list[ChecklistItemPayload]] = Field(
        description="Items grouped by category for renderer convenience"
    )


class ChecklistGatesProjectPayload(_Base):
    project_id: str
    project_name: str
    checklists: list[ChecklistPayload]


class ChecklistGatesViewPayload(_Base):
    view_type: Literal[ViewType.CHECKLIST_GATES] = ViewType.CHECKLIST_GATES
    projects: list[ChecklistGatesProjectPayload]


# ---------------------------------------------------------------------------
# View-model: notification-feed (V2 — schema-only)
# ---------------------------------------------------------------------------

class NotificationStatus(str, Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class NotificationItemPayload(_Base):
    """One Event surfaced as a Notification. Mirrors entity 13 wrapping entity 17."""

    event_id: str
    notification_id: str
    type: str = Field(description="selection-deadline | weather-delay | material-no-show | sub-no-show | inspection-failure | schedule-slip | extensible")
    severity: Severity
    status: NotificationStatus
    summary: str
    project_id: str | None = None
    phase_id: str | None = None
    age_seconds: int = Field(ge=0)
    created_at: _date
    acknowledged_at: _date | None = None
    resolved_at: _date | None = None
    acknowledge_action: str | None = Field(default=None, description="UserAction emit target")
    resolve_action: str | None = Field(default=None, description="UserAction emit target")
    click_action: str | None = Field(default=None, description="Deep-link to related entity")


class NotificationFeedViewPayload(_Base):
    view_type: Literal[ViewType.NOTIFICATION_FEED] = ViewType.NOTIFICATION_FEED
    items: list[NotificationItemPayload]


# ---------------------------------------------------------------------------
# Convenience: union for response typing
# ---------------------------------------------------------------------------

ViewPayload = (
    MasterViewPayload
    | DailyViewPayload
    | WeeklyViewPayload
    | MonthlyViewPayload
    | ChecklistGatesViewPayload
    | NotificationFeedViewPayload
)


# ---------------------------------------------------------------------------
# HBEngineActivity — audit log row for /v1/turtles/home-builder/activity
# ---------------------------------------------------------------------------
#
# Shape mirrors home_builder.engine_activity from migration 003. Returned as
# a list payload from the route handler with pagination params.

class ActivitySurface(str, Enum):
    """Where a Claude-autonomous action originated. Mirrors the SQL CHECK."""
    CHAT = "chat"
    VOICE = "voice"
    CLI = "cli"
    BACKGROUND = "background"


class ActivityOutcome(str, Enum):
    """Terminal outcome of an action dispatched by hb-router."""
    SUCCESS = "success"
    PARTIAL = "partial"
    ERROR = "error"
    REJECTED = "rejected"


class HBEngineActivityPayload(_Base):
    """One row from home_builder.engine_activity as it surfaces to iOS.

    Wire format for the Activity tab. Each row represents one autonomous
    Claude action through hb-router. Lookup by actor_user_id (recent-first)
    is the default; per-project filtering is via the project_id field.
    """

    id: str = Field(description="UUID of this activity row")
    actor_user_id: str | None = Field(
        default=None,
        description="auth.users id of who triggered this. Null for background actions.",
    )
    project_id: str | None = Field(
        default=None,
        description="Project UUID this action affects. Null for cross-project actions.",
    )
    surface: ActivitySurface
    invoked_agent: str | None = Field(
        default=None,
        description="Which underlying agent ran (e.g. 'hb-receipt'). Null when classified as 'unknown' (no dispatch).",
    )
    user_intent: str = Field(
        description="Chad's original NL input — what shows as the row's primary line.",
    )
    classified_command_type: str | None = Field(
        default=None,
        description="Router slug ('log-receipt', 'phase-update', etc.) for filtering/grouping.",
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured params extracted by the router.",
    )
    outcome: ActivityOutcome
    result_summary: str = Field(
        default="",
        description="Human-readable summary for the row's secondary line. Empty allowed; iOS renders no-summary case as gray italic.",
    )
    affected_entity_type: str | None = Field(
        default=None,
        description="'phase' | 'invoice' | 'change-order' | etc. — for deep-link.",
    )
    affected_entity_id: str | None = Field(
        default=None,
        description="UUID of the affected entity. Tap to navigate to its detail screen.",
    )
    cost_usd: float | None = Field(
        default=None,
        description="Anthropic API cost for this single activity.",
    )
    duration_ms: int | None = Field(
        default=None,
        description="Wall-clock duration end-to-end.",
    )
    error_message: str | None = Field(
        default=None,
        description="If outcome=error or partial, what failed (plain text, not stack trace).",
    )
    created_at: _datetime = Field(description="When the activity row was inserted.")


# ---------------------------------------------------------------------------
# HBAskStreamEvent — discriminated union over the 6 SSE event types
# ---------------------------------------------------------------------------
#
# Per migration_003_review.md § SSE stream contract. The engine yields
# (event_id, event_type, payload) tuples; the route handler serializes
# them into SSE wire format. iOS-side, each event's `data:` JSON parses
# as one of these typed payloads. The discriminator is the SSE `event:`
# line value, copied into the `type` field on the payload.
#
# All six event types share the discriminator field name `type` (literal
# const) so a Swift Codable enum can switch on it cleanly.

class HBAskTextDeltaEvent(_Base):
    """A token batch from Claude as it composes the answer."""
    type: Literal["text_delta"] = "text_delta"
    delta: str = Field(description="Token text to append to the active answer bubble.")


class HBAskToolUseEvent(_Base):
    """Claude invoked a tool. Surface as a 'thinking…' indicator with the tool name."""
    type: Literal["tool_use"] = "tool_use"
    id: str = Field(description="Anthropic tool_use block id; pairs this event with the matching tool_result.")
    name: str = Field(description="Tool name (e.g. 'list_projects', 'search_drive').")
    input: dict[str, Any] = Field(default_factory=dict, description="Full tool input.")


class HBAskToolResultEvent(_Base):
    """Tool returned. Surface as a status update; full result feeds Claude's next turn server-side."""
    type: Literal["tool_result"] = "tool_result"
    id: str = Field(description="Matches the corresponding tool_use event's id.")
    name: str
    duration_ms: int = Field(ge=0)
    summary: str = Field(description="One-line summary, truncated to ~160 chars.")


class HBAskCitationAddedEvent(_Base):
    """A Drive file was opened (read_drive_file). Render as a citation chip immediately."""
    type: Literal["citation_added"] = "citation_added"
    file_id: str
    name: str = Field(description="Drive file display name (e.g. 'Tracker – Whitfield Residence').")
    webViewLink: str = Field(description="Direct Drive URL the chip opens on tap.")


class HBAskMessageCompleteEvent(_Base):
    """Terminal event. Full final answer + citations + cost + duration."""
    type: Literal["message_complete"] = "message_complete"
    answer: str
    citations: list[HBAskCitationAddedEvent] = Field(
        default_factory=list,
        description="All citations from this answer. Same shape as citation_added events.",
    )
    tools_called: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Audit log of tools invoked: [{name, input, duration_ms}, ...]",
    )
    model: str
    cost_usd: float = Field(ge=0)
    duration_ms: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class HBAskErrorEvent(_Base):
    """Terminal event. Stream encountered an error before completing."""
    type: Literal["error"] = "error"
    error_type: str = Field(
        description="Pythonic exception class name or domain-specific code "
                    "(e.g. 'AuthenticationError', 'MaxIterationsReached', 'stream_expired').",
    )
    message: str


# Discriminated union — Pydantic v2 picks the right model based on `type`.
# iOS-side: this becomes a `HBAskStreamEvent` Swift enum with associated values
# per case, decoded via `init(from decoder:)` switching on the `type` field.
HBAskStreamEvent = Annotated[
    Union[
        HBAskTextDeltaEvent,
        HBAskToolUseEvent,
        HBAskToolResultEvent,
        HBAskCitationAddedEvent,
        HBAskMessageCompleteEvent,
        HBAskErrorEvent,
    ],
    Field(discriminator="type"),
]


class HBAskStreamEventEnvelope(_Base):
    """Full SSE event as it's surfaced to clients — id + type discriminator + payload.

    The engine emits (event_id, event_type, payload) tuples. The route handler
    serializes them into SSE wire format (id: ..., event: ..., data: {...}).
    iOS reconstructs an envelope per emitted event by combining the SSE id +
    event lines with the data payload. Pinning the envelope as a Pydantic model
    so iOS Codable matches what's actually on the wire.
    """

    event_id: int = Field(ge=1, description="Monotonic per-stream ID. Matches SSE 'id:' line.")
    event: HBAskStreamEvent


# ---------------------------------------------------------------------------
# JSON Schema export (called by docs/specs build pipeline)
# ---------------------------------------------------------------------------

def export_combined_json_schema() -> dict:
    """Build a single JSON Schema document covering all canonical wire formats.

    Output is suitable for Swift Codable generation, OpenAPI extension, or any
    other downstream type-generation tool. iOS-side, all wire types come out
    of this single artifact — no other source of truth.

    Sections:
      - Six view-model payloads (master/daily/weekly/monthly + checklist/notification stubs)
      - HBEngineActivity (audit log row for /activity)
      - Six HBAskStream event payloads + the discriminated-union envelope
        (for /ask/stream SSE consumer)
    """
    schemas = {
        # View models
        "MasterView": MasterViewPayload.model_json_schema(),
        "DailyView": DailyViewPayload.model_json_schema(),
        "WeeklyView": WeeklyViewPayload.model_json_schema(),
        "MonthlyView": MonthlyViewPayload.model_json_schema(),
        "ChecklistGatesView": ChecklistGatesViewPayload.model_json_schema(),
        "NotificationFeedView": NotificationFeedViewPayload.model_json_schema(),
        # Activity log
        "HBEngineActivity": HBEngineActivityPayload.model_json_schema(),
        # SSE stream events (discriminated by `type`)
        "HBAskStreamEnvelope": HBAskStreamEventEnvelope.model_json_schema(),
        "HBAskTextDelta": HBAskTextDeltaEvent.model_json_schema(),
        "HBAskToolUse": HBAskToolUseEvent.model_json_schema(),
        "HBAskToolResult": HBAskToolResultEvent.model_json_schema(),
        "HBAskCitationAdded": HBAskCitationAddedEvent.model_json_schema(),
        "HBAskMessageComplete": HBAskMessageCompleteEvent.model_json_schema(),
        "HBAskError": HBAskErrorEvent.model_json_schema(),
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://patton.ai/schemas/home-builder/view-models-v1.json",
        "title": "Home Builder Service Turtle — Wire Format Schemas v1",
        "description": (
            "Canonical wire formats emitted by the Home Builder Agent for the "
            "Patton AI iOS Shell to deserialize without transformation. Covers: "
            "the six view-model payloads (master/daily/weekly/monthly/checklist-gates/"
            "notification-feed), the engine_activity audit log row, and the six "
            "SSE event types streamed by /v1/turtles/home-builder/ask/stream "
            "(discriminated by the `type` field). See "
            "docs/specs/canonical-data-model.md, docs/specs/migration_003_review.md, "
            "and patton-ai-ios docs/03_build/turtle_contract_v1.md."
        ),
        "version": "1.1.0",
        "definitions": schemas,
    }
