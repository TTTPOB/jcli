"""jcli setup — install integrations (e.g. Claude Code hooks)."""

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

import click

from jupyter_jcli.cli import Context, pass_ctx
from jupyter_jcli.output import emit, emit_error

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
@click.option("--user",    "scope", flag_value="user",    help="Write to ~/.claude/settings.json")
@click.option("--project", "scope", flag_value="project", help="Write to ./.claude/settings.json")
@click.option("--local",   "scope", flag_value="local",   default=True,
              help="Write to ./.claude/settings.local.json (default, gitignored)")
@pass_ctx
def claude(ctx: Context, scope: str):
    """Install Claude Code PreToolUse hooks: notebook-exec-guard and pair-drift-guard."""
    path = _resolve_path(scope)
    path.parent.mkdir(parents=True, exist_ok=True)

    settings = _load_settings(path, ctx.use_json)
    for block_desc in _MANAGED_BLOCKS:
        _merge_hook(settings, block_desc)
    _write_settings(path, settings)

    emit(
        {
            "status": "ok",
            "path": str(path),
            "_human": f"Wrote Claude Code hooks to {path}",
        },
        ctx.use_json,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_path(scope: str) -> Path:
    if scope == "user":
        return Path.home() / ".claude" / "settings.json"
    if scope == "project":
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


# ---------------------------------------------------------------------------
# setup git
# ---------------------------------------------------------------------------

@setup.command("git")
@click.option(
    "--local", "scope", flag_value="local",
    help="Write to .git/hooks/pre-commit (this clone only).",
)
@click.option(
    "--project", "scope", flag_value="project", default=True,
    help="Write to scripts/git-hooks/pre-commit and set core.hooksPath (default).",
)
@click.option(
    "--include", "include_globs", multiple=True, metavar="GLOB",
    help="Only sync .py files matching this glob (repeatable; written into hook shim).",
)
@pass_ctx
def git_setup(ctx: Context, scope: str, include_globs: tuple[str, ...]) -> None:
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

    # Determine hook path
    if scope == "local":
        hook_path = repo_root / ".git" / "hooks" / "pre-commit"
    else:
        hook_path = repo_root / "scripts" / "git-hooks" / "pre-commit"

    # Build --include args for shim (shell-safe)
    include_args = "".join(f" --include {shlex.quote(g)}" for g in include_globs)

    shim_content = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"exec j-cli _hooks pre-commit-pair-sync{include_args}\n"
    )

    # Warn if overwriting a non-empty existing hook (--local only)
    if scope == "local" and hook_path.exists() and hook_path.stat().st_size > 0:
        click.echo(f"warning: overwrote existing hook at {hook_path}", err=True)

    # Write hook shim
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(shim_content, encoding="utf-8")
    os.chmod(hook_path, 0o755)

    # --project: configure core.hooksPath
    if scope == "project":
        try:
            old = subprocess.run(
                ["git", "config", "--local", "--get", "core.hooksPath"],
                capture_output=True, text=True, check=False,
                cwd=str(repo_root),
            )
            old_val = old.stdout.strip() if old.returncode == 0 else ""
            if old_val and old_val != "scripts/git-hooks":
                click.echo(
                    f"warning: overrode existing core.hooksPath={old_val!r}",
                    err=True,
                )
            subprocess.run(
                ["git", "config", "--local", "core.hooksPath", "scripts/git-hooks"],
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
            "status": "ok",
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
