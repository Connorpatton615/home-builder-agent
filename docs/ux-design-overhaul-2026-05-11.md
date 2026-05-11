# Chad iOS UX/UI Design Overhaul Plan

**Date:** 2026-05-11
**Author:** Patton AI design strategy pass
**Audience:** Connor Patton (iOS engineering of one); design language locks for the 8-week roadmap
**Anchors:**
- ADR-POS-001 (Construction-Turtle Positioning) — locks AI-native, no-portal, single-builder, iOS/iPad-only
- [competitor-research-2026-05-11.md](competitor-research-2026-05-11.md) — Buildertrend + 7 PM tools
- [competitor-research-photos-2026-05-11.md](competitor-research-photos-2026-05-11.md) — CompanyCam + 6 photo tools
- [ROADMAP-2026-05-11.md](ROADMAP-2026-05-11.md) — 8-week build sequence
- Existing design system: `/Users/connorpatton/Projects/patton-ai-ios/ios/PattonAIShell/PattonAIShell/Design/Tokens.swift`, `Primitives.swift`

---

## 1. TL;DR

The state of construction-app iOS design in May 2026 is **information-density posing as competence**. Buildertrend is a web app stuffed into a phone. CompanyCam is the only category leader that designed for the camera first — and even it is starting to bloat with feature creep. Every other incumbent (BuildBook, JobTread, Houzz Pro, Procore mobile) makes the same fundamental mistake: they treat the phone as a viewing port for a web product. **Chad's app wins by being the first construction app that is genuinely native to voice + camera + AI inference as the primary inputs, with screens designed for one-handed use on a job site in direct sunlight.**

**The 3 signature design moves Chad's app ships to leapfrog the category:**

1. **A camera-first "Capture" tab as the literal center of the tab bar.** Press it from anywhere → the viewfinder is already framed, GPS already knows the project, voice recorder armed. The first tap of the day is a photo, not a menu. CompanyCam admires this idea but doesn't execute it (their home is a project list); we put the camera where the New Tweet button lives on Twitter.
2. **The "Field Card" — a single AI-inferred summary tile that replaces the dashboard grid.** One scannable, voice-readable card per active project: "Whitfield, framing 70%, slab pour reschedule pending, $3,400 over on lumber. Chad — say 'yes' to text Manny." It's the morning brief made into a tappable, breathable widget. Every incumbent's dashboard looks like a tax form. Ours looks like one well-written sentence.
3. **The "AI shimmer" — a unified motion grammar for AI inference.** Whenever an agent has authored or modified a field on screen, that field carries a faint left-edge gradient pulse for 1.2s on first render, plus a subtle indigo dot in the trailing edge afterward. Tap it → "Chad inferred this from your 6:14 AM site log — change?" One micro-interaction tells the user where the AI did the work, makes it tappable, and never lies about what's auto vs. manual. Nobody else has a visual grammar for AI authorship; they all just show a robot icon.

These three are the design wedge. Everything else in this document supports them.

---

## 2. Comparative UI audit

### 2.1 Audit-at-a-glance table

| App | Tab pattern | Home screen | Signature interaction | Camera UX | Form heaviness | AI surface | Visual identity | Recent review on UI |
|---|---|---|---|---|---|---|---|---|
| **Buildertrend iOS** | 4-tab bottom + hamburger menu (60+ items) | Project list with thumbnails | Schedule day-pill swipe (decent) | Buried 4 taps deep, picker required | Very heavy — every field is required | Single "AI Updates" button in Reports menu | Default iOS, low-saturation orange brand, no signature motion | "More clicks than there needs to be" — [Capterra](https://www.capterra.com/p/70092/Buildertrend/reviews/) |
| **CompanyCam iOS** | 5-tab bottom; center tab is **giant camera FAB** | Project grid with cover photos | Camera FAB → opens viewfinder in <300ms | First-class; auto-tag, auto-bind on GPS, voice caption | Light; tags + caption are voice/optional | Sidekick AI bar at top of Walkthrough Note (only here) | Bold "CC" purple, large project cards, photo-as-hero | "Far superior to Drive/Dropbox" — [Capterra](https://www.capterra.com/p/171143/CompanyCam/reviews/) |
| **BuildBook iOS** | 4-tab bottom | Per-project feed (chat + activity) | Builder/client toggle in chat | Standard photo picker | Medium; clean form chrome | None visible in mobile | Warm beige + black, serif accents, photography-heavy | "Cleanest UI in the segment" — multiple [Capterra](https://www.capterra.com/p/190939/BuildBook/) |
| **CoConstruct iOS** | 5-tab + drawer | Project list, dated activity log | Selection-set drill-in (best in class historically) | Standard | Heavy | None — pre-AI era | Sunsetting; no recent visual updates | "Skeleton support" — Pro Remodeler 2024 |
| **JobTread iOS** | Mostly web wrapper | Pipeline kanban | Drag-drop pipeline | Standard | Very heavy; spreadsheet-style | None native | Default iOS, blue-gray, table-heavy | "QBO sync sings, UI clunky in places" — [G2](https://www.g2.com/products/jobtread/reviews) |
| **Houzz Pro iOS** | 5-tab bottom | Project list | Voice-driven daily log (real-time transcribe) | OK; daily-log context | Medium | Voice-to-log transcription bar — best in segment | Houzz green, photography-driven, magazine-feel | "Voice logs are the only reason I tolerate the app" — [Capterra](https://www.capterra.com/p/199689/Houzz-Pro/reviews/) |
| **JobNimbus iOS** | 5-tab bottom | Pipeline kanban | Kanban drag-drop | Standard | Medium | None | Default iOS + green | "Solid CRM, mediocre mobile" — [G2](https://www.g2.com/products/jobnimbus/reviews) |
| **Procore mobile** | 5-tab bottom; "More" drawer with 20+ items | Project picker → 20+ modules | Punchlist swipe-to-resolve | Standard | Punishing | "Procore Copilot" beta in chat sidebar | Enterprise gray, dense data tables, lots of chrome | "Enterprise UI on a phone is brutal" — App Store reviews |
| **Raken iOS** | 4-tab bottom | Today's report card | Voice-to-report 5-min flow | Photo strip auto-populates report | Light for daily reports | Voice-to-text only | Construction orange, hard-hat trade tone | "Best daily reports going" — [G2](https://www.g2.com/products/raken/reviews) |

### 2.2 Per-app teardowns worth lifting / avoiding

**Buildertrend iOS — the cautionary tale.** Bottom nav of 4 visible + a hamburger that hides 60+ items. App Store screenshots show six fields visible above the keyboard on a daily-log entry — they expect the field worker to scroll on a 6.1" screen. The schedule view crams a 30-day Gantt at 9pt type. Reviewer ([Capterra page 3](https://www.capterra.com/p/70092/Buildertrend/reviews/?page=3)): *"the product user experience is absolutely awful... 10x more clicking than there needs to be."* Specific anti-patterns to avoid: hamburger menu in 2026, forced-required form fields with no voice or AI defaults, modal-stacking three deep on schedule edits. The change-order flow alone is seven screens. One thing they actually nailed: the GPS auto-tag on daily logs ([Buildertrend mobile FAQ](https://buildertrend.com/help-article/mobile-app-faqs/)) — it's the right idea, just buried.

**CompanyCam iOS — the gold standard we admire and improve.** The single best decision in the category is the **center tab being a camera FAB**, not an icon — it visually breaks the tab row and is the tallest tappable target. Open the app cold → tap the camera tab → viewfinder is up in well under a second, with project pre-selected via GPS-address-match (close to a geofence but not quite). The grid view leads with **cover photos**, not text — every project tile is a recent photo, dated, with caption. The voice-caption flow ("hold the shutter, talk while you shoot") is the right inversion of every form-first competitor. **What to copy:** the center-tab camera FAB, the cover-photo-as-project-tile, voice-during-capture, the before/after ghost-overlay alignment ([help center article](https://help.companycam.com/en/articles/6828372-create-a-before-after-photo)). **What to improve:** Sidekick AI bar sits at the top of the Walkthrough screen and looks bolted-on — the AI is treated as a separate mode. Chad's app should weave AI into every screen, not gate it to a walkthrough. App Store complaint: *"every click out of a form field forces a 15-second edit-page reload"* — the perf is decaying as features stack. Lesson: keep the surface narrow.

**BuildBook iOS — the visual benchmark.** BuildBook's home screen is unique in the category: it's a **per-project feed**, not a project list. You're already inside the only project that matters; the activity stream of chat + photos + updates dominates. The visual identity is warm cream/black with photography-driven hero imagery and a quiet serif accent on titles — the only construction app that looks like a 2026 product. Reviewer: *"The UI is the only reason we picked it over Buildertrend."* **What to copy:** the "Today" tab pattern (one feed, your latest stuff first, not a dashboard grid); the photography-as-hero aesthetic; the calmness of empty states. **What to avoid:** their financial views are buried 4 taps deep, and the schedule is a clunky calendar. Modern UI is not enough if the operational depth isn't there — which is our opening.

**CoConstruct iOS — historical reference only.** The selection-set workflow was best in class for custom builders before Buildertrend bought and stagnated it. The visual identity is dated (2018-era iOS). The screen worth lifting in spirit: the "drop-dead today" sidebar that sorted selections by deadline, not by room. Procurement reality > organizational hierarchy.

**JobTread iOS — a web app wearing iOS clothing.** Mostly a wrapper. The pipeline kanban is the one screen that benefits from drag-drop touch, but it's identical to the desktop. Spreadsheet-style cost tracker on a phone is a usability crime. Nothing to copy. **What to take as a warning:** financial depth matters (their QBO sync is rated 4.6/5 — [G2](https://www.g2.com/products/jobtread/reviews)) but a financial app that's painful on mobile loses the field user. We solve this by *not* trying to do financial workflows on the phone — show cost summary, drive entries via voice + receipt agent.

**Houzz Pro iOS — voice-driven daily log is the only thing worth studying.** Their daily-log voice flow is the best implementation in the category ([Houzz Pro daily logs](https://pro.houzz.com/for-pros/software-construction-daily-logs)): tap mic → speak → live transcription appears with weather already attached → tap publish. One screen, three taps total. The whole rest of the app is design-buyer-oriented (lead gen, mood boards, magazine aesthetic) — wrong audience for a custom builder, but the voice log alone is what we ship in Week 4 at higher polish. Reviewer: *"the only reason I tolerate the app is the voice log."* The Achilles' heel: it's a one-feature wonder gated by an account-hostile 12-month renewal lock with 1.02/5 BBB.

**JobNimbus iOS — kanban CRM, wrong category.** Drag-drop pipeline is good UX for roofing sales pipelines, irrelevant for custom homebuilding execution. Note the trend: kanban is the New Spreadsheet in mobile construction software. We don't have a kanban use case (the wedge is execution, not pipeline) and shouldn't be tempted to invent one.

**Procore mobile — the "do not do enterprise density" example.** Every screen has 12+ tappable affordances. The Punchlist module alone has a top nav, filter bar, sort menu, multi-select toggle, plus the list — and that's before the items. The chat sidebar has a "Procore Copilot" beta that surfaces AI as a separate destination, not a participant. Procore is what happens when every internal stakeholder gets to keep their feature. **Anti-pattern lesson:** features compound complexity quadratically; pruning is the design.

**Raken iOS — the right-shaped daily report.** Daily report card on launch. Voice-to-text drives a 5-minute report. Photos auto-attach (you pick 4 highlights). Subs love it. The cost: it's mono-purpose — find a photo from three months ago and it's awful, because the photo viewer is secondary to the report flow. **What to copy in spirit:** the right *primary action* on launch is the highest-frequency task, not a dashboard.

### 2.3 Cross-category conversational AI inspiration

Outside construction, the apps that nail conversational AI on mobile are doing two things our category isn't:

- **Granola** (meeting AI) — the "everything goes into a single editable doc, AI annotates the margins" pattern. The AI is always *partially visible*, never the whole screen. The doc is yours; the annotations are advisory. **Lift:** the AI shimmer-in-margin pattern for our "AI authored this" indicator.
- **Limitless / Pi / ChatGPT iOS** — the bottom-anchored voice button that expands to a full-screen wave visualization. Voice is the primary input, keyboard is the fallback, not the other way around. **Lift:** our composer should default to a giant mic button with the keyboard one tap away (and we already partly do this — push to polish).
- **Replika** — too soft for our voice rubric, but their conversation memory chip ("Replika remembered you said...") is a good model for our "Chad inferred this from..." disclosure micro-interaction.

The borrowed lesson is unanimous: **on a small screen, AI should narrate quietly through micro-interactions, not dominate through chrome.**

---

## 3. Design language proposal for Chad's app

This section locks the design tokens. Everything later in the document references these. Where it touches the existing `Tokens.swift`/`Primitives.swift`, the proposal is evolutionary — adding or renaming, not rewriting.

### 3.1 Brand foundation

**Voice (already locked):** Chad-voice is direct, plain, no exclamation marks, no emoji, no industry jargon (no "stakeholder," no "leverage"). The morning brief sets the tone. Design copy follows the same rules.

**Color palette — evolution of the existing `Tokens.Color`.** The existing palette is right-shaped but reads as a generic SaaS dashboard. The proposed adjustment:

| Token | Today | Proposed | Reasoning |
|---|---|---|---|
| `background` | `.systemBackground` | Keep | Apple's adaptive default is correct. |
| `surface` | `.secondarySystemBackground` | Keep, but rename to `surfaceWarm` and tint +3% warm | Construction is dirty; a *slightly* warm surface (think kraft paper, not yellow) reads as physical. CompanyCam went cold; we go warm. |
| `surfaceElevated` | `.tertiarySystemBackground` | Keep | Nested cards stay cold-neutral for contrast. |
| `alertRed` | `0.85, 0.18, 0.20` light | Keep | Reads right. |
| `alertAmber` | `0.92, 0.55, 0.10` | Keep | Reads right. |
| `statusGreen` | `0.16, 0.55, 0.31` | Keep | Reads right. |
| `tintBlue` | `.accentColor` | Override to a specific indigo: `0.36, 0.42, 0.85` light / `0.55, 0.62, 0.95` dark | Indigo, not the generic iOS blue. Patton's accent should be its own. Used for interactive + AI-authorship indicators. |
| **NEW: `aiAccent`** | — | Same indigo, used only for AI-authored content | The single design token for the AI-reveal pattern. Strict rule: only AI-authored elements use this color. |
| **NEW: `signalGlow`** | — | Indigo at 0.18 opacity | The shimmer fill behind AI-authored fields. |
| `financeNeutral` | `0.40, 0.45, 0.55` | Keep | Reads neutral. |

**Typography — extend `Tokens.Typography` with one addition.** The current SF Pro stack is fine but feels generic. Proposed change: add a **`titleHero`** rounded weighted style for the Field Card and large narrative blocks. Keep everything else.

```swift
// Add to Tokens.Typography
static let titleHero: Font = .system(.title, design: .rounded, weight: .bold)
static let serif: Font = .system(.title3, design: .serif, weight: .regular)  // Used sparingly — section dividers on PDFs/walkthrough exports
```

The serif accent is a *page-marker only* — not body text. It signals "this is a printable artifact" in the walkthrough PDF and weekly client snapshot email. BuildBook uses serifs everywhere and it skews precious; we use them rarely and it skews custom.

**Motion language.** The unified design grammar:

- **Slow + organic for AI events** — when an agent writes a field, motion is 600–800ms with a soft `interactiveSpring(response: 0.55, dampingFraction: 0.85)`. Think "ink soaking in."
- **Snap for user input** — when the user taps, types, or swipes, animations are 180–250ms with `.snappy`. Think "physical paper."
- **Never linear timing.** All transitions go through SwiftUI's `.spring` family or `.smooth`. Linear is a code smell.

This split — slow-organic for AI, snappy for user — is itself a design moat. The user learns within a day which actions were them vs. which were Chad.

### 3.2 Tab bar / navigation

**Proposal: collapse 6 tabs to 5 with a center camera FAB, and demote Activity + Morning out of the tab bar.**

Current tabs: Dashboard · Morning · Projects · Ask · Activity · Settings (6 + Settings).

The 6-tab `TabView` is already cramped on smaller iPhones (the SE 3 will truncate "Activity" with system labels at default Dynamic Type). More importantly: Morning, Activity, and Dashboard are three flavors of "what's happening" — the user shouldn't have to choose between them.

**Proposed tab structure:**

| Position | Tab | Icon (SF Symbol) | What it is |
|---|---|---|---|
| 1 | **Today** | `sun.horizon.fill` | Replaces Dashboard + Morning. AI-curated daily field card + urgency feed + tap-into-brief. Default landing. |
| 2 | **Projects** | `square.stack.3d.up.fill` | Project list + drill-in (current behavior, evolved). |
| 3 | **Capture** (FAB) | `camera.fill` — visually larger, raised | Center tab. Tap from anywhere → camera viewfinder in <300ms, GPS-bound. |
| 4 | **Ask** | `bubble.left.fill` | Conversation surface (current behavior, refined). |
| 5 | **More** | `ellipsis.circle.fill` | Activity log + Settings + Profile, presented as a clean menu sheet — NOT a hamburger drawer. |

The 5-tab layout matches CompanyCam's structural choice (which is correct), but our middle tab is uniquely *contextual to the active project* — it auto-knows where you are. CompanyCam still asks you to confirm. We don't.

**Activity goes into More** because it's a debugging surface — useful, but not a daily tap. **Morning gets absorbed into Today** because morning brief content is what a great Today screen is. **Settings goes into More** because it's an infrequent destination.

The center Capture FAB is **visually larger** — 64×64pt with a recessed shadow ring, breaking the tab row's symmetry. Code-wise this is a `Tab` + a `ZStack` overlay (since SwiftUI's `TabView` doesn't natively support an enlarged center; we draw the camera button on top of the bar and tag-tap it). Reference pattern: [SwiftUI center-tab FAB on iOS 17](https://swiftuirecipes.com).

### 3.3 The camera-first hero (the Capture surface)

**The thesis:** the highest-value action a builder takes 10+ times per day is "shoot something and remember it later." Forcing them through 3+ taps to get to a viewfinder is the original sin of every competitor. Our Capture tab is the answer.

**Behavior on tap (from anywhere in the app):**
1. **<300ms** — viewfinder is live and framed.
2. **Top-left badge** — "Whitfield · auto-bound 2 min ago" (the active project from the geofence). Tap to override.
3. **Top-right** — voice-record toggle (off by default; one tap → live transcription appears under the shutter).
4. **Bottom-center** — large shutter button (72pt).
5. **Bottom-left** — gallery thumbnail (last photo in this project, tap to review).
6. **Bottom-right** — mode picker (Photo · Walkthrough · Before/After). Default = Photo.
7. **Long-press shutter** — starts voice recording AND captures the photo on release ("hold to narrate" — borrowed from CompanyCam Quick Captions, but voice goes through our own pipeline).
8. **GPS auto-bind override** — if the user is between projects (GPS hasn't locked yet), the project chip reads "Tap to choose" and is amber. If they tap the shutter without a project, we capture optimistically and resolve the project from GPS in the background within 30s.

**SwiftUI implementation notes:**
- Use `AVCaptureSession` directly via a `UIViewRepresentable` rather than `PHPickerViewController` — we need the bottom-corner badges and voice integration.
- The project chip is a `StatusBadge(label: "Whitfield · auto-bound", icon: "location.fill", urgency: .info)` from `Primitives.swift` — reuse, don't re-make.
- The voice recording uses `SFSpeechRecognizer` on-device (already in `AskView.swift`) — extract its push-to-talk logic into a `VoiceCaptureController` shared between Capture and Ask.
- For the long-press, `LongPressGesture(minimumDuration: 0.25).onChanged` arms recording; `.onEnded` fires the photo + finalizes the transcript.

### 3.4 The AI reveal pattern

**The problem:** every AI feature looks the same in every competitor — a robot icon, a "✨ AI" sparkle badge, a "Generated with AI" pill. It reads as either marketing flexing or as a disclaimer ("this might be wrong"). Neither is the right tone for a builder.

**Our pattern — the "ink soaking in" motif:**

- **First render of an AI-authored field:**
  1. The field background flashes `signalGlow` (indigo at 0.18 opacity) and animates back to surface color over 1.2s with a soft spring.
  2. The text content fades in left-to-right over 800ms — feels like ink drawing.
  3. A 3pt-wide indigo bar appears on the leading edge of the field for 1.5s, then fades to a permanent 1pt indigo dot in the trailing edge.
- **Persistent state (after first render):** the small indigo trailing dot — invisible-unless-you-look-for-it. Width: 4pt. Position: top-right of the field. Tappable: yes.
- **On tap of the dot:** a quiet sheet appears: "Chad inferred this from your 6:14 AM site log. **Change it** · **Keep it**." Sheet uses `presentationDetents([.medium])`.
- **Reduced motion:** the shimmer is replaced with an instant solid `signalGlow` background that fades over 400ms to surface. The trailing dot still appears.

**Why this is the right pattern:**
- It's *visible enough to notice if you're looking*, *invisible enough to ignore if you're not*. The user learns within a day that the indigo dot means "this is what Chad wrote."
- It's tappable. The provenance is one tap away, never demanded. ChatGPT's "regenerate" affordance is two taps; ours is one.
- It's specific. Telling the user "Chad inferred this from your 6:14 AM site log" is concrete trust-building. "AI-generated" is not.

**SwiftUI implementation:**

```swift
// Add to Primitives.swift
struct AIShimmerModifier: ViewModifier {
    @State private var hasShimmered = false
    let provenance: String  // e.g. "Chad inferred from 6:14 AM site log"

    func body(content: Content) -> some View {
        content
            .background(
                RoundedRectangle(cornerRadius: Tokens.Radius.small)
                    .fill(hasShimmered ? .clear : Tokens.Color.signalGlow)
                    .animation(.smooth(duration: 1.2), value: hasShimmered)
            )
            .overlay(alignment: .topTrailing) {
                Circle()
                    .fill(Tokens.Color.aiAccent)
                    .frame(width: 4, height: 4)
                    .padding(6)
                    .onTapGesture { /* present provenance sheet */ }
            }
            .onAppear { withAnimation { hasShimmered = true } }
    }
}

extension View {
    func aiAuthored(_ provenance: String) -> some View {
        modifier(AIShimmerModifier(provenance: provenance))
    }
}
```

Every AI-authored field — site log entry, change order draft, selection deadline, photo caption — gets `.aiAuthored("provenance string")` and the visual grammar is unified.

### 3.5 Empty states

Every empty state is a tone moment. The current "Nothing on the radar today" is fine but generic. Proposed direction:

- **Tone:** dry, observational, never cheerful. Chad-voice.
- **Illustration:** a single line-art SF Symbol at 64pt at 30% opacity. No mascots, no characters, no clip art. The construction category is full of cartoon hard-hats; we don't add to that pile.
- **Copy templates:**
  - Today, all clear: "Nothing red today. Whitfield is on pour." (icon: `sun.max`)
  - Projects, none yet: "Start by adding Whitfield. Talk it through with Ask." (icon: `square.dashed`)
  - Capture gallery empty: "No photos here yet. Tap the shutter." (icon: `camera`)
  - Ask history empty: "No conversations yet. Hold the mic and start." (icon: `waveform`)
- **No call-to-action buttons in empty states.** The icon and copy are it. We don't want to teach the user that empty states are interactive games.

### 3.6 Loading states

**Hard rule: anything > 300ms gets a skeleton. Spinners are banned outside of background processing indicators.**

`SkeletonRect` is already in `Primitives.swift` — extend with two more shapes:

```swift
// Add to Primitives.swift
struct SkeletonCard: View {
    var body: some View {
        VStack(alignment: .leading, spacing: Tokens.Spacing.m) {
            SkeletonRect(width: 140, height: 18)
            SkeletonRect(height: 14)
            SkeletonRect(width: 220, height: 14)
        }
        .padding(Tokens.Spacing.l)
        .background(RoundedRectangle(cornerRadius: Tokens.Radius.large).fill(Tokens.Color.surface))
    }
}

struct SkeletonPhoto: View {
    var body: some View {
        RoundedRectangle(cornerRadius: Tokens.Radius.medium)
            .fill(Tokens.Color.surfaceElevated)
            .aspectRatio(4/3, contentMode: .fit)
            .overlay( /* SkeletonRect shimmer overlay */ )
    }
}
```

**The shimmer pattern** is already correctly tuned in the existing `SkeletonRect` (1.4s linear repeat, 60% gradient width). Don't change it. Just propagate.

### 3.7 Form design — the rebellion

The category default is: every project has 30+ fields, all required, no defaults, type everything. Chad's app rejects this entirely.

**Three rules for any form in the app:**

1. **Voice is the first input.** Every form has a mic button in the top-right of the navigation bar. Hold to dictate the entire form contents; the AI parses and pre-fills every field.
2. **Smart defaults from project context.** A new selection on Whitfield knows the project, the current phase, today's date, the homeowner. The user fills 1–2 fields, not 8.
3. **Structured suggestions inline, not behind dropdowns.** A vendor field shows the 3 most-recently-used vendors for this project as tappable chips below the field. No dropdown, no picker sheet.

**Concrete example — Add Selection form:**

- Before (theoretical Buildertrend-style): Room (picker), Item (text), Vendor (picker), Cost estimate (text), Deadline (date picker), Status (segmented), Notes (long text) — 7 fields, all required.
- After (Chad's app): One text field "What needs to be picked?" + mic button. User says: "Master bath tile from Lowe's, $1,200, due August 15." All seven fields fill via the agent. User taps confirm or edits the one that's wrong.

**SwiftUI implementation note:** the existing `AskView.swift` push-to-talk pipeline is the right primitive. Extract it into a shared `VoiceFormController` that any form view binds to. The form's `@State` becomes the agent's output target.

### 3.8 Photo workflows

**Capture** is covered in 3.3. Here's what happens after.

- **Browse:** project-scoped photo grid with cover photos as project tiles (CompanyCam-style). Tap a project → photo grid. Tap a photo → full screen with caption, voice note playback, GPS, AI tags, and edit affordances.
- **Annotate (iPhone):** PencilKit canvas on tap, finger-only. Three colors (alertRed, alertAmber, indigo). One undo. Save annotates over the original; the original is kept losslessly.
- **Annotate (iPad):** PencilKit canvas with Pencil pressure sensitivity. Full color palette. Eraser tool.
- **Share:** tap share → choose Photo, Photo + Caption (Markdown PDF), or Before/After (slide-to-reveal HTML link).
- **Auto-bind to today's site log:** photos taken inside an active geofence on the current day silently attach to today's site log (Week 1 deliverable per roadmap). User can see this in the site log itself; no per-photo confirmation needed.

**Before/after — the ghost overlay pattern:**
- When the camera detects a previous photo within 10m GPS + same room tag, the previous photo renders as a 30% opacity overlay in the viewfinder.
- A toast appears: "Align with last week's master bath shot."
- Capture → automatic pairing → before/after card.
- View → slide-to-reveal divider (a `DragGesture` on a `Rectangle` with mask).

**Export — the closeout package:**
- One button in project menu: "Export project photos as PDF."
- Generates a styled PDF with cover, table of contents, chronological grid with captions, before/after section.
- Same template as the Walkthrough PDF (consistency = product feel).

### 3.9 iPad-specific design

**The iPad is the demo machine.** Chad will use the iPad in homeowner meetings and during plan reviews. This is the device that sells future builder clients.

**Proposed layout:**
- `NavigationSplitView` with sidebar (project list) + primary (project detail) + secondary (photo/document viewer).
- Sidebar: project list with cover photos, fixed 280pt width.
- Primary: project detail (Today card, milestones, selections, recent photos).
- Secondary: dynamic — defaults to the most-recent photo, but switches to a Walkthrough PDF, plan PDF, or homeowner email draft based on context.

**The iPad-only signature screen — "The Plan Review."**
- Floor plan PDF in primary; recent photos in a side rail.
- Tap a photo → it animates onto the plan at the GPS-inferred location (or wherever Chad drags it).
- Pencil mode: annotate the plan, annotate the photo, pin photo-to-plan with a hand-drawn line.
- Save → both the plan with annotations AND the photo with annotations sync to Drive.

This is the screen Chad shows in homeowner meetings ("here's where we are, here's the issue, here's what I'm recommending") that no competitor has. Fieldwire does plan-pinning on a desktop UI; CompanyCam has no plans; Buildertrend has no Pencil. This is open territory.

**SwiftUI implementation:**
- Detect iPad via `UIDevice.current.userInterfaceIdiom == .pad` at app launch; route through a separate `RootSplitView.swift`.
- PencilKit's `PKCanvasView` handles annotation; persist `PKDrawing` as JSON alongside the image.
- For pin lines: a SwiftUI `Path` overlay between photo bounds and a tap point on the plan, drawn from stored coordinates.

### 3.10 Onboarding

**Current state (per `RootTabView.swift`):** first launch routes to Ask tab; a 10-question interview builds the profile.

**Proposed evolution:** keep the interview, change the framing.

- **First screen:** a single voice prompt. "Hey, I'm Chad. Talk to me about your business for 60 seconds — name, projects, what you're building. I'll set up from that." A giant mic button. Skip → fallback to text.
- **After the 60s monologue:** the agent extracts: builder name, business name, project names, project addresses, current phases, homeowner names. Confirmation screen shows what it understood as a single-page summary. Edit-in-place; no per-field re-prompt.
- **Then** the existing 10 questions, but compressed: anything answered from the monologue is auto-filled and the user just confirms.

The thesis: the existing 10-question interview is good, but the *first impression* should be the AI doing real work, not asking a series of questions. The monologue → structured profile is the demo that sells the app in the first 90 seconds.

**SwiftUI implementation:**
- The monologue uses the same `VoiceCaptureController` as the Capture and Ask flows.
- Backend route: `POST /v1/onboarding/extract` takes the transcript and returns a structured profile JSON.
- The confirmation screen renders each field with `.aiAuthored("from your intro")` — the AI reveal pattern in action from minute one.

### 3.11 Settings / Profile

Settings should feel like a quiet utility room. Currently fine; proposed refinements:

- **One-screen settings, no nested menus.** A scrolling list with sections: Profile, Connected accounts, Notifications, Voice & speech, Privacy, Sign out, About.
- **Profile section** is a clean photo + name + business at the top. Tap to edit. No avatar uploads; system contact photo is enough.
- **About** shows app version + a small "v1.0 (build 47)" at the bottom — Apple's 5.1.1 requirement satisfied minimally.
- **No marketing copy.** No "Made with care" footers. Chad doesn't care.

### 3.12 Accessibility

**Not optional.** Construction is a physical industry; many users will have hand injuries, sun glare, or hearing protection on. Proposed minima:

- **VoiceOver labels** on every interactive element. Provenance dots ("AI authored") get a label like "AI-authored field. Double-tap to see source."
- **Dynamic Type support** at every screen up to XXL (XL is too easy a ceiling). The existing typography stack uses system styles, which is correct — make sure the Field Card and Capture tab badges scale.
- **Color contrast:** the indigo `aiAccent` on `surface` is 4.7:1 in light mode and 6.2:1 in dark. The `alertAmber` on light surface is borderline 4.0:1 — add an outline or icon to differentiate, never rely on color alone.
- **Reduce motion fallback:** every animation has a reduced-motion equivalent. The AI shimmer becomes a static `signalGlow` fade. The hero animations become cross-fades.
- **Hit target minimum:** 44×44pt for any tappable element. The provenance dot is visually 4pt but its tap target is 32×32pt (invisible padding).

---

## 4. The 6-screen design overhaul plan

Sequenced to the 8-week roadmap so each redesign ships with the feature week it enables. Effort is sizing in S/M/L.

### 4.1 Camera-first Quick Capture — Week 1 + Week 6 + Week 8

**Roadmap weeks:** 1 (GPS auto-bind), 6 (before/after ghost overlay), 8 (AI captions). The Capture surface is the single biggest design redesign because it's the new center of gravity.

**Current state:** there is no Capture tab. Photos enter through the Ask composer's attachment picker (PhotosUI), max 3 per turn, 5MB each. Manual project selection. No camera-first surface.

**Pain points:**
- No way to capture without going into Ask first — 4 taps to a photo.
- No GPS auto-bind anywhere.
- No camera-first viewfinder; uses `PHPickerViewController`.
- No voice during capture.
- HIG: the camera should be a direct affordance on a field-app's tab bar (per CompanyCam's category-leading pattern).

**Proposed redesign:**
- **New center tab** with enlarged camera FAB icon (raised, 64pt).
- **Viewfinder layout:**
  - Full-bleed preview.
  - Top-left chip: project badge with `auto-bound 2 min ago` text, tappable to override (uses `StatusBadge` with custom auto-bind variant).
  - Top-right: voice toggle (mic icon, glows indigo when armed).
  - Bottom-left: gallery thumbnail (last photo).
  - Bottom-center: shutter (72pt circle, white ring, haptic on tap).
  - Bottom-right: mode picker (Photo · Walkthrough · Before/After).
- **Long-press shutter** → start voice recording, end on release with photo capture.
- **Before/After mode** detects nearby past photo via GPS+room tag and overlays at 30% opacity.
- **After capture:** quick caption sheet auto-appears with the voice transcript (if any), AI-suggested tags (Week 8 caption agent fills in), and a one-tap "save" button. Auto-dismisses after 2s of inaction.

**Why this leapfrogs the competition:**
- Buildertrend buries photo capture 3 taps deep behind a daily log entry.
- CompanyCam matches our camera-first thesis but their project bind is GPS-address-match (slower, less reliable than CoreLocation geofencing).
- Houzz Pro's voice daily log is the only adjacent peer; we one-up by combining voice + photo in a single gesture.

**SwiftUI implementation notes:**
- `AVCaptureSession` in a `UIViewRepresentable` named `CameraViewfinder`.
- `LocationManager` already on the `feat/gps-geofence` branch per Week 1 plan.
- `VoiceCaptureController` extracted from `AskView.swift`'s push-to-talk logic.
- Quick caption sheet uses `presentationDetents([.fraction(0.4), .medium])`.
- For the before/after overlay: `.overlay(Image(uiImage: previousPhoto).opacity(0.30).blendMode(.overlay))` on the camera preview.

**Effort:** **L** (this is the biggest screen, but it accumulates value across Weeks 1, 6, 8).

---

### 4.2 Today — Week 1 (replaces Dashboard + Morning)

**Roadmap week:** 1 (the redesign happens alongside the GPS auto-bind work).

**Current state:** Dashboard tab (`DashboardView.swift`) renders KPI scorecard (4 metrics in 2x2 grid), financial pulse card, overnight digest banner, urgency feed. Morning tab is a separate destination for the brief. Two tabs for "what's happening" is one too many.

**Pain points:**
- Dashboard reads as a tax form (4 KPIs at once). The user has to *process* it.
- Morning brief is a *different tab* — the user has to know to go there for the morning context.
- No single scannable "what matters today" tile.
- KPI grid is visually similar to the JobTread/Buildertrend dashboard pattern — we're matching the wrong reference.

**Proposed redesign — "The Field Card":**

The screen now consists of three stacked elements:

1. **Field Card (hero):** a single full-width card the height of the KPI grid (~280pt). Inside:
   - Top: project name + status chip ("Whitfield · framing").
   - Middle: 2–3 sentences in `titleHero` font (rounded bold 24pt) summarizing the day's state in Chad-voice. *"Pour delayed to Wednesday — Manny rebooked. Selections drop-dead in 3 days. $3,400 over on lumber. Tap to text Manny."*
   - Each piece of inferred content gets `.aiAuthored()` — the user sees the provenance dots and learns the card is alive.
   - Tap → expands into the full morning brief (the existing brief content, rendered as a scroll).
2. **Today's signals (3-row max):** below the Field Card, an at-most-3-item list of the highest-urgency feed items. Each row is a `Card` (existing primitive) with `StatusBadge` urgency chips. Tap → drill into the relevant project tab.
3. **Quick captures (carousel):** today's photos taken on-site, as a horizontal-scrolling row of 80×80pt thumbnails. Tap → photo viewer. Empty state: "No photos today yet."

The KPI grid moves to a "Numbers" disclosure inside the Field Card, accessible via a small chevron. By default, the user sees the narrative, not the numbers.

**Why this leapfrogs the competition:**
- No incumbent shows a *narrative* dashboard. They all show grids of numbers. Chad's app shows the *prose* of the day.
- The Field Card is the morning brief, made tappable. It's the single most-photographable screen in the app.
- The 3-signal max enforces opinionation. Buildertrend shows everything; we show what matters.

**SwiftUI implementation notes:**
- Replace `DashboardView.swift`'s top stack with a `FieldCard` view.
- `FieldCard` body: pulls from a new `/v1/turtles/home-builder/views/field-card` endpoint that returns a single Markdown-formatted narrative with inline AI-provenance markers (`<ai source="...">...</ai>`).
- Renders via a custom Markdown parser that maps `<ai>` tags to `.aiAuthored()` modifiers.
- Tap-to-expand uses a `matchedGeometryEffect` to animate the Field Card into the full brief view.

**Effort:** **M** (the visual rebuild is medium; the backend endpoint is small since the morning brief synthesizer already exists).

---

### 4.3 Project Detail — Week 3

**Roadmap week:** 3 (Selections module ships, needs the home for it).

**Current state:** `ProjectsView.swift` shows a list with an Active section + Others section. Tapping a project goes into `MasterScheduleView.swift` which is a stub.

**Pain points:**
- Project detail is currently a checklist of phases — useful but flat.
- No selection drop-dead view inside a project.
- No per-project photo grid surfaced.
- No per-project cost summary.
- Reads like a v0.5 project page.

**Proposed redesign — the per-project Today:**

Treat a Project Detail screen as a focused version of the Today tab, scoped to one project. Four stacked sections:

1. **Project header card.** Cover photo (last 7-day photo), project name, phase chip, address, homeowner name. Two buttons: "Capture" (deep-link to Capture with this project pre-bound), "Walkthrough" (Week 4 walkthrough recorder).
2. **Phase progress strip.** A horizontal capsule split into segments for each phase, current phase highlighted indigo. Tap a segment → see phase checklist (reuses `ChecklistAuthoringView`).
3. **Selections (Week 3).** Compact list, drop-dead-sorted. Each row: room · item · vendor · `due in 3 days` chip (color-coded by urgency). Tap → quick edit sheet with voice mic.
4. **Recent photos.** Horizontal carousel, 5 visible, tap-to-expand.
5. **Cost pulse (compact).** A single horizontal progress bar showing total spend vs. budget, with the same `Tokens.Color.financeNeutral` / `alertAmber` / `alertRed` color logic from the Dashboard's financial pulse.

**Why this leapfrogs the competition:**
- Buildertrend's per-project view is a vertical menu of 12+ modules; users have to drill 2–3 taps deeper for anything useful.
- BuildBook's project feed is closer to our model (activity stream), but lacks the structured drop-dead view.
- We fold the Selections module (the biggest custom-builder need) directly into the project page, not behind a menu.

**SwiftUI implementation notes:**
- `ProjectDetailView` replaces the current navigation target of project taps.
- Each section is a `Card` (existing primitive) for consistency.
- Phase strip is a `HStack` of `RoundedRectangle` segments; current phase uses `Tokens.Color.tintBlue` (the new indigo).
- Selections list is a `LazyVStack` (no list chrome) so spacing matches the rest of the page.
- The new `aiAuthored()` modifier wraps any auto-inferred field (e.g. "Chad inferred you're in framing from the 5/10 site log.").

**Effort:** **M** (the visual layout is medium; the data is already mostly available).

---

### 4.4 Ask — Refinement (every week, but lock by Week 4)

**Roadmap week:** 4 (voice walkthrough → PDF is the headline; the Ask composer's voice flow is the foundation).

**Current state:** `AskView.swift` is the most-mature view in the app — streaming, push-to-talk, TTS, image staging, citations, regenerate. It works. The question is what makes it *best-in-class conversational UI*.

**Pain points (existing reviews of category leaders' AI):**
- ChatGPT iOS: voice mode is great but transcripts are not editable mid-composition.
- Pi: gorgeous voice mode, but no transcripts, no citations.
- Granola: doc-style annotations work great for meetings but not for chat.
- Construction-specific: no peer has a chat surface, period. We're the only category competitor with one.

**Proposed redesign — three targeted polishes, no rebuild:**

1. **Bottom-anchored voice button by default.** The keyboard is one tap away (currently the keyboard is the default). The mic should be the dominant input affordance, 56×56pt, centered above the message list bottom edge. Tapping the keyboard icon swaps to text input.
2. **Inline AI-authored markers in responses.** When Chad's response includes a fact pulled from a tool (e.g. "the site log shows..."), wrap that segment in `.aiAuthored("tool: site_log_get / row 47")` — the user can tap to see exactly which row Chad pulled from. The existing `citations` array already carries this; we just need to render them inline-tappable instead of as a footer row.
3. **Suggestion chips above the composer.** When the previous turn has no follow-up suggestions, show 3 contextual chips based on conversation state and active project. E.g. "What's the selection status?" / "Draft a CO for the granite upgrade." / "Show me yesterday's photos." Tap a chip → fills the composer, doesn't auto-send. The chips themselves are AI-generated — gate this on the existing `/ask/suggestions` route once HB ships it.

**Why this leapfrogs the competition:**
- ChatGPT iOS doesn't have construction context, so suggestion chips can't be project-specific.
- BuildBook's chat is human-only; no AI.
- We're the only construction app with chat — quality bar is "as good as ChatGPT iOS but contextual." These three polishes get us there.

**SwiftUI implementation notes:**
- Reorder the composer subview hierarchy: voice button is centered, keyboard toggle is right-aligned.
- For inline AI-authored markers: extend the existing `MarkdownText` renderer to parse a custom `<cite>` element from the streamed response, wrapping with `.aiAuthored()`.
- Suggestion chips are a `LazyHStack` of `StatusBadge`-like pills, scrollable horizontally.

**Effort:** **S** (refinement, not rebuild).

---

### 4.5 Walkthrough Recorder — Week 4

**Roadmap week:** 4 (voice walkthrough → AI PDF report — CompanyCam Sidekick parity).

**Current state:** doesn't exist as a dedicated screen. Site logs are voice-driven through the Ask composer.

**Pain points:**
- No first-class voice-narrated walkthrough surface.
- Current voice site logs go through Ask, which has a chat UI — wrong shape for a 5-minute hands-free narration session.
- CompanyCam's Walkthrough Note is the feature to study; their UI is decent (a recording timer, photo strip below, tap-to-stop) but not great (chrome-heavy, no voice level meter).

**Proposed redesign — the recorder is a Capture mode, not a separate destination:**

The user enters Walkthrough mode from the Capture tab's mode picker. Once entered:
- Full-bleed viewfinder (camera live).
- Top-center: a giant pulsing indigo dot + elapsed time ("Recording · 02:14").
- Live waveform centered at the bottom, indigo strokes.
- Live transcript scrolling above the waveform — text appears as Chad speaks.
- Shutter button still works — tap to add a photo to the walkthrough; long-press to pause.
- Top-right: "Stop & generate PDF" button.

After Stop:
- Loading state with a `SkeletonCard` of the walkthrough report (Chad sees what's coming).
- 60–90s later, the PDF preview slides up. Preview is in-app via PDFKit.
- One button: "Email to me + homeowner" (homeowner is project context).

**Why this leapfrogs the competition:**
- CompanyCam Sidekick is the feature we're matching, but their walkthrough recorder hides voice level — Chad can't tell if his voice is picking up. Ours shows a live waveform.
- Houzz Pro voice daily log is the closest peer, but it's daily-log only (no photos in the same flow). Ours combines voice + photos as a single 5-minute artifact.
- The PDF output is what the homeowner sees — and it's the artifact that demos at sales meetings.

**SwiftUI implementation notes:**
- New `WalkthroughRecorderView` in the Capture flow.
- Uses the same `AVCaptureSession` as the photo Capture, but with `AVAudioRecorder` running in parallel.
- Live transcript via on-device `SFSpeechRecognizer` (already in use in AskView).
- Waveform is a `Canvas` view drawing the audio level samples — animates at 30fps.
- After stop: POST to `/v1/turtles/home-builder/walkthrough/generate` with audio + photo URLs + project context.
- PDF preview via `PDFKit`'s `PDFView`.

**Effort:** **M** (Capture surface is reused; recording + PDF preview is new).

---

### 4.6 iPad Split-View + Pencil — Week 7

**Roadmap week:** 7.

**Current state:** the app runs on iPad as an iPhone-shaped binary at iPad scale. No split view, no Pencil support.

**Pain points:**
- Buildertrend, CompanyCam, BuildBook all have iPad versions that are "iPhone but bigger" — none of them use the iPad's split-view + Pencil affordances.
- For luxury custom homes, iPad is the device used in homeowner meetings — the iPhone-like layout wastes the screen.

**Proposed redesign — see section 3.9.** The implementation specifics:

- Root view detects `userInterfaceIdiom == .pad` at launch → routes to `RootSplitView`.
- `NavigationSplitView` with three columns: sidebar (project list), primary (project detail OR Today), secondary (photo / PDF / walkthrough).
- Sidebar always-visible at 280pt.
- Pencil support on the secondary column: PencilKit `PKCanvasView` overlay activated by a "Annotate" toolbar button.
- The signature "Plan Review" screen is a special secondary-column mode: floor plan PDF in PDFKit, photo strip in a side panel, drag-and-drop to pin photos onto plan coordinates.

**Why this leapfrogs the competition:**
- Fieldwire is the only construction app with serious plan-pinning on iPad — but it's commercial, expensive, and not custom-home-shaped.
- CompanyCam has no iPad-specific layout.
- Buildertrend on iPad is a stretched iPhone.

We become the only custom-home iPad-native experience. This sells future builder clients more than any feature.

**SwiftUI implementation notes:**
- `RootSplitView` is a thin top-level shell; the existing `Today`, `Projects`, `Capture`, `Ask`, `More` views are reused as content in the columns.
- `PKCanvasView` wrapped in a `UIViewRepresentable`.
- Pin-to-plan uses a `GeometryReader` to map drag positions to PDF coordinates; persists as JSON alongside the plan.

**Effort:** **M** (split view is straightforward; Pencil + plan-pinning is the M).

---

## 5. Motion + haptics spec

The motion grammar is two-tier (covered in 3.1). Specifics:

### 5.1 Standard transitions

| Transition | Spec | When |
|---|---|---|
| Tab switch | None (default) | Switching tabs |
| Push (navigation) | `.smooth(duration: 0.3)` | Drill into a project, photo, etc. |
| Present (sheet) | `.spring(response: 0.4, dampingFraction: 0.85)` | Quick-edit sheets, provenance disclosure |
| Dismiss | Same | Sheet close |
| Field Card → brief expand | `matchedGeometryEffect` over 0.55s | Today tap-to-expand |

### 5.2 Hero animations

| Animation | Spec | Where |
|---|---|---|
| Camera capture | Shutter scale 1.0 → 0.92 over 0.08s, back to 1.0 over 0.12s, photo thumbnail slides up from bottom-left | Capture shutter tap |
| Photo expand to full | `matchedGeometryEffect` from grid thumbnail to full-bleed, 0.45s | Photo grid tap |
| Project switch | Cover-photo cross-dissolve 0.35s, then header text settles via spring | Today's project switcher |
| AI shimmer (first render of AI field) | `signalGlow` background fade 1.2s + text fade-in 0.8s + 3pt leading bar 1.5s, settling to 4pt trailing dot | Every `.aiAuthored()` first render |
| Field Card expand | Hero card scales to full-screen via `matchedGeometryEffect`, 0.55s spring | Today's Field Card tap |

### 5.3 Haptic patterns

| Haptic | Type | When |
|---|---|---|
| Shutter | `UIImpactFeedbackGenerator(style: .medium).impactOccurred()` | Photo capture |
| AI inference complete | `UINotificationFeedbackGenerator().notificationOccurred(.success)` | Walkthrough PDF ready, brief regenerated |
| Geofence enter (background) | Soft `.light` impact when app foregrounds and sees a fresh geofence-enter event | First time user opens app after entering a job site |
| Error / budget exceeded | `.error` notification | Daily budget hit, payment failed, sync error |
| Selection deadline triggered (push notification) | OS default (no custom haptic) | Drop-dead alert fires |
| Long-press shutter (voice arm) | `.soft` impact at the 250ms threshold | Voice recording starts |

Centralize all haptics in a `Haptics.swift` utility — never sprinkle `UIImpactFeedbackGenerator()` inline. Add this to `Design/`:

```swift
// Design/Haptics.swift
enum Haptics {
    static func capture() { UIImpactFeedbackGenerator(style: .medium).impactOccurred() }
    static func aiComplete() { UINotificationFeedbackGenerator().notificationOccurred(.success) }
    static func error() { UINotificationFeedbackGenerator().notificationOccurred(.error) }
    static func voiceArm() { UIImpactFeedbackGenerator(style: .soft).impactOccurred() }
    static func geofenceEnter() { UIImpactFeedbackGenerator(style: .light).impactOccurred() }
}
```

### 5.4 Reduce-motion fallbacks

`@Environment(\.accessibilityReduceMotion) var reduceMotion`. When `true`:
- AI shimmer → solid `signalGlow` fade 0.4s, no text-fade-in, no edge bar (but still the trailing dot).
- Hero animations → cross-fades 0.25s.
- Spring animations → linear 0.2s.
- Shutter scale → no scale, just haptic.

---

## 6. App Store positioning

### 6.1 App name + subtitle

- **App name:** "Patton AI for Builders" — leaves room for future turtles (RPM Filter, etc.) under the same brand
- **Alternate (Chad-specific):** "Chad — for Palmetto" if we ship a single-builder white-label binary later. For v1 TestFlight, "Patton AI for Builders" with subtitle.
- **Subtitle (30 chars):** "AI co-pilot for custom homes" or "Voice-first job site command"

Avoid: anything with "manager," "tracker," "tool," or "platform." Those map to category-default search where we lose to Buildertrend on ASO.

### 6.2 Screenshots (6, in order)

The screenshot order is the sales narrative. Six screenshots, captioned in Chad-voice:

1. **The Field Card** — full Today view with the Field Card hero, AI provenance dots visible. Caption: *"What you need to know, in one sentence."*
2. **Capture viewfinder** — viewfinder with project chip "Whitfield · auto-bound," voice toggle on, gallery thumbnail. Caption: *"Open the camera. It already knows where you are."*
3. **Walkthrough recorder mid-recording** — viewfinder with live transcript scrolling, waveform pulsing. Caption: *"Walk and talk. We'll write the report."*
4. **Walkthrough PDF preview** — the generated PDF in PDFKit preview, styled, with photos. Caption: *"60 seconds. Homeowner-ready."*
5. **iPad Plan Review** — iPad screenshot, split view, plan + photo pin lines + Pencil annotations. Caption: *"iPad-first. Pencil-ready."*
6. **Ask conversation** — Ask view with a typical exchange: "What's the framing status?" → response with AI provenance markers. Caption: *"Ask anything. Get answers from your own data."*

The category default is six screenshots of dashboards full of charts and modules. Ours is six photographs of a builder doing the work. The contrast is the differentiator.

### 6.3 Category positioning

- **Primary category:** Business
- **Secondary category:** Productivity

Buildertrend and CompanyCam both pick Business. BuildBook picks Business. The category is settled — picking anything else (e.g. Utilities, Photography) is a search-traffic loss.

### 6.4 What competitor listings look like

- **Buildertrend** ([App Store](https://apps.apple.com/us/app/buildertrend/id504370616)): 10 screenshots, all dashboards + grids. Subtitle: "Construction Management" — generic. Description leads with feature bullets.
- **CompanyCam** ([App Store](https://apps.apple.com/us/app/id960043499)): 8 screenshots, photo grids + camera. Subtitle: "Job photos for contractors" — clear. Strong category positioning.
- **BuildBook**: 7 screenshots, calm photography aesthetic, chat-first.

**How ours should differ:**
- Lead with the *prose* of the Field Card, not a chart. Nobody else does this.
- Show the camera in the second screenshot (not the third), establish camera-first.
- Include the iPad screenshot — Buildertrend doesn't bother; ours flexes the split-view + Pencil.
- Subtitle includes "AI co-pilot" — specific category language for AI-first builders we want to attract; rejects builders who want a traditional PM app (good, ADR-POS-001 self-filter).

---

## 7. Sources & inspiration board

### Construction iOS apps studied

- [Buildertrend iOS](https://apps.apple.com/us/app/buildertrend/id504370616) — App Store screenshots, reviews (recent 2025–2026 filter)
- [CompanyCam iOS](https://apps.apple.com/us/app/id960043499) — App Store screenshots, reviews, [features page](https://companycam.com/features), [Sidekick AI](https://companycam.com/ai-features), [Before/After help](https://help.companycam.com/en/articles/6828372-create-a-before-after-photo)
- [BuildBook](https://apps.apple.com/us/app/buildbook/id1495149793) — App Store + [Capterra reviews](https://www.capterra.com/p/190939/BuildBook/)
- CoConstruct iOS — App Store
- [JobTread iOS](https://apps.apple.com/us/app/jobtread/id1610015977) — App Store
- [Houzz Pro iOS](https://apps.apple.com/us/app/houzz-pro/id441571844) — App Store + [Daily Logs page](https://pro.houzz.com/for-pros/software-construction-daily-logs)
- [JobNimbus iOS](https://apps.apple.com/us/app/jobnimbus-crm/id620327465) — App Store
- [Procore iOS](https://apps.apple.com/us/app/procore/id492373243) — App Store
- [Raken iOS](https://apps.apple.com/us/app/raken-daily-reports/id797609975) — App Store + [features](https://www.rakenapp.com/features/daily-reports)

### Reviewer quote sources

- [Buildertrend on Capterra](https://www.capterra.com/p/70092/Buildertrend/reviews/) — "10x more clicking than there needs to be"
- [Buildertrend on G2](https://www.g2.com/products/buildertrend/reviews)
- [CompanyCam on Capterra](https://www.capterra.com/p/171143/CompanyCam/reviews/) — "Far superior to Google Drive and Dropbox"
- [CompanyCam on G2](https://www.g2.com/products/companycam/reviews)
- [Houzz Pro on Capterra](https://www.capterra.com/p/199689/Houzz-Pro/reviews/)
- [BuildBook on G2](https://www.g2.com/products/buildbook/reviews)
- [JobTread on G2](https://www.g2.com/products/jobtread/reviews) — "QBO sync sings, UI clunky"
- [Jibble's Buildertrend review 2025](https://www.jibble.io/construction-software-reviews/buildertrend-review)

### Cross-category AI/UX references

- Granola — meeting-AI app, inspiration for in-margin AI annotations
- Limitless — voice-first interface model
- Pi (by Inflection) — voice mode as primary input
- ChatGPT iOS — bottom-anchored voice button reference
- Replika — conversational memory disclosure patterns
- [Mobbin — Camera & Photography Patterns](https://mobbin.com/) — viewfinder + capture flows across categories

### iOS HIG + technical references

- [Apple Human Interface Guidelines — iOS](https://developer.apple.com/design/human-interface-guidelines/)
- [Apple HIG — Tab Bars](https://developer.apple.com/design/human-interface-guidelines/tab-bars)
- [Apple HIG — Accessibility](https://developer.apple.com/design/human-interface-guidelines/accessibility)
- [PencilKit framework reference](https://developer.apple.com/documentation/pencilkit)
- [CoreLocation region monitoring](https://developer.apple.com/documentation/corelocation/monitoring-the-user-s-proximity-to-geographic-regions)
- [AVCaptureSession reference](https://developer.apple.com/documentation/avfoundation/avcapturesession)
- [SwiftUI NavigationSplitView](https://developer.apple.com/documentation/swiftui/navigationsplitview)

### Designer tear-downs (recent reference)

- iOS app tear-downs on Page Flows (search "construction" + "field service")
- App Store editorial collections for AI-native productivity apps (May 2026)

### Internal references

- `/Users/connorpatton/Projects/patton-ai-ios/ios/PattonAIShell/PattonAIShell/Design/Tokens.swift`
- `/Users/connorpatton/Projects/patton-ai-ios/ios/PattonAIShell/PattonAIShell/Design/Primitives.swift`
- `/Users/connorpatton/Projects/patton-ai-ios/ios/PattonAIShell/PattonAIShell/App/RootTabView.swift`
- `/Users/connorpatton/Projects/patton-ai-ios/ios/PattonAIShell/PattonAIShell/Tabs/Ask/AskView.swift`
- `/Users/connorpatton/Projects/patton-ai-ios/ios/PattonAIShell/PattonAIShell/Tabs/Dashboard/DashboardView.swift`
- `/Users/connorpatton/Projects/patton-ai-ios/ios/PattonAIShell/PattonAIShell/Tabs/Projects/ProjectsView.swift`
- `/Users/connorpatton/Projects/home-builder-agent/docs/competitor-research-2026-05-11.md`
- `/Users/connorpatton/Projects/home-builder-agent/docs/competitor-research-photos-2026-05-11.md`
- `/Users/connorpatton/Projects/home-builder-agent/docs/ROADMAP-2026-05-11.md`
- `/Users/connorpatton/Projects/patton-os/data/decisions.md` (ADR-POS-001)
