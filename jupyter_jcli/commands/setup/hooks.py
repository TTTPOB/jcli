"""Claude Code and Codex managed JSON hook installation."""

import json
import re
from pathlib import Path

import click

from jupyter_jcli._enums import ResponseStatus
from jupyter_jcli.cli import CliContext, pass_ctx
from jupyter_jcli.output import emit, emit_error

from .common import Scope

# ---------------------------------------------------------------------------
# Managed hook blocks
#
# Each block descriptor has:
#   event     - hook event type ("PreToolUse" or "PostToolUse")
#   matcher   - tool matcher string
#   platforms - list of platforms this block applies to (e.g. ["claude", "codex"])
#   entry     - the hook entry dict to install (must contain _jcli_managed key)
#               command may contain {platform_flag} placeholder substituted at install
#   legacy    - frozenset of old _jcli_managed values to replace on upgrade
# ---------------------------------------------------------------------------

_MANAGED_KEY = "_jcli_managed"

_MANAGED_BLOCKS: list[dict] = [
    {
        "event": "PreToolUse",
        "matcher": "Bash",
        "platforms": ["claude", "codex"],
        "entry": {
            "type": "command",
            "command": "j-cli _hooks notebook-exec-guard{platform_flag}",
            "_jcli_managed": "notebook-exec-guard",
        },
        "legacy": frozenset({"nbconvert-guard"}),
    },
    {
        "event": "PreToolUse",
        "matcher": "Edit|Write",
        "platforms": ["claude", "codex"],
        "entry": {
            "type": "command",
            "command": "j-cli _hooks pair-drift-guard-pre{platform_flag}",
            "_jcli_managed": "pair-drift-guard-pre",
        },
        "legacy": frozenset({"pair-drift-guard"}),
    },
    {
        "event": "PreToolUse",
        "matcher": "NotebookEdit",
        "platforms": ["claude"],  # Codex has no NotebookEdit tool
        "entry": {
            "type": "command",
            "command": "j-cli _hooks notebook-edit-guard{platform_flag}",
            "_jcli_managed": "notebook-edit-guard",
        },
        "legacy": frozenset({"pair-drift-guard-notebook"}),
    },
    {
        "event": "PostToolUse",
        "matcher": "Edit|Write",
        "platforms": ["claude", "codex"],
        "entry": {
            "type": "command",
            "command": "j-cli _hooks pair-drift-guard-post{platform_flag}",
            "_jcli_managed": "pair-drift-guard-post",
        },
        "legacy": frozenset(),
    },
    {
        "event": "PreToolUse",
        "matcher": "Bash",
        "platforms": ["claude", "codex"],
        "entry": {
            "type": "command",
            "command": "j-cli _hooks python-run-guard{platform_flag}",
            "_jcli_managed": "python-run-guard",
        },
        "legacy": frozenset(),
    },
]

# All managed values across all blocks (current + legacy) — used for upgrade detection
_ALL_MANAGED_VALS: frozenset[str] = frozenset(
    val
    for block in _MANAGED_BLOCKS
    for val in ({block["entry"][_MANAGED_KEY]} | block["legacy"])
)


@click.command("claude")
@click.option(
    "--user",
    "scope",
    flag_value=Scope.USER.value,
    help="Write to ~/.claude/settings.json",
)
@click.option(
    "--project",
    "scope",
    flag_value=Scope.PROJECT.value,
    help="Write to ./.claude/settings.json",
)
@click.option(
    "--local",
    "scope",
    flag_value=Scope.LOCAL.value,
    default=True,
    help="Write to ./.claude/settings.local.json (default, gitignored)",
)
@click.option(
    "--remove",
    is_flag=True,
    default=False,
    help="Remove all j-cli managed hooks from the target settings file.",
)
@pass_ctx
def claude(ctx: CliContext, scope: str, remove: bool):
    """Install Claude Code hooks: notebook-exec-guard, python-run-guard, pair-drift-guard-pre, notebook-edit-guard, and pair-drift-guard-post."""
    path = _resolve_claude_path(scope)
    _install_or_remove("claude", path, remove, ctx)


@click.command("codex")
@click.option(
    "--user", "scope", flag_value=Scope.USER.value, help="Write to ~/.codex/hooks.json"
)
@click.option(
    "--project",
    "scope",
    flag_value=Scope.PROJECT.value,
    default=True,
    help="Write to ./.codex/hooks.json (default)",
)
@click.option(
    "--local",
    "scope",
    flag_value=Scope.LOCAL.value,
    help="Alias for --project (Codex has no settings.local.json layer)",
)
@click.option(
    "--remove",
    is_flag=True,
    default=False,
    help="Remove all j-cli managed hooks from the target hooks file.",
)
@pass_ctx
def codex(ctx: CliContext, scope: str, remove: bool):
    """Install Codex hooks: notebook-exec-guard, python-run-guard, pair-drift-guard-pre, and pair-drift-guard-post.

    notebook-edit-guard is not installed (Codex has no NotebookEdit tool).

    Codex hook schema sources:
      https://developers.openai.com/codex/hooks
      https://github.com/openai/codex/tree/main/codex-rs/hooks/schema/generated
    """
    path = _resolve_codex_path(scope)
    if Scope(scope) == Scope.LOCAL:
        click.echo(
            "Note: Codex has no hooks.local.json layer; --local writes to ./.codex/hooks.json",
            err=True,
        )
    _install_or_remove("codex", path, remove, ctx)


def _install_or_remove(
    platform: str, path: Path, remove: bool, ctx: CliContext
) -> None:
    """Install or remove managed hooks for a given platform."""
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
        removed = _remove_managed_hooks(settings)

        # Prune empty hook structures
        if "hooks" in settings:
            for _event_key in list(settings["hooks"].keys()):
                if not settings["hooks"].get(_event_key):
                    settings["hooks"].pop(_event_key, None)
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

    if platform == "codex":
        _ensure_codex_feature_flag(path)

    settings = _load_settings(path, ctx.use_json)
    for block_desc in _MANAGED_BLOCKS:
        if platform not in block_desc.get("platforms", []):
            continue
        _merge_hook(settings, block_desc, platform)
    _write_settings(path, settings)

    emit(
        {
            "status": ResponseStatus.OK,
            "path": str(path),
            "_human": f"Wrote {platform.title()} hooks to {path}",
        },
        ctx.use_json,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_codex_feature_flag(path: Path) -> None:
    """Check that codex_hooks feature flag is enabled in a .codex/config.toml.

    Searches for ``[features]`` section line followed by ``codex_hooks = true``.
    Prints a warning to stderr if the flag is missing.  Does NOT modify the file.
    """
    scope_dir = path.parent
    config_toml = scope_dir / "config.toml"

    if not config_toml.exists():
        click.echo(
            f"warning: {config_toml} not found — "
            f"Codex hooks require '[features]\ncodex_hooks = true' in config.toml",
            err=True,
        )
        return

    text = config_toml.read_text(encoding="utf-8")
    # Scan for codex_hooks = true under [features] section.
    # Line-based text scan avoids pulling in a TOML parser for a single check.
    # Limitation: does not handle multi-line TOML strings containing "[features]"
    # as literal text.  In practice this is vanishingly rare and the function
    # only warns (does not block), so the trade-off is acceptable.
    in_features = False
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()  # strip inline comments
        if stripped == "[features]":
            in_features = True
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_features = False
            continue
        if in_features and re.match(r"^codex_hooks\s*=\s*true\b", stripped):
            return  # flag found

    click.echo(
        f"warning: codex_hooks feature flag not enabled in {config_toml} — "
        f"add '[features]\ncodex_hooks = true' to activate hooks",
        err=True,
    )


def _resolve_claude_path(scope: str) -> Path:
    s = Scope(scope)
    if s == Scope.USER:
        return Path.home() / ".claude" / "settings.json"
    if s == Scope.PROJECT:
        return Path.cwd() / ".claude" / "settings.json"
    return Path.cwd() / ".claude" / "settings.local.json"


def _resolve_codex_path(scope: str) -> Path:
    s = Scope(scope)
    if s == Scope.USER:
        return Path.home() / ".codex" / "hooks.json"
    # Codex only reads hooks.json — there is no hooks.local.json layer.
    # Both --project and --local write to ./.codex/hooks.json.
    return Path.cwd() / ".codex" / "hooks.json"


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


def _merge_hook(settings: dict, block_desc: dict, platform: str) -> None:
    """Merge one managed hook block into settings for the given platform.

    Only inserts/updates if block_desc['platforms'] includes *platform*.
    Substitutes {platform_flag} in the command string:
      "claude" -> "" (backward compat, no flag)
      "codex"  -> " --platform codex"
    """
    target_event: str = block_desc.get("event", "PreToolUse")
    target_matcher: str = block_desc["matcher"]
    current_entry: dict = block_desc["entry"]
    current_val: str = current_entry[_MANAGED_KEY]
    all_vals: frozenset[str] = frozenset({current_val}) | block_desc["legacy"]

    # Substitute platform flag (shallow-copy to avoid mutating _MANAGED_BLOCKS)
    platform_flag = " --platform codex" if platform == "codex" else ""
    current_entry = {
        **current_entry,
        "command": current_entry["command"].replace("{platform_flag}", platform_flag),
    }

    hooks_map: dict = settings.setdefault("hooks", {})
    event_list: list = hooks_map.setdefault(target_event, [])

    placed = False
    for block in event_list:
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
        event_list.append({"matcher": target_matcher, "hooks": [current_entry]})


def _write_settings(path: Path, settings: dict) -> None:
    path.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _remove_managed_hooks(settings: dict) -> int:
    """Remove all jcli-managed entries from settings["hooks"] (all event types).

    Returns the number of entries removed.  Empty event-type blocks are
    dropped; the caller is responsible for pruning empty "hooks" / top-level
    dicts afterwards.
    """
    hooks_map = settings.get("hooks")
    if not hooks_map:
        return 0

    removed = 0
    for event_key in list(hooks_map.keys()):
        event_list = hooks_map.get(event_key)
        if not event_list:
            continue

        new_event_list = []
        for block in event_list:
            if not isinstance(block, dict):
                new_event_list.append(block)
                continue
            inner = block.get("hooks", [])
            new_inner = [
                entry
                for entry in inner
                if not (
                    isinstance(entry, dict)
                    and entry.get(_MANAGED_KEY) in _ALL_MANAGED_VALS
                )
            ]
            removed += len(inner) - len(new_inner)
            if new_inner:
                new_event_list.append({**block, "hooks": new_inner})
            # else: block is empty after pruning — drop it
        hooks_map[event_key] = new_event_list

    return removed
