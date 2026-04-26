---
description: Scaffold a new agent file with the package's import conventions
argument-hint: <agent-name>
---

Create a new agent at `home_builder_agent/agents/$ARGUMENTS.py` following the
project conventions documented in CLAUDE.md.

Before scaffolding:
1. Ask me what the agent does in 1-2 sentences.
2. Ask which integrations it needs (Drive? Docs? Sheets? Gmail? new one?).
3. Ask if it's user-triggered (an agent) or interval-polling (a watcher) — if the latter, redirect me to `/add-watcher` instead.

Then scaffold:
- A new file at `home_builder_agent/agents/$ARGUMENTS.py` with:
  - Module docstring describing what it does, the CLI, and cost expectation
  - Imports from `home_builder_agent.config`, `core.*`, `integrations.*`
  - A `main()` function with the standard structure (auth → KB load → API calls → cost summary)
- A new `[project.scripts]` entry in `pyproject.toml`:
  `hb-$ARGUMENTS = "home_builder_agent.agents.$ARGUMENTS:main"`
- A new entry in the agent table in CLAUDE.md
- A new slash command at `.claude/commands/$ARGUMENTS.md` if the agent will be invoked frequently

After scaffolding, remind me to run `pip install -e . --break-system-packages` so the new shell command becomes available.

Don't write the agent's core logic — leave a clearly-marked TODO block. The
scaffold is for structure, not content.
