# Design: `j-cli setup codex` with platform-aware codebase

## Context

OpenAI Codex now supports hooks with the same event model as Claude Code. jcli currently has `setup claude` which installs guard hooks into Claude Code's `settings.json`. We need `setup codex` for Codex's `hooks.json` + `config.toml`.

Additionally, much of the existing code assumes Claude Code is the only platform — functions and constants lack platform qualifiers (e.g., `_remove_claude_hooks` is actually generic, `_resolve_path` returns Claude-specific paths). These must be renamed so the codebase is unambiguous when both platforms coexist.

## Schema sources

Codex hook schemas live in the public `openai/codex` repo. Access paths confirmed during design:

- **PreToolUse input schema**: `codex-rs/hooks/schema/generated/pre-tool-use.command.input.schema.json` — 10 required fields. `tool_input` is unrestricted (`true` in JSON Schema).
- **PostToolUse input schema**: `codex-rs/hooks/schema/generated/post-tool-use.command.input.schema.json` — 11 required fields (+`tool_response`).
- **apply_patch grammar**: documented in openai/codex#2578 (`codex-rs/core/apply-patch.rs` Lark grammar). Confirms content lines always have `+`/`-`/` ` prefix at column 0, directives (`*** Update File:`, etc.) appear at column 0.
- **Official docs**: https://developers.openai.com/codex/hooks
- **Claude Code hook docs**: https://code.claude.com/docs/en/hooks

These URLs will be included as code comments in the relevant source files.

## Key differences: Claude Code vs Codex hooks

| Aspect | Claude Code | Codex |
|--------|-------------|-------|
| Config file | `.claude/settings.json` | `.codex/hooks.json` |
| Feature flag | none | `[features] codex_hooks = true` in `.codex/config.toml` |
| File-edit tool | `Edit`/`Write`/`NotebookEdit` with `tool_input.file_path` | `apply_patch` with `tool_input.command` (array `["apply_patch", "<patch_text>"]`) |
| Bash `tool_input.command` | string | array `["bash", "-c", "<cmd>"]` (possibly string — schema unconstrained) |
| NotebookEdit tool | exists | does not exist |
| `model` field in stdin | only SessionStart | **all events** (reliable discriminator) |
| Hook output | `hookSpecificOutput.permissionDecision` | same shape — compatible |

## Design

### 1. `--platform` flag for explicit dispatch (no runtime detection)

Although `model` field presence is a reliable discriminator (always present in Codex, absent in Claude Code PreToolUse), it relies on undocumented implementation details. Instead, `setup codex` installs hook commands with `--platform codex`:

```json
{"type": "command", "command": "j-cli _hooks pair-drift-guard-pre --platform codex"}
```

Every hook subcommand gets a `--platform` option (default `"claude"`). Extraction functions are suffixed `_claude` / `_codex`:

```python
# hooks_cmd.py — extraction layer

def _extract_bash_command_claude(payload: dict) -> str:
    """Claude Code: tool_input.command is a plain string."""
    return payload.get("tool_input", {}).get("command", "") or ""

def _extract_bash_command_codex(payload: dict) -> str:
    """Codex: tool_input.command may be an array ['bash', '-c', '<cmd>'] or string.
    tool_input is unrestricted per Codex hook schema (see file header for URL)."""
    cmd = payload.get("tool_input", {}).get("command", "")
    if isinstance(cmd, list):
        if len(cmd) >= 3 and cmd[1] == "-c":
            return cmd[2] or ""
        return cmd[-1] if cmd else ""
    return cmd or ""

def _extract_file_path_claude(payload: dict) -> str:
    """Claude Code: Edit/Write tools have file_path in tool_input."""
    return payload.get("tool_input", {}).get("file_path", "") or ""

def _extract_file_paths_codex(payload: dict) -> list[str]:
    """Codex: apply_patch tool_input.command = ['apply_patch', '<patch_text>']."""
    cmd = payload.get("tool_input", {}).get("command", "")
    if isinstance(cmd, list):
        patch_text = cmd[1] if len(cmd) > 1 else ""
    else:
        patch_text = cmd if isinstance(cmd, str) else ""
    return _parse_codex_apply_patch_file_paths(patch_text)

def _parse_codex_apply_patch_file_paths(patch_text: str) -> list[str]:
    """Extract file paths from Codex apply_patch patch text.
    Grammar from codex-rs apply-patch.rs (openai/codex#2578):
      add_hunk:    "*** Add File: " filename LF add_line+
      update_hunk: "*** Update File: " filename LF change_move? change?
      change_line: ("+" | "-" | " ") /(.+)/ LF
    Content lines always have + / - / space prefix at column 0, so directives
    at column 0 cannot be confused with file content. ^ anchor enforces this."""
    import re
    return re.findall(
        r'^\*{3} (?:Update|Add|Delete) File: (.+)$',
        patch_text,
        re.MULTILINE,
    )
```

Each guard dispatches by platform:

```python
@hooks.command("pair-drift-guard-pre")
@click.option("--platform", default="claude")
@click.option("--debug", is_flag=True, default=False)
def pair_drift_guard_pre(platform: str, debug: bool):
    if platform == "codex":
        return _pair_drift_guard_pre_codex(debug)
    else:
        return _pair_drift_guard_pre_claude(debug)
```

Bash guards (`notebook-exec-guard`, `python-run-guard`, `notebook-edit-guard`) follow the same dispatch pattern even though the extraction functions currently point to identical implementations — future-proofing.

### 2. `setup_cmd.py` — `_MANAGED_BLOCKS` with `platforms` field

Single list + filter approach. Each block declares which platforms it applies to:

```python
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
    # ... pair-drift-guard-pre, python-run-guard, pair-drift-guard-post
    #     all with platforms: ["claude", "codex"]
    {
        "event": "PreToolUse",
        "matcher": "NotebookEdit",
        "platforms": ["claude"],           # Codex has no NotebookEdit
        "entry": {
            "type": "command",
            "command": "j-cli _hooks notebook-edit-guard{platform_flag}",
            "_jcli_managed": "notebook-edit-guard",
        },
        "legacy": frozenset({"pair-drift-guard-notebook"}),
    },
]
```

`_merge_hook()` accepts a `platform` parameter: substitutes `{platform_flag}` with ` --platform codex` or `""` (for claude, backward-compat), and filters blocks by `platforms` field.

### 3. Renames for platform clarity

| File | Current | New |
|------|---------|-----|
| `setup_cmd.py` | `_remove_claude_hooks()` | `_remove_managed_hooks()` |
| `setup_cmd.py` | `_resolve_path()` | `_resolve_claude_path()` |
| `setup_cmd.py` | (new) | `_resolve_codex_path()` |
| `setup_cmd.py` | `def claude(ctx, ...)` | unchanged (Click command name) |
| `hook_decision.py` | module docstring: "Claude Code output schema" | "agent hook output schema" |
| `hook_decision.py` | `HookEvent` docstring: "Claude Code hook event names" | "Hook event names (used by Claude Code and Codex)" |
| `hook_decision.py` | `HookDecision`, `PreToolUseDecision`, `PostToolUseContext` docstrings | remove "Claude" references |
| `hooks_cmd.py` | module docstring: "Claude Code harness integration" | "agent harness integration (Claude Code / Codex)" |
| `_enums.py` | comment: "Claude Code hooks" | "agent hooks" |
| `tests/test_setup_cmd.py` | split into | `test_setup_claude.py` + `test_setup_codex.py` |

### 4. `setup codex` command

```python
@setup.command("codex")
@click.option("--user", "scope", flag_value=Scope.USER.value)
@click.option("--project", "scope", flag_value=Scope.PROJECT.value)
@click.option("--local", "scope", flag_value=Scope.LOCAL.value, default=True)
@click.option("--remove", is_flag=True, default=False)
@pass_ctx
def codex(ctx: Context, scope: str, remove: bool):
    """Install Codex hooks ..."""
```

- `_resolve_codex_path(scope)` → `~/.codex/hooks.json` / `.codex/hooks.json` / `.codex/hooks.local.json`
- `_ensure_codex_feature_flag(scope)` → checks `.codex/config.toml` for `[features]\ncodex_hooks = true` via text scan; warns if missing
- Reuses `_merge_hook()`, `_write_settings()`, `_remove_managed_hooks()`

### 5. SKILL.md update

Add `setup codex` section alongside existing `setup claude` section.

## Verification

1. `j-cli setup codex --local` → creates `.codex/hooks.json` with 4 guard entries (no notebook-edit-guard), warns if feature flag missing
2. `j-cli setup codex --local` (rerun) → idempotent, no duplicates
3. `j-cli setup codex --local --remove` → removes managed entries, prunes empty structures
4. `j-cli setup claude --local` → existing behavior unchanged (regression)
5. Simulate Codex apply_patch: `echo '{"tool_name":"apply_patch","tool_input":{"command":["apply_patch","*** Update File: foo.py\\n@@ ...\\n- old\\n+ new"]},"model":"gpt-5",...}' | j-cli _hooks pair-drift-guard-pre --platform codex` → extracts `foo.py`, runs drift check
6. Simulate Codex Bash: `echo '{"tool_name":"Bash","tool_input":{"command":["bash","-c","jupyter nbconvert --execute foo.ipynb"]},...}' | j-cli _hooks notebook-exec-guard --platform codex` → hard-deny
7. Existing test suite passes unchanged
