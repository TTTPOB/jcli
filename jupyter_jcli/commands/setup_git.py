"""Git hook installation for setup commands."""

import os
import re
import shlex
import subprocess
from pathlib import Path

import click

from jupyter_jcli._enums import ResponseStatus
from jupyter_jcli.cli import CliContext, pass_ctx
from jupyter_jcli.commands.setup_common import Scope
from jupyter_jcli.output import emit, emit_error

# ---------------------------------------------------------------------------
# .gitignore managed block helpers
# ---------------------------------------------------------------------------

_GITIGNORE_BLOCK = (
    "# >>> jcli managed (git hooks) >>>\n*.ipynb\n# <<< jcli managed (git hooks) <<<\n"
)

_GITIGNORE_BLOCK_RE = re.compile(
    r"# >>> jcli managed \(git hooks\) >>>\n.*?\n# <<< jcli managed \(git hooks\) <<<\n?",
    re.DOTALL,
)


def _inject_gitignore_block(gitignore_path: Path) -> None:
    """Inject or idempotently replace the jcli managed block in .gitignore."""
    content = (
        gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""
    )

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


@click.command("git")
@click.option(
    "--local",
    "scope",
    flag_value=Scope.LOCAL.value,
    help="Write to .git/hooks/pre-commit (this clone only).",
)
@click.option(
    "--project",
    "scope",
    flag_value=Scope.PROJECT.value,
    default=True,
    help="Write to .githooks/pre-commit and set core.hooksPath (default).",
)
@click.option(
    "--include",
    "include_globs",
    multiple=True,
    metavar="GLOB",
    help="Only sync .py files matching this glob (repeatable; written into hook shim).",
)
@click.option(
    "--remove",
    is_flag=True,
    default=False,
    help="Remove j-cli managed git hooks and the managed .gitignore block.",
)
@pass_ctx
def git_setup(
    ctx: CliContext, scope: str, include_globs: tuple[str, ...], remove: bool
) -> None:
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
            capture_output=True,
            text=True,
            check=False,
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
                    capture_output=True,
                    text=True,
                    check=False,
                    cwd=str(repo_root),
                )
                current_val = (
                    current.stdout.strip() if current.returncode == 0 else None
                )
                if current_val == ".githooks":
                    subprocess.run(
                        ["git", "config", "--local", "--unset", "core.hooksPath"],
                        check=True,
                        cwd=str(repo_root),
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
                    if not noop
                    else f"Nothing to remove in {repo_root}."
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
                capture_output=True,
                text=True,
                check=False,
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
                check=True,
                cwd=str(repo_root),
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
