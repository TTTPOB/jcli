"""jcli _hooks — internal hook handlers for agent harness integration (Claude Code / Codex).

Codex hook schema sources:
  https://developers.openai.com/codex/hooks
  https://github.com/openai/codex/tree/main/codex-rs/hooks/schema/generated
  openai/codex#2578 — apply_patch Lark grammar
"""

import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import click

from jupyter_jcli.cli import CliContext, pass_ctx

from .debug import HookDebugLogger, read_hook_stdin
from .decision import (
    HookDecision,
    PostToolUseContext,
    PreToolUseDecision,
    PreToolUseOutcome,
)
from .pair_drift import (
    _merge_post_contexts,
    _run_post_drift_check,
    _run_pre_drift_check,
)
from .payload import (
    _extract_bash_command_claude,
    _extract_bash_command_codex,
    _extract_file_path_claude,
    _extract_file_paths_codex,
)
from .pre_commit import _run_pre_commit_pair_sync

_T = TypeVar("_T")

# ---------------------------------------------------------------------------
# Guard patterns — each entry is (label, compiled_regex).
# A match on *any* pattern causes a deny.
# ---------------------------------------------------------------------------

_HINT = (
    "`{label}` is intercepted by j-cli. Use j-cli instead:\n"
    "  1. j-cli healthcheck\n"
    "  2. j-cli session list           # reuse an existing session when possible\n"
    "  3. j-cli session create --kernel <spec> --path <file>   # only if none fits\n"
    "  4. j-cli exec <session_selector> --file <notebook-or-py> [--cell N | --cell N:M | --cell N: | --cell :M]   # 0-indexed slice\n"
    "See the `j-cli` skill for the full workflow."
)


@click.group(hidden=True)
def hooks():
    """Internal hook handlers (not intended for direct use)."""


def _run_guard(
    hook_name: str,
    debug: bool,
    log_dir: Path,
    *,
    extract: Callable[[dict], _T],
    handle: Callable[[_T, HookDebugLogger], None],
) -> None:
    """Run one hook handler with shared stdin/logging/exit plumbing.

    Reads the hook payload from stdin, extracts the interesting value, and
    hands it to *handle*.  Malformed input or an unexpected payload shape
    exits silently with 0 — a hook must never break the agent harness.
    """
    with HookDebugLogger(hook_name, enabled=debug, log_dir=log_dir) as log:
        try:
            payload = read_hook_stdin(log)
        except (json.JSONDecodeError, ValueError):
            sys.exit(0)

        try:
            value = extract(payload)
        except (AttributeError, TypeError) as exc:
            log.record_exception(exc)
            sys.exit(0)

        handle(value, log)

    sys.exit(0)


def _check_exec_guard(sc) -> str | None:
    """Return the guard label if *sc* should be denied, else ``None``.

    Checks for: jupyter nbconvert --execute, papermill, runipy,
    ipython with a notebook argument, and python -m jupyter nbconvert --execute.
    """
    name = sc.name.lower()
    args = sc.args

    if name == "jupyter":
        if (
            args
            and args[0] == "nbconvert"
            and any(a == "--execute" or a.startswith("--execute=") for a in args)
        ):
            return "nbconvert --execute"
        return None

    # python -m jupyter nbconvert --execute …
    if re.fullmatch(r"python\d*(?:\.\d+)?", name) and args and args[0] == "-m":
        rest = args[1:]
        if rest and rest[0] == "jupyter":
            from .parser import SimpleCommand

            inner = SimpleCommand(name="jupyter", args=rest[1:], assigns={}, raw=sc.raw)
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
@click.option("--platform", default="claude", help="Agent platform: claude or codex")
@click.option(
    "--debug",
    "debug",
    is_flag=True,
    default=False,
    help="Log stdin/stdout/stderr to /tmp/jcli-{uid}/notebook-exec-guard-{ts}.log.",
)
@pass_ctx
def nbconvert_guard(ctx: CliContext, platform: str, debug: bool):
    """PreToolUse hook: deny notebook-execution bypass tools and redirect to j-cli."""
    extract = (
        _extract_bash_command_codex
        if platform == "codex"
        else _extract_bash_command_claude
    )
    _run_guard(
        "notebook-exec-guard",
        debug,
        ctx.config.debug_log_dir,
        extract=extract,
        handle=_deny_bypass_commands,
    )


def _deny_bypass_commands(command: str, log: HookDebugLogger) -> None:
    """Deny notebook-execution bypass commands parsed from *command*."""
    from .parser import iter_simple_commands, unwrap_runner

    try:
        simple_commands = iter_simple_commands(command)
    except Exception as exc:  # noqa: BLE001
        log.record_exception(exc)
        sys.exit(0)

    for sc in simple_commands:
        inner = unwrap_runner(sc)
        label = _check_exec_guard(inner)
        if label is not None:
            _emit_decision(
                PreToolUseDecision(PreToolUseOutcome.DENY, _HINT.format(label=label)),
                logger=log,
            )
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
    "  4. j-cli exec <session_selector> --file {file} [--cell N | --cell N:M]\n\n"
    "If you truly need a one-shot script execution (e.g. the file also doubles as\n"
    "a CLI entrypoint), rename the entrypoint so it no longer shadows the notebook\n"
    "pair, or invoke it via `python -m <module>` to make the intent explicit."
)


@hooks.command("python-run-guard")
@click.option("--platform", default="claude", help="Agent platform: claude or codex")
@click.option(
    "--debug",
    "debug",
    is_flag=True,
    default=False,
    help="Log stdin/stdout/stderr to /tmp/jcli-{uid}/python-run-guard-{ts}.log.",
)
@pass_ctx
def python_run_guard(ctx: CliContext, platform: str, debug: bool):
    """PreToolUse hook: soft guard against running py:percent files as scripts."""
    extract_command = (
        _extract_bash_command_codex
        if platform == "codex"
        else _extract_bash_command_claude
    )

    def extract(payload: dict) -> tuple[str, str]:
        return (extract_command(payload), payload.get("cwd", "") or "")

    _run_guard(
        "python-run-guard",
        debug,
        ctx.config.debug_log_dir,
        extract=extract,
        handle=_deny_python_run,
    )


def _deny_python_run(command_and_cwd: tuple[str, str], log: HookDebugLogger) -> None:
    """Deny running a py:percent file that has a paired notebook."""
    command, cwd = command_and_cwd
    cwd_path = Path(cwd) if cwd else Path.cwd()

    from jupyter_jcli.parser import find_paired_ipynb

    from .parser import (
        extract_script_target,
        iter_simple_commands,
        unwrap_runner,
    )

    try:
        simple_commands = iter_simple_commands(command)
    except Exception as exc:  # noqa: BLE001
        log.record_exception(exc)
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
        except Exception as exc:  # noqa: BLE001
            log.record_exception(exc)
            sys.exit(0)
        if ipynb is not None:
            _emit_decision(
                PreToolUseDecision(
                    PreToolUseOutcome.DENY,
                    _PYTHON_HINT.format(
                        label="python script",
                        file=file_str,
                        ipynb=ipynb.name,
                    ),
                ),
                logger=log,
            )
            sys.exit(0)


# ---------------------------------------------------------------------------
# pair-drift-guard-pre  (PreToolUse — detects drift that existed before agent's edit)
# ---------------------------------------------------------------------------


_IPYNB_EDIT_DENY_TEMPLATE = (
    "{verb} of `{name}` is not supported — edit notebooks "
    "via the py:percent round-trip instead:\n"
    "  1. j-cli convert ipynb-to-py {name} {stem}.py\n"
    "  2. Edit {stem}.py{edit_suffix}\n"
    "  3. j-cli convert py-to-ipynb {stem}.py {name}\n"
    "(Outputs in the `.ipynb` are preserved through the round-trip.)"
)


def _ipynb_edit_deny_message(path: Path, *, with_edit_tool: bool) -> str:
    return _IPYNB_EDIT_DENY_TEMPLATE.format(
        verb="Direct Edit/Write" if with_edit_tool else "apply_patch",
        name=path.name,
        stem=path.stem,
        edit_suffix=" with Edit/Write" if with_edit_tool else "",
    )


@hooks.command("pair-drift-guard-pre")
@click.option("--platform", default="claude", help="Agent platform: claude or codex")
@click.option(
    "--debug",
    "debug",
    is_flag=True,
    default=False,
    help="Log stdin/stdout/stderr to /tmp/jcli-{uid}/pair-drift-guard-pre-{ts}.log.",
)
@pass_ctx
def pair_drift_guard_pre(ctx: CliContext, platform: str, debug: bool) -> None:
    """PreToolUse hook: detect pre-existing py/ipynb pair drift before an edit."""
    if platform == "codex":
        _run_guard(
            "pair-drift-guard-pre",
            debug,
            ctx.config.debug_log_dir,
            extract=_extract_file_paths_codex,
            handle=_deny_drift_pre_multi,
        )
    else:
        _run_guard(
            "pair-drift-guard-pre",
            debug,
            ctx.config.debug_log_dir,
            extract=_extract_file_path_claude,
            handle=_deny_drift_pre_single,
        )


def _deny_drift_pre_single(file_path: str, log: HookDebugLogger) -> None:
    """Claude Code: deny pre-existing drift for one edited file."""
    if not file_path:
        sys.exit(0)

    path = Path(file_path)

    if path.suffix == ".ipynb":
        _emit_decision(
            PreToolUseDecision(
                PreToolUseOutcome.DENY,
                _ipynb_edit_deny_message(path, with_edit_tool=True),
            ),
            logger=log,
        )
        sys.exit(0)

    if not path.exists():
        sys.exit(0)

    try:
        deny_reason = _run_pre_drift_check(path, log)
        if deny_reason is not None:
            _emit_decision(
                PreToolUseDecision(PreToolUseOutcome.DENY, deny_reason),
                logger=log,
            )
            sys.exit(0)
    except Exception as exc:  # noqa: BLE001
        log.record_exception(exc)
        print(f"pair-drift-guard-pre: unexpected error: {exc}", file=sys.stderr)
        sys.exit(0)


def _deny_drift_pre_multi(file_paths: list[str], log: HookDebugLogger) -> None:
    """Codex: deny pre-existing drift for every apply_patch file."""
    if not file_paths:
        sys.exit(0)

    deny_reasons: list[str] = []

    for file_path in file_paths:
        path = Path(file_path)

        if path.suffix == ".ipynb":
            deny_reasons.append(_ipynb_edit_deny_message(path, with_edit_tool=False))
            continue

        if not path.exists():
            continue

        try:
            deny_reason = _run_pre_drift_check(path, log)
            if deny_reason is not None:
                deny_reasons.append(deny_reason)
        except Exception as exc:  # noqa: BLE001
            log.record_exception(exc)
            print(f"pair-drift-guard-pre: unexpected error: {exc}", file=sys.stderr)

    if deny_reasons:
        merged = "\n\n---\n\n".join(deny_reasons)
        _emit_decision(
            PreToolUseDecision(PreToolUseOutcome.DENY, merged),
            logger=log,
        )

    sys.exit(0)


def _emit_decision(decision: HookDecision, *, logger=None) -> None:
    payload = decision.to_payload()
    raw = json.dumps(payload)
    if logger is not None:
        logger.set_stdout(raw, payload)
    print(raw)


# ---------------------------------------------------------------------------
# notebook-edit-guard  (PreToolUse — hard-deny NotebookEdit)
# ---------------------------------------------------------------------------


@hooks.command("notebook-edit-guard")
@click.option("--platform", default="claude", help="Agent platform: claude or codex")
@click.option(
    "--debug",
    "debug",
    is_flag=True,
    default=False,
    help="Log stdin/stdout/stderr to /tmp/jcli-{uid}/notebook-edit-guard-{ts}.log.",
)
@pass_ctx
def notebook_edit_guard(ctx: CliContext, platform: str, debug: bool) -> None:
    """PreToolUse hook: hard-deny NotebookEdit; redirect to py:percent round-trip."""
    # Codex has no NotebookEdit tool — this guard only fires on Claude Code.
    # --platform accepted for interface uniformity; not used for dispatch.
    _run_guard(
        "notebook-edit-guard",
        debug,
        ctx.config.debug_log_dir,
        extract=lambda payload: payload.get("tool_name", "") or "",
        handle=_deny_notebook_edit,
    )


def _deny_notebook_edit(tool_name: str, log: HookDebugLogger) -> None:
    """Deny the NotebookEdit tool with the py:percent round-trip hint."""
    if tool_name != "NotebookEdit":
        sys.exit(0)

    _emit_decision(
        PreToolUseDecision(
            PreToolUseOutcome.DENY,
            "NotebookEdit is disabled in this project — edit notebooks via the "
            "py:percent round-trip instead:\n"
            "  1. j-cli convert ipynb-to-py <nb.ipynb> <nb.py>\n"
            "  2. Edit <nb.py> with Edit/Write\n"
            "  3. j-cli convert py-to-ipynb <nb.py> <nb.ipynb>\n"
            "(The paired `.py` round-trip preserves outputs and keeps the pair "
            "in sync via `pair-drift-guard-pre`.)",
        ),
        logger=log,
    )
    sys.exit(0)


# ---------------------------------------------------------------------------
# pair-drift-guard-post  (PostToolUse — auto-sync pair after agent's own edit)
# ---------------------------------------------------------------------------


@hooks.command("pair-drift-guard-post")
@click.option("--platform", default="claude", help="Agent platform: claude or codex")
@click.option(
    "--debug",
    "debug",
    is_flag=True,
    default=False,
    help="Log stdin/stdout/stderr to /tmp/jcli-{uid}/pair-drift-guard-post-{ts}.log.",
)
@pass_ctx
def pair_drift_guard_post(ctx: CliContext, platform: str, debug: bool) -> None:
    """PostToolUse hook: auto-sync py/ipynb pair after agent's own edit."""
    if platform == "codex":
        _run_guard(
            "pair-drift-guard-post",
            debug,
            ctx.config.debug_log_dir,
            extract=_extract_file_paths_codex,
            handle=_sync_drift_post_multi,
        )
    else:
        _run_guard(
            "pair-drift-guard-post",
            debug,
            ctx.config.debug_log_dir,
            extract=_extract_file_path_claude,
            handle=_sync_drift_post_single,
        )


def _sync_drift_post_single(file_path: str, log: HookDebugLogger) -> None:
    """Claude Code: auto-sync the pair after one edited file."""
    if not file_path:
        sys.exit(0)

    path = Path(file_path)

    if path.suffix == ".ipynb":
        sys.exit(0)

    if not path.exists():
        sys.exit(0)

    try:
        context_str = _run_post_drift_check(path, log)
        if context_str is not None:
            _emit_decision(PostToolUseContext(context_str), logger=log)
    except Exception as exc:  # noqa: BLE001
        log.record_exception(exc)
        print(f"pair-drift-guard-post: unexpected error: {exc}", file=sys.stderr)
        sys.exit(0)


def _sync_drift_post_multi(file_paths: list[str], log: HookDebugLogger) -> None:
    """Codex: auto-sync the pair for every apply_patch file."""
    if not file_paths:
        sys.exit(0)

    contexts: list[str] = []

    for file_path in file_paths:
        path = Path(file_path)

        if path.suffix == ".ipynb":
            continue

        if not path.exists():
            continue

        try:
            context_str = _run_post_drift_check(path, log)
            if context_str is not None:
                contexts.append(context_str)
        except Exception as exc:  # noqa: BLE001
            log.record_exception(exc)
            print(f"pair-drift-guard-post: unexpected error: {exc}", file=sys.stderr)

    if contexts:
        merged = _merge_post_contexts(contexts)
        _emit_decision(PostToolUseContext(merged), logger=log)

    sys.exit(0)


# ---------------------------------------------------------------------------
# pre-commit-pair-sync
# ---------------------------------------------------------------------------


@hooks.command("pre-commit-pair-sync")
@click.option(
    "--include",
    "include_globs",
    multiple=True,
    metavar="GLOB",
    help="Only process .py files matching this glob (repeatable).",
)
@click.option(
    "--debug",
    "debug",
    is_flag=True,
    default=False,
    help="Log stdin/stdout/stderr to /tmp/jcli-{uid}/pre-commit-pair-sync-{ts}.log.",
)
@pass_ctx
def pre_commit_pair_sync(
    ctx: CliContext, include_globs: tuple[str, ...], debug: bool
) -> None:
    """Git pre-commit hook: sync py/ipynb pairs before commit."""
    with HookDebugLogger(
        "pre-commit-pair-sync", enabled=debug, log_dir=ctx.config.debug_log_dir
    ) as _log:
        _run_pre_commit_pair_sync(include_globs)


@hooks.command("gc-pair-sync-refs")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report stale refs without deleting them.",
)
def gc_pair_sync_refs(dry_run: bool) -> None:
    """Delete stale sticky pair-sync refs under refs/jcli/pair-sync."""
    from jupyter_jcli import pair_baseline
    from jupyter_jcli.gitutil import git_root

    repo_root = git_root(Path.cwd())
    if repo_root is None:
        print("gc-pair-sync-refs: not in a git repo, skipping", file=sys.stderr)
        sys.exit(0)

    refs = pair_baseline.list_all_refs(repo_root)
    for ref_info in refs:
        status, reason = pair_baseline._classify_ref(repo_root, ref_info)
        rel_display = ref_info.rel_posix_path or "<unknown>"
        if status == "keep":
            print(f"keep\t{rel_display}\t{reason}", file=sys.stderr)
        elif dry_run:
            print(f"would-remove\t{rel_display}\t{reason}", file=sys.stderr)
        else:
            print(f"remove\t{rel_display}\t{reason}", file=sys.stderr)

    removed, kept = pair_baseline.gc_stale_refs(repo_root, dry_run)
    print(f"removed {removed}, kept {kept}", file=sys.stderr)
    sys.exit(0)
