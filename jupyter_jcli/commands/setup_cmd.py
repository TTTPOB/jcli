"""jcli setup — install integrations (e.g. Claude Code hooks)."""

import json
import os
import re
import shlex
import subprocess
from enum import Enum
from pathlib import Path

import click

from jupyter_jcli._enums import ResponseStatus
from jupyter_jcli.cli import Context, pass_ctx
from jupyter_jcli.output import emit, emit_error


class Scope(str, Enum):
    """Target scope for settings files written by setup commands."""
    USER = "user"
    PROJECT = "project"
    LOCAL = "local"

# ---------------------------------------------------------------------------
# Managed hook blocks
#
# Each block descriptor has:
#   matcher   - PreToolUse matcher string
#   entry     - the hook entry dict to install (must contain _jcli_managed key)
#   legacy    - frozenset of old _jcli_managed values to replace on upgrade
# ---------------------------------------------------------------------------

_MANAGED_KEY = "_jcli_managed"

_MANAGED_BLOCKS: list[dict] = [
    {
        "matcher": "Bash",
        "entry": {
            "type": "command",
            "command": "j-cli _hooks notebook-exec-guard",
            "_jcli_managed": "notebook-exec-guard",
        },
        "legacy": frozenset({"nbconvert-guard"}),
    },
    {
        "matcher": "Edit|Write",
        "entry": {
            "type": "command",
            "command": "j-cli _hooks pair-drift-guard",
            "_jcli_managed": "pair-drift-guard",
        },
        "legacy": frozenset(),
    },
    {
        "matcher": "NotebookEdit",
        "entry": {
            "type": "command",
            "command": "j-cli _hooks pair-drift-guard",
            "_jcli_managed": "pair-drift-guard-notebook",
        },
        "legacy": frozenset(),
    },
    {
        "matcher": "Bash",
        "entry": {
            "type": "command",
            "command": "j-cli _hooks python-run-guard",
            "_jcli_managed": "python-run-guard",
        },
        "legacy": frozenset(),
    },
]

# All managed values across all blocks (current + legacy) — used for upgrade detection
_ALL_MANAGED_VALS: frozenset[str] = frozenset(
    val
    for block in _MANAGED_BLOCKS
    for val in (
        {block["entry"][_MANAGED_KEY]}
        | block["legacy"]
    )
)


@click.group()
def setup():
    """Install integrations for external tools."""


@setup.command("claude")
@click.option("--user",    "scope", flag_value=Scope.USER.value,    help="Write to ~/.claude/settings.json")
@click.option("--project", "scope", flag_value=Scope.PROJECT.value, help="Write to ./.claude/settings.json")
@click.option("--local",   "scope", flag_value=Scope.LOCAL.value,   default=True,
              help="Write to ./.claude/settings.local.json (default, gitignored)")
@click.option("--remove", is_flag=True, default=False,
              help="Remove all j-cli managed hooks from the target settings file.")
@pass_ctx
def claude(ctx: Context, scope: str, remove: bool):
    """Install Claude Code PreToolUse hooks: notebook-exec-guard, python-run-guard, and pair-drift-guard."""
    path = _resolve_path(scope)

    if remove:
        if not path.exists():
            emit(
                {
                    "status": ResponseStatus.NOOP,
                    "path": str(path),
                    "_human": f"Nothing to remove: {path} does not exist.",
                },
                ctx.use_json,
            )
            return

        settings = _load_settings(path, ctx.use_json)
        removed = _remove_claude_hooks(settings)

        # Prune empty hook structures
        if "hooks" in settings:
            if not settings["hooks"].get("PreToolUse"):
                settings["hooks"].pop("PreToolUse", None)
            if not settings["hooks"]:
                del settings["hooks"]

        if settings:
            _write_settings(path, settings)
        else:
            path.unlink()

        if removed == 0:
            emit(
                {
                    "status": ResponseStatus.NOOP,
                    "removed": 0,
                    "path": str(path),
                    "_human": f"No managed hooks found in {path}; nothing removed.",
                },
                ctx.use_json,
            )
        else:
            emit(
                {
                    "status": ResponseStatus.OK,
                    "removed": removed,
                    "path": str(path),
                    "_human": f"Removed {removed} managed hook(s) from {path}.",
                },
                ctx.use_json,
            )
        return

    # Install path
    path.parent.mkdir(parents=True, exist_ok=True)

    settings = _load_settings(path, ctx.use_json)
    for block_desc in _MANAGED_BLOCKS:
        _merge_hook(settings, block_desc)
    _write_settings(path, settings)

    emit(
        {
            "status": ResponseStatus.OK,
            "path": str(path),
            "_human": f"Wrote Claude Code hooks to {path}",
        },
        ctx.use_json,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_path(scope: str) -> Path:
    s = Scope(scope)
    if s == Scope.USER:
        return Path.home() / ".claude" / "settings.json"
    if s == Scope.PROJECT:
        return Path.cwd() / ".claude" / "settings.json"
    return Path.cwd() / ".claude" / "settings.local.json"


def _load_settings(path: Path, use_json: bool) -> dict:
    """Load existing settings or return an empty dict."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return {}
        return json.loads(text)
    except json.JSONDecodeError as exc:
        emit_error("SETTINGS_INVALID", f"{path}: {exc}", use_json)
        raise SystemExit(1) from exc


def _merge_hook(settings: dict, block_desc: dict) -> None:
    """Merge one managed hook block into settings.

    For the given block descriptor, scans all PreToolUse blocks whose matcher
    matches ``block_desc["matcher"]`` for any entry whose _jcli_managed value is
    the current name or any legacy name. The first such entry is replaced with the
    current entry dict; additional managed entries are dropped to prevent duplicates.
    If no existing managed entry is found, a new block is appended.
    """
    target_matcher: str = block_desc["matcher"]
    current_entry: dict = block_desc["entry"]
    current_val: str = current_entry[_MANAGED_KEY]
    all_vals: frozenset[str] = frozenset({current_val}) | block_desc["legacy"]

    hooks_map: dict = settings.setdefault("hooks", {})
    pre_list: list = hooks_map.setdefault("PreToolUse", [])

    placed = False
    for block in pre_list:
        if not isinstance(block, dict) or block.get("matcher") != target_matcher:
            continue
        inner: list = block.get("hooks", [])
        new_inner = []
        for entry in inner:
            if isinstance(entry, dict) and entry.get(_MANAGED_KEY) in all_vals:
                if not placed:
                    new_inner.append(current_entry)
                    placed = True
                # else: drop stale duplicate
            else:
                new_inner.append(entry)
        block["hooks"] = new_inner

    if not placed:
        pre_list.append({"matcher": target_matcher, "hooks": [current_entry]})


def _write_settings(path: Path, settings: dict) -> None:
    path.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _remove_claude_hooks(settings: dict) -> int:
    """Remove all jcli-managed entries from settings["hooks"]["PreToolUse"].

    Returns the number of entries removed.  Empty PreToolUse blocks are
    dropped; the caller is responsible for pruning empty "hooks" / top-level
    dicts afterwards.
    """
    hooks_map = settings.get("hooks")
    if not hooks_map:
        return 0
    pre_list = hooks_map.get("PreToolUse", [])
    if not pre_list:
        return 0

    removed = 0
    new_pre_list = []
    for block in pre_list:
        if not isinstance(block, dict):
            new_pre_list.append(block)
            continue
        inner = block.get("hooks", [])
        new_inner = [
            entry for entry in inner
            if not (isinstance(entry, dict) and entry.get(_MANAGED_KEY) in _ALL_MANAGED_VALS)
        ]
        removed += len(inner) - len(new_inner)
        if new_inner:
            new_pre_list.append({**block, "hooks": new_inner})
        # else: block is empty after pruning — drop it

    hooks_map["PreToolUse"] = new_pre_list
    return removed


# ---------------------------------------------------------------------------
# .gitignore managed block helpers
# ---------------------------------------------------------------------------

_GITIGNORE_BLOCK = (
    "# >>> jcli managed (git hooks) >>>\n"
    "*.ipynb\n"
    "# <<< jcli managed (git hooks) <<<\n"
)

_GITIGNORE_BLOCK_RE = re.compile(
    r"# >>> jcli managed \(git hooks\) >>>\n.*?\n# <<< jcli managed \(git hooks\) <<<\n?",
    re.DOTALL,
)


def _inject_gitignore_block(gitignore_path: Path) -> None:
    """Inject or idempotently replace the jcli managed block in .gitignore."""
    content = gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""

    if _GITIGNORE_BLOCK_RE.search(content):
        new_content = _GITIGNORE_BLOCK_RE.sub(lambda _: _GITIGNORE_BLOCK, content)
    else:
        if content and not content.endswith("\n"):
            content += "\n"
        new_content = content + _GITIGNORE_BLOCK

    # Ensure exactly one trailing newline
    new_content = new_content.rstrip("\n") + "\n"
    gitignore_path.write_text(new_content, encoding="utf-8")


def _clean_gitignore_block(path: Path) -> bool:
    """Remove the jcli managed block from .gitignore.

    Returns True if the block was found and removed.  Deletes the file if it
    becomes empty; otherwise rewrites with exactly one trailing newline.
    """
    if not path.exists():
        return False
    content = path.read_text(encoding="utf-8")
    if not _GITIGNORE_BLOCK_RE.search(content):
        return False
    new_content = _GITIGNORE_BLOCK_RE.sub("", content).rstrip("\n")
    if new_content:
        path.write_text(new_content + "\n", encoding="utf-8")
    else:
        path.unlink()
    return True


# ---------------------------------------------------------------------------
# setup git
# ---------------------------------------------------------------------------

@setup.command("git")
@click.option(
    "--local", "scope", flag_value=Scope.LOCAL.value,
    help="Write to .git/hooks/pre-commit (this clone only).",
)
@click.option(
    "--project", "scope", flag_value=Scope.PROJECT.value, default=True,
    help="Write to .githooks/pre-commit and set core.hooksPath (default).",
)
@click.option(
    "--include", "include_globs", multiple=True, metavar="GLOB",
    help="Only sync .py files matching this glob (repeatable; written into hook shim).",
)
@click.option(
    "--remove", is_flag=True, default=False,
    help="Remove j-cli managed git hooks and the managed .gitignore block.",
)
@pass_ctx
def git_setup(ctx: Context, scope: str, include_globs: tuple[str, ...], remove: bool) -> None:
    """Install the pre-commit pair-sync hook and update .gitignore."""

    if os.name == "nt":
        emit_error(
            "UNSUPPORTED_OS",
            "bash shim requires a Unix shell; Windows is not supported in v1.",
            ctx.use_json,
        )
        raise SystemExit(1)  # unreachable — satisfies type checker

    # Locate repo root
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False,
        )
        if top.returncode != 0:
            emit_error(
                "NOT_A_GIT_REPO",
                "Current directory is not inside a git repository.",
                ctx.use_json,
            )
            raise SystemExit(1)
        repo_root = Path(top.stdout.strip())
    except (OSError, FileNotFoundError):
        emit_error("NOT_A_GIT_REPO", "git not found in PATH.", ctx.use_json)
        raise SystemExit(1)

    scope_e = Scope(scope)

    if remove:
        # Remove path
        if scope_e == Scope.LOCAL:
            hook_path = repo_root / ".git" / "hooks" / "pre-commit"
        else:
            hook_path = repo_root / ".githooks" / "pre-commit"

        hook_removed = False
        if hook_path.exists():
            content = hook_path.read_text(encoding="utf-8")
            if "j-cli _hooks pre-commit-pair-sync" in content:
                hook_path.unlink()
                hook_removed = True
            else:
                click.echo(
                    f"warning: {hook_path} is not a jcli-managed hook; skipped",
                    err=True,
                )

        hookspath_unset = False
        if scope_e == Scope.PROJECT:
            try:
                current = subprocess.run(
                    ["git", "config", "--local", "--get", "core.hooksPath"],
                    capture_output=True, text=True, check=False,
                    cwd=str(repo_root),
                )
                current_val = current.stdout.strip() if current.returncode == 0 else None
                if current_val == ".githooks":
                    subprocess.run(
                        ["git", "config", "--local", "--unset", "core.hooksPath"],
                        check=True, cwd=str(repo_root),
                    )
                    hookspath_unset = True
                elif current_val:
                    click.echo(
                        f"warning: core.hooksPath={current_val!r} is not .githooks; left alone",
                        err=True,
                    )
            except (OSError, FileNotFoundError):
                pass

        gitignore_path = repo_root / ".gitignore"
        gitignore_cleaned = _clean_gitignore_block(gitignore_path)

        noop = not hook_removed and not hookspath_unset and not gitignore_cleaned
        emit(
            {
                "status": ResponseStatus.NOOP if noop else ResponseStatus.OK,
                "hook_removed": hook_removed,
                "gitignore_cleaned": gitignore_cleaned,
                "hookspath_unset": hookspath_unset,
                "_human": (
                    f"Removed git hook installation from {repo_root}."
                    if not noop else
                    f"Nothing to remove in {repo_root}."
                ),
            },
            ctx.use_json,
        )
        return

    # Install path
    if scope_e == Scope.LOCAL:
        hook_path = repo_root / ".git" / "hooks" / "pre-commit"
    else:
        hook_path = repo_root / ".githooks" / "pre-commit"

    # Build --include args for shim (shell-safe)
    include_args = "".join(f" --include {shlex.quote(g)}" for g in include_globs)

    shim_content = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"exec j-cli _hooks pre-commit-pair-sync{include_args}\n"
    )

    # Warn if overwriting a non-empty existing hook (--local only)
    if scope_e == Scope.LOCAL and hook_path.exists() and hook_path.stat().st_size > 0:
        click.echo(f"warning: overwrote existing hook at {hook_path}", err=True)

    # Write hook shim
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(shim_content, encoding="utf-8")
    os.chmod(hook_path, 0o755)

    # --project: configure core.hooksPath
    if scope_e == Scope.PROJECT:
        try:
            old = subprocess.run(
                ["git", "config", "--local", "--get", "core.hooksPath"],
                capture_output=True, text=True, check=False,
                cwd=str(repo_root),
            )
            old_val = old.stdout.strip() if old.returncode == 0 else ""
            if old_val and old_val != ".githooks":
                click.echo(
                    f"warning: overrode existing core.hooksPath={old_val!r}",
                    err=True,
                )
            subprocess.run(
                ["git", "config", "--local", "core.hooksPath", ".githooks"],
                check=True, cwd=str(repo_root),
            )
        except (OSError, FileNotFoundError):
            emit_error(
                "GIT_ERROR",
                "git not found when setting core.hooksPath.",
                ctx.use_json,
            )
            raise SystemExit(1)

    # Inject .gitignore managed block
    gitignore_path = repo_root / ".gitignore"
    _inject_gitignore_block(gitignore_path)

    emit(
        {
            "status": ResponseStatus.OK,
            "hook_path": str(hook_path),
            "gitignore_path": str(gitignore_path),
            "scope": scope,
            "include": list(include_globs),
            "_human": (
                f"Installed git pre-commit hook at {hook_path}\n"
                f"Updated .gitignore at {gitignore_path}"
            ),
        },
        ctx.use_json,
    )
