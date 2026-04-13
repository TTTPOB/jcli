"""jcli setup — install integrations (e.g. Claude Code hooks)."""

import json
from pathlib import Path

import click

from jupyter_jcli.cli import Context, pass_ctx
from jupyter_jcli.output import emit, emit_error

# The hook block written into settings.json.
_HOOK_ENTRY = {
    "type": "command",
    "command": "j-cli _hooks notebook-exec-guard",
    "_jcli_managed": "notebook-exec-guard",
}

_HOOK_BLOCK = {
    "matcher": "Bash",
    "hooks": [_HOOK_ENTRY],
}

# Stable marker key used for de-duplication.
_MANAGED_KEY = "_jcli_managed"
_MANAGED_VAL = "notebook-exec-guard"

# Legacy values from older j-cli versions — recognised on upgrade so the old
# entry is replaced in-place rather than leaving a stale duplicate.
_LEGACY_MANAGED_VALS: frozenset[str] = frozenset({"nbconvert-guard"})
_ALL_MANAGED_VALS: frozenset[str] = frozenset({_MANAGED_VAL}) | _LEGACY_MANAGED_VALS


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
    """Install a Claude Code PreToolUse hook that redirects nbconvert to j-cli."""
    path = _resolve_path(scope)
    path.parent.mkdir(parents=True, exist_ok=True)

    settings = _load_settings(path, ctx.use_json)
    _merge_hook(settings)
    _write_settings(path, settings)

    emit(
        {
            "status": "ok",
            "path": str(path),
            "_human": f"Wrote Claude Code hook to {path}",
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
    # local (default)
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
        raise SystemExit(1) from exc  # emit_error already calls sys.exit(1)


def _merge_hook(settings: dict) -> None:
    """Merge our PreToolUse hook block into settings.

    Scans all Bash PreToolUse blocks for any entry whose _jcli_managed value
    is in _ALL_MANAGED_VALS (current name or any legacy name from older j-cli
    versions).  The first such entry is replaced with the current _HOOK_ENTRY;
    any additional managed entries found afterwards are dropped so that
    upgrading from an old version never leaves a stale duplicate block.
    """
    hooks_map: dict = settings.setdefault("hooks", {})
    pre_list: list = hooks_map.setdefault("PreToolUse", [])

    placed = False
    for block in pre_list:
        if not isinstance(block, dict) or block.get("matcher") != "Bash":
            continue
        inner: list = block.get("hooks", [])
        new_inner = []
        for entry in inner:
            if isinstance(entry, dict) and entry.get(_MANAGED_KEY) in _ALL_MANAGED_VALS:
                if not placed:
                    new_inner.append(_HOOK_ENTRY)
                    placed = True
                # else: drop — stale duplicate from a previous install / old version
            else:
                new_inner.append(entry)
        block["hooks"] = new_inner

    if not placed:
        pre_list.append(_HOOK_BLOCK)


def _write_settings(path: Path, settings: dict) -> None:
    path.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
