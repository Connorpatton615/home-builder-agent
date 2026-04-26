"""home_builder_agent — multi-agent AI system for Chad's Custom Homes.

See README.md at the repo root for the architecture overview. The package is
organized as:

    home_builder_agent/
        config.py          — paths, scopes, model names, pricing (single source of truth)
        core/              — cross-cutting concerns: auth, Claude client, KB loader
        integrations/      — Google API wrappers (Drive, Docs, Sheets, Gmail)
        agents/            — the 4 Phase 1 agents, each as a module
        watchers/          — long-running poll loops invoked by launchd

Each agent is self-contained at the CLI level (has its own main()) but shares
core + integration modules. Adding a new agent = drop a new file in agents/
and import the helpers it needs.
"""

__version__ = "0.2.0"
