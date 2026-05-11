# Construction Photo Management — Competitor Deep Dive
**Date:** 2026-05-11
**Author:** Photo-workflow research pass (companion to `competitor-research-2026-05-11.md`)
**Subject:** What Chad's app should steal from CompanyCam et al.

---

## 1. TL;DR

The construction photo-management category in May 2026 is consolidated around **CompanyCam** (the de-facto "photo CRM" for residential/specialty contractors), with **OpenSpace + Disperse** owning the AI-vision/progress-tracking high end (October 2025 acquisition closed) and **Fieldwire**, **Raken**, and **Autodesk Build** pulling photos into adjacent workflows (plans, daily reports, punch lists). The frontier in 2026 is no longer photo capture — it's what AI does with the photos. CompanyCam's "Sidekick" features (voice-narrated walkthroughs → PDF, AI captions, AI daily logs, AI checklists) shipped in 2025 and now sit in the Premium tier at $129/mo per 3 seats. Nobody has nailed the sub-without-an-app upload flow — CompanyCam still requires subs to create a free account.

**The 3 features Chad's app should steal from CompanyCam:**
1. **Voice-narrated walkthrough → AI-generated PDF report** (the "Sidekick Walkthrough Note" — Chad already has a site-log agent; bolt photos onto it).
2. **GPS + project auto-binding on photo capture** (no manual "which project?" picker — geofence the 2–6 active sites and auto-tag).
3. **Before/after pairing UX** (one-tap "Take After Photo" with ghost overlay of the before). Trivial to build, huge sales/marketing payoff.

Skip: per-seat pricing model, generic photo CRM, social-marketing suite. Chad has 2–6 projects, not 50 — the multi-tenant photo-CRM model is the wrong shape.

---

## 2. CompanyCam Deep Dive

### 2.1 Core feature surface

| Feature | What it does |
|---|---|
| **Photo & video capture** | Unlimited cloud storage; auto-timestamp + GPS on every shot; up to 5-min video (Pro) / 10-min (Premium) / dual-camera (Elite) |
| **Annotations** | Draw arrows, circles, text directly on photos in-app |
| **Before/After** | Pick a "before" photo → "Take After Photo" with ghost alignment overlay → auto-tagged "Before and After" |
| **Project timeline** | Auto-updating, shareable feed (live link to clients/subs) |
| **Galleries** | Curated photo collections, shareable as link, PDF, or (Elite) embeddable on a website |
| **Tags & Labels** | Custom tags per company; filter/search across all projects |
| **Document scanning** | In-app scanner for receipts, plans, paperwork |
| **Checklists** | Photo-required task lists for crew SOPs |
| **Pages** | "Digital notebook" — mixed photos + text, like a Notion page per project |
| **Photo reports** | PDF generator with header, narrative, photo grid |
| **LiDAR mode** | iPhone Pro / iPad Pro only, Elite tier. Tap ruler icon, draw line on a depth-captured photo, get a measurement |
| **Marketing suite** | Auto-convert project photos to social posts; review requests (Elite) |
| **In-app messaging** | Threaded comments per project & per photo |
| **Payment requests** | Embedded payment collection on shared galleries |

### 2.2 CompanyCam Sidekick — the AI bet (deep dive)

This is the area to study. Sidekick is CompanyCam's umbrella for AI features shipped 2024–2025, positioned as "the admin work you hate, done while you work." All features run on the photo stream — voice + image is the input modality. They're built around six primitives:

1. **Quick Captions** — Hold a photo button, talk while you shoot. Speech-to-text becomes the photo caption. No typing. Replaces the universal "untitled photo dumped in a folder" problem.
2. **Walkthrough Note** — The flagship. Walk the site narrating ("here's the kitchen, framing is up, electrical rough-in is ready for inspection, this window opening is wrong by 2 inches"). AI fuses voice + photos into a structured PDF — scope of work, closeout package, or punch list. Output is text-with-embedded-images, shareable as PDF or link.
3. **AI Checklists** — Same walkthrough, different output. Speak observations → AI produces a task list for the crew.
4. **AI Daily Logs** — Auto-summarizes "what happened today + what's next tomorrow" from the day's photos and voice notes.
5. **Photo Summaries** — Select up to 50 photos → AI writes paragraph-length narrative context.
6. **Translations** — Photo comments + chat auto-translate (English ↔ Spanish primarily; multilingual crews are the use case).

**Notably absent from native Sidekick:**
- No documented AI vision **progress estimation** ("framing is 60% done") — that's OpenSpace/Disperse territory.
- **Auto-tagging by content** (e.g. "this is a roof shot" → auto-tag `#roofing`) is not native. A community workaround uses Zapier + ChatGPT to extract keywords from photo descriptions and write them back as CompanyCam tags — telling that this is third-party glue, not a first-class feature.
- No auto-binding of photos to a daily log (you can include them, but not auto-attach today's photos to today's log).

Sidekick is gated to **Premium and above** ($129/mo). Pro tier ($79/mo) gets no AI.

### 2.3 Subcontractor experience

This is where CompanyCam is supposedly famous and where the reality is weaker than the marketing implies. Two paths:

- **Collaborator** — Free, no per-seat cost. Sub must create a CompanyCam account, accept project invite via email or shared link. Then uses the app like a normal user but scoped to the projects they're invited to.
- **Shared project link** — Real-time gallery view, but for *viewing* and limited adding. Subs still must sign in to add photos.

There is **no SMS-photo-upload, no email-photo-forward, no QR-scan-to-upload-without-account** flow. The "subs hate portals" problem CompanyCam is credited with solving — they solved it by making sub seats free, not by eliminating login. App-only, must create account. This is a real gap, and an obvious place to differentiate.

### 2.4 Integrations

50+ CRMs/FSMs. Major ones:
- **JobTread** — two-way: creating a JobTread job auto-creates the CompanyCam project (contacts, address, coords)
- **JobNimbus** — heavy in roofing
- **AccuLynx, Jobber, Leap, Markate, Beam** — official partners on the features page
- **Buildertrend** — partner integration available (not surfaced in their primary list, but supported via the JobNimbus-style pattern)
- **Zapier** — the back door for everything else, including QuickBooks, GSuite, Slack

No QuickBooks-direct (Zapier-mediated). No Procore-direct (commercial gap — CompanyCam is residential/specialty).

### 2.5 Pricing (May 2026)

| Tier | Monthly | Per add'l user | Key AI/features |
|---|---|---|---|
| **Pro** | $79 (3 seats) | $29 | No AI. 5-min video, checklists, PDF reports, before/after, payment requests. |
| **Premium** | $129 (3 seats) | $29 | + **Sidekick AI walkthroughs**, custom templates, 10-min video, insights dashboard, collaborator access. |
| **Elite** | $199 (3 seats) | $29 | + Customer reviews, digital signatures, embedded web galleries, dual-camera video, **LiDAR measurement**. |
| **Enterprise** | Custom | — | 50+ employees. |

3-seat floor on every tier. Annual billing only at advertised rates. 14-day free trial.

### 2.6 What users love (sourced)

| Quote | Source |
|---|---|
| "CompanyCam became crucial for our day-to-day work, especially for keeping all job photos" | Capterra, roofing contractor |
| "Time-stamped photos make it easier to prove work to insurers and keep crews aligned" | Software Advice |
| "The most valuable tool I use daily — I can find any job instantly and share it professionally with clients" | App Store review |
| "Far superior to Google Drive and Dropbox… a phenomenal tool for field techs and truly invaluable for the office" | Capterra |
| Avg user takes **47 photos/day** | CompanyCam metrics |

The reliable wins: (1) auto-GPS-and-timestamp removes the "which job is this from?" tax; (2) live project feed kills phone calls between office and field; (3) insurance/litigation defense via time-stamped record.

### 2.7 What users hate (sourced)

| Quote | Source |
|---|---|
| "Expensive for what it does, especially as we added more users" — ~$500/mo for photo management | G2 |
| "Updates added complexity and made generating reports significantly slower than before" | Capterra |
| "We struggled to cancel and stop charges, which really soured our experience" | Capterra |
| "Great for photos, but we still need separate software for scheduling and invoicing" | Reddit |
| Every click out of a form field forces a 15-second edit-page reload | App Store |
| Pic count at top of project doesn't match scroll count; some photos appear invisible | App Store |
| Locked out after 30-day trial with no warning, forced into $110/mo or log out | App Store |

The pattern: **photo capture is excellent, everything around it is mediocre**. App is bloating, perf is degrading, and at $26/user effective minimum you're paying SaaS prices for a feature that arguably could be a free Google Photos folder plus a thin layer of metadata.

---

## 3. Adjacent photo tools roundup

| Tool | Target user | Photo differentiator | Pricing | Top 2 strengths | Top 2 weaknesses |
|---|---|---|---|---|---|
| **Fieldwire (Hilti)** | Commercial subs, GCs, mid-large | Photos pinned to blueprints; AI photo-tagging by discipline (2025) | $54–$74/user/mo | Plans-first; offline-first | Pricey, complex onboarding |
| **Autodesk Build / PlanGrid** | Commercial GCs | Photo overlay on drawings, version diffing | Custom (~$80+/user) | Drawings + photos + RFI in one; ACC ecosystem | Heavy; PlanGrid in maintenance mode |
| **Raken** | Subs/GCs, commercial | Photos auto-attach to daily report (pick 4 highlights) | $15–$49/user/mo | Best-in-class daily logs; voice-to-text reports | Photo viewer is secondary to report flow |
| **OpenSpace + Disperse** | Mid-large GCs | 360° walks + computer-vision % complete | Enterprise | Auto-progress tracking 700+ visual components; AI Autolocation indoor positioning | Enterprise-only, expensive, requires 360 cam |
| **Photolog / Site PhotoLog** | Solo / inspector | Stamps photos with company, time, location, GPS as overlay | One-time/cheap | Dead simple; works offline | No project structure, no AI |
| **Buildup** | GCs, dev/owner | Punch-list-first photos (with follow-up shot when issue closed) | Mid-market SaaS | Strong punch-list workflow | Weak reporting, no AI photos |

**150-word notes on the most relevant three:**

**Fieldwire (by Hilti).** Commercial-leaning. Where CompanyCam is a photo CRM, Fieldwire is plans + tasks first; photos are attached to tasks and pinned to coordinates on a drawing. They shipped AI photo tagging by discipline in 2025. $54/user/mo Pro and up — meaningfully more than CompanyCam. Top strengths: plan markup with photos attached at specific x,y on the sheet; works offline at scale. Top weaknesses: pricey if your team's not on drawings (Chad's isn't — luxury custom homes, but Palmetto runs from architect PDFs not active markup workflows), and the UI is overweight for a 2–6 project shop.

**Raken.** Daily-report-first. Photos exist to populate the daily report. Voice-to-text drives 5-minute reports, and the workflow picks "best 4 photos" automatically — closer to what Chad already does on the text side. Pricing $15–$49/user/mo is the most reasonable in the segment. Strengths: speed of daily-log creation; OSHA toolbox-talk content. Weakness: not a photo-search tool — find-this-photo-from-3-months-ago is awful.

**OpenSpace + Disperse.** The AI-vision answer. October 2025 acquisition combined OpenSpace's 360-walk capture with Disperse's computer-vision progress tracking. Tracks 700+ visual components and percent-complete by area/trade. AI Autolocation places indoor shots without GPS or beacons. This is the frontier, but it's enterprise-only — Palmetto is too small for the workflow (and the cost). Worth knowing because the AI primitives (auto-locate, % complete from photo) are exactly what a 2026 custom-home agent could ape at a small scale.

---

## 4. The 10 frontier questions — answered

**1. Auto-tagging photos by jobsite/project — who does this best, and is geofencing the right approach?**
GPS auto-tagging is universal (CompanyCam, Raken, 123onsite all do it). But **auto-binding to a project** is rarer — CompanyCam matches GPS coords to known project addresses to pre-select the project. For a 2–6 active project shop like Palmetto, **iOS geofencing via CoreLocation (region monitoring)** is the right tool: define a 100m radius around each active site, fire a project-assignment event when the foreman crosses in. BLE beacons solve indoor (OpenSpace's AI Autolocation does this in software for commercial), but Palmetto's job sites have wide-open lots — GPS is fine. Manual override picker should always exist as a fallback.

**2. Photo → daily log auto-attach — does anyone do this today?**
**Raken** is closest: photos taken on-site automatically attach to the daily report; the foreman picks 4 to highlight. CompanyCam doesn't have it natively — their AI Daily Log summarizes a day's photos but you still build the log document separately. **ConstructionOnline**, **SmartBarrel**, and **BuildStackHub** also auto-attach. The Chad app already has a site-log agent; bolting today's GPS-on-project photos onto today's log is a straightforward win.

**3. Photo → progress estimation ("framing 60% complete") from vision models?**
**OpenSpace + Disperse**, **DroneDeploy AI**, **iFactory**. They track 700+ visual components across 200+ tasks (OpenSpace's number). Tied into BIM, accuracy is claimed at 98% structural. This is research-grade today for residential — none of it works on iPhone photos taken by Chad. But a constrained version is feasible: GPT-4o-vision can absolutely classify "this is a framing photo, walls up, no roof, no drywall" with ≥85% confidence. Building a Chad-specific schedule-aware classifier on top of his ~20 build phases is realistic for a v2.

**4. Before/after comparison UX — slickest implementation?**
CompanyCam's slide-to-reveal divider on a single composite image is the consumer-grade leader; the "Take After Photo" flow has a ghost-overlay of the before to align the camera. Time-lapse (Raken's monthly progress slideshow) is a different use case. For Chad: slide-to-reveal + ghost-align is table stakes; the killer add would be **auto-pairing** — when a new photo's GPS+orientation matches an older one within 5°, suggest "create before/after from these."

**5. Sub photo upload without app/login — does this work?**
**Not really, today.** CompanyCam requires account creation (even for free collaborators). **FieldChat** does inbound SMS → auto-route to construction PM system, but it's a separate SaaS. The **email-forward** pattern (Receipt AI does this for receipts) is underused for photos. **A QR-on-site-trailer + magic-link upload form** is technically trivial and no competitor has shipped it well. This is a credible differentiation for Chad: print "Scan to upload photos to this project" QR codes for each lot, drop the link into a public-magic-link Twilio MMS gateway, done.

**6. AI photo descriptions — anyone else, quality?**
CompanyCam Quick Captions are good for narration ("here's the kitchen window, frame's out") but only because the user is the speaker. **Fieldwire's AI photo tagging by discipline (2025)** is auto-categorization without user input — described as "decent for plumbing/electrical/framing but generic for finishes." Generic LLM-vision captions ("a photo of a wooden floor") are unhelpful. The winning pattern is **voice-narrated capture** — let the foreman talk, transcribe, structure. Chad's app should do this; pure-vision auto-captions are a trap.

**7. Voice notes on photos — common or rare?**
Common in newer products (CompanyCam, Raken, OpenSpace Field), rare in older ones. Sub adoption: anecdotally mixed — subs use it more than they use typing, but the feature only gets traction if the workflow forces the photo first (e.g., the daily-log button takes a photo and starts a voice recording in one tap). Chad's existing voice-text site-log agent is already aligned with this.

**8. Photo → Cost Tracker / receipt OCR — state of the art vs Chad's receipt agent?**
Chad's receipt agent already does receipt photo → OCR → Google Sheets. The CompanyCam path is weaker — they have document scanning, but no auto-categorize-into-cost-codes flow. **Receipt-AI**'s SMS path (text photo → categorized expense) is closer to Chad's pattern. **JobTread** has receipt-attachment-to-line-items but no AI categorization. Chad is already at or ahead of state-of-the-art here; just extend the agent to handle job-cost-code tagging via project context, not just vendor matching.

**9. Plan markup / overlay onto photos — is "here's where this photo lives on the floor plan" a thing?**
**Fieldwire** is the leader (pins photos to x,y on a drawing). **Autodesk Build** (and dead PlanGrid) do it. **OpenSpace** does it automatically with AI Autolocation (no manual pinning). **CompanyCam doesn't.** For Palmetto's luxury-custom-homes workflow with architect PDFs, this would be high-value but high-effort — you need the floor plan as a known asset per project. Realistic v2 feature, not v1.

**10. Mac/iPad native vs web — photo-workflow gap?**
**CompanyCam has no native Mac app** — web-only on desktop, plus a WebCatalog wrapper. iPad is supported (universal iOS binary). **Fieldwire** has native iPad with offline plan caching. The Mac gap is real for office workflows (sales/admin sorting photos, building proposals) — but most office users do this in browsers, so the gap is theoretical, not painful. iPad-native is more important. Chad's app is already native Swift — that's a structural advantage over CompanyCam's web-on-desktop.

---

## 5. Gap analysis vs Chad's app

| Capability | CompanyCam | Chad today | Verdict |
|---|---|---|---|
| GPS auto-tag photo | Yes | No (photos aren't first-class) | **Build** |
| Auto-bind to project via geofence | Pseudo (GPS+address match) | No | **Build** (better than CC — true geofence + active-project list is small) |
| Before/after with ghost align | Yes | No | **Build** (S effort, huge sales value) |
| Voice-narrated walkthrough → PDF | Sidekick Walkthrough | Site-log agent does text only | **Extend** (add photos to existing agent) |
| AI photo captions | Quick Captions | No | **Build** (cheap LLM call) |
| AI daily log from photos | Yes | Partial (text-based morning brief, no photo→log) | **Extend** site-log agent |
| Photo report PDF generation | Yes | No | **Build** |
| Sub photo upload via SMS/QR | **No** | No | **Build — differentiator** |
| Receipt OCR → Cost Tracker | Document scan (no AI categorize) | **Yes** (receipt agent) | **Already ahead** |
| Annotation/drawing on photos | Yes | No | **Build** (M effort) |
| Tags + search across all photos | Yes | No | **Build** |
| LiDAR measurement | Yes (Elite, iPhone Pro) | No | **Skip v1** (cool but Palmetto won't measure on phone — they have laser tools) |
| Plans/drawings overlay | No | No | Skip (Fieldwire territory) |
| Vision-based progress % | No native | No | Skip v1 (OpenSpace territory, but viable v2 with GPT-4o-vision) |
| Marketing suite (social, reviews) | Yes (Elite) | No | Skip (not Palmetto's pain) |
| Per-user pricing | Yes | N/A | **Skip the model** — Chad's a single-tenant agent, not multi-tenant SaaS |

**Chad already does better:**
- Receipt → Google Sheets cost tracker (CC has no AI receipt categorization).
- Native Swift iOS (faster than CC's app on every device).
- AI morning brief (no equivalent in CC — they have daily log generation, but not a 6 AM email synthesis).

**Skip (not a fit for 2–6 active luxury custom-home projects):**
- LiDAR (overkill, laser tool exists)
- Plans overlay (Fieldwire's commercial play)
- Marketing suite (Palmetto doesn't need IG content from CC)
- Per-seat sub model (Chad is single-tenant)
- Photo CRM bloat (47 photos/day is CC's scale; Palmetto's volume is lower, value-per-photo higher)

---

## 6. Prioritized feature recommendations

Ranked for Chad's specific shape: one homebuilder, 2–6 projects, ~5 people, existing receipt + site-log agents.

| # | Feature | Effort | Why this matters |
|---|---|---|---|
| 1 | **GPS-geofence project auto-bind on photo** | S | Eliminates the "which job?" picker. With 2–6 active sites, CoreLocation region monitoring is trivial and the UX win is enormous. |
| 2 | **Photo → today's site log auto-attach** | S | Chad's site-log agent already exists; just add today's photos. CompanyCam doesn't even do this. |
| 3 | **Sub SMS/QR-magic-link photo upload (no account)** | M | True differentiator vs CompanyCam. Twilio MMS gateway + per-project magic link. Subs already text photos — meet them there. |
| 4 | **Before/after with ghost-align capture** | S | Visual proof of work for Palmetto's marketing/sales. Slide-to-reveal viewer + auto-pair suggestion. |
| 5 | **Voice-narrated walkthrough → PDF** | M | Extend the site-log agent: foreman talks while taking photos, AI fuses to a structured walkthrough PDF. Sidekick's flagship. |
| 6 | **AI photo captions (voice-driven, not vision-only)** | S | Cheap LLM call; pair Quick Caption pattern with the existing voice flow. |
| 7 | **Photo gallery PDF / shareable link per project** | M | Closeout package, client recap, marketing asset — single-button generation. |
| 8 | **Annotation / drawing on photo (arrows + text)** | M | Table-stakes for any photo doc product. Use PencilKit on iOS. |
| 9 | **Photo search / tag system** | M | Auto-extract tags via LLM from voice caption + GPS. Search across the project. |
| 10 | **AI vision progress phase detection (v2)** | L | GPT-4o-vision classifier mapping each photo to one of Palmetto's ~20 build phases. Enables progress tracking, schedule alerts, "framing complete" auto-events. Not v1. |
| 11 | **Plan overlay with photo pins (v3)** | L | Architect PDF per project, pin photos to x,y. High value for luxury custom but heavy lift. |
| 12 | **Receipt agent extension: photo-driven cost-code categorization** | S | Already have the receipt agent. Add LLM classification into the cost code structure. Closes the loop from CC's weakest spot. |

**Opinionated bet:** Build 1, 2, 3, 4, 6 in v1. That gives Chad most of CompanyCam's daily-driver value (auto-organize photos, before/after marketing, voice captions, photos in daily log) plus one thing CompanyCam doesn't have (sub SMS upload). 5 + 7 in v2. 10–11 are bigger swings — earn them by shipping v1 first.

---

## 7. Sources

- [CompanyCam AI Features page](https://companycam.com/ai-features)
- [CompanyCam Features](https://companycam.com/features)
- [CompanyCam Pricing](https://companycam.com/pricing)
- [Talk, Snap, Done: How CompanyCam AI Works on the Job (blog)](https://companycam.com/resources/blog/talk-snap-done-how-companycam-ai-works-on-the-job)
- [How AI Walkthrough Note Organizes Job Site Reports](https://companycam.com/resources/blog/how-ai-walkthrough-note-organizes-job-site-reports)
- [Working with Subcontractors in CompanyCam](https://companycam.com/resources/blog/working-with-subcontractors-in-companycam)
- [Project Collaborators & Guest Access](https://companycam.com/advanced-features/project-collaborators-guest-access)
- [Photo Gallery & Project Timelines](https://companycam.com/features/galleries-timelines)
- [Create a Before & After Photo (help center)](https://help.companycam.com/en/articles/6828372-create-a-before-after-photo)
- [Best LiDAR Apps for Contractors (CompanyCam blog)](https://companycam.com/resources/blog/best-lidar-apps-for-contractors)
- [Using LiDAR to Measure Photos](https://help.companycam.com/en/articles/12294390-using-lidar-to-measure-photos)
- [JobTread × CompanyCam Integration](https://www.jobtread.com/companycam)
- [CompanyCam Integrations](https://companycam.com/integrations)
- [CompanyCam Review 2026 — fieldcamp.ai](https://fieldcamp.ai/reviews/companycam/)
- [CompanyCam Pricing 2026 — Scan Manifold](https://www.scanmanifold.com/blog-posts/companycam-pricing-2026-complete-guide-f0fd7)
- [CompanyCam Capterra reviews](https://www.capterra.com/p/171143/CompanyCam/reviews/)
- [CompanyCam G2 Pricing](https://www.g2.com/products/companycam/pricing)
- [CompanyCam App Store reviews](https://apps.apple.com/us/app/id960043499?see-all=reviews)
- [Fieldwire pricing 2026 — Scan Manifold](https://www.scanmanifold.com/blog-posts/fieldwire-pricing-2026-comparison)
- [Fieldwire vs CompanyCam — Capterra Ireland](https://www.capterra.ie/compare/142801/171143/fieldwire/vs/companycam)
- [Raken Daily Reports](https://www.rakenapp.com/features/daily-reports)
- [Raken Photo Documentation](https://www.rakenapp.com/features/photo-documentation)
- [Raken review 2026 — constructionbids.ai](https://constructionbids.ai/blog/raken-app-review-daily-reports-construction)
- [OpenSpace](https://www.openspace.ai/)
- [OpenSpace Acquires Disperse (Oct 28, 2025)](https://www.openspace.ai/openspace-acquires-disperse/)
- [OpenSpace Field GA announcement](https://www.prnewswire.com/news-releases/openspace-announces-general-availability-of-openspace-field-bringing-visual-intelligence-directly-into-field-execution-302677091.html)
- [Autodesk Construction Cloud (PlanGrid successor)](https://construction.autodesk.com/products/plangrid/)
- [Moving from PlanGrid to Autodesk Build](https://resources.imaginit.com/building-solutions-blog/moving-from-plangrid-to-autodesk-build-what-to-expect)
- [Buildup Software profile (Software Advice)](https://www.softwareadvice.com/punch-list/buildup-profile/)
- [Site PhotoLog App Store](https://apps.apple.com/us/app/site-photolog/id6749919983)
- [FieldChat — sub SMS uploads](https://www.everythingbuildingenvelope.com/field-chat-text-messaging-for-construction-projects.htm)
- [Receipt AI — SMS/email receipt pattern](https://receipt-ai.com/construction_purchase_order)
- [iOS CoreLocation geofencing (Bugfender)](https://bugfender.com/blog/ios-geofencing/)
- [Automating CompanyCam Photo Descriptions with ChatGPT + Zapier](https://noahcoffey.com/blog/companycam-custom-integration-with-chatgpt/)
