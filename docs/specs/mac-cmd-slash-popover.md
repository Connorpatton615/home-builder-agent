# Mac Cmd+/ Chat Pop-over

> One-line summary: the global pop-over that brings `hb-chad` to every surface of the Mac shell. Not a chat tab — a keyboard-summoned overlay that inherits the active surface's project context, persists conversations per-project, and is the difference between "Mac app" and "Patton AI on a desk."

**Status:** Spec — slots into Phase 4 (Build) of [`desktop-renderer.md`](desktop-renderer.md), step 11 (polish). Small enough to ship before the V2 surfaces.
**Phase:** Active — depends on `hb-chad` (SHIPPED steps 1–3) + the Mac shell scaffold (Phase 4 steps 1–4).
**Owner:** CP.
**Last updated:** 2026-05-08.
**Lives in:** `~/Projects/patton-ai-ios/Mac/Views/ChatPopover/` (Mac target only — iOS uses the Ask tab instead).
**Cross-references:**
- [`chad-agent.md`](chad-agent.md) — defines the agent this surface invokes; this doc is the Mac entry into channel routing (step 4 in that spec)
- [`desktop-renderer.md`](desktop-renderer.md) — the surface this lives on top of
- [`desktop-design-language.md`](desktop-design-language.md) § Component vocabulary — the visual contract
- `~/Projects/patton-ai-ios/CLAUDE.md` — Mac shell conventions

---

## What it is

A floating pop-over panel summoned by **Cmd+/** from anywhere in the Mac shell. Anchored visually to the agent button in the toolbar (top-right). 480pt × 600pt. Scrolls. Persists conversation history per project. Closes on Esc or click-outside. Reopens to the same conversation it had when last closed.

It is **not**:
- A chat tab (would compete with the six surfaces in `desktop-renderer.md`)
- A modal (would interrupt the workspace)
- A separate window (would lose the workspace context)
- A clone of the iOS Ask tab (different posture — desk vs truck-cab)

It **is**:
- The fastest path to `hb-chad` from any surface
- A composer-first surface (input always visible, history scrolls above)
- Project-context-aware (knows which project the active surface is showing)
- Conversation-persistent (per-project history, not per-session)

The shorthand: **Cmd+/ is to Patton AI what Cmd+K is to Linear.** Always available, always remembers the room you're in.

---

## Why a pop-over and not a tab

The `desktop-renderer.md` six-surface inventory is built around *operating* the engine — viewing, editing, authoring. The pop-over is for *asking* — questions, judgment calls, drafts.

Three reasons it can't be a tab:

1. **Context loss on tab switch.** If Chad is staring at the Gantt and wants to ask "should I push trim?", a tab switch loses the visual context. A pop-over overlays it.
2. **Workspace continuity.** Tabs are for what you *do*; the pop-over is for the side conversation that happens *while* you do it. Linear doesn't make Cmd+K a tab; Notion doesn't make / a tab. Same logic.
3. **iOS / Mac symmetry.** iOS already has the Ask tab — phones are mode-switched, you commit to one screen at a time. Macs are window-multitasked. Forcing Mac into iOS's shape is the Catalyst trap.

---

## Anatomy

```
                                    ┌────────────────┐  ← anchored to
                                  ▲ │  agent button  │     toolbar button
                                 ╱  └────────────────┘
                                ╱                                            
┌────────────────────────────────────────────┐  ← pop-over panel
│  Whitfield Residence  ▾                  ✕ │  header (44pt) — project + close
├────────────────────────────────────────────┤
│                                            │
│  Chad asked:                               │  
│  Should I push trim if cabinets slip 2w?  │  message (user) — type.body
│                                            │
│  ─────────                                 │
│                                            │
│  Yeah, push it. Cabinets shouldn't        │  message (chad) — type.body
│  block trim — but the homeowner needs    │
│  a heads-up tomorrow either way.          │
│                                            │
│  [ View options ]  [ Draft email ]        │  inline actions on a response
│                                            │
│  ─────────                                 │
│                                            │
│  …more history scrolls here…              │
│                                            │
│                                            │  scroll region
│                                            │
├────────────────────────────────────────────┤
│  ▎ Ask Chad something…              ⌘↩    │  composer (88pt) — multiline
│                                            │     pinned to bottom
└────────────────────────────────────────────┘
```

Dimensions:
- Panel: 480pt wide, 600pt tall, 12pt corner radius
- Background: `surface.raised` with `.regularMaterial` blur — translucent vibrancy
- Header: 44pt, project switcher (left, type.title), close button (right, 24pt)
- Composer: 88pt minimum, grows to 200pt max, then scrolls
- History region: fills remaining space, scrolls vertically, newest at bottom
- Anchor: top-right of panel aligns with bottom-right of the toolbar agent button; 8pt vertical gap

---

## Conversation model

### One conversation per project

Each project gets its own persistent conversation thread. Switching projects in the pop-over header (or by changing the active surface) loads that project's thread. Closing and reopening the pop-over returns you to the same thread you had open.

```
{
  "conversations": {
    "whitfield-residence": [ ...messages ],
    "pelican-point":       [ ...messages ],
    "no-project":          [ ...messages ]   // multi-project / global asks
  },
  "last_active_project": "whitfield-residence"
}
```

Persisted to `~/Library/Application Support/PattonAIShellMac/chat_history.json` (per-machine, per-user), not synced across devices in v1. iCloud sync is a v1.5 deferred item.

Each conversation is capped at 200 messages or 30 days, whichever comes first — older messages roll off. A "Clear conversation" item in the project switcher menu blows the current thread.

### Project-context binding

The pop-over inherits the active surface's project automatically:

| Active surface | Pop-over project |
|---|---|
| Master schedule for Whitfield | Whitfield |
| Daily view filtered to Pelican Point | Pelican Point |
| Daily view, no filter (all projects) | "no-project" — global thread |
| Notification feed | The project of the most-recent unack'd notification, or "no-project" |
| Settings / Onboarding | "no-project" |

The header shows the inherited project as a switcher. Click → menu of all active projects + "Global." Manually switching via the menu **overrides** auto-binding for the rest of this pop-over session — it stays on the chosen project even if the active surface changes, until the pop-over is closed.

This rule prevents an annoying class of bug: the user is composing a question about Whitfield while glancing at Pelican Point's schedule, and an auto-rebind would scramble the context mid-thought.

### What `hb-chad` receives per turn

```
{
  "user_input":  "<the typed message>",
  "channel":     "mac-popover",                      // new channel value
  "project_id":  "<bound project, or null for global>",
  "active_view": "master | daily | weekly | monthly | checklist | feed | none",
  "conversation_history": [ ...last N messages ],   // sliding window, ~20
  "user_profile": <hb-profile json>                  // unchanged from chad-agent.md
}
```

`channel: "mac-popover"` is a new value that joins `ios | email | terminal` per `chad-agent.md` § "What it is, in code." The agent's verbosity rules vary by channel:

- `mac-popover` — concise (desk-side asks, not field), 1–3 paragraphs default, code/data formatted with mono blocks
- `ios` — even more concise, voice-friendly phrasing, no code blocks
- `email` — full prose, headers, sign-off
- `terminal` — verbose, structured, debug-friendly

`active_view` is a hint to the agent about what Chad's looking at — lets it answer "what does this mean?" with implicit reference to the current screen.

---

## Backend wiring

The Mac pop-over hits the existing FastAPI shell-backend, not `hb-chad` directly:

```
Mac pop-over → POST /v1/turtles/home-builder/chat
  body: { user_input, project_id, active_view, history }
  ↓
shell-backend ─ resolve user_id from JWT
              ─ load user_profile, recent activity (engine_activity)
              ─ invoke hb-chad with full context, channel="mac-popover"
              ↓
              ← stream response chunks (SSE)
              ↓
Mac pop-over renders streaming response
```

Two things to land on the backend side:

1. **`POST /v1/turtles/home-builder/chat`** — new route, similar shape to the iOS Ask tab's existing route. May reuse the same handler with a `channel` parameter.
2. **Streaming via SSE.** The agent's response streams token-by-token so the pop-over feels instant. Already supported in the shell-backend pattern; just needs wiring on the new route.

**Zero engine changes.** The pop-over is a renderer over `hb-chad`; the engine doesn't know about it. Action writes triggered by inline action buttons (e.g., "Draft email") go through the same `POST /actions` → reconcile path as everything else.

---

## Keyboard contract

The pop-over is keyboard-first. Mouse works, but a power user should never need it.

| Key | Action |
|---|---|
| **Cmd+/** | Open pop-over (anywhere in app); refocus composer if already open |
| **Esc** | Close pop-over (preserves draft + history) |
| **Cmd+Enter** | Send message |
| **Enter** | New line in composer (multiline support) |
| **Shift+Enter** | New line (alternate; matches iMessage convention) |
| **Cmd+K** | Clear current conversation (with confirm sheet) |
| **Cmd+\\** | Toggle project switcher menu open |
| **Cmd+1 / Cmd+2 / Cmd+3** | Switch to project 1/2/3's conversation (matches sidebar shortcuts) |
| **Up arrow (when composer empty)** | Recall last message for editing |
| **Cmd+C (in a response)** | Copy response text |
| **Cmd+L** | Scroll to latest message |

Cmd+/ is global within the app. It is **not** a system-wide shortcut in v1 (would require Accessibility permissions); v1.5 may add a system shortcut.

---

## Visual + motion

Inherits from `desktop-design-language.md`:

| Element | Token |
|---|---|
| Panel background | `surface.raised` with `.regularMaterial` blur |
| Panel border | `surface.divider` (1pt) |
| Panel shadow | 1pt elevation, 8pt blur, `#000` @ 30% (Mac-native window shadow) |
| Header text | `type.title` |
| Composer placeholder | `fg.tertiary` |
| User message text | `fg.primary` |
| Chad message text | `fg.primary` |
| Inline action buttons | `status.healthy` outline, `tap.standard` height (36pt on desktop) |
| Streaming cursor | `status.healthy` block, blinking 800ms |
| "Sending…" indicator | Dots animation, `motion.snap` |
| Open animation | Slide+fade from anchor button, 200ms ease-out |
| Close animation | Reverse, 150ms ease-out |

**Streaming behavior.** Tokens render as they arrive, no spinner. The cursor block sits at the end of the streaming text and blinks until the response completes. If the stream stalls >3s, a subtle "still thinking…" line appears below the cursor in `fg.tertiary`.

**Inline actions.** Some agent responses end with action buttons ("Draft email," "View options," "Apply update"). These are rendered as `status.healthy`-outline chips below the response, clickable, fire `UserAction` writes through the existing reconcile path. Max 3 per response — beyond that, the agent's prompt should be tightened.

---

## Edge cases

### No active project

Active surface = settings / onboarding / multi-project rolldown. Header shows "Global." Conversation thread is `no-project`. The agent receives `project_id: null` and answers based on the user profile + cross-project context (e.g., "How's my pipeline looking?").

### Multi-window

Each window can have the pop-over open independently. They share conversation history (one thread per project, app-wide), but the pop-over instances are per-window. Two windows showing the same project = two pop-over views of the same thread; opening / closing one doesn't affect the other.

### Connection lost

Composer goes into a "queued" state on send: the message is added to local history with a "queued" badge, retries 3× with exponential backoff (2s, 4s, 8s), then surfaces a "tap to retry" affordance. Mirrors the iOS offline-write pattern but without the global status strip (Mac assumes desk = wifi).

### Long responses

Responses cap at ~2000 tokens before the agent self-truncates with a "[continued — ask for more]" tail. The pop-over scrolls to the bottom on each new chunk; the user can scroll up to read while streaming, and a "↓ Latest" pill appears bottom-right when scrolled away from the tail.

### Conversation history limit

When a thread hits 200 messages, oldest 50 roll off silently. The pop-over shows "—— earlier messages cleared ——" at the top of history when this has happened, so it's not a mystery.

### App quit during stream

The in-progress response is saved as a partial message with a "[interrupted]" tail. On next launch, the pop-over shows it as the last message; the user can re-ask or continue.

---

## What this unlocks

- **Per-surface "what does this mean?" asks.** Chad on the Gantt: "Why is foundation orange?" → agent reads `active_view: "master"` + `project_id` + recent activity → answers from real engine state.
- **Composition without context-switch.** "Draft an email to the homeowner about the cabinet slip" → agent composes → inline action → email goes to Gmail drafts via the existing `client_update_agent` / Gmail integration.
- **Judgment glue.** The example from `chad-agent.md` — "should I push trim if cabinets slip 2w?" — the pop-over is the surface where that question gets asked from a desk.
- **Onboarding the user into the agent.** First-launch pop-over hint: "Try asking 'what's blocking framing?' — Cmd+/ opens this anywhere."

---

## Implementation order (when this gets built)

Slots into Phase 4 of `desktop-renderer.md`. Suggested sub-order:

1. **`POST /v1/turtles/home-builder/chat` route** on the shell-backend. SSE streaming, channel parameter, project + active_view inputs. *(~half day backend.)*
2. **`hb-chad` channel branch.** Add `"mac-popover"` to channel enum, add the verbosity rule. *(~hour, this repo.)*
3. **Pop-over panel scaffold** — anchored, blurred, sized, dismissible. *(~half day Mac.)*
4. **Composer + streaming history rendering.** *(~day Mac.)*
5. **Conversation persistence** — JSON file, per-project threads, load/save lifecycle. *(~half day.)*
6. **Project-context binding rules** — auto-bind from active surface, manual override. *(~half day.)*
7. **Keyboard contract** — Cmd+/, Esc, Cmd+Enter, project switching shortcuts. *(~half day.)*
8. **Inline action buttons** — render from agent response, fire UserActions. *(~half day.)*
9. **Edge case polish** — offline state, long responses, app-quit recovery. *(~day.)*

**Total: ~5 days from a working Mac shell.** Could be parallelized with the V2 surface work if a designer is available.

---

## Anti-patterns

- **Don't make it a chat tab.** It's an overlay; the moment it becomes a tab it's competing with the six surfaces.
- **Don't auto-rebind project mid-conversation.** Annoying as hell — Chad's halfway through composing a question about Whitfield, glances at Pelican's Gantt, the question now goes to the wrong project.
- **Don't show a typing indicator from Chad.** It's an agent, not a person. Streaming tokens are the indicator.
- **Don't add reactions / emojis / threading.** This isn't Slack. One linear conversation per project, no collaboration features.
- **Don't sync conversation history across devices in v1.** Privacy + complexity trade-off. iCloud sync deferred to v1.5.
- **Don't allow Cmd+/ to also dismiss when open.** Cmd+/ when open should refocus composer, not toggle. Esc dismisses. Different keys for different actions; toggling is sloppy.
- **Don't render the pop-over inside the active window's content area.** It's an overlay, anchored to chrome. Anchoring it to content means it scrolls with content — bad.

---

## Open questions

- **Project switcher — menu or sheet?** Menu is faster but caps at ~10 projects before getting unwieldy. Sheet is more browseable but slower to a single tap. Lean: **menu** with a "More projects…" item that opens a sheet when count > 8.
- **Should the pop-over show recent activity above the composer?** A 2-line "what's happened on Whitfield in the last 24h" header could ground the user before they ask. Lean: **no for v1** — too noisy; the agent already has activity in its context, ground via response not chrome.
- **Does the pop-over close when an inline action fires?** "Draft email" creates a draft in Gmail — should the pop-over close to celebrate, or stay open for follow-up? Lean: **stay open**, show a confirmation chip ("Drafted ✓"), let the user follow up with "make it warmer" or close themselves.
- **Voice input on Mac?** macOS dictation is Cmd+Esc by default. Just rely on system dictation in the composer — no custom voice UI. Lean: **yes, system-only.**
- **Conversation export.** Should the user be able to export a thread to Markdown / a Drive doc? Useful for "I asked Chad about Whitfield's framing slip — show me the convo" → ledgered as a project artifact. Lean: **defer to v1.5** but design history storage in a format that makes export trivial when added.
- **Across-window single-source-of-truth.** Two open windows, one types into the pop-over — does the other window's pop-over (if open) update live? Lean: **yes, via NSNotificationCenter broadcast** — the conversation file is the truth, both views render from it. ~half day extra.
- **Token cost surfaced to the user?** A footer line "this conversation: ~$0.08" might help Chad understand the spend, or might be tacky. Lean: **no in chrome**, available in settings as a per-conversation cost view if desired.
