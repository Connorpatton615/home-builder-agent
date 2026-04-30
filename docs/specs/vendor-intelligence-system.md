# Vendor Intelligence System

> One-line summary: scrape every vendor's catalog, normalize into a comparable schema, and recommend the best buy for any product based on price, lead time, stock, distance, and Chad's preferences — with a plain-English rationale.

**Status:** Spec — not yet scheduled. See "Open questions" below for blockers.
**Phase:** 3 (anchor initiative).
**Owner:** CP.
**Last updated:** 2026-04-29.

## Problem

Chad currently picks vendors by gut, spreadsheet, or memory. For any given product (paint, lumber, plumbing fitting, tile, appliance, hardware) he wants the agent to recommend the best vendor based on price, lead time, in-stock availability, distance, and his own preferred-vendor weight — with a plain-English rationale he can sanity-check in a glance.

The system has to match Chad's judgment, not override it: when he has a relationship reason to always buy fittings from Wholesale Plumbing, the agent honors that.

## Scope — four layers

### 1. Per-vendor scraper adapters

Vendor-by-vendor crawlers that pull each vendor's complete public catalog. For every SKU:

- SKU
- Product name
- Category
- Variants (size / color / finish)
- Unit
- Price
- In-stock status
- Lead time
- Vendor URL

Different adapters for different vendor types because catalog structures vary — paint store color libraries are nothing like lumber yard dimensional stock.

Initial adapter targets:

- Paint store
- Lumber yard
- Plumbing supply
- Electrical supply
- Tile / flooring
- Cabinet vendor
- Appliance distributor
- Hardware / general supply

### 2. Normalized product schema

All scraped data lands in one shared shape so cross-vendor comparison works. A "2x4x8 stud" from Yard A is comparable to one from Yard B even when they're named differently.

Matching strategy:

- **UPC / GTIN** where vendors expose it (deterministic).
- **Embeddings** for fuzzy matching the rest (similarity over name + variants + category).
- **Manual override table** for naming weirdness the model gets wrong.
- **Confidence scoring** on every match so Chad can spot when the system is guessing.

### 3. Decision engine

When Chad needs a product, multi-criteria optimization across:

- Price
- Lead time
- In-stock (today vs days out)
- Distance from job site
- Chad's preferred-vendor weight

Returns a ranked recommendation with a plain-English rationale.

Example:

> Buying the 5-gallon Sherwin-Williams Naval at Eastern Shore Paint — $18 cheaper than Daphne Paint and in stock today vs. a 3-day lead at Daphne.

### 4. Override / preference layer

Chad can pin product → vendor relationships ("always buy plumbing fittings from Wholesale Plumbing regardless of price") and the agent honors them. Capture his judgment, don't override it.

## Operational details

- **Vendor onboarding.** Chad uploads a list of his vendors (URLs + vendor type) and the system kicks off the right adapter per entry.
- **Refresh cadence.** Weekly default, more frequent for volatile-priced categories like lumber. Configurable per category.
- **Compliance.** Respect each vendor's TOS. Prefer official feeds / partner programs / API where available. Lean on publicly listed catalog endpoints over hidden APIs. Polite rate limits. Flag a legal review before production.
- **Strategic note.** This productizes — every blue-collar SMB has the same pain. Build it for Chad, template it, sell across Patton AI's customer base. Tier 2 / Tier 3 anchor feature in Patton AI's pricing model.

## Relationship to existing work

Phase 2 includes a **Supplier-email watcher** (item 3 in the Phase 2 backlog) that would scan supplier emails and auto-update `KNOWLEDGE BASE/baldwin_county_supplier_research.md`. That watcher is conceptually adjacent but takes a different ingestion path (parse inbound emails) than this initiative (crawl public catalogs).

Two ways they could fit together:

1. **Complementary feeders.** The email watcher captures one-off updates ("our 2x4 price went up Monday") and writes to the same normalized schema. Catalog scrapers handle bulk + standing data.
2. **Subsumed.** Vendor Intelligence's normalized schema becomes the authoritative store and the supplier-email watcher is reframed as one more adapter feeding into it.

Decision deferred until vendor list is in hand and we know how often suppliers actually email Chad with pricing vs. update their site.

## Open questions (TBD)

- **What vendors does Chad use today?** Need the list to prioritize adapters.
- **How does Chad currently make this call?** Gut, spreadsheet, "always cheapest", loyalty? Capture his pattern so the agent matches his judgment.
- **Single-tenant for Chad now, or multi-tenant from day one** for Patton AI's other future customers? Affects schema design (per-tenant overrides table vs global) and infra choices.
- **Refresh cadence preference per category.** Default weekly, but which categories does Chad want daily / hourly?
- **Legal review timing.** Before any production scraping, get a TOS-and-CFAA pass. Who signs off?
- **Where does the normalized catalog live?** Sheets (consistent with the rest of the stack)? Postgres? SQLite? Depends on row count once a real vendor list is in.
- **Distance computation source.** Google Distance Matrix API has cost; cached lookups by vendor address may be enough.

## What "done" looks like for the first slice

A defensible Phase 3 v0 that Chad can use:

1. Three adapters live (best guesses: paint store, lumber yard, plumbing supply — confirm with Chad).
2. Normalized schema in whatever store we pick, refreshed weekly.
3. CLI command (`hb-vendor "5gal Naval interior paint"`) that returns a ranked recommendation with rationale.
4. Override table Chad can edit in Sheets.
5. No surfacing in the timeline / order schedule yet — that's a follow-on once the recommendation engine is trusted.
