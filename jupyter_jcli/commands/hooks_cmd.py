"""jcli _hooks — internal hook handlers for Claude Code harness integration."""

import json
import re
import sys

import click

# ---------------------------------------------------------------------------
# Guard patterns — each entry is (label, compiled_regex).
# A match on *any* pattern causes a deny.
# ---------------------------------------------------------------------------

GUARDS: list[tuple[str, re.Pattern[str]]] = [
    (
        "nbconvert --execute",
        re.compile(
            r"""
            (?:^|[\s;&|`(])                            # start-of-string or shell boundary
            (?:python\d?\s+-m\s+|uv\s+run\s+|!\s*)?   # optional python -m / uv run / shell-bang
            jupyter\s+nbconvert\b                      # the target command
            (?=.*?(?:\s--execute\b|\s--execute=))      # somewhere later: --execute flag
            """,
            re.IGNORECASE | re.VERBOSE | re.DOTALL,
        ),
    ),
    (
        "papermill",
        re.compile(
            r"(?:^|[\s;&|`(])(?:uv\s+run\s+)?papermill\b",
            re.IGNORECASE,
        ),
    ),
    (
        "runipy",
        re.compile(
            r"(?:^|[\s;&|`(])(?:uv\s+run\s+)?runipy\b",
            re.IGNORECASE,
        ),
    ),
    (
        "ipython run-notebook",
        re.compile(
            r"""
            (?:^|[\s;&|`(])
            (?:uv\s+run\s+)?ipython\b
            (?=.*?(?:%run\s+\S+\.ipynb|\s\S+\.ipynb\b))
            """,
            re.IGNORECASE | re.VERBOSE | re.DOTALL,
        ),
    ),
]

_HINT = (
    "`{label}` is intercepted by j-cli. Use j-cli instead:\n"
    "  1. j-cli healthcheck\n"
    "  2. j-cli session list           # reuse an existing session when possible\n"
    "  3. j-cli session create --kernel <spec> --path <file>   # only if none fits\n"
    "  4. j-cli exec <session_id> --file <notebook-or-py> [--cell N | --cell N:M | --cell N: | --cell :M]   # 0-indexed slice\n"
    "See the `j-cli` skill for the full workflow."
)


@click.group(hidden=True)
def hooks():
    """Internal hook handlers (not intended for direct use)."""


@hooks.command("nbconvert-guard")
def nbconvert_guard():
    """PreToolUse hook: deny notebook-execution bypass tools and redirect to j-cli."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Fail-open — malformed stdin must not brick the harness.
        sys.exit(0)

    command: str = ""
    try:
        command = payload.get("tool_input", {}).get("command", "") or ""
    except (AttributeError, TypeError):
        sys.exit(0)

    for label, pattern in GUARDS:
        if pattern.search(command):
            decision = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": _HINT.format(label=label),
                }
            }
            print(json.dumps(decision))
            sys.exit(0)

    # No match — allow (empty stdout).
    sys.exit(0)
