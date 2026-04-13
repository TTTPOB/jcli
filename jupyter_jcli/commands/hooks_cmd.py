"""jcli _hooks — internal hook handlers for Claude Code harness integration."""

import json
import re
import sys
from pathlib import Path

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


@hooks.command("notebook-exec-guard")
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


# ---------------------------------------------------------------------------
# pair-drift-guard
# ---------------------------------------------------------------------------

@hooks.command("pair-drift-guard")
def pair_drift_guard() -> None:
    """PreToolUse hook: detect py/ipynb pair drift and deny NotebookEdit."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)  # fail-open

    try:
        tool_name: str = payload.get("tool_name", "") or ""
        tool_input: dict = payload.get("tool_input", {}) or {}
        file_path: str = tool_input.get("file_path", "") or ""
    except (AttributeError, TypeError):
        sys.exit(0)  # fail-open

    # Policy: NotebookEdit is always denied — use py:percent round-trip instead
    if tool_name == "NotebookEdit":
        _print_decision(
            "deny",
            "NotebookEdit is disabled. Edit notebooks via py:percent round-trip:\n"
            "  1. j-cli convert ipynb-to-py <nb.ipynb> <nb.py>\n"
            "  2. Edit <nb.py> with normal text tools\n"
            "  3. j-cli convert py-to-ipynb <nb.py> <nb.ipynb>",
        )
        sys.exit(0)

    if not file_path:
        sys.exit(0)  # no file to check, allow

    path = Path(file_path)
    if not path.exists():
        sys.exit(0)  # new file, no drift possible

    try:
        _run_drift_check(tool_name, path)
    except Exception as exc:  # noqa: BLE001 — fail-open on any error
        print(f"pair-drift-guard: unexpected error: {exc}", file=sys.stderr)
        sys.exit(0)


def _run_drift_check(tool_name: str, path: Path) -> None:
    """Run drift check and emit a decision if action is needed."""
    from jupyter_jcli.parser import find_pair

    pair = find_pair(path)
    if pair is None:
        return  # not a paired file, allow

    # Determine which is py and which is ipynb
    if path.suffix == ".ipynb":
        py_path, ipynb_path = pair, path
    else:
        py_path, ipynb_path = path, pair

    if not py_path.exists() or not ipynb_path.exists():
        return  # one side missing, allow

    try:
        from jupyter_jcli.drift import check_drift
        result = check_drift(py_path, ipynb_path)
    except UnicodeDecodeError:
        print("pair-drift-guard: non-UTF-8 content, skipping drift check", file=sys.stderr)
        return
    except Exception:  # noqa: BLE001
        return

    if result.status == "in_sync":
        return  # no action needed

    if result.status in ("conflict", "drift_only"):
        idx_str = ", ".join(str(i) for i in result.conflict_indices)
        _print_decision(
            "ask",
            f"Pair drift detected between {py_path.name} and {ipynb_path.name}. "
            f"Conflicting cell indices: [{idx_str}]. "
            "Please resolve manually before proceeding.",
        )
        return

    if result.status == "merged":
        _apply_merge_and_decide(path, py_path, ipynb_path, result)


def _apply_merge_and_decide(
    target: Path,
    py_path: Path,
    ipynb_path: Path,
    result,  # DriftResult
) -> None:
    """Write merged content and emit allow/deny based on which file changed."""
    from jupyter_jcli.pair_io import emit_py_percent, update_ipynb_sources
    from jupyter_jcli.parser import parse_py_percent

    # Hash target before writing so we can detect write-time races
    try:
        target_before = target.read_bytes()
    except OSError:
        return

    wrote_target = False

    if result.py_needs_update:
        try:
            py_parsed = parse_py_percent(str(py_path))
            # Swap in merged cells, keep front_matter_raw
            from jupyter_jcli.parser import ParsedFile
            merged_parsed = ParsedFile(
                kernel_name=py_parsed.kernel_name,
                cells=result.merged_cells,
                source_path=py_parsed.source_path,
                front_matter_raw=py_parsed.front_matter_raw,
            )
            new_text = emit_py_percent(merged_parsed)
            # Check for races before writing
            if py_path.read_bytes() == target_before or py_path != target:
                py_path.write_text(new_text, encoding="utf-8")
                if py_path == target:
                    wrote_target = True
                else:
                    print(
                        f"pair-drift-guard: auto-synced {py_path.name} with merged content",
                        file=sys.stderr,
                    )
        except Exception as exc:  # noqa: BLE001
            print(f"pair-drift-guard: could not write {py_path.name}: {exc}", file=sys.stderr)

    if result.ipynb_needs_update:
        try:
            if target.read_bytes() == target_before or ipynb_path != target:
                update_ipynb_sources(ipynb_path, result.merged_cells)
                if ipynb_path == target:
                    wrote_target = True
                else:
                    print(
                        f"pair-drift-guard: auto-synced {ipynb_path.name} with merged content",
                        file=sys.stderr,
                    )
        except Exception as exc:  # noqa: BLE001
            print(f"pair-drift-guard: could not write {ipynb_path.name}: {exc}", file=sys.stderr)

    if wrote_target:
        # The file the agent is about to edit was rewritten — its cached content
        # (old_string) is now stale. Deny and ask for a re-read.
        _print_decision(
            "deny",
            f"Auto-merged pair drift into {target.name}. "
            f"Re-read {target} and retry your edit.",
        )


def _print_decision(decision: str, reason: str) -> None:
    print(
        json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        })
    )
