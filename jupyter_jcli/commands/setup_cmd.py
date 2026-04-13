"""jcli setup — install integrations (e.g. Claude Code hooks)."""

import json
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
