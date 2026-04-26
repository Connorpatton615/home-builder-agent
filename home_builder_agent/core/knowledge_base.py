"""knowledge_base.py — load Baldwin County reference + Chad communication rules.

Knowledge base files are MARKDOWN files in Drive that every agent reads at
runtime. The runtime read pattern means CP can edit a KB file in Drive and
the next agent run picks up the change — no code change, no redeploy.

Three KB files in Phase 1:
  - baldwin_county_construction_reference.md  (codes, climate, permitting)
  - baldwin_county_supplier_research.md       (verified luxury suppliers)
  - chad_communication_rules.md               (DISC-tuned voice rules)

Loaders return empty string on missing file (with stderr warning), so a
partial KB doesn't crash the agent — it just produces a less-grounded output.
"""

import os
import sys

from home_builder_agent.config import (
    COMM_RULES_FILE,
    CONSTRUCTION_REF_FILE,
    KNOWLEDGE_BASE_DIR,
    SUPPLIER_REF_FILE,
    WORKSPACE,
)


def _read_kb_file(filename):
    """Read one KB file from WORKSPACE/KNOWLEDGE BASE/. Return text or ''."""
    path = os.path.join(WORKSPACE, KNOWLEDGE_BASE_DIR, filename)
    if not os.path.exists(path):
        print(f"  WARNING: knowledge base file not found: {path}",
              file=sys.stderr)
        return ""
    with open(path) as f:
        return f.read()


def load_construction_reference():
    """Return the Baldwin County construction reference markdown."""
    return _read_kb_file(CONSTRUCTION_REF_FILE)


def load_supplier_research():
    """Return the Baldwin County luxury supplier research markdown."""
    return _read_kb_file(SUPPLIER_REF_FILE)


def load_comm_rules():
    """Return Chad's DISC-tuned communication rules markdown."""
    return _read_kb_file(COMM_RULES_FILE)


def load_full_kb():
    """Load all three KB files at once. Returns (construction, supplier, comm).

    Convenience for agents that need everything (timeline generator).
    Single call also makes the loading log lines appear together rather than
    interleaved with other output.
    """
    construction = load_construction_reference()
    supplier = load_supplier_research()
    comm = load_comm_rules()

    print(f"  Loaded construction reference: {len(construction):,} chars")
    print(f"  Loaded supplier research:      {len(supplier):,} chars")
    print(f"  Loaded communication rules:    {len(comm):,} chars")

    return construction, supplier, comm
