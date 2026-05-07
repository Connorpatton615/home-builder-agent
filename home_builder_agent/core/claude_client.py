"""claude_client.py — Anthropic client setup + cost reporting helpers.

Three concerns live here:
  1. Building a configured Anthropic client (loads .env, validates key)
  2. Computing per-call cost from a Usage object + the right pricing constants
  3. Pretty-printing a cost summary at the end of a run

Agents call `make_client()` once and use the helper functions when reporting
to stdout. Pricing constants live in config.py — DON'T duplicate them here.
"""

import os

from anthropic import Anthropic
from dotenv import load_dotenv

from home_builder_agent.config import (
    HAIKU_INPUT_COST,
    HAIKU_OUTPUT_COST,
    SONNET_CACHE_READ_COST,
    SONNET_CACHE_WRITE_COST,
    SONNET_INPUT_COST,
    SONNET_OUTPUT_COST,
    find_project_file,
)


def make_client():
    """Load .env (if present) and build an Anthropic client.

    Raises a clear error if ANTHROPIC_API_KEY isn't set, instead of the
    confusing 'unauthorized' error from Anthropic at first request time.
    """
    # find_project_file resolves .env via PACKAGE_ROOT first then falls
    # back to the canonical install dir — same pattern as credentials.json
    # and token.json — so agents work from any CWD (worktree, IDE, launchd).
    # override=True so a parent process injecting an EMPTY ANTHROPIC_API_KEY
    # (e.g. some IDE sessions) doesn't shadow the real value in .env.
    env_path = find_project_file(".env")
    load_dotenv(env_path, override=True)
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. Add it to .env in the project root, "
            "or export it in the shell before running."
        )
    return Anthropic(api_key=key)


def sonnet_cost(usage):
    """Compute total cost for a Sonnet call with optional cache hits.

    Returns a dict: { 'cache_write', 'cache_read', 'fresh_input', 'output', 'total' }
    Each value is a USD float. Use for printing a per-call cost line.
    """
    cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    fresh_in = usage.input_tokens
    out = usage.output_tokens

    cache_create_usd = cache_create * SONNET_CACHE_WRITE_COST / 1_000_000
    cache_read_usd = cache_read * SONNET_CACHE_READ_COST / 1_000_000
    in_usd = fresh_in * SONNET_INPUT_COST / 1_000_000
    out_usd = out * SONNET_OUTPUT_COST / 1_000_000

    return {
        "cache_write_tokens": cache_create,
        "cache_read_tokens": cache_read,
        "fresh_input_tokens": fresh_in,
        "output_tokens": out,
        "cache_write": cache_create_usd,
        "cache_read": cache_read_usd,
        "fresh_input": in_usd,
        "output": out_usd,
        "total": cache_create_usd + cache_read_usd + in_usd + out_usd,
    }


def haiku_cost(usage):
    """Compute total cost for a Haiku call. Returns USD float."""
    return (
        usage.input_tokens * HAIKU_INPUT_COST / 1_000_000
        + usage.output_tokens * HAIKU_OUTPUT_COST / 1_000_000
    )


def print_sonnet_cost_block(cost, label="Token usage:"):
    """Pretty-print a sonnet_cost() result. Used at end of agent runs."""
    print(label)
    if cost["cache_write_tokens"]:
        print(f"  Cache write:  {cost['cache_write_tokens']:>7,} tokens "
              f"(${cost['cache_write']:.4f})")
    if cost["cache_read_tokens"]:
        print(f"  Cache read:   {cost['cache_read_tokens']:>7,} tokens "
              f"(${cost['cache_read']:.4f})  ← 90% off")
    print(f"  Fresh input:  {cost['fresh_input_tokens']:>7,} tokens "
          f"(${cost['fresh_input']:.4f})")
    print(f"  Output:       {cost['output_tokens']:>7,} tokens "
          f"(${cost['output']:.4f})")
    print(f"  TOTAL:                        ${cost['total']:.4f}")
