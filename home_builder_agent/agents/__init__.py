"""agents — each Phase 1 agent as a self-contained module.

Each agent module exports a `main()` callable that's the CLI entry point. The
pyproject.toml [project.scripts] block exposes them as shell commands:

    hb-timeline       (was: python3 agent_2_v1.py)
    hb-update         (was: python3 agent_2_5_update.py)
    hb-dashboard      (was: python3 agent_2_5_dashboard.py)
    hb-inbox          (was: python3 agent_1_gmail.py)

Adding a new agent: drop a `my_new_agent.py` here with a `main()`, add an
entry to pyproject.toml [project.scripts], `pip install -e .` to register.
"""
