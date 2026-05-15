# Patton Build Scaling Strategy Implementation

Source: `docs/strategy/Patton_Build_Scaling_Strategy_For_COO.pdf`

## Strategic Principle

Patton Build is a scalable operational intelligence platform for owner-operated businesses, beginning with custom home builders. Chad is the operational template and first scalable tenant implementation, not a bespoke software branch.

## Architecture Direction

Use two layers:

- Shared Platform Infrastructure: authentication, projects, schedules, notifications, memory systems, permissions, dashboards, AI orchestration, task systems, messaging, SOP engines, mobile shell, and operational feeds.
- Tenant Configuration Layer: customer branding, workflow terminology, SOPs, automations, communication preferences, project stages, operational templates, agent personalities, subcontractor flows, and reporting logic.

Tenant-specific behavior should be configurable through database rows, JSON schemas, prompt templates, feature flags, and workflow definitions. Avoid hardcoding customer-specific logic into application code.

## Product Loop Priority

The first critical loop is Builder Morning Feed. Every day the owner should immediately understand:

- What is late
- What is urgent
- What is blocked
- What needs decisions
- What is at risk
- What communication requires action

## Scaling Guardrails

- Target 80 percent shared infrastructure and 20 percent customer-specific configuration.
- Scale through reusable infrastructure, workflows, AI primitives, memory systems, and operational systems.
- Do not scale through one-off engineering, hardcoded client requests, or fully custom deployments.
- Prefer PostgreSQL-first memory now; add vector memory when the use case is real.
- Avoid premature infrastructure complexity before product-market fit.

## Engineering Review Checklist

- Does this change strengthen a shared platform primitive?
- Is tenant-specific logic represented as configuration?
- Can client #2 reuse this without copying code?
- Does telemetry capture usage and operational value?
- Does it improve operational awareness before attempting autonomous execution?
