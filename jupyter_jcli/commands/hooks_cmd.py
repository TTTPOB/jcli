"""jcli _hooks — internal hook handlers for Claude Code harness integration."""

import fnmatch
import json
import re
import subprocess
import sys
from enum import Enum
from pathlib import Path

import click

from jupyter_jcli._enums import DriftStatus


class HookDecision(str, Enum):
    """Permission decision values for Claude Code hook payloads.

    Values are constrained by the Claude Code PreToolUse hook protocol.
    Changing them requires synchronising with the Claude Code harness.
    """
    DENY = "deny"
    ASK = "ask"
    ALLOW = "allow"


class HookEvent(str, Enum):
    """Hook event names emitted in hook payloads.

    Values are constrained by the Claude Code hook protocol.
    """
    PRE_TOOL_USE = "PreToolUse"

# ---------------------------------------------------------------------------
# Guard patterns — each entry is (label, compiled_regex).
# A match on *any* pattern causes a deny.
# ---------------------------------------------------------------------------

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


def _check_exec_guard(sc) -> str | None:
    """Return the guard label if *sc* should be denied, else ``None``.

    Checks for: jupyter nbconvert --execute, papermill, runipy,
    ipython with a notebook argument, and python -m jupyter nbconvert --execute.
    """
    name = sc.name.lower()
    args = sc.args

    if name == "jupyter":
        if args and args[0] == "nbconvert":
            if any(a == "--execute" or a.startswith("--execute=") for a in args):
                return "nbconvert --execute"
        return None

    # python -m jupyter nbconvert --execute …
    if re.fullmatch(r"python\d*(?:\.\d+)?", name) and args and args[0] == "-m":
        rest = args[1:]
        if rest and rest[0] == "jupyter":
            from jupyter_jcli.hooks_parser import SimpleCommand
            inner = SimpleCommand(
                name="jupyter", args=rest[1:], assigns={}, raw=sc.raw
            )
            return _check_exec_guard(inner)
        return None

    if name == "papermill":
        return "papermill"

    if name == "runipy":
        return "runipy"

    if name == "ipython":
        for a in args:
            if a.endswith(".ipynb"):
                return "ipython run-notebook"
            if "%run" in a and ".ipynb" in a:
                return "ipython run-notebook"

    return None


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

    from jupyter_jcli.hooks_parser import iter_simple_commands, unwrap_runner

    try:
        simple_commands = iter_simple_commands(command)
    except Exception:  # noqa: BLE001 — fail-open on parse error
        sys.exit(0)

    for sc in simple_commands:
        inner = unwrap_runner(sc)
        label = _check_exec_guard(inner)
        if label is not None:
            _print_decision(HookDecision.DENY, _HINT.format(label=label))
            sys.exit(0)

    # No match — allow (empty stdout).
    sys.exit(0)


# ---------------------------------------------------------------------------
# python-run-guard
# ---------------------------------------------------------------------------

_PYTHON_HINT = (
    "`{label}` on `{file}` would execute a py:percent file that has a paired\n"
    "notebook (`{ipynb}`). Reconsider — in most cases this is not what you want:\n"
    "running it as a script throws away kernel state, rich outputs, and the\n"
    "py/ipynb pair sync that j-cli maintains.\n\n"
    "Think carefully about intent. If you want to run the notebook's code against\n"
    "a live kernel (the common case), use a j-cli session instead:\n"
    "  1. j-cli healthcheck\n"
    "  2. j-cli session list           # reuse an existing session when possible\n"
    "  3. j-cli session create --kernel <spec> --path {file}\n"
    "  4. j-cli exec <session_id> --file {file} [--cell N | --cell N:M]\n\n"
    "If you truly need a one-shot script execution (e.g. the file also doubles as\n"
    "a CLI entrypoint), rename the entrypoint so it no longer shadows the notebook\n"
    "pair, or invoke it via `python -m <module>` to make the intent explicit."
)


@hooks.command("python-run-guard")
def python_run_guard():
    """PreToolUse hook: soft guard against running py:percent files as scripts."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)  # fail-open

    command: str = ""
    cwd: str = ""
    try:
        command = payload.get("tool_input", {}).get("command", "") or ""
        cwd = payload.get("cwd", "") or ""
    except (AttributeError, TypeError):
        sys.exit(0)  # fail-open

    cwd_path = Path(cwd) if cwd else Path.cwd()

    from jupyter_jcli.hooks_parser import extract_script_target, iter_simple_commands, unwrap_runner
    from jupyter_jcli.parser import find_paired_ipynb

    try:
        simple_commands = iter_simple_commands(command)
    except Exception:  # noqa: BLE001 — fail-open on parse error
        sys.exit(0)

    for sc in simple_commands:
        inner = unwrap_runner(sc)
        file_str = extract_script_target(inner)
        if file_str is None:
            continue
        try:
            file_path = Path(file_str)
            if not file_path.is_absolute():
                file_path = cwd_path / file_path
            ipynb = find_paired_ipynb(file_path)
        except Exception:  # noqa: BLE001 — fail-open on filesystem errors
            sys.exit(0)
        if ipynb is not None:
            _print_decision(
                HookDecision.DENY,
                _PYTHON_HINT.format(
                    label="python script",
                    file=file_str,
                    ipynb=ipynb.name,
                ),
            )
            sys.exit(0)

    # No paired notebook found — allow (empty stdout).
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

    if result.status == DriftStatus.IN_SYNC:
        return  # no action needed

    if result.status == DriftStatus.CONFLICT:
        idx_str = ", ".join(str(i) for i in result.conflict_indices)
        _print_decision(
            "deny",
            f"Pair conflict between {py_path.name} and {ipynb_path.name} "
            f"at cell(s) [{idx_str}] — both sides changed the same cell(s), "
            "auto-merge is not possible.\n"
            "Pick a side with j-cli convert:\n"
            f"  j-cli convert ipynb-to-py {ipynb_path.name} {py_path.name}"
            "   # take ipynb as truth\n"
            f"  j-cli convert py-to-ipynb {py_path.name} {ipynb_path.name}"
            "   # take py as truth",
        )
        return

    if result.status == DriftStatus.DRIFT_ONLY:
        _print_decision(
            "deny",
            f"No git base found; {py_path.name} and {ipynb_path.name} have diverged "
            "and cannot be auto-merged.\n"
            "Pick a side with j-cli convert:\n"
            f"  j-cli convert ipynb-to-py {ipynb_path.name} {py_path.name}"
            "   # take ipynb as truth\n"
            f"  j-cli convert py-to-ipynb {py_path.name} {ipynb_path.name}"
            "   # take py as truth",
        )
        return

    if result.status == DriftStatus.MERGED:
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


def _print_decision(decision: HookDecision, reason: str) -> None:
    print(
        json.dumps({
            "hookSpecificOutput": {
                "hookEventName": HookEvent.PRE_TOOL_USE,
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        })
    )


# ---------------------------------------------------------------------------
# pre-commit-pair-sync
# ---------------------------------------------------------------------------

@hooks.command("pre-commit-pair-sync")
@click.option(
    "--include", "include_globs", multiple=True, metavar="GLOB",
    help="Only process .py files matching this glob (repeatable).",
)
def pre_commit_pair_sync(include_globs: tuple[str, ...]) -> None:
    """Git pre-commit hook: sync py/ipynb pairs before commit."""

    # ------------------------------------------------------------------
    # Step 1: locate repo root (fail-open if git missing / not a repo)
    # ------------------------------------------------------------------
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False,
        )
        if top.returncode != 0:
            print("pre-commit-pair-sync: not in a git repo, skipping", file=sys.stderr)
            sys.exit(0)
        repo_root = Path(top.stdout.strip())
    except (OSError, FileNotFoundError):
        print("pre-commit-pair-sync: git not found in PATH, skipping", file=sys.stderr)
        sys.exit(0)

    # ------------------------------------------------------------------
    # Step 2: staged files
    # ------------------------------------------------------------------
    try:
        diff = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            capture_output=True, text=True, check=False,
            cwd=str(repo_root),
        )
        if diff.returncode != 0:
            print(
                "pre-commit-pair-sync: could not list staged files, skipping",
                file=sys.stderr,
            )
            sys.exit(0)
        staged_rel = [p for p in diff.stdout.splitlines() if p.strip()]
    except (OSError, FileNotFoundError):
        print("pre-commit-pair-sync: git not found in PATH, skipping", file=sys.stderr)
        sys.exit(0)

    # ------------------------------------------------------------------
    # Step 3: block staged .ipynb
    # ------------------------------------------------------------------
    staged_ipynb = [p for p in staged_rel if p.endswith(".ipynb")]
    if staged_ipynb:
        print(
            "pre-commit-pair-sync: staged .ipynb files found — "
            "unstage them and commit the .py pair instead:",
            file=sys.stderr,
        )
        for p in staged_ipynb:
            print(f"  {p}", file=sys.stderr)
        print("  Hint: git restore --staged <file>.ipynb", file=sys.stderr)
        sys.exit(1)

    # ------------------------------------------------------------------
    # Step 4: filter staged .py files
    # ------------------------------------------------------------------
    staged_py_rel = [p for p in staged_rel if p.endswith(".py")]
    if include_globs:
        staged_py_rel = [
            p for p in staged_py_rel
            if any(fnmatch.fnmatch(p, g) for g in include_globs)
        ]

    # ------------------------------------------------------------------
    # Step 5: process each candidate
    # ------------------------------------------------------------------
    from jupyter_jcli.parser import find_pair

    updated_py: list[str] = []
    updated_ipynb: list[str] = []
    conflicts: list[tuple[str, str, list[int]]] = []
    drifts: list[tuple[str, str]] = []

    for rel_path in staged_py_rel:
        py_path = repo_root / rel_path

        pair = find_pair(py_path)
        if pair is None:
            continue
        ipynb_path = pair

        # Initial sync: .py missing on disk but .ipynb exists
        if not py_path.exists() and ipynb_path.exists():
            try:
                from jupyter_jcli.parser import parse_ipynb
                from jupyter_jcli.pair_io import emit_py_percent
                parsed_nb = parse_ipynb(str(ipynb_path))
                py_text = emit_py_percent(parsed_nb)
                py_path.parent.mkdir(parents=True, exist_ok=True)
                py_path.write_text(py_text, encoding="utf-8")
                subprocess.run(
                    ["git", "add", str(py_path)],
                    check=False, cwd=str(repo_root),
                )
                updated_py.append(rel_path)
                print(
                    f"pre-commit-pair-sync: initial sync "
                    f"{py_path.name} from {ipynb_path.name}",
                    file=sys.stderr,
                )
            except UnicodeDecodeError:
                print(
                    f"pre-commit-pair-sync: non-UTF-8 content in {ipynb_path.name}",
                    file=sys.stderr,
                )
                sys.exit(1)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"pre-commit-pair-sync: error syncing {py_path.name}: {exc}",
                    file=sys.stderr,
                )
                sys.exit(1)
            continue

        if not py_path.exists() or not ipynb_path.exists():
            continue

        # Drift check (fail-closed for decode/format errors)
        try:
            from jupyter_jcli.drift import check_drift
            result = check_drift(py_path, ipynb_path)
        except UnicodeDecodeError:
            print(
                f"pre-commit-pair-sync: non-UTF-8 content in "
                f"{py_path.name}/{ipynb_path.name}",
                file=sys.stderr,
            )
            sys.exit(1)
        except Exception as exc:  # noqa: BLE001
            print(
                f"pre-commit-pair-sync: error checking {py_path.name}: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)

        if result.status == DriftStatus.IN_SYNC:
            continue

        if result.status == DriftStatus.MERGED:
            if result.py_needs_update:
                try:
                    from jupyter_jcli.parser import parse_py_percent, ParsedFile
                    from jupyter_jcli.pair_io import emit_py_percent
                    py_parsed = parse_py_percent(str(py_path))
                    merged_parsed = ParsedFile(
                        kernel_name=py_parsed.kernel_name,
                        cells=result.merged_cells,
                        source_path=py_parsed.source_path,
                        front_matter_raw=py_parsed.front_matter_raw,
                    )
                    py_path.write_text(emit_py_percent(merged_parsed), encoding="utf-8")
                    subprocess.run(
                        ["git", "add", str(py_path)],
                        check=False, cwd=str(repo_root),
                    )
                    updated_py.append(rel_path)
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"pre-commit-pair-sync: could not write {py_path.name}: {exc}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
            if result.ipynb_needs_update:
                try:
                    from jupyter_jcli.pair_io import update_ipynb_sources
                    update_ipynb_sources(ipynb_path, result.merged_cells)
                    try:
                        ipynb_rel = str(ipynb_path.relative_to(repo_root))
                    except ValueError:
                        ipynb_rel = str(ipynb_path)
                    updated_ipynb.append(ipynb_rel)
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"pre-commit-pair-sync: could not write {ipynb_path.name}: {exc}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
            continue

        if result.status == DriftStatus.CONFLICT:
            try:
                ipynb_rel = str(ipynb_path.relative_to(repo_root))
            except ValueError:
                ipynb_rel = str(ipynb_path)
            conflicts.append((rel_path, ipynb_rel, result.conflict_indices))
            continue

        if result.status == DriftStatus.DRIFT_ONLY:
            try:
                ipynb_rel = str(ipynb_path.relative_to(repo_root))
            except ValueError:
                ipynb_rel = str(ipynb_path)
            drifts.append((rel_path, ipynb_rel))

    # ------------------------------------------------------------------
    # Step 6: report and exit
    # ------------------------------------------------------------------
    if conflicts:
        print(
            "pre-commit-pair-sync: merge conflicts — "
            "resolve manually or pick a side via j-cli convert:",
            file=sys.stderr,
        )
        for py_rel, ipynb_rel, indices in conflicts:
            idx_str = ", ".join(str(i) for i in indices)
            print(
                f"  {py_rel} ↔ {ipynb_rel}  [conflict cells: {idx_str}]",
                file=sys.stderr,
            )
        print(
            "  j-cli convert ipynb-to-py <nb.ipynb> <nb.py>  "
            "OR  j-cli convert py-to-ipynb <nb.py> <nb.ipynb>",
            file=sys.stderr,
        )
        sys.exit(1)

    if drifts:
        print(
            "pre-commit-pair-sync: no git base to auto-merge; "
            "pick a side via j-cli convert:",
            file=sys.stderr,
        )
        for py_rel, ipynb_rel in drifts:
            print(f"  {py_rel} ↔ {ipynb_rel}", file=sys.stderr)
        print(
            "  j-cli convert ipynb-to-py <nb.ipynb> <nb.py>  "
            "OR  j-cli convert py-to-ipynb <nb.py> <nb.ipynb>",
            file=sys.stderr,
        )
        sys.exit(1)

    if updated_py:
        print(
            f"pre-commit-pair-sync: auto-synced .py: {', '.join(updated_py)}",
            file=sys.stderr,
        )
    if updated_ipynb:
        print(
            f"pre-commit-pair-sync: auto-synced .ipynb (not staged): "
            f"{', '.join(updated_ipynb)}",
            file=sys.stderr,
        )
