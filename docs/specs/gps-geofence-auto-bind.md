# GPS-Geofence Project Auto-Bind

> One-line summary: when Chad pulls into a job site, the iOS app knows which project he's at and pre-selects it for the next photo, site log, or voice note — no manual project picker, ever.

**Status:** Spec — initial skeleton landed on `feat/gps-geofence` branch in `patton-ai-ios`.
**Phase:** Construction-turtle roadmap Week 1 (post-competitor-research, 2026-05-11).
**Owner:** CP.
**Last updated:** 2026-05-11.
**Motivation:** [competitor-research-2026-05-11.md](../competitor-research-2026-05-11.md) + [competitor-research-photos-2026-05-11.md](../competitor-research-photos-2026-05-11.md) both flagged "which project?" pickers as the biggest friction point in field workflows. CompanyCam, Buildertrend, BuildBook all force the user to pick a project before doing anything; none auto-detect from location even though they could. Filling that gap is a one-week win.

## Overview

Today every "take a photo" / "log a site note" / "voice walkthrough" flow starts with: *pick a project from a list*. With 2–6 active projects, that's not painful, but it's a tax on every action. More importantly, it's the friction subs hate about every competitor — the app gets in the way of the work.

This spec wires Core Location region monitoring around each active project's address so the iOS app auto-detects the current project as Chad pulls in and pre-selects it everywhere. The user can still override; geofencing is a *hint*, not a lock.

Three downstream flows benefit immediately:

1. **Photo capture** — open camera, project already chosen.
2. **Site log** — voice-dictated note auto-tagged with project + phase.
3. **Receipt agent** — photo → cost tracker writes to the right project's sheet.

Future flows (selections picker, walkthrough recorder, change-order draft) ride the same `CurrentProjectStore`.

## Architecture

```
┌────────────────────────────┐         ┌──────────────────────────────┐
│ Backend (FastAPI)          │         │ iOS (PattonAIShell)          │
│                            │         │                              │
│ GET /v1/turtles/{tid}/     │ ──────► │ APIClient.fetchActive        │
│   projects/with-locations  │         │   ProjectLocations()         │
│                            │         │            │                 │
│ projects.lat,              │         │            ▼                 │
│   projects.lng,            │         │ LocationManager              │
│   projects.geofence_radius │         │   - CLLocationManager        │
│   _meters                  │         │   - startMonitoring(regions) │
└────────────────────────────┘         │            │                 │
                                       │            ▼                 │
                                       │ didEnterRegion / didExit     │
                                       │            │                 │
                                       │            ▼                 │
                                       │ NotificationCenter post      │
                                       │   .pattonProjectGeofence*    │
                                       │            │                 │
                                       │            ▼                 │
                                       │ CurrentProjectStore          │
                                       │   @Observable                │
                                       │   currentProjectID: UUID?    │
                                       │            │                 │
                                       │            ▼                 │
                                       │ PhotoView / SiteLogView      │
                                       │   observe currentProjectID   │
                                       │   to pre-select project      │
                                       └──────────────────────────────┘
```

Same pattern as `PushNotificationManager` (the existing reference): manager owns the OS-API flow, posts `NotificationCenter` events, an `@Observable` store consumes them.

## Backend changes

### Schema migration (new)

Add three columns to the existing `projects` table:

```sql
ALTER TABLE projects
  ADD COLUMN lat                  DOUBLE PRECISION,
  ADD COLUMN lng                  DOUBLE PRECISION,
  ADD COLUMN geofence_radius_m    INTEGER DEFAULT 150;
```

Why 150m default: Baldwin County lots are big. 150m covers the lot + driveway approach without false-firing when Chad drives past on a county road. Adjustable per project once we have real data.

Migration file: `home_builder_agent/scheduling/migrations/010_project_geofences.sql` (TODO — not yet committed).

### New endpoint

```
GET /v1/turtles/{turtle_id}/projects/with-locations
```

Returns active projects only, each with `lat`, `lng`, `geofence_radius_m`, alongside the existing `HBProjectListItem` fields. Projects with `lat=NULL` are returned but the iOS client skips them when registering regions.

Why a separate endpoint instead of overloading the existing `/projects`: the location fields are only needed by the iOS app's region setup. Keeping the list endpoint lean avoids leaking precise coordinates to every dashboard call.

Response shape:

```json
{
  "projects": [
    {
      "project_id": "976d146d-dd1e-4a88-9022-158f9e348010",
      "name": "Whitfield Residence",
      "customer_name": "Whitfield",
      "status": "active",
      "lat": 30.5446,
      "lng": -87.7038,
      "geofence_radius_m": 150,
      "target_completion_date": "2027-01-30",
      "updated_at": "2026-05-11T20:50:20+00:00"
    }
  ]
}
```

### Geocoding the address

When a new project is created, we need to populate `lat`/`lng`. Two options:

- **Manual entry** (v1): Chad enters lat/lng when creating the project via `hb-create-project`. Easiest, zero new dependencies. Could pull coords from Google Maps URL paste.
- **Auto-geocode** (v2): hit Google Geocoding API on project creation. Costs ~$0.005 per geocode, trivial at 6 projects.

Ship v1 manual entry. Add auto-geocode in v2 when more clients land.

## iOS changes

Files landing in `ios/PattonAIShell/PattonAIShell/Location/`:

### `LocationManager.swift`

Wraps `CLLocationManager`. Responsibilities:

- Requests `whenInUse` authorization on first call (Chad approves once).
- Calls `requestAlwaysAuthorization` when the user opts into background detection — separate user choice.
- Fetches active projects with locations from the backend at app launch + every 6 hours.
- Builds a `CLCircularRegion` for each project with `lat != nil`.
- Calls `startMonitoring(for:)` on every region (iOS limit: 20 regions per app; we have 2–6).
- On `didEnterRegion` / `didExitRegion`, posts `.pattonProjectGeofenceEntered(projectId:)` / `.pattonProjectGeofenceExited(projectId:)` notifications.

State enum mirrors `PushNotificationManager.AuthorizationStatus`.

### `CurrentProjectStore.swift`

`@MainActor @Observable` store. Holds:

- `currentProjectID: UUID?` — observed by photo/site-log/etc. views to pre-select.
- `currentProjectSource: Source` — enum: `.geofence`, `.manualOverride`, `.lastUsed`, `.none`. Tells downstream UI where the binding came from so it can show "Auto: Whitfield ✓" vs "Selected: Pelican Pt".
- `lastBoundAt: Date?` — used to ignore stale geofence events (debounce 30s).

Subscribes to:
- `.pattonProjectGeofenceEntered` → sets `currentProjectID` + source `.geofence`.
- `.pattonProjectGeofenceExited` → clears `currentProjectID` if the exited project was the current one AND no manual override is active.

Public:
- `setManualOverride(projectId:)` — view code calls this when the user explicitly picks a project. Pins for the rest of the app session (or until they pick a different one).
- `clearOverride()` — falls back to whatever the latest geofence says.

### `APIClient+ProjectLocations.swift`

Extension on existing `APIClient`. Adds:

```swift
func fetchActiveProjectLocations() async throws -> [HBProjectWithLocation]
```

Hits `GET /v1/turtles/{turtleId}/projects/with-locations`. Decodes into `HBProjectWithLocation` (new wire type alongside existing `HBProjectListItem`).

### `Models/HBProjectWithLocation.swift`

New file alongside `HBProjectListItem.swift`. Same fields plus optional `lat`, `lng`, `geofenceRadiusM`. Could be a superset extension instead, but a separate type keeps the dashboard list code from accidentally rendering coordinates.

### Adding the new files to the Xcode target

The Xcode project (`PattonAIShell.xcodeproj`) uses explicit `PBXFileReference` entries — files dropped into the source folders on disk are **not** automatically picked up by the build. After checking out the branch, in Xcode:

1. File → Add Files to "PattonAIShell"…
2. Select the new `Location/` folder + the new `Models/HBProjectWithLocation.swift` file
3. Make sure "Add to targets: PattonAIShell" is checked
4. Click Add

This writes the references into `project.pbxproj` and the next build picks them up. Without this step, `LocationManager` and friends are dead source files on disk.

### Info.plist additions

The `Generated-Info.plist` is auto-generated from `INFOPLIST_KEY_*` build settings in the Xcode project. Add to project settings (Xcode UI → Build Settings):

```
INFOPLIST_KEY_NSLocationWhenInUseUsageDescription =
  "Patton AI uses your location to detect which job site you're at, so you don't have to pick a project before taking photos or logging site notes."

INFOPLIST_KEY_NSLocationAlwaysAndWhenInUseUsageDescription =
  "Background location lets the app pre-select the right project when you arrive at a job site, even before you open the app."

INFOPLIST_KEY_UIBackgroundModes = "location"   # only if we wire background launch
```

### Entitlements

Current `PattonAIShell.entitlements` has `aps-environment` + `applesignin`. Add nothing here — location is permission-driven, not entitlement-driven, except for `com.apple.developer.location.push` (silent location pushes) which we do NOT need.

## Privacy & permissions

| Decision | Rationale |
|---|---|
| Default to `whenInUse` only | Cheaper privacy ask. Chad opens the app when he arrives at a site anyway — auto-detect kicks in then. |
| `always` is opt-in via Settings tab | Power-user feature. When on, app pre-detects from background and shows a push notification ("You're at Whitfield — open Site Log?") |
| No location pings to server | Region matching is 100% on-device. We never send Chad's coordinates to the backend. The server only knows project coordinates (which Chad gave us). |
| iOS Settings → Privacy → Location Services → Patton AI | The user controls everything from here. App respects the setting. |

## Edge cases

| Case | Handling |
|---|---|
| iOS region limit (20) | We have 2–6 active projects. Safe. If we ever exceed 20, prioritize by `updatedAt` (most recently touched first). |
| Project lat/lng is NULL | Skip; the project just doesn't trigger auto-bind. Manual picker still works. |
| Chad lives near a job site | Debounce: if `didExit` fired <5 min ago for project X, don't immediately re-fire `didEnter` for the same X. Avoids constant toggling at the edge of the geofence. |
| User explicitly picks a different project while auto-bound | Manual override wins. Geofence stops updating `currentProjectID` until override is cleared OR the user crosses a different project's geofence. |
| GPS accuracy < 100m on a 150m radius | Treat as "probably at site." 100m horizontal accuracy is typical for urban GPS, fine for our use. |
| Multiple overlapping geofences (two sites next to each other) | Use whichever fired most recently. Chad can manually override. |
| Permission denied | App still works; manual picker is the fallback. Show a one-time tooltip pointing at Settings. |
| Simulator | Region monitoring works in simulator with Xcode's Location simulation (`Features → Location → Custom Location`). |

## Open questions

- **Background launch power cost** — region monitoring is OS-level and low-power, but worth measuring on a real device over a week of normal use before defaulting to `always`.
- **Polygon vs circular** — irregular-shaped luxury lots could be modeled as polygons. CLCircularRegion is fine for v1; revisit if a project's geofence routinely false-fires.
- **Geocoding source** — Google Maps URL paste vs typed lat/lng vs a tap-on-map picker in the iOS app. Tap-on-map is the slickest UX; defer to v2.
- **Sub geofences** — when SMS sub coordination lands (Week 2 of roadmap), should subs also get auto-project-tagging on their MMS uploads? Yes, via Twilio carrier-provided location, but accuracy is cell-tower-level (~1km). Could be good enough.

## Acceptance criteria

v1 done when:

1. Schema migration ships with the three new columns.
2. `GET /v1/turtles/{tid}/projects/with-locations` returns coordinates.
3. iOS app requests location permission and registers regions for active projects on launch.
4. `CurrentProjectStore.currentProjectID` populates within ~30s of crossing a project's geofence in foreground.
5. Photo view + Site Log view consume `currentProjectID` to pre-select the project picker.
6. Manual override works and persists for the session.
7. No coordinates leave the device.

v2 (out of scope for this iteration):

- Background launch + push notification on geofence enter.
- Tap-on-map project geocoding UI inside the iOS app.
- Polygon geofences for irregular lots.
- Twilio cell-tower fallback for sub MMS uploads.

## Cross-references

- iOS branch: `feat/gps-geofence` in `~/Projects/patton-ai-ios/`
- Backend changes: TBD branch in `~/Projects/home-builder-agent/`
- Roadmap: see [ROADMAP-2026-05-11.md](../ROADMAP-2026-05-11.md) Week 1
- Positioning ADR: `~/Projects/patton-os/data/decisions.md` — search "construction turtle positioning"
