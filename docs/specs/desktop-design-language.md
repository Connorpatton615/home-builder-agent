# Desktop Design Language

> One-line summary: the visual + interaction vocabulary for the Patton AI Mac shell. Defines palette, typography, spacing, motion, and component anatomy so the six surfaces in [`desktop-renderer.md`](desktop-renderer.md) ship cohesive instead of drifting per-surface.

**Status:** Spec — companion to `desktop-renderer.md`.
**Phase:** Precedes Phase 3 (Design) of the desktop renderer build.
**Owner:** CP.
**Last updated:** 2026-05-08.
**Lives in:** `~/Projects/patton-ai-ios/Mac/DesignSystem/` once built; tokens exported as a Swift `enum DesignTokens` and a `Color+Tokens` extension.
**Cross-references:**
- [`desktop-renderer.md`](desktop-renderer.md) — what gets rendered
- [`mobile-design-language.md`](mobile-design-language.md) — companion doc for the iOS truck-cab surface; mobile inherits brand + discipline, diverges where field posture demands it
- [`scheduling-engine.md`](scheduling-engine.md) — the data being rendered
- `~/Projects/home-builder-agent/brand/` — source-of-truth logo + palette

---

## North star

A workspace, not a dashboard. A homebuilder — Chad — sits down at a Mac with a coffee and *operates*. The interface should feel like a precision instrument: dark, calm, dense where it counts, fast everywhere. References that match the tone:

- **Linear** — keyboard-first, command palette, no decoration
- **Things 3** — desktop-native restraint, content over chrome
- **Superhuman** — speed signaling, no spinners on common ops
- **Apple's Logic Pro / Final Cut** — pro-tool dark UI density done right

Anti-references — what this is *not*:

- **Procore / Buildertrend** — busy, web-app feel, color soup
- **Generic SaaS** — gradient hero sections, stock illustrations, mascots
- **Catalyst-on-Mac** — iPad-app-pretending-to-be-Mac
- **Notion** — fine for docs, too soft for an operating workspace

---

## Brand foundation

The Patton AI logo (`brand/logo.svg`) is the source of truth. Three colors carry the brand:

| Token | Hex | Role |
|---|---|---|
| `brand.ink` | `#080b0f` | Primary surface — near-black, faint cyan undertone |
| `brand.signal` | `#22c55e` | Single brand accent — used sparingly, signals "Patton AI is alive and on-track" |
| `brand.bone` | `#edf2f7` | Foreground — off-white, warm-neutral |

The logo's discipline is one green block on a black field. The Mac app inherits that discipline: **color is reserved for status semantics**. Decorative color is forbidden.

---

## Color tokens

Semantic tokens (used by views) → mapped to raw values (defined once). Views never reference raw hex.

### Surface

| Token | Light | Dark (default) | Usage |
|---|---|---|---|
| `surface.canvas` | `#fafbfc` | `#080b0f` | App background |
| `surface.raised` | `#ffffff` | `#0e1217` | Sidebar, inspector pane |
| `surface.sunken` | `#f1f4f8` | `#05080c` | Input fields, code blocks, scrollable interiors |
| `surface.divider` | `#edf2f7` @ 60% | `#edf2f7` @ 8% | Hairlines between regions |
| `surface.row.banded` | — | `#edf2f7` @ 3% | Category-banded Gantt rows |

Dark is default. Light mode is a v1.5 deliverable; the tokens exist so we don't have to refactor when it lands.

### Foreground

| Token | Dark mode value | Usage |
|---|---|---|
| `fg.primary` | `#edf2f7` @ 95% | Body text, phase names, headlines |
| `fg.secondary` | `#edf2f7` @ 70% | Time header, metadata, captions |
| `fg.tertiary` | `#edf2f7` @ 45% | Disabled, hints, axis labels |
| `fg.inverse` | `#080b0f` | Text on signal-green chips |

### Status (functional color — the only place hue lives)

| Token | Hex | Meaning | Where it appears |
|---|---|---|---|
| `status.healthy` | `#22c55e` | On-track, alive, available | Today line, in-progress bars, healthy drop-deads, "go" buttons |
| `status.warning` | `#f59e0b` | Attention soon | Drop-deads 14–30 days out, weather risk, late check-ins |
| `status.critical` | `#ef4444` | Action required | Drop-deads <14d, blocked phases, failed inspections, missed deliveries |
| `status.muted` | `#edf2f7` @ 35% | Done and quiet | Complete phases, dismissed notifications |
| `status.info` | `#60a5fa` | Informational only — no action | System messages; used rarely |

Discipline: **a screen with no color is a screen with no problems.** Walking up to a quiet Gantt should feel reassuring, not boring.

### Translucency

Mac-native `.regularMaterial` for the toolbar and inspector pane backgrounds when over the canvas. This is what makes it feel native — translucent vibrancy that responds to wallpaper and window-behind state.

---

## Typography

Two faces, three weights, one scale.

| Face | Use |
|---|---|
| **SF Pro Text** | All UI text, body, headlines |
| **SF Mono** | Dates, money, durations, IDs, anything tabular |

Weights: `regular` (400), `medium` (500), `semibold` (600). No bold, no light, no italic for UI chrome (italic is allowed in user-authored notes only).

Scale (Mac defaults — match macOS native):

| Token | Size | Line height | Use |
|---|---|---|---|
| `type.display` | 22pt semibold | 28pt | Window title, project name in toolbar |
| `type.title` | 17pt semibold | 22pt | Section headers, inspector pane title |
| `type.body` | 13pt regular | 18pt | Default body, phase names, list items |
| `type.body-mono` | 13pt regular SF Mono | 18pt | Tabular data |
| `type.caption` | 11pt regular | 14pt | Time header, metadata, secondary labels |
| `type.caption-mono` | 11pt regular SF Mono | 14pt | Drop-dead countdowns, durations |

No custom fonts. No type below 11pt — accessibility floor.

---

## Spacing scale

4pt base unit. Use semantic names, never raw values:

| Token | Value | Use |
|---|---|---|
| `space.hair` | 1pt | Borders, dividers |
| `space.xs` | 4pt | Inside chips, between icon and label |
| `space.sm` | 8pt | Tight padding, inter-row gap |
| `space.md` | 12pt | Default cell padding |
| `space.lg` | 20pt | Pane padding, section gap |
| `space.xl` | 32pt | Window-edge padding, between major regions |
| `space.xxl` | 48pt | Hero spacing (rare) |

Density between containers, calm within them: containers butt up close (`space.sm` between cards), interiors breathe (`space.lg` inside).

---

## Motion

The product feels fast because motion is **fast and predictable**. Snappy, not bouncy.

| Token | Duration | Curve | Use |
|---|---|---|---|
| `motion.instant` | 80ms | linear | Hover state, focus ring |
| `motion.snap` | 150ms | ease-out | Most state changes — selection, button press, tab switch |
| `motion.slide` | 200ms | ease-out | Inspector pane slide-in, sidebar collapse |
| `motion.zoom` | 240ms | ease-in-out | Gantt zoom, view transitions |
| `motion.scrub` | 400ms | ease-in-out | Cmd+T scroll-to-today, calendar nav |

No spring physics. No bouncing. No overshoot. This isn't a phone toy. The one place to invest in fluidity is Cmd+Scroll on the Gantt — it should feel like Apple Maps zoom (anchor at cursor, not center).

Reduce-motion respects: all `motion.*` collapses to instant when `accessibilityReduceMotion` is on.

---

## Component vocabulary

Reusable primitives. Every surface in the app is built from these.

### Bar (Gantt + earned-time)

A horizontal status-bearing rectangle, 20pt tall, 4pt corner radius, 2pt vertical inset from its row. See § The Gantt.

### Chip

Inline status pill. 18pt tall, 6pt horizontal padding, 9pt corner radius. Hosts a glyph + 11pt mono label (e.g. "28 days", "BLOCKED"). Color = the status token of its content.

### Inspector pane

380pt wide, slides in from right, `surface.raised` background, `space.lg` interior padding. Header row (44pt) with title + close button + pin/unpin toggle. Body scrolls. Footer (48pt) for primary actions, sticks to bottom.

Pinning behavior: unpinned closes on Esc / outside click. Pinned stays open across selection changes — content updates to match the new selection.

### Side-pane is not a modal

Modals are forbidden except for destructive confirmation ("delete this checklist?"). Detail, edit, and authoring all happen in the inspector pane. Modals interrupt; inspectors compose.

### Command palette

Cmd+K. Centered floating panel, 540pt wide, max 8 visible rows, `surface.raised` with stronger blur. Type to filter. Shows: navigate-to commands, recent projects, recent phases, agent dispatch (`/route ...` triggers the router agent). Esc dismisses.

### Chat pop-over (`hb-chad`)

Cmd+/. Anchored to the active toolbar button (top-right). 480pt × 600pt, scrolls. Conversation history persisted per-project. Input at bottom. This is the in-app surface for the Chad master agent. Closes on Esc; preserves draft.

### Sidebar

220pt wide default, collapsible to 56pt (icon-only) or hidden entirely (Cmd+0). `surface.raised`. Contains: project list (top), nav (G/D/W/M/C/N), settings (bottom). Section dividers are `surface.divider`, not labels.

### Toolbar

32pt tall, traffic-light spaced (leave 70pt clear left for the close/min/zoom dots), `surface.raised` with `.regularMaterial` blur. Holds: project switcher (left), view toggle pill (center), agent button + settings (right).

### Status bar

24pt tall, bottom of window, `fg.tertiary` text. Holds: today indicator, zoom level, count of visible items, last-sync time.

### Empty states

Always actionable. Never decorative.
- Empty checklist → "Drop a Precon-style template, or generate one from this phase's industry standards." Two buttons.
- Empty notification feed → "No alerts. Patton AI checks every 60 seconds."
- Empty project list → "Drop a `spec.md` here, or run `hb-timeline` from Terminal." Drop zone is live.

---

## The Gantt — anatomy in detail

The Gantt is the desktop's signature surface. It carries more design weight than any other view, so it gets a dedicated section.

### Layout regions

```
┌─────────────────────────────────────────────────────────────────────┐
│  WHITFIELD RESIDENCE                          May 2026 ▸  ⚙  ⌘+/    │  toolbar (32pt)
├──────────────┬──────────────────────────────────────────────────────┤
│              │  MAY        JUN        JUL        AUG        SEP     │  time header (40pt)
│ PHASES       │  ─┬─┬─┬─┬─  ─┬─┬─┬─┬─  ─┬─┬─┬─┬─  ─┬─┬─┬─┬─  ─┬─    │
│              │     ┊ today                                          │
├──────────────┼──────────────────────────────────────────────────────┤
│ Pre-con      │ ████████████░                                        │  category band A
│ Permit       │           ░░░▓▓▓▓▓▓▓▓                                │  category band A
│ Site Prep    │                  ░░░░░░░░░░░░                        │  category band B
│              │                       ▽                              │
│ Foundation   │                        ░░░░░░░░░                     │  category band B
│ Framing      │                                ░░░░░░░░░░░░░         │  category band C
│              │                                  ▼ < 14d (red)       │
│ ...          │                                                      │
├──────────────┴──────────────────────────────────────────────────────┤
│  ┃ TODAY                                          [zoom: month ▾]   │  status bar (24pt)
└─────────────────────────────────────────────────────────────────────┘
```

Four regions:
1. **Phase rail** — 200pt wide, sticky horizontally, scrolls vertically with the body. Phase name (`type.body`) + status glyph.
2. **Time header** — 40pt tall, sticky vertically, scrolls horizontally with the body. Month labels at quarter zoom; week ticks at month zoom; day numbers at week zoom.
3. **Body** — the canvas. Phase bars + drop-dead triangles + today line + weekend striping + category bands.
4. **Status bar** — 24pt tall, bottom. Today reminder, zoom control, visible-phase count.

### Today line

**Dashed**, 1pt, `status.healthy` (`#22c55e`), full canvas height. Pattern: 4pt on, 4pt off. Drawn over everything except the hover ring. Anchored to the date midnight in the user's timezone — moves at midnight without redrawing the body.

### Phase bars — visual encoding

| Status | Treatment |
|---|---|
| Not started | Stroke `fg.tertiary` (1pt), no fill, dashed (3pt on / 3pt off) |
| In progress | Fill `status.healthy` @ 22%, stroke `status.healthy` (1pt), solid |
| Complete | Fill `status.muted`, no stroke |
| Blocked | Stroke `status.critical` (1.5pt), fill `status.critical` @ 15%, dashed (4pt on / 2pt off) |

Bar height 20pt, corner radius 4pt, 8pt vertical padding inside the 36pt row.

### Drop-dead triangles

Anchored 4pt below the install phase's bar, at the order date's x-coordinate. 10pt equilateral, pointing **up** at the bar so the eye traces "this triangle gates this phase."

| Urgency | Glyph | Color | Behavior |
|---|---|---|---|
| > 30 days | ▽ outlined | `status.healthy` | Static |
| 14–30 days | ▼ filled | `status.warning` | Static |
| < 14 days | ▼ filled | `status.critical` | Static |
| Hit today | ▼ filled | `status.critical` | Pulses opacity 60→100% over 1.4s |

Pulse runs only for hit-today drop-deads, on a separate `TimelineView(.animation)` overlay so it doesn't invalidate the static body cache.

### Category banding

Phase rows group into seven categories. Alternating groups get a `surface.row.banded` (3% white) wash; the next group is plain `surface.canvas`. No hue, only luminance. The bands give the eye categorical grouping without breaking the "color = status" discipline.

Default category groupings (from `home_builder_agent.scheduling.phase_template`):

| Group | Phases | Banded? |
|---|---|---|
| Pre-construction | Pre-con, Permit | Yes |
| Earthwork | Site Prep, Foundation | No |
| Structure | Framing, Roofing | Yes |
| MEP rough-ins | Plumb rough, Elec rough, HVAC rough | No |
| Enclosure | Windows, Exterior cladding | Yes |
| Finishes | Drywall, Trim, Paint, Flooring, Cabinets, Tile | No |
| Closeout | Punch list, Final inspections, CO | Yes |

The banding pattern is deterministic per category, not per row, so visual grouping is stable when phases reorder.

### Weekend stripe

At week zoom only: Saturday and Sunday columns get a `#edf2f7` @ 3% wash across the body height. Skipped at month and quarter zoom — too noisy.

### Interactions (priority order)

1. **Cmd+Scroll** — zoom (week ↔ month ↔ quarter). `motion.zoom`. Anchor at cursor x, not center.
2. **Drag horizontally on empty body** — pan time. Inertia 50ms decay.
3. **Click bar** — open inspector pane. Bar gets `status.healthy` @ 60% selection ring (1.5pt outset).
4. **Hover bar (250ms delay)** — tooltip with phase name, dates, duration, status, open-checklist count.
5. **Click drop-dead** — opens drop-dead inspector (separate from phase inspector — material category, vendor candidates).
6. **Cmd+T** — animated scroll-to-today, `motion.scrub`.
7. **Cmd+drag across bars** — multi-select. Inspector enters batch mode.
8. **Right-click bar** — context menu: mark complete, mark blocked, apply override, open checklist, copy link.
9. **Two-finger horizontal swipe** — pan (trackpad-native).
10. **Esc** — clear selection, close unpinned inspector.

### Rendering strategy

The 60fps @ 1000-phase bar in `desktop-renderer.md` Phase 1 is non-negotiable. Approach:

- **SwiftUI `Canvas`** for the body. One draw call per frame.
- **Viewport culling** — only draw bars whose date range intersects the visible time window. A 1000-phase project at month zoom shows ~120 on screen.
- **Static layer cache** — phases keyed by `(phaseId, statusVersion, zoomLevel)` rasterize to a `CGImage`. Cache invalidates on `engine_activity` watermark bump.
- **Hover/selection on overlay layer** — never invalidates the static cache.
- **Today line on overlay** — moves at midnight without body redraw.
- **Drop-dead pulse on `TimelineView(.animation)` at 30fps** — only the pulsing triangles tick; static drop-deads don't.

Spike Phase 1 exit bar should be **1000 phases at 60fps**, not the 200 currently in `desktop-renderer.md`.

---

## Per-surface notes (deltas from the Gantt baseline)

### Daily view (S2)

Three-pane: project list (left, 200pt, sidebar-styled) | today's items (center, flex) | activity timeline (right, 320pt, inspector-styled). Item rows are 56pt — taller than Gantt rows because each row hosts a primary action button (Mark Complete, Confirm Delivered, Pass/Fail). Activity timeline is 13pt mono with 11pt mono timestamps.

### Weekly view (S3)

7-column grid, columns equal width, rows = active projects. Cell height 80pt minimum. Pinned header strip shows "this week's drop-deads" as horizontal chip row. Cmd+arrow for prev/next week.

### Monthly view (S4)

Calendar grid (rows = weeks, columns = days). Per-day cell hosts up to 3 badges (deliveries / installs / inspections / drop-deads) — overflow shows "+2 more". Per-project earned-time bar pinned at the top, full window width. Earned-time bar uses the same `Bar` primitive as the Gantt — visual continuity.

### Checklist authoring (S5)

Tree on left (categories collapsible), item detail on right when selected. Tree rows 32pt. Drop-dead chip pinned right of each item. Multi-select with Cmd+Click; batch toolbar slides in from the bottom (44pt tall, `surface.raised`) when 2+ items are selected.

### Notification feed (S6)

Reverse-chrono list. Each row 72pt: severity icon (left, 24pt) | subject + 2-line body (center, flex) | age + quick actions (right, 120pt). Filter chips pinned at top. Critical-count badge mirrors to the macOS menu bar via `NSStatusItem`.

---

## Anti-patterns (do not ship)

- Gradients (except Mac-native `.regularMaterial` blur)
- Drop shadows beyond 1pt elevation
- Bouncing or springing animations
- Skeuomorphic icons (no leather, no notebook paper, no fake stitching)
- Custom fonts
- Color used decoratively (rainbow phase tags, mood-colored backgrounds)
- Modals for primary actions
- Spinners on common ops — if it takes >200ms, that's a backend bug
- Fake construction iconography (hardhats, hammers, blueprints) — Patton AI is software, not a Procore knockoff
- Notification dings on non-critical events
- Hover sounds, click sounds — silent by default

---

## Open questions

- **Light mode parity** — defer to v1.5 or ship at v1? Lean: v1.5. The brand is dark-first, the early audience (Chad in his office, Connor at his desk) won't ask for light, and shipping both doubles design QA.
- **Window background opacity** — should the canvas be fully opaque `#080b0f` or use vibrancy material so wallpaper bleeds through subtly? Lean: opaque. Vibrancy is decorative; this is a tool.
- **Custom app icon dark/light variants** — macOS Sonoma supports per-mode icons. Lean: yes, ship both — light variant is the same logo with `brand.bone` background instead of `brand.ink`.
- **Token export format** — generate `DesignTokens.swift` from a single source-of-truth JSON, or hand-write Swift? Lean: hand-write Swift initially, formalize a generator if a designer joins.
- **Accessibility contrast on `status.healthy` over `surface.canvas`** — `#22c55e` on `#080b0f` clears AAA for non-text but only AA for 13pt body. For text on signal-green chips, use `fg.inverse` (the brand near-black) — clears AAA. Audit every text-on-color pair before shipping.
- **Per-category banding source** — phase categorization is hard-coded in this doc. Should it move to `home_builder_agent/scheduling/phase_template.py` as a `category` field on each PhaseTemplate so the Mac and iOS read the same grouping? Lean: yes, do this in engine V2 wiring (Phase 2 of `desktop-renderer.md`).
