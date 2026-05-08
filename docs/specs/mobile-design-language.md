# Mobile Design Language

> One-line summary: the visual + interaction vocabulary for the Patton AI iOS shell — the truck-cab surface. Inherits brand and discipline from [`desktop-design-language.md`](desktop-design-language.md), diverges where field posture demands it (sun glare, gloves, thumb arc, push-first, offline-tolerant). Defines what the iOS app feels like so the existing tabs stay coherent and the field experience matches the same brand promise as the desktop.

**Status:** Spec — companion to `desktop-design-language.md`. Written for tomorrow's build run.
**Phase:** Active — iOS shell is shipped (PattonAIShell), this codifies what the next pass conforms to.
**Owner:** CP.
**Last updated:** 2026-05-08.
**Lives in:** `~/Projects/patton-ai-ios/PattonAIShell/DesignSystem/` once built; tokens shared with the Mac target via `Shared/DesignTokens.swift`.
**Cross-references:**
- [`desktop-design-language.md`](desktop-design-language.md) — the parent doc; everything not overridden here defers to it
- [`desktop-renderer.md`](desktop-renderer.md) — the Mac counterpart surface
- [`scheduling-engine.md`](scheduling-engine.md) — the engine view-models both surfaces consume
- [`canonical-data-model.md`](canonical-data-model.md) — entity contract + view-model schemas
- [`chad-agent.md`](chad-agent.md) — the Ask tab's master agent
- `~/Projects/home-builder-agent/brand/` — source-of-truth logo + palette

---

## North star

A **truck-cab instrument**. Chad is standing on a slab in the sun, or sitting in his truck between job sites, or walking a framing inspection with a clipboard in one hand and his phone in the other. The iOS app must answer "what now?" in under two seconds and accept a one-thumb input in under three taps.

References that match the tone:

- **Apple Weather / Apple Maps** — calm, glanceable, sun-readable; type scale leans large
- **Strava on a ride** — answers the most important question immediately, secondary detail one tap deep
- **Things 3 mobile** — restraint at phone scale, single accent, no clutter
- **Linear mobile** — content over chrome, fast feel

Anti-references — what this is *not*:

- **Procore mobile / Buildertrend mobile** — busy, multi-color, web-feel-on-phone
- **Generic SaaS dashboards-on-phone** — everything crammed in, nothing emphasized
- **A mini desktop app** — phones are not small Macs; layout density and input affordances differ

The shorthand: **same instrument, different posture.** Desktop is the workshop bench; mobile is the tool belt.

---

## Continuity with desktop (do not relitigate)

These come from `desktop-design-language.md` unmodified. Mobile inherits, never forks.

- **Brand palette.** `#080b0f` ink, `#22c55e` signal, `#edf2f7` bone.
- **Color discipline.** Hue reserved for status semantics. A screen with no color is a screen with no problems.
- **Typography faces.** SF Pro Text + SF Mono. Three weights max.
- **Anti-patterns.** No hardhats, no blueprints, no leather, no fake stitching, no decorative gradients, no spring physics, no notification dings on non-critical events.
- **Status semantics.** Green = healthy, amber = attention soon, red = action required, muted = done and quiet, info = blue (rare).

If a tonal question isn't answered in this doc, default to the desktop doc's answer.

---

## Divergence — what the field demands

These are the deltas mobile owns. Each is justified by truck-cab posture, not aesthetic preference.

### Type scale bumps up

Sun glare and a bumpy ride eat small type. Mobile body is **15pt**, captions never go below **13pt**.

| Token | Size | Line height | Use |
|---|---|---|---|
| `type.display` | 28pt semibold | 34pt | Tab title, project hero |
| `type.title` | 20pt semibold | 26pt | Section header, sheet title |
| `type.body` | 15pt regular | 21pt | Default body — list rows, paragraph |
| `type.body-emph` | 15pt semibold | 21pt | List row primary text, button label |
| `type.body-mono` | 15pt regular SF Mono | 21pt | Dates, money, durations |
| `type.caption` | 13pt regular | 18pt | Metadata, secondary labels |
| `type.caption-mono` | 13pt regular SF Mono | 18pt | Drop-dead countdowns, timestamps |
| `type.micro` | 11pt regular | 14pt | **Last resort only** — system bars, tab bar labels |

Rule of thumb: if it carries information Chad acts on, it's at least `type.body`. `type.micro` is for OS-imposed surfaces only (tab bar labels, status bar).

### Stroke weights go heavier

Hairlines die in sunlight. Minimum stroke is **1.5pt**, divider opacity bumps to 12% (vs desktop's 8%).

| Token | Mobile value | Desktop value | Why |
|---|---|---|---|
| `surface.divider` | `#edf2f7` @ 12% | `#edf2f7` @ 8% | Glare visibility |
| `border.minimum` | 1.5pt | 1pt | Same |
| `selection.ring` | 2pt | 1.5pt | Glove-tap precision |

### Tap targets respect the glove rule

Apple's 44pt floor is the **secondary** target size. Primary field actions (Mark Delivered, Pass / Fail / Reinspect, Confirm, Reschedule) get **56pt** — 44pt is reachable with a thumb, 56pt is reachable with a glove or a wet hand.

| Token | Size | Use |
|---|---|---|
| `tap.primary` | 56pt | Field action buttons, sheet primary CTAs |
| `tap.standard` | 44pt | List rows, secondary buttons |
| `tap.compact` | 36pt | Toolbar items, dense controls (use sparingly) |

Adjacent tappables get **8pt minimum** between them — fat-finger tolerance.

### Status color goes louder

A drop-dead red on mobile must grab attention from a peripheral glance across the cab. Desktop uses tiny triangles; mobile uses **full-row treatments**.

| Severity | Desktop | Mobile |
|---|---|---|
| Healthy | Triangle outline | Inline chip, no row treatment |
| Attention soon | Filled amber triangle | Left-edge 4pt amber bar on the row |
| Action required | Filled red triangle | Left-edge 4pt red bar **+** subtle red row tint @ 8% |
| Hit today | Pulsing red triangle | Left-edge 4pt red bar **+** pulsing red tint @ 8→16% over 1.4s |

The escalation is volumetric — more screen real estate as urgency rises. From across a job site, a Chad-glance lands on the red row first.

### Layout: thumb arc, bottom-weighted

Anything Chad acts on lives in the **bottom third** of the screen. The top third is read-only context. The middle is content scroll.

```
┌─────────────────────────┐
│ ┃ synced  Whitfield ▾  │  status strip (24pt) + project switcher (40pt)
├─────────────────────────┤
│                         │
│   read-only context     │
│   (today, drop-deads,   │
│    next phase, etc.)    │  top third — eyes
│                         │
├─────────────────────────┤
│                         │
│   content scroll        │
│   (list rows, cards)    │  middle — eyes + thumb
│                         │
│                         │
├─────────────────────────┤
│  [ PRIMARY ACTION ]     │  thumb arc — primary CTA (56pt)
│  [ secondary ] [ alt ]  │
├─────────────────────────┤
│  ◉  ◯  ◯  ◯  ◯         │  tab bar (49pt iOS standard)
└─────────────────────────┘
```

The top is for "what's the situation," the bottom is for "what do I do about it."

### Haptics replace hover

Mobile has no cursor; haptics carry the feedback weight.

| Token | iOS API | When |
|---|---|---|
| `haptic.select` | `.selectionChanged` | Picker scroll, tab change, list selection |
| `haptic.confirm` | `.impactOccurred(.medium)` | Primary action committed (Mark Delivered, Confirm) |
| `haptic.success` | `.notificationOccurred(.success)` | Sync completed, photo saved |
| `haptic.warn` | `.notificationOccurred(.warning)` | Destructive confirm, offline action queued |
| `haptic.error` | `.notificationOccurred(.error)` | Sync failed, validation error |

Every primary action lands a haptic. No silence on tap.

### Camera + voice are first-class

Field hands are dirty, the truck is loud, the slab is windy. Photo and voice are not buried in menus.

- **Persistent camera affordance.** Top-right toolbar button on every tab (or in a tab-specific FAB position). Tap → opens camera in receipt-or-site-photo mode. The disambiguation modal (receipt vs site log photo vs phase progress) appears *after* capture, not before — capture first, classify second.
- **Voice composer everywhere text is input.** Native iOS dictation works, but the composer must be sized for it: minimum 88pt tall when active, dictation indicator visible, no thin chrome. The Ask tab composer is `tap.primary` height even when collapsed.
- **Voice-only quick capture.** Long-press the Ask tab → starts dictation immediately, sends on release. Sub-second time to first word.

### Offline indicator is permanent UI

A 24pt status strip at the very top of the screen, above any tab content, never hidden:

| State | Color | Label | When |
|---|---|---|---|
| Synced | `surface.canvas` (invisible) | — | Online, no queued actions |
| Synced + activity | `status.healthy` @ 12% | "synced" | After a successful write, fades after 2s |
| Queued | `status.warning` @ 20% | "n queued" | One or more actions in the offline buffer |
| Offline | `status.critical` @ 20% | "offline {duration}" | No connection > 30s |
| Conflict | `status.critical` @ 30% | "tap to resolve" | Engine rejected a queued write |

Tapping the strip when in **queued / offline / conflict** opens the sync inspector — list of pending writes, retry / discard controls, manual resync button.

### Glanceability over density

The desktop's job is to fit a 1000-phase Gantt on screen. The mobile's job is to fit *one good answer* on screen. Mobile rows are **taller** (72pt vs desktop's 36pt), content per screen is **lower**, hierarchy is **steeper**.

A daily-view row example:

```
┌─────────────────────────────────────┐
│ ┃                                   │
│ ┃ Tile delivery                     │  primary line — type.body-emph
│ ┃ today · 10:00 · J&D Tile Co.      │  secondary — type.caption-mono
│ ┃                                   │  72pt total
│ ┃             [ CONFIRM RECEIVED ]  │  inline action — tap.primary
└─────────────────────────────────────┘
```

One row = one situation = one action. No "click to expand" for the primary case.

---

## Component vocabulary (deltas from desktop)

Inherited components (Bar, Chip, etc.) work the same; these are the ones that differ or are mobile-only.

### Tab bar (iOS standard)

49pt tall (iOS spec), `surface.raised` with `.regularMaterial` blur. **5 tabs maximum** — beyond that, glanceability dies. Each tab: SF Symbol icon (28pt) + `type.micro` label.

The tab bar uses the brand palette, not iOS defaults:
- Selected tab icon + label: `status.healthy` (`#22c55e`)
- Unselected: `fg.tertiary`

The selected tab is the only place the signal green appears in chrome. Everything else green is content-status.

### Sheet (replaces desktop's inspector pane)

Bottom-sheet pattern. `surface.raised` background, 16pt corner radius on top edge, drag handle (4pt × 36pt rounded bar) at top center. Three detents:

| Detent | Height | Use |
|---|---|---|
| `sheet.peek` | 25% | Hint preview — drop-dead detail summary |
| `sheet.medium` | 50% | Default — phase detail, checklist item, drop-dead context |
| `sheet.large` | 92% | Full takeover — long lists, conversation, photo gallery |

Drag between detents. Swipe down past `peek` dismisses. Tap outside dismisses to previous detent (medium → peek → dismissed).

Inspector pane affordances (pin / collapse) don't exist on mobile — sheets are inherently transient.

### Floating action button (FAB) — used sparingly

One per tab, only when there's a single dominant action. The Ask tab has none (the composer *is* the action). The Today tab has a camera FAB. The Schedule tab has none (selection drives action). Don't add a FAB just because the tab feels empty.

FAB spec: 56pt circle, `status.healthy` fill, `fg.inverse` (`#080b0f`) icon, 1pt elevation shadow, anchored 16pt from bottom-right above the tab bar.

### Push notification (front-door surface)

The lock-screen notification *is* the product for many interactions. Treat it like any other view:

```
┌────────────────────────────────────────┐
│ Patton AI · now                       …│
│ Tile drop-dead in 12 days             │
│ Whitfield · order by May 20           │
│                                        │
│  [ Order now ]   [ Snooze 1d ]        │
└────────────────────────────────────────┘
```

| Element | Spec |
|---|---|
| Title | 1 line, ≤ 32 chars, leads with the situation not the brand |
| Subtitle | 1 line, ≤ 40 chars, project · context |
| Quick actions | Max 2, both must be valid one-tap operations (no "open app to continue") |
| Sound | Default for critical-severity events; silent for warning; silent for info |
| Critical-alert entitlement | Reserved for inspection-failure-blocking and weather-stop-work only |

Push notifications are a writable surface — quick action taps fire `UserAction` writes through the same reconcile pass desktop uses. **No new endpoints.**

### Photo capture sheet

Camera opens in `sheet.large` detent. Capture button is 72pt circle (bigger than FAB — gloves), centered bottom. Top-right has a single mode chip: "Receipt" / "Site Log" / "Phase Progress" — defaults to last-used. Tap chip to cycle. Capture writes immediately; the disambiguation / annotation step happens in a second sheet *after* capture, never blocking the shutter.

### Today's situation card (mobile-only)

The hero element on the Today / Daily tab. Fills the top third. Shows:

- Project name (`type.display`)
- One-line "what's happening" summary, generated by the engine (`type.title`)
- Up to 3 inline status chips (next drop-dead, weather, sub-on-site)
- Background tint reflects severity (canvas → amber wash → red wash, never green)

This card replaces what the desktop renders as a multi-pane daily view. Mobile gets the *answer*; desktop gets the *layout*.

---

## Today view — mobile's signature surface

Where the Gantt is the desktop's signature, the **Today view** is the mobile's. It's the first screen Chad sees when he opens the app from a push notification or a cold launch.

### Anatomy

```
┌─────────────────────────────────────┐
│ ┃ synced                  ⚙        │  status strip + settings (32pt)
├─────────────────────────────────────┤
│                                     │
│  WHITFIELD RESIDENCE                │  type.display
│  Framing day 3 of 14                │
│  ⏰ 2 drop-deads this week          │  type.title + chips (top third)
│                                     │
├─────────────────────────────────────┤
│  ┃ Tile delivery       10:00 ─►    │
│  ┃ today · J&D Tile               │  72pt rows, primary action inline
│  ┃        [ CONFIRM RECEIVED ]     │
├─────────────────────────────────────┤
│  ┃ Slab inspection     14:00 ─►    │
│  ┃ today · J. Vincent              │
│  ┃        [ PASS ] [ FAIL ]        │
├─────────────────────────────────────┤
│   ▓ Lumber order       ⚠ <14 days │  drop-dead row, amber edge
│   ▓ order by May 14                │
├─────────────────────────────────────┤
│                                     │
│         [ + LOG SITE NOTE ]         │  thumb-arc primary
│                                     │
├─────────────────────────────────────┤
│  ◉ Today  ◯ Schedule  ◯ Ask  ◯ +  │  tab bar
└─────────────────────────────────────┘
```

### Sections (top to bottom)

1. **Status strip** (24pt) — sync state, never hidden
2. **Project context** (~120pt) — current project, day-in-phase, headline status
3. **Today's items** (scrollable) — 72pt rows, each one situation + one action
4. **This week's drop-deads** (collapsed by default) — tappable header to expand
5. **Quick capture** — site log button, voice composer access
6. **Tab bar** (49pt) — fixed

### Interactions

- **Pull to refresh** — re-fetches the daily view-model. Standard iOS spec.
- **Swipe left on a row** — secondary actions (snooze, reassign, mark blocked)
- **Swipe right on a row** — primary action one-tap commit (where applicable — e.g., swipe right on a delivery row = Confirm Received with `haptic.confirm`)
- **Long-press a row** — opens `sheet.medium` with phase detail
- **Tap drop-dead row** — opens drop-dead detail sheet (vendor candidates, lead time, install phase link)
- **Tap project name** — `sheet.medium` project switcher (when multi-project goes live)

### What this view is *not*

- Not a Gantt — that's read-only and lives one tap deep on the Schedule tab
- Not a Tracker — Sheets remain a desktop / Drive surface
- Not a notification feed — that's its own tab; this surface shows *today's actionable items*, not the historical alert log

If something Chad sees on this view requires more than a sheet to handle, it shouldn't be on this view in the first place. The Today view is for **answers + one-tap actions only**.

---

## Push notification anatomy

Push is the front door for half of Chad's interactions. Spec'd as a first-class surface.

### Severity → presentation

| Severity | Sound | Banner style | Lock-screen behavior |
|---|---|---|---|
| `info` | none | Banner, auto-dismiss | Does not wake screen |
| `warning` | none | Banner, persists | Wakes screen, no sound |
| `critical` | default iOS alert | Alert, persists | Wakes screen, alert sound |
| `critical-blocking` | critical-alert (entitlement) | Critical alert | Bypasses Do Not Disturb |

`critical-blocking` is reserved for: inspection failure that blocks framing inspection, weather stop-work, missed-payment lien risk. **Never** for marketing or "just FYI."

### Quick actions — design rules

- Max 2 actions per notification
- Both actions must be valid one-tap operations (no "open app to continue")
- Action labels are verbs: "Order now," "Confirm," "Snooze 1d," "Mark blocked"
- The destructive action (if any) is the second slot
- Action taps fire `UserAction` rows through the existing reconcile pass — no new endpoints

### Notification content rules

- Title leads with the situation, not "Patton AI says…"
- Subtitle carries project + context, never restates the title
- No emoji (the brand doesn't use them)
- No exclamation marks (the brand doesn't use them)
- Time references are relative: "in 12 days," "now," "tomorrow 8am" — never raw timestamps
- Money references use SF Mono in app — push notifications get the system font, but format with thousands separators

---

## Tab structure

The current tabs you have are the structure this doc commits to — **don't expand the tab count without a real reason**, and don't shrink it either if the existing distribution is working. iOS tab bars hold 5 tabs comfortably; beyond that, glanceability dies.

This section is intentionally **a placeholder** until the tab list is locked. When you confirm the tabs (likely **Today / Schedule / Ask / Activity / +One**, based on existing spec references), this section gets fleshed out per-tab. Each tab gets:

- **Purpose** — one sentence: what question does this tab answer?
- **Hero element** — the one thing that fills the top third
- **Primary action** — what's the dominant interaction in the bottom third?
- **Sheet vocabulary** — which sheets does this tab open?
- **What it explicitly is not** — boundary against scope creep into adjacent tabs

Until then, every tab honors the global rules in this doc.

---

## Performance + perceived speed

The desktop has a 60fps @ 1000-phase Gantt bar. Mobile has an analogous bar, but framed differently: **every primary tap lands a visual response in <100ms, every view loads its first meaningful pixel in <300ms.**

Tactics:

- **Optimistic writes.** Every `UserAction` (Mark Delivered, Confirm, Pass) updates the local UI immediately, queues the write, and reconciles. The status strip carries the truth about whether it actually shipped.
- **Skeleton over spinner.** Loading states use shape-shaped placeholders (rounded rects in `surface.sunken` color) at the size of the eventual content, not spinners. A spinner says "I'm working"; a skeleton says "this is going to be here in a moment."
- **Pre-fetch on tab switch.** The view-model for the destination tab loads before the animation completes. Tab transitions are 200ms — enough budget for a network round-trip on LTE.
- **Cache aggressively.** The last good view-model for each tab persists in `UserDefaults` (small) or Keychain (auth) or local SQLite (bulky). Cold launches render cached state immediately, then refresh.
- **No common op gets a spinner.** If something takes >300ms, that's a backend bug to file, not a UI element to design around.

---

## Anti-patterns (mobile-specific additions)

In addition to the desktop anti-pattern list, mobile bans:

- **Tap targets <44pt** — never. iOS rejects the app at review for this; the brand rejects it earlier.
- **Multiple toasts stacking** — one toast at a time, max 3 seconds, never blocking content.
- **Bottom sheets that don't dismiss on swipe-down** — every sheet supports swipe-down dismissal; non-dismissible sheets are reserved for destructive confirms.
- **Hidden gestures without affordance** — if a swipe action exists, the row shows a hint on first appearance.
- **Scroll-jacking on launch** — the Today view doesn't auto-scroll; Chad's thumb is in charge.
- **Modals for primary actions** — sheets only, modals reserved for destructive confirm.
- **Auto-playing media** — no auto-play video, no sound on launch.
- **Decorative iconography in the tab bar** — SF Symbols only, brand-aligned weight (regular). No custom icons.
- **More than 5 tabs** — period.

---

## Open questions

- **Tab roster lock-in.** Currently inferred (Ask, Activity confirmed; Today, Schedule, +One implied). Connor confirms tomorrow; this doc gets a per-tab section once locked.
- **Website tone reference.** Connor will share the live site URL; tonal references (palette accent intensity, type rhythm, voice) feed back into this doc and may surface deltas worth applying. Default: this doc holds, the website inherits from `desktop-design-language.md`'s discipline anyway.
- **Critical-alert entitlement.** Apple requires a request submission for critical alerts. Worth requesting for the inspection-blocking + weather-stop-work cases? Lean: **yes, request it** — these are exactly the cases the entitlement exists for, and Apple grants for construction safety contexts.
- **Offline buffer hard cap.** What's the max number of queued writes the app holds before refusing new ones / surfacing "your truck has been offline for too long"? Lean: 100 actions or 24h, whichever first.
- **Light mode parity.** Inherits from the desktop answer (defer to v1.5). But field readability under direct sun may push light mode up the priority list — dark on a bright phone screen has its own glare profile. Worth a real-sun field test before committing.
- **Voice-only entry point.** A long-press gesture on the Ask tab triggers dictation. Should there be a system-wide shortcut (Siri intent? Action Button on iPhone 15 Pro+?) for "log this voice note to the active project" without opening the app? Lean: ship Siri intent in v1.1.
- **Photo-first vs receipt-classification first.** This doc says capture first, classify second. Validate with Chad — does he prefer to declare "this is a receipt" before opening the camera, or tap-shoot and let the model classify? Lean: capture-first is correct, but worth a 30-second observation in the field.
- **Glove test.** Specs assume gloves; reality may demand different — a real-day field test (gloves on, sun overhead, dirty screen) before locking the 56pt floor.

---

## What this doc does for tomorrow's run

If you're picking this up tomorrow, the actionable contents are:

1. **Type scale + tap target floors** — apply to every existing screen as a sweep pass.
2. **Status color escalation** — the row-edge bar + tinted background pattern replaces the desktop triangle pattern in any list view.
3. **Sheet detents** — convert any modal currently in use to the three-detent sheet pattern.
4. **Offline strip** — implement as a global overlay, not per-screen.
5. **Today view hero card** — the situation card up top is the new pattern for the Today tab; existing content slides below it.
6. **Push notification rules** — apply the title/subtitle/quick-action rules to the existing notification dispatch paths.
7. **Haptics on every primary action** — sweep pass; nothing in the field should commit silently.

Everything else (per-tab sections, light mode, voice shortcuts, glove test) is post-tomorrow follow-up.
