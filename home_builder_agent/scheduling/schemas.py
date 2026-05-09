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
    """The seven view-model types projected by the engine."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    MASTER = "master"
    CHECKLIST_GATES = "checklist-gates"
    NOTIFICATION_FEED = "notification-feed"
    MORNING = "morning"


class UrgencyBand(str, Enum):
    """3-value urgency model used on morning view's today_on_site +
    todays_drop_deads sections. Per per-kind rules in
    morning-view-model.md § urgency_band semantics."""

    CALM = "calm"
    WATCH = "watch"
    URGENT = "urgent"


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
    """Mirrors canonical-data-model.md § entity 7 ChecklistItem.

    `photo_required` and `photos` flag/carry photo-evidence support per
    item — Chad's flow of approval treats some items as requiring a
    photo upload before close. The renderer surfaces a camera affordance
    on photo_required items; uploaded photos land in Drive under
    Site Logs/<Project>/Checklist Photos/<phase>/<item-slug>/.
    """

    id: str
    category: str = Field(
        description="One of 10 for Precon (Client & Contract, Plans & Engineering, ...); smaller set for other phases"
    )
    label: str
    is_complete: bool
    completed_by: str | None = None
    completed_at: _date | None = None
    notes: str | None = None
    photo_required: bool = Field(
        default=False,
        description="True if Chad's flow of approval expects photo evidence to close this item",
    )
    photos: list[dict] = Field(
        default_factory=list,
        description="Drive references for uploaded photos: [{drive_file_id, drive_url, uploaded_at, uploaded_by}, ...]",
    )
    tap_action: str | None = None
    photo_upload_action: str | None = Field(
        default=None,
        description="UserAction emit target for photo upload (e.g. 'checklist-item-photo-upload:<id>')",
    )


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
# DraftAction wire format (entity 18 — Chad's judgment queue)
# ---------------------------------------------------------------------------
# Mirrors home_builder.draft_action from migration 007. Read by the
# morning view-model's judgment_queue section. The Pydantic model is
# the wire-format projection; the engine-side dataclass lives in
# scheduling/draft_actions.py.
# ---------------------------------------------------------------------------


class DraftKind(str, Enum):
    """V1 judgment-queue vocabulary. Per morning-view-model.md § DraftKind."""

    GMAIL_REPLY_DRAFT = "gmail-reply-draft"
    CHANGE_ORDER_APPROVAL = "change-order-approval"
    LIEN_WAIVER_FOLLOWUP = "lien-waiver-followup"
    CLIENT_UPDATE_EMAIL = "client-update-email"
    VENDOR_ETA_CONFIRMATION = "vendor-eta-confirmation"
    INSPECTION_SCHEDULING_REQUEST = "inspection-scheduling-request"


class DraftStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    EDITED_THEN_APPROVED = "edited-then-approved"
    DISCARDED = "discarded"


class DraftActionPayload(_Base):
    """One judgment-queue item on the wire.

    Mirrors canonical-data-model.md § entity 18. Renderer reads this
    array out of the morning view's `judgment_queue.items` field;
    Chad's tap-to-approve / edit / discard emits a UserAction targeting
    the `draft_action_id`.
    """

    draft_action_id: str
    project_id: str
    kind: DraftKind
    status: DraftStatus
    originating_agent: str = Field(description="Which agent produced the draft (e.g. 'hb-inbox')")
    summary: str = Field(description="One-line preview for the queue card")
    subject_line: str | None = None
    from_or_to: str | None = Field(default=None, description="'From: …' or 'To: …' chip for the card")
    external_ref: str | None = Field(default=None, description="Pointer to underlying artifact (Gmail draft id, Drive doc id)")
    age_seconds: int = Field(ge=0)
    created_at: _date

    decided_at: _date | None = None
    decided_by: str | None = None

    approve_action: str | None = Field(default="draft-action-approve", description="UserAction emit target")
    edit_action: str | None = Field(default="draft-action-edit", description="UserAction emit target")
    discard_action: str | None = Field(default="draft-action-discard", description="UserAction emit target")
    click_action: str | None = Field(default=None, description="Deep-link to the inline edit surface")


# ---------------------------------------------------------------------------
# Morning view — Chad's coffee-cup landing surface (entity-anchored projection)
# ---------------------------------------------------------------------------
# Per docs/specs/morning-view-model.md. Section ordering is part of the
# contract; field semantics are documented in that spec. Caller supplies
# pre-computed weather + voice_brief + action_items (those involve
# external calls — NOAA, Anthropic — that don't belong in pure
# projection). All other sections derive from engine state via
# scheduling/view_models.py:morning_view().
# ---------------------------------------------------------------------------


class MorningWeatherRiskPhasePayload(_Base):
    """A phase whose work is at risk from the forecast."""

    phase_id: str | None = None
    phase_name: str
    risk_kind: str = Field(description="rain | wind | extreme-cold | extreme-heat")
    detail: str = Field(description="Plain-English chip — 'Wed-Thu rain conflicts with exterior trim install'")
    severity: Severity


class MorningWeatherPayload(_Base):
    """Weather block — pinned to top of morning surface when risk_phases non-empty."""

    summary_today: str
    summary_tomorrow: str | None = None
    risk_phases: list[MorningWeatherRiskPhasePayload] = Field(default_factory=list)


class MorningVoiceBriefPayload(_Base):
    """hb-chad narrator-voice synthesis. Composed in the same Sonnet
    call as action_items (one call, two deliverables)."""

    text: str = Field(description="3-5 sentences in Chad's voice synthesizing the day's state")
    model: str | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None


class MorningJudgmentQueuePayload(_Base):
    """Highest-leverage section. Drafts pending Chad's review."""

    count: int = Field(ge=0)
    items: list[DraftActionPayload] = Field(default_factory=list)


class MorningTodayItemPayload(_Base):
    """One item on the today_on_site surface — a phase active today, a
    delivery expected, an inspection scheduled. urgency_band drives the
    renderer's visual emphasis; per-kind rules in
    morning-view-model.md § urgency_band semantics."""

    kind: DailyItemKind = Field(description="phase-active | delivery | inspection (drop-dead lives in todays_drop_deads)")
    phase_id: str | None = None
    phase_name: str | None = None
    day_n: int | None = None
    of_total: int | None = None
    material_category: str | None = None
    install_phase_name: str | None = None
    install_date: _date | None = None
    urgency_band: UrgencyBand = UrgencyBand.CALM
    urgency_reason: str | None = Field(default=None, description="Plain-English chip explaining urgency, surfaced in expanded density")
    tap_action: str | None = None


class MorningTodayOnSitePayload(_Base):
    """Subset of daily view filtered to today + (project_id) + kinds in
    {phase-active, delivery, inspection}."""

    items: list[MorningTodayItemPayload] = Field(default_factory=list)


class MorningDropDeadItemPayload(_Base):
    """One drop-dead item on the morning surface. Spec mandates only
    OVERDUE / ORDER NOW band reaches this view; later bands belong on
    the daily/weekly surfaces."""

    material_category: str
    install_phase_name: str
    install_date: _date
    drop_dead_date: _date
    lead_time_days: int = Field(ge=0)
    urgency_band: UrgencyBand = UrgencyBand.URGENT
    tap_action: str | None = None


class MorningDropDeadsPayload(_Base):
    items: list[MorningDropDeadItemPayload] = Field(default_factory=list)


class MorningOvernightEventsPayload(_Base):
    """Subset of notification-feed projection filtered to created_at >
    now - 14h AND severity ≥ warning."""

    items: list[NotificationItemPayload] = Field(default_factory=list)


class MorningViewPayload(_Base):
    """Chad's coffee-cup landing payload.

    Section ordering reflects contract priority — the renderer is
    expected to honor it. Empty-state behavior per spec § Section
    ordering: weather risk + overnight_events omit when empty;
    judgment_queue + today_on_site + todays_drop_deads render with
    explicit empty-state copy; voice_brief + action_items never empty.
    """

    view_type: Literal[ViewType.MORNING] = ViewType.MORNING
    project_id: str
    project_name: str
    generated_at: _datetime
    as_of_local_date: _date
    tz: str = Field(default="America/Chicago", description="IANA tz from user_profile.working_hours")

    # Section 1
    weather: MorningWeatherPayload | None = None
    # Section 2
    voice_brief: MorningVoiceBriefPayload | None = None
    # Section 3 — highest-leverage real estate
    judgment_queue: MorningJudgmentQueuePayload = Field(default_factory=lambda: MorningJudgmentQueuePayload(count=0))
    # Section 4
    today_on_site: MorningTodayOnSitePayload = Field(default_factory=MorningTodayOnSitePayload)
    # Section 5
    todays_drop_deads: MorningDropDeadsPayload = Field(default_factory=MorningDropDeadsPayload)
    # Section 6
    overnight_events: MorningOvernightEventsPayload = Field(default_factory=MorningOvernightEventsPayload)
    # Section 7
    action_items: list[str] = Field(default_factory=list, description="1–5 imperative items from hb-chad")


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
    | MorningViewPayload
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

# ---------------------------------------------------------------------------
# Migration 004 — user_signal + user_profile (always-learning Chad)
# ---------------------------------------------------------------------------
#
# See docs/specs/migration_004_review.md for the table-level rationale and
# the open-enum signal vocabulary. These Pydantic models pin the wire
# format for /v1/signals (POST batch) and /v1/me/profile (GET) once the
# routes ship. iOS Codable mirrors the same shapes.

class UserSignalType(str, Enum):
    """v1 vocabulary of in-app behavior signals.

    Open-enum at the DB layer per migration 004 decision 3, but pinned
    here so engine + iOS + profile-builder agree on the names. Adding
    a new type = update this enum + iOS Codable + the profile-builder's
    aggregator. No DB migration required.
    """
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    SCREEN_VIEW = "screen_view"
    PROJECT_SWITCHED = "project_switched"
    ASK_QUERY = "ask_query"
    ASK_FOLLOWUP = "ask_followup"
    TOOL_INVOKED = "tool_invoked"
    NOTIFICATION_ACTED = "notification_acted"
    NOTIFICATION_DISMISSED = "notification_dismissed"
    VOICE_INPUT_USED = "voice_input_used"
    VOICE_INPUT_CANCELED = "voice_input_canceled"
    SHARE_RECEIVED = "share_received"


class HBUserSignalPayload(_Base):
    """One in-app behavior signal as it surfaces on the wire.

    POST /v1/signals accepts an array of these from iOS (batched per
    decision Q-A in migration 004 review). Schema mirrors the SQL columns
    in home_builder.user_signal.

    The `payload` dict is polymorphic — see signal vocabulary table in
    migration_004_review.md for the per-type shape contract. Pydantic
    keeps this loose at the wire layer; profile-builder dispatches on
    `signal_type` to a typed payload model when reading.
    """
    id: str = Field(description="UUID of this signal row")
    actor_user_id: str = Field(description="auth.users id of who emitted this signal")
    signal_type: UserSignalType = Field(description="v1 signal vocabulary discriminator")
    surface: ActivitySurface = Field(
        default=ActivitySurface.CHAT,
        description="Surface that emitted the signal — mirrors engine_activity.surface",
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Signal-specific structured data; shape varies by signal_type",
    )
    project_id: str | None = Field(
        default=None,
        description="Optional project context — null for app-wide signals like screen_view of project picker",
    )
    session_id: str | None = Field(
        default=None,
        description="Optional foreground-session UUID; iOS generates per session_start, omitted for background-emitted",
    )
    client_timestamp: _datetime | None = Field(
        default=None,
        description="When the iOS device recorded the event (different from server-received `created_at`)",
    )
    created_at: _datetime = Field(description="When the signal row was inserted server-side")


# ---------------------------------------------------------------------------
# HBUserProfileV1 — current preference state per user
# ---------------------------------------------------------------------------
#
# v1 shape per migration_004_review.md § Profile JSONB v1. Stored as
# JSONB inside home_builder.user_profile.profile + a `version` int on
# the row. Engine reads with `version` first, dispatches to the matching
# Pydantic model — lets us evolve the profile shape without DB migrations.

class ProfileVocabulary(_Base):
    preferred_terms: list[str] = Field(
        default_factory=list,
        description="Phrases Chad uses naturally that Claude should mirror",
    )
    avoid: list[str] = Field(
        default_factory=list,
        description="Phrases / formality levels Chad doesn't respond to",
    )


class ProfileWorkingHours(_Base):
    weekday_start_hour: int | None = Field(default=None, ge=0, le=23)
    weekday_end_hour: int | None = Field(default=None, ge=0, le=23)
    weekend_active: bool | None = Field(default=None)
    timezone: str | None = Field(
        default=None,
        description="IANA timezone (e.g. 'America/Chicago'). Notification dispatcher consults this.",
    )


class ProfileDecisionPatterns(_Base):
    common_vendors: dict[str, str] = Field(
        default_factory=dict,
        description="Material category → vendor name (e.g. 'windows' → 'Anderson')",
    )
    common_amounts: dict[str, float] = Field(
        default_factory=dict,
        description="Named typical dollar amounts (e.g. 'permit_fee_typical' → 850.00)",
    )


class ProfileAnswerStyle(_Base):
    length_preference: str | None = Field(
        default=None,
        description="'short' | 'medium' | 'long' — derived from session re-ask patterns",
    )
    format: str | None = Field(
        default=None,
        description="'bullets-then-implication' | 'paragraph' | 'numbered-steps' etc.",
    )
    include_dollar_amounts: bool | None = Field(default=None)
    include_dates: bool | None = Field(default=None)


class HBUserProfileV1(_Base):
    """Current preference state JSONB stored at user_profile.profile (version=1).

    Read by every Claude-touching surface (hb-ask, hb-router, push
    notification dispatcher) via system-prompt injection. Built nightly
    by hb-profile from user_signal + engine_activity + Drive/Gmail
    activity.
    """
    version: Literal[1] = 1
    vocabulary: ProfileVocabulary = Field(default_factory=ProfileVocabulary)
    working_hours: ProfileWorkingHours = Field(default_factory=ProfileWorkingHours)
    attention_weights: dict[str, float] = Field(
        default_factory=dict,
        description="project_id → 0..1 weight indicating relative attention. "
                    "Used by hb-router to disambiguate 'the project' references.",
    )
    decision_patterns: ProfileDecisionPatterns = Field(default_factory=ProfileDecisionPatterns)
    ignored_alert_types: list[str] = Field(
        default_factory=list,
        description="Notification template types Chad consistently dismisses; dispatcher suppresses these.",
    )
    answer_style: ProfileAnswerStyle = Field(default_factory=ProfileAnswerStyle)
    voice_input_pct: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="Fraction of recent inputs that used voice — telemetry, not behavioral.",
    )
    session_count_30d: int | None = Field(default=None, ge=0)
    ask_query_count_30d: int | None = Field(default=None, ge=0)


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
        # Migration 004 — personalization layer
        "HBUserSignal": HBUserSignalPayload.model_json_schema(),
        "HBUserProfileV1": HBUserProfileV1.model_json_schema(),
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
        "version": "1.2.0",
        "definitions": schemas,
    }
