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

from jupyter_jcli._enums import HookPlatform
from jupyter_jcli.cli import CliContext, pass_ctx

from .debug import HookDebugLogger, read_hook_stdin
from .decision import (
    HookDecision,
    HookOutcome,
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
    _extract_bash_command_dsh,
    _extract_dsh_bash_command_and_cwd,
    _extract_dsh_file_path,
    _extract_file_path_claude,
    _extract_file_paths_codex,
)
from .pre_commit import _run_pre_commit_pair_sync

_T = TypeVar("_T")


def _platform_option():
    """Validate platform values and pass enum members to hook callbacks."""
    return click.option(
        "--platform",
        type=click.Choice([p.value for p in HookPlatform]),
        default=HookPlatform.CLAUDE.value,
        callback=lambda _ctx, _param, value: HookPlatform(value),
        help="Agent platform.",
    )


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
    handle: Callable[[_T, HookDebugLogger], HookOutcome | None],
) -> None:
    """Run one hook handler with shared stdin/logging/exit plumbing.

    Handlers return a typed outcome.  Parsing, extraction, and unexpected
    handler failures are operation failures rather than silent allows.
    """
    with HookDebugLogger(hook_name, enabled=debug, log_dir=log_dir) as log:
        try:
            payload = read_hook_stdin(log)
            if not isinstance(payload, dict):
                raise TypeError("hook payload must be a JSON object")
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            log.record_exception(exc)
            outcome = HookOutcome.failure(f"malformed hook payload: {exc}")
        else:
            try:
                tool_input = payload.get("tool_input")
                if "tool_input" in payload and not isinstance(tool_input, dict):
                    raise TypeError("tool_input must be an object")
                tool_name = payload.get("tool_name")
                if "tool_name" in payload and not isinstance(tool_name, str):
                    raise TypeError("tool_name must be a string")
                cwd = payload.get("cwd")
                if "cwd" in payload and not isinstance(cwd, str):
                    raise TypeError("cwd must be a string")
                if isinstance(tool_input, dict):
                    command = tool_input.get("command")
                    if "command" in tool_input and not (
                        isinstance(command, str)
                        or (
                            isinstance(command, list)
                            and all(isinstance(item, str) for item in command)
                        )
                    ):
                        raise TypeError(
                            "tool_input.command must be a string or argv list"
                        )
                    file_path = tool_input.get("file_path")
                    if "file_path" in tool_input and not isinstance(file_path, str):
                        raise TypeError("tool_input.file_path must be a string")
                    workdir = tool_input.get("workdir")
                    if "workdir" in tool_input and not isinstance(workdir, str):
                        raise TypeError("tool_input.workdir must be a string")
                value = extract(payload)
            except (AttributeError, TypeError, ValueError) as exc:
                log.record_exception(exc)
                outcome = HookOutcome.failure(f"malformed hook payload: {exc}")
            else:
                try:
                    outcome = handle(value, log) or HookOutcome.success()
                except Exception as exc:  # noqa: BLE001
                    log.record_exception(exc)
                    outcome = HookOutcome.failure(str(exc) or type(exc).__name__)

        if outcome.diagnostic:
            print(f"{hook_name}: {outcome.diagnostic}", file=sys.stderr)
        raise SystemExit(int(outcome.code))


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
@_platform_option()
@click.option(
    "--debug",
    "debug",
    is_flag=True,
    default=False,
    help="Log stdin/stdout/stderr to /tmp/jcli-{uid}/notebook-exec-guard-{ts}.log.",
)
@pass_ctx
def nbconvert_guard(ctx: CliContext, platform: HookPlatform, debug: bool):
    """PreToolUse hook: deny notebook-execution bypass tools and redirect to j-cli."""
    extract = {
        HookPlatform.CLAUDE: _extract_bash_command_claude,
        HookPlatform.CODEX: _extract_bash_command_codex,
        HookPlatform.DSH: _extract_bash_command_dsh,
    }[platform]
    _run_guard(
        "notebook-exec-guard",
        debug,
        ctx.config.debug_log_dir,
        extract=extract,
        handle=_deny_bypass_commands,
    )


def _deny_bypass_commands(command: str, log: HookDebugLogger) -> HookOutcome:
    """Deny notebook-execution bypass commands parsed from *command*."""
    from .parser import iter_simple_commands, unwrap_runner

    simple_commands = iter_simple_commands(command)
    for sc in simple_commands:
        inner = unwrap_runner(sc)
        label = _check_exec_guard(inner)
        if label is not None:
            reason = _HINT.format(label=label)
            _emit_decision(
                PreToolUseDecision(PreToolUseOutcome.DENY, reason),
                logger=log,
            )
            return HookOutcome.denied(reason)
    return HookOutcome.success()


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
@_platform_option()
@click.option(
    "--debug",
    "debug",
    is_flag=True,
    default=False,
    help="Log stdin/stdout/stderr to /tmp/jcli-{uid}/python-run-guard-{ts}.log.",
)
@pass_ctx
def python_run_guard(ctx: CliContext, platform: HookPlatform, debug: bool):
    """PreToolUse hook: soft guard against running py:percent files as scripts."""
    if platform == HookPlatform.DSH:
        extract = _extract_dsh_bash_command_and_cwd
    else:
        extract_command = (
            _extract_bash_command_codex
            if platform == HookPlatform.CODEX
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


def _deny_python_run(
    command_and_cwd: tuple[str, str], log: HookDebugLogger
) -> HookOutcome:
    """Deny running a py:percent file that has a paired notebook."""
    command, cwd = command_and_cwd
    cwd_path = Path(cwd) if cwd else Path.cwd()

    from jupyter_jcli.parser import find_paired_ipynb

    from .parser import (
        extract_script_target,
        iter_simple_commands,
        unwrap_runner,
    )

    simple_commands = iter_simple_commands(command)
    for sc in simple_commands:
        inner = unwrap_runner(sc)
        file_str = extract_script_target(inner)
        if file_str is None:
            continue
        file_path = Path(file_str)
        if not file_path.is_absolute():
            file_path = cwd_path / file_path
        ipynb = find_paired_ipynb(file_path)
        if ipynb is not None:
            reason = _PYTHON_HINT.format(
                label="python script",
                file=file_str,
                ipynb=ipynb.name,
            )
            _emit_decision(
                PreToolUseDecision(PreToolUseOutcome.DENY, reason),
                logger=log,
            )
            return HookOutcome.denied(reason)
    return HookOutcome.success()


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
@_platform_option()
@click.option(
    "--debug",
    "debug",
    is_flag=True,
    default=False,
    help="Log stdin/stdout/stderr to /tmp/jcli-{uid}/pair-drift-guard-pre-{ts}.log.",
)
@pass_ctx
def pair_drift_guard_pre(ctx: CliContext, platform: HookPlatform, debug: bool) -> None:
    """PreToolUse hook: detect pre-existing py/ipynb pair drift before an edit."""
    if platform == HookPlatform.CODEX:
        _run_guard(
            "pair-drift-guard-pre",
            debug,
            ctx.config.debug_log_dir,
            extract=_extract_file_paths_codex,
            handle=_deny_drift_pre_multi,
        )
    elif platform == HookPlatform.DSH:
        _run_guard(
            "pair-drift-guard-pre",
            debug,
            ctx.config.debug_log_dir,
            extract=_extract_dsh_file_path,
            handle=_deny_drift_pre_single,
        )
    else:
        _run_guard(
            "pair-drift-guard-pre",
            debug,
            ctx.config.debug_log_dir,
            extract=_extract_file_path_claude,
            handle=_deny_drift_pre_single,
        )


def _deny_drift_pre_single(file_path: str, log: HookDebugLogger) -> HookOutcome:
    """Claude Code: deny pre-existing drift for one edited file."""
    if not file_path:
        return HookOutcome.success()

    path = Path(file_path)

    if path.suffix == ".ipynb":
        reason = _ipynb_edit_deny_message(path, with_edit_tool=True)
        _emit_decision(
            PreToolUseDecision(PreToolUseOutcome.DENY, reason),
            logger=log,
        )
        return HookOutcome.denied(reason)

    if not path.exists():
        return HookOutcome.success()

    deny_reason = _run_pre_drift_check(path, log)
    if deny_reason is not None:
        _emit_decision(
            PreToolUseDecision(PreToolUseOutcome.DENY, deny_reason),
            logger=log,
        )
        return HookOutcome.denied(deny_reason)
    return HookOutcome.success()


def _deny_drift_pre_multi(file_paths: list[str], log: HookDebugLogger) -> HookOutcome:
    """Codex: deny pre-existing drift for every apply_patch file."""
    if not file_paths:
        return HookOutcome.success()

    deny_reasons: list[str] = []

    for file_path in file_paths:
        path = Path(file_path)

        if path.suffix == ".ipynb":
            deny_reasons.append(_ipynb_edit_deny_message(path, with_edit_tool=False))
            continue

        if not path.exists():
            continue

        deny_reason = _run_pre_drift_check(path, log)
        if deny_reason is not None:
            deny_reasons.append(deny_reason)

    if deny_reasons:
        merged = "\n\n---\n\n".join(deny_reasons)
        _emit_decision(
            PreToolUseDecision(PreToolUseOutcome.DENY, merged),
            logger=log,
        )
        return HookOutcome.denied(merged)
    return HookOutcome.success()


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
@_platform_option()
@click.option(
    "--debug",
    "debug",
    is_flag=True,
    default=False,
    help="Log stdin/stdout/stderr to /tmp/jcli-{uid}/notebook-edit-guard-{ts}.log.",
)
@pass_ctx
def notebook_edit_guard(ctx: CliContext, platform: HookPlatform, debug: bool) -> None:
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


def _deny_notebook_edit(tool_name: str, log: HookDebugLogger) -> HookOutcome:
    """Deny the NotebookEdit tool with the py:percent round-trip hint."""
    if tool_name != "NotebookEdit":
        return HookOutcome.success()

    reason = (
        "NotebookEdit is disabled in this project — edit notebooks via the "
        "py:percent round-trip instead:\n"
        "  1. j-cli convert ipynb-to-py <nb.ipynb> <nb.py>\n"
        "  2. Edit <nb.py> with Edit/Write\n"
        "  3. j-cli convert py-to-ipynb <nb.py> <nb.ipynb>\n"
        "(The paired `.py` round-trip preserves outputs and keeps the pair "
        "in sync via `pair-drift-guard-pre`.)"
    )
    _emit_decision(
        PreToolUseDecision(PreToolUseOutcome.DENY, reason),
        logger=log,
    )
    return HookOutcome.denied(reason)


# ---------------------------------------------------------------------------
# pair-drift-guard-post  (PostToolUse — auto-sync pair after agent's own edit)
# ---------------------------------------------------------------------------


@hooks.command("pair-drift-guard-post")
@_platform_option()
@click.option(
    "--debug",
    "debug",
    is_flag=True,
    default=False,
    help="Log stdin/stdout/stderr to /tmp/jcli-{uid}/pair-drift-guard-post-{ts}.log.",
)
@pass_ctx
def pair_drift_guard_post(ctx: CliContext, platform: HookPlatform, debug: bool) -> None:
    """PostToolUse hook: auto-sync py/ipynb pair after agent's own edit."""
    if platform == HookPlatform.CODEX:
        _run_guard(
            "pair-drift-guard-post",
            debug,
            ctx.config.debug_log_dir,
            extract=_extract_file_paths_codex,
            handle=_sync_drift_post_multi,
        )
    elif platform == HookPlatform.DSH:
        _run_guard(
            "pair-drift-guard-post",
            debug,
            ctx.config.debug_log_dir,
            extract=_extract_dsh_file_path,
            handle=_sync_drift_post_single,
        )
    else:
        _run_guard(
            "pair-drift-guard-post",
            debug,
            ctx.config.debug_log_dir,
            extract=_extract_file_path_claude,
            handle=_sync_drift_post_single,
        )


def _sync_drift_post_single(file_path: str, log: HookDebugLogger) -> HookOutcome:
    """Claude Code: auto-sync the pair after one edited file."""
    if not file_path:
        return HookOutcome.success()

    path = Path(file_path)

    if path.suffix == ".ipynb" or not path.exists():
        return HookOutcome.success()

    notice = _run_post_drift_check(path, log)
    if notice is None:
        return HookOutcome.success()
    _emit_decision(PostToolUseContext(notice.context), logger=log)
    return notice.outcome


def _sync_drift_post_multi(file_paths: list[str], log: HookDebugLogger) -> HookOutcome:
    """Codex: auto-sync the pair for every apply_patch file."""
    if not file_paths:
        return HookOutcome.success()

    contexts: list[str] = []
    diagnostics: list[str] = []

    for file_path in file_paths:
        path = Path(file_path)

        if path.suffix == ".ipynb" or not path.exists():
            continue

        try:
            notice = _run_post_drift_check(path, log)
        except Exception as exc:  # noqa: BLE001
            log.record_exception(exc)
            diagnostics.append(f"{path.name}: {exc}")
            continue
        if notice is not None:
            contexts.append(notice.context)
            if notice.outcome.diagnostic:
                diagnostics.append(notice.outcome.diagnostic)

    if contexts:
        merged = _merge_post_contexts(contexts)
        _emit_decision(PostToolUseContext(merged), logger=log)

    if diagnostics:
        return HookOutcome.failure("; ".join(diagnostics))
    return HookOutcome.success()


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
    ) as log:
        try:
            outcome = _run_pre_commit_pair_sync(include_globs)
        except Exception as exc:  # noqa: BLE001
            log.record_exception(exc)
            outcome = HookOutcome.failure(str(exc) or type(exc).__name__)
        if outcome.diagnostic:
            print(f"pre-commit-pair-sync: {outcome.diagnostic}", file=sys.stderr)
        raise SystemExit(int(outcome.code))


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
    from jupyter_jcli.gitutil import resolve_git_root

    root_result = resolve_git_root(Path.cwd())
    if root_result.error is not None:
        print(
            f"gc-pair-sync-refs: git lookup failed: {root_result.error}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    repo_root = root_result.root
    if repo_root is None:
        print("gc-pair-sync-refs: not in a git repo, skipping", file=sys.stderr)
        return

    try:
        refs = pair_baseline.list_all_refs(repo_root, strict=True)
        for ref_info in refs:
            status, reason = pair_baseline._classify_ref(
                repo_root, ref_info, strict=True
            )
            rel_display = ref_info.rel_posix_path or "<unknown>"
            if status == "keep":
                print(f"keep\t{rel_display}\t{reason}", file=sys.stderr)
            elif dry_run:
                print(f"would-remove\t{rel_display}\t{reason}", file=sys.stderr)
            else:
                print(f"remove\t{rel_display}\t{reason}", file=sys.stderr)

        removed, kept = pair_baseline.gc_stale_refs(repo_root, dry_run, strict=True)
    except Exception as exc:
        print(f"gc-pair-sync-refs: git operation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"removed {removed}, kept {kept}", file=sys.stderr)
