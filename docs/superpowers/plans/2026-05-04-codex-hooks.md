# `j-cli setup codex` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `j-cli setup codex` subcommand and make the codebase platform-aware so both Claude Code and Codex hooks can coexist without naming confusion.

**Architecture:** Each hook subcommand gets a `--platform` flag (default `"claude"`). Platform-specific input extraction is isolated in `_extract_*_claude()` / `_extract_*_codex()` functions. `setup_cmd.py` gains `_MANAGED_BLOCKS` with a `platforms` field for filtering, plus a `setup codex` command that writes to `.codex/hooks.json`. Existing Claude-assuming names are renamed for clarity.

**Tech Stack:** Python 3.11+, Click, pytest with CliRunner

---

### Task 1: Rename docstrings and comments (de-Claude the language)

**Files:**
- Modify: `jupyter_jcli/hook_decision.py:1-9,19-23,33-35,40-44,58-64`
- Modify: `jupyter_jcli/_enums.py:8`
- Modify: `jupyter_jcli/commands/hooks_cmd.py:1`

- [ ] **Step 1: Update `hook_decision.py` module docstring and class docstrings**

Replace Claude-specific wording in all docstrings:

```python
# jupyter_jcli/hook_decision.py

"""Typed hook decisions — each class owns the agent hook output schema.

PreToolUse and PostToolUse have fundamentally different JSON shapes
and semantics. PreToolUse is a permission gate (allow/deny/ask).
PostToolUse can only inject context — the tool already ran, so there
is no meaningful way to "deny" it. Each valid (event, outcome) pair
is modelled as its own dataclass so the emitter has no branches.

See https://code.claude.com/docs/en/hooks for Claude Code wire schema.
See https://developers.openai.com/codex/hooks for Codex wire schema.
"""
```

```python
class HookEvent(str, Enum):
    """Hook event names (used by Claude Code and Codex) — single source of truth
    for the wire strings that appear in payload's hookEventName field."""
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
```

```python
class HookDecision(Protocol):
    """Anything that serializes to an agent hook JSON payload."""
    def to_payload(self) -> dict: ...
```

```python
@dataclass(frozen=True)
class PreToolUseDecision:
    """Permission gate emitted from a PreToolUse hook.

    - ALLOW/ASK: reason shown to the user
    - DENY:      reason shown to the agent (fed into its context)
    """
```

```python
@dataclass(frozen=True)
class PostToolUseContext:
    """Non-blocking context injection from a PostToolUse hook.

    The tool already ran — we can only tell the agent what happened.
    Used for both "paired file auto-synced" and "paired file drifted,
    someone else may have edited it" notifications.
    """
```

- [ ] **Step 2: Update `_enums.py` comment**

```python
# jupyter_jcli/_enums.py, line 8
# Enums that are constrained by external protocols (nbformat, Jupyter REST API,
# agent hooks) carry a note in their docstring. Changing their values requires
```

- [ ] **Step 3: Update `hooks_cmd.py` module docstring**

```python
# jupyter_jcli/commands/hooks_cmd.py, line 1
"""jcli _hooks — internal hook handlers for agent harness integration (Claude Code / Codex).

Codex hook schema sources:
  https://developers.openai.com/codex/hooks
  https://github.com/openai/codex/tree/main/codex-rs/hooks/schema/generated
  openai/codex#2578 — apply_patch Lark grammar
"""
```

- [ ] **Step 4: Update `_post_drift_notice` docstring**

```python
# hooks_cmd.py, line 443-448
def _post_drift_notice(drift_reason: str) -> str:
    """Rewrap a drift reason as a post-hoc notification to the agent.

    The edit has already been applied; we can only inform the agent that
    the paired file is now out of sync because someone changed it
    behind our back.
    """
```

- [ ] **Step 5: Run existing tests to verify no breakage**

```bash
python -m pytest tests/test_setup_cmd.py -v --timeout=60
```

Expected: all tests pass (docstring changes only).

- [ ] **Step 6: Commit**

```bash
git add jupyter_jcli/hook_decision.py jupyter_jcli/_enums.py jupyter_jcli/commands/hooks_cmd.py
git commit -m "docs: replace Claude-specific wording with platform-agnostic terms"
```

---

### Task 2: Add `--platform` flag and extraction layer to `hooks_cmd.py`

**Files:**
- Modify: `jupyter_jcli/commands/hooks_cmd.py`

**Design:** Add `--platform` Click option to all 5 hook commands. Add platform-suffixed extraction functions. Each guard dispatches by platform. Currently `_claude` and `_codex` extraction functions point to identical code for Bash guards (future-proofing).

- [ ] **Step 1: Add extraction functions after the `_HINT` constant (after line 33)**

```python
# ---------------------------------------------------------------------------
# Platform extraction layer
#
# Each extraction function isolates platform-specific stdin field access.
# Claude Code and Codex pass different tool_input shapes (see header URLs).
# Guards call the appropriate function based on --platform flag.
# ---------------------------------------------------------------------------


def _extract_bash_command_claude(payload: dict) -> str:
    """Claude Code: tool_input.command is a plain string."""
    return payload.get("tool_input", {}).get("command", "") or ""


def _extract_bash_command_codex(payload: dict) -> str:
    """Codex: tool_input.command may be array ['bash', '-c', '<cmd>'] or string.

    PreToolUse schema declares tool_input unrestricted ("true" in JSON Schema).
    See codex-rs/hooks/schema/generated/pre-tool-use.command.input.schema.json
    """
    cmd = payload.get("tool_input", {}).get("command", "")
    if isinstance(cmd, list):
        # Codex shell tool: command is array; '-c' flag means third element
        # is the actual user command.
        if len(cmd) >= 3 and cmd[1] == "-c":
            return cmd[2] or ""
        # Fallback: return the last non-empty element.
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

    Content lines always have + / - / space prefix at column 0, so directive
    markers at column 0 cannot be confused with file content.  ^ anchor
    enforces this guarantee.
    """
    import re
    return re.findall(
        r'^\*{3} (?:Update|Add|Delete) File: (.+)$',
        patch_text,
        re.MULTILINE,
    )
```

- [ ] **Step 2: Rewrite `notebook-exec-guard` with `--platform` dispatch**

Replace the existing `nbconvert_guard` function (lines 83-116):

```python
@hooks.command("notebook-exec-guard")
@click.option("--platform", default="claude",
              help="Agent platform: claude or codex")
@click.option("--debug", "debug", is_flag=True, default=False,
              help="Log stdin/stdout/stderr to /tmp/jcli-{uid}/notebook-exec-guard-{ts}.log.")
def nbconvert_guard(platform: str, debug: bool):
    """PreToolUse hook: deny notebook-execution bypass tools and redirect to j-cli."""
    with HookDebugLogger("notebook-exec-guard", enabled=debug) as log:
        try:
            payload = read_hook_stdin(log)
        except (json.JSONDecodeError, ValueError):
            sys.exit(0)

        try:
            if platform == "codex":
                command = _extract_bash_command_codex(payload)
            else:
                command = _extract_bash_command_claude(payload)
        except (AttributeError, TypeError) as exc:
            log.record_exception(exc)
            sys.exit(0)

        from jupyter_jcli.hooks_parser import iter_simple_commands, unwrap_runner

        try:
            simple_commands = iter_simple_commands(command)
        except Exception as exc:  # noqa: BLE001
            log.record_exception(exc)
            sys.exit(0)

        for sc in simple_commands:
            inner = unwrap_runner(sc)
            label = _check_exec_guard(inner)
            if label is not None:
                _emit_decision(PreToolUseDecision(PreToolUseOutcome.DENY, _HINT.format(label=label)), logger=log)
                sys.exit(0)

        sys.exit(0)
```

- [ ] **Step 3: Rewrite `python-run-guard` with `--platform` dispatch**

Replace the existing `python_run_guard` function (lines 140-198):

```python
@hooks.command("python-run-guard")
@click.option("--platform", default="claude",
              help="Agent platform: claude or codex")
@click.option("--debug", "debug", is_flag=True, default=False,
              help="Log stdin/stdout/stderr to /tmp/jcli-{uid}/python-run-guard-{ts}.log.")
def python_run_guard(platform: str, debug: bool):
    """PreToolUse hook: soft guard against running py:percent files as scripts."""
    with HookDebugLogger("python-run-guard", enabled=debug) as log:
        try:
            payload = read_hook_stdin(log)
        except (json.JSONDecodeError, ValueError):
            sys.exit(0)

        cwd: str = ""
        try:
            if platform == "codex":
                command = _extract_bash_command_codex(payload)
            else:
                command = _extract_bash_command_claude(payload)
            cwd = payload.get("cwd", "") or ""
        except (AttributeError, TypeError) as exc:
            log.record_exception(exc)
            sys.exit(0)

        cwd_path = Path(cwd) if cwd else Path.cwd()

        from jupyter_jcli.hooks_parser import extract_script_target, iter_simple_commands, unwrap_runner
        from jupyter_jcli.parser import find_paired_ipynb

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

        sys.exit(0)
```

- [ ] **Step 4: Rewrite `pair-drift-guard-pre` with `--platform` dispatch**

Replace the existing `pair_drift_guard_pre` function (lines 205-251):

```python
@hooks.command("pair-drift-guard-pre")
@click.option("--platform", default="claude",
              help="Agent platform: claude or codex")
@click.option("--debug", "debug", is_flag=True, default=False,
              help="Log stdin/stdout/stderr to /tmp/jcli-{uid}/pair-drift-guard-pre-{ts}.log.")
def pair_drift_guard_pre(platform: str, debug: bool) -> None:
    """PreToolUse hook: detect pre-existing py/ipynb pair drift before an edit."""
    if platform == "codex":
        return _pair_drift_guard_pre_codex(debug)
    else:
        return _pair_drift_guard_pre_claude(debug)


def _pair_drift_guard_pre_claude(debug: bool) -> None:
    """Claude Code: read tool_input.file_path from Edit/Write tools."""
    with HookDebugLogger("pair-drift-guard-pre", enabled=debug) as log:
        try:
            payload = read_hook_stdin(log)
        except (json.JSONDecodeError, ValueError):
            sys.exit(0)

        try:
            tool_input: dict = payload.get("tool_input", {}) or {}
            file_path: str = tool_input.get("file_path", "") or ""
        except (AttributeError, TypeError) as exc:
            log.record_exception(exc)
            sys.exit(0)

        if not file_path:
            sys.exit(0)

        path = Path(file_path)

        if path.suffix == ".ipynb":
            _emit_decision(
                PreToolUseDecision(
                    PreToolUseOutcome.DENY,
                    f"Direct Edit/Write of `{path.name}` is not supported — edit notebooks "
                    "via the py:percent round-trip instead:\n"
                    f"  1. j-cli convert ipynb-to-py {path.name} {path.stem}.py\n"
                    f"  2. Edit {path.stem}.py with Edit/Write\n"
                    f"  3. j-cli convert py-to-ipynb {path.stem}.py {path.name}\n"
                    "(Outputs in the `.ipynb` are preserved through the round-trip.)",
                ),
                logger=log,
            )
            sys.exit(0)

        if not path.exists():
            sys.exit(0)

        try:
            _run_pre_drift_check(path, log)
        except Exception as exc:  # noqa: BLE001
            log.record_exception(exc)
            print(f"pair-drift-guard-pre: unexpected error: {exc}", file=sys.stderr)
            sys.exit(0)


def _pair_drift_guard_pre_codex(debug: bool) -> None:
    """Codex: parse apply_patch command for *** Update File: / *** Add File: paths."""
    with HookDebugLogger("pair-drift-guard-pre", enabled=debug) as log:
        try:
            payload = read_hook_stdin(log)
        except (json.JSONDecodeError, ValueError):
            sys.exit(0)

        try:
            file_paths = _extract_file_paths_codex(payload)
        except (AttributeError, TypeError) as exc:
            log.record_exception(exc)
            sys.exit(0)

        if not file_paths:
            sys.exit(0)

        for file_path in file_paths:
            path = Path(file_path)

            if path.suffix == ".ipynb":
                _emit_decision(
                    PreToolUseDecision(
                        PreToolUseOutcome.DENY,
                        f"apply_patch of `{path.name}` is not supported — edit notebooks "
                        "via the py:percent round-trip instead:\n"
                        f"  1. j-cli convert ipynb-to-py {path.name} {path.stem}.py\n"
                        f"  2. Edit {path.stem}.py\n"
                        f"  3. j-cli convert py-to-ipynb {path.stem}.py {path.name}\n"
                        "(Outputs in the `.ipynb` are preserved through the round-trip.)",
                    ),
                    logger=log,
                )
                sys.exit(0)

            if not path.exists():
                continue

            try:
                _run_pre_drift_check(path, log)
            except Exception as exc:  # noqa: BLE001
                log.record_exception(exc)
                print(f"pair-drift-guard-pre: unexpected error: {exc}", file=sys.stderr)

        sys.exit(0)
```

- [ ] **Step 5: Rewrite `pair-drift-guard-post` with `--platform` dispatch**

Replace the existing `pair_drift_guard_post` function (lines 515-549):

```python
@hooks.command("pair-drift-guard-post")
@click.option("--platform", default="claude",
              help="Agent platform: claude or codex")
@click.option("--debug", "debug", is_flag=True, default=False,
              help="Log stdin/stdout/stderr to /tmp/jcli-{uid}/pair-drift-guard-post-{ts}.log.")
def pair_drift_guard_post(platform: str, debug: bool) -> None:
    """PostToolUse hook: auto-sync py/ipynb pair after agent's own edit."""
    if platform == "codex":
        return _pair_drift_guard_post_codex(debug)
    else:
        return _pair_drift_guard_post_claude(debug)


def _pair_drift_guard_post_claude(debug: bool) -> None:
    """Claude Code: read tool_input.file_path from Edit/Write tools."""
    with HookDebugLogger("pair-drift-guard-post", enabled=debug) as log:
        try:
            payload = read_hook_stdin(log)
        except (json.JSONDecodeError, ValueError):
            sys.exit(0)

        try:
            tool_input: dict = payload.get("tool_input", {}) or {}
            file_path: str = tool_input.get("file_path", "") or ""
        except (AttributeError, TypeError) as exc:
            log.record_exception(exc)
            sys.exit(0)

        if not file_path:
            sys.exit(0)

        path = Path(file_path)

        if path.suffix == ".ipynb":
            sys.exit(0)

        if not path.exists():
            sys.exit(0)

        try:
            _run_post_drift_check(path, log)
        except Exception as exc:  # noqa: BLE001
            log.record_exception(exc)
            print(f"pair-drift-guard-post: unexpected error: {exc}", file=sys.stderr)
            sys.exit(0)


def _pair_drift_guard_post_codex(debug: bool) -> None:
    """Codex: parse apply_patch command for file paths, run post-edit sync."""
    with HookDebugLogger("pair-drift-guard-post", enabled=debug) as log:
        try:
            payload = read_hook_stdin(log)
        except (json.JSONDecodeError, ValueError):
            sys.exit(0)

        try:
            file_paths = _extract_file_paths_codex(payload)
        except (AttributeError, TypeError) as exc:
            log.record_exception(exc)
            sys.exit(0)

        if not file_paths:
            sys.exit(0)

        for file_path in file_paths:
            path = Path(file_path)

            if path.suffix == ".ipynb":
                continue

            if not path.exists():
                continue

            try:
                _run_post_drift_check(path, log)
            except Exception as exc:  # noqa: BLE001
                log.record_exception(exc)
                print(f"pair-drift-guard-post: unexpected error: {exc}", file=sys.stderr)

        sys.exit(0)
```

- [ ] **Step 6: Rewrite `notebook-edit-guard` with `--platform` flag (no-op for now)**

Replace the existing `notebook_edit_guard` function (lines 475-508):

```python
@hooks.command("notebook-edit-guard")
@click.option("--platform", default="claude",
              help="Agent platform: claude or codex")
@click.option("--debug", "debug", is_flag=True, default=False,
              help="Log stdin/stdout/stderr to /tmp/jcli-{uid}/notebook-edit-guard-{ts}.log.")
def notebook_edit_guard(platform: str, debug: bool) -> None:
    """PreToolUse hook: hard-deny NotebookEdit; redirect to py:percent round-trip."""
    # Codex has no NotebookEdit tool — this guard only fires on Claude Code.
    # --platform accepted for interface uniformity; not used for dispatch.
    with HookDebugLogger("notebook-edit-guard", enabled=debug) as log:
        try:
            payload = read_hook_stdin(log)
        except (json.JSONDecodeError, ValueError):
            sys.exit(0)

        try:
            tool_name: str = payload.get("tool_name", "") or ""
        except (AttributeError, TypeError) as exc:
            log.record_exception(exc)
            sys.exit(0)

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
```

- [ ] **Step 7: Run existing tests — all must pass (regression check)**

```bash
python -m pytest tests/test_setup_cmd.py tests/test_hooks.py -v --timeout=120
```

Expected: all existing tests pass. The `--platform` flag defaults to `"claude"` so existing behavior is preserved.

- [ ] **Step 8: Commit**

```bash
git add jupyter_jcli/commands/hooks_cmd.py
git commit -m "feat(hooks): add --platform flag with extraction layer for claude/codex dispatch"
```

---

### Task 3: Restructure `_MANAGED_BLOCKS` and rename helpers in `setup_cmd.py`

**Files:**
- Modify: `jupyter_jcli/commands/setup_cmd.py`

- [ ] **Step 1: Add `platforms` field to `_MANAGED_BLOCKS` and add `{platform_flag}` placeholder**

Replace the block comment (lines 24-32) and `_MANAGED_BLOCKS` (lines 36-87):

```python
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
```

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
        "platforms": ["claude"],           # Codex has no NotebookEdit tool
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
```

- [ ] **Step 2: Rename `_resolve_path` → `_resolve_claude_path`**

```python
# line 189 — rename function
def _resolve_claude_path(scope: str) -> Path:
    s = Scope(scope)
    if s == Scope.USER:
        return Path.home() / ".claude" / "settings.json"
    if s == Scope.PROJECT:
        return Path.cwd() / ".claude" / "settings.json"
    return Path.cwd() / ".claude" / "settings.local.json"
```

- [ ] **Step 3: Add `_resolve_codex_path`**

```python
# after _resolve_claude_path
def _resolve_codex_path(scope: str) -> Path:
    s = Scope(scope)
    if s == Scope.USER:
        return Path.home() / ".codex" / "hooks.json"
    if s == Scope.PROJECT:
        return Path.cwd() / ".codex" / "hooks.json"
    return Path.cwd() / ".codex" / "hooks.local.json"
```

- [ ] **Step 4: Add `platform` parameter to `_merge_hook`**

Modify `_merge_hook` to accept and filter by platform, and substitute `{platform_flag}`:

```python
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

    # Substitute platform flag
    platform_flag = " --platform codex" if platform == "codex" else ""
    current_entry = {
        **current_entry,
        "command": current_entry["command"].format(platform_flag=platform_flag),
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
```

- [ ] **Step 5: Rename `_remove_claude_hooks` → `_remove_managed_hooks`**

```python
# line 257 — rename function
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
                entry for entry in inner
                if not (isinstance(entry, dict) and entry.get(_MANAGED_KEY) in _ALL_MANAGED_VALS)
            ]
            removed += len(inner) - len(new_inner)
            if new_inner:
                new_event_list.append({**block, "hooks": new_inner})
            # else: block is empty after pruning — drop it
        hooks_map[event_key] = new_event_list

    return removed
```

- [ ] **Step 6: Update `def claude(...)` to use new names and pass platform**

Update the `claude` function (lines 113-182): change `_resolve_path` → `_resolve_claude_path`, `_remove_claude_hooks` → `_remove_managed_hooks`, pass `platform="claude"` to `_merge_hook`, and filter `_MANAGED_BLOCKS` by platform:

```python
@setup.command("claude")
@click.option("--user",    "scope", flag_value=Scope.USER.value,    help="Write to ~/.claude/settings.json")
@click.option("--project", "scope", flag_value=Scope.PROJECT.value, help="Write to ./.claude/settings.json")
@click.option("--local",   "scope", flag_value=Scope.LOCAL.value,   default=True,
              help="Write to ./.claude/settings.local.json (default, gitignored)")
@click.option("--remove", is_flag=True, default=False,
              help="Remove all j-cli managed hooks from the target settings file.")
@pass_ctx
def claude(ctx: Context, scope: str, remove: bool):
    """Install Claude Code hooks: notebook-exec-guard, python-run-guard, pair-drift-guard-pre, notebook-edit-guard, and pair-drift-guard-post."""
    path = _resolve_claude_path(scope)
    platform = "claude"

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
            "_human": f"Wrote Claude Code hooks to {path}",
        },
        ctx.use_json,
    )
```

- [ ] **Step 7: Run existing tests — must pass with renames**

```bash
python -m pytest tests/test_setup_cmd.py -v --timeout=60
```

Expected: all tests pass with renamed functions.

- [ ] **Step 8: Commit**

```bash
git add jupyter_jcli/commands/setup_cmd.py
git commit -m "refactor(setup): add platforms field to _MANAGED_BLOCKS, rename claude-specific symbols"
```

---

### Task 4: Add `setup codex` command

**Files:**
- Modify: `jupyter_jcli/commands/setup_cmd.py`

- [ ] **Step 1: Add `_ensure_codex_feature_flag` helper**

Insert after `_resolve_codex_path`:

```python
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
            f"Codex hooks require '[features]\\ncodex_hooks = true' in config.toml",
            err=True,
        )
        return

    text = config_toml.read_text(encoding="utf-8")
    # Scan for codex_hooks = true under [features] section (line-based, no TOML parser needed)
    in_features = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "[features]":
            in_features = True
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_features = False
            continue
        if in_features and re.match(r"^codex_hooks\s*=\s*true\s*$", stripped):
            return  # flag found

    click.echo(
        f"warning: codex_hooks feature flag not enabled in {config_toml} — "
        f"add '[features]\\ncodex_hooks = true' to activate hooks",
        err=True,
    )
```

- [ ] **Step 2: Add `setup codex` command**

Insert after the `claude` function (before the `# Helpers` section):

```python
@setup.command("codex")
@click.option("--user",    "scope", flag_value=Scope.USER.value,    help="Write to ~/.codex/hooks.json")
@click.option("--project", "scope", flag_value=Scope.PROJECT.value, help="Write to ./.codex/hooks.json")
@click.option("--local",   "scope", flag_value=Scope.LOCAL.value,   default=True,
              help="Write to ./.codex/hooks.local.json (default, gitignored)")
@click.option("--remove", is_flag=True, default=False,
              help="Remove all j-cli managed hooks from the target hooks file.")
@pass_ctx
def codex(ctx: Context, scope: str, remove: bool):
    """Install Codex hooks: notebook-exec-guard, python-run-guard, pair-drift-guard-pre, and pair-drift-guard-post.

    notebook-edit-guard is not installed (Codex has no NotebookEdit tool).

    Codex hook schema sources:
      https://developers.openai.com/codex/hooks
      https://github.com/openai/codex/tree/main/codex-rs/hooks/schema/generated
    """
    path = _resolve_codex_path(scope)
    platform = "codex"

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
            "_human": f"Wrote Codex hooks to {path}",
        },
        ctx.use_json,
    )
```

- [ ] **Step 3: Verify `setup codex --local` generates correct hooks.json**

```bash
cd /tmp && rm -rf test_codex && mkdir test_codex && cd test_codex && \
j-cli setup codex --local && \
cat .codex/hooks.json | python3 -m json.tool
```

Expected output: 4 hook entries (no notebook-edit-guard). Each command includes ` --platform codex`. The `_jcli_managed` keys match the correct guards.

- [ ] **Step 4: Verify idempotency**

```bash
j-cli setup codex --local && j-cli setup codex --local && \
python3 -c "import json; s=json.load(open('.codex/hooks.json')); print(sum(len(b['hooks']) for b in s['hooks'].get('PreToolUse',[])))"
```

Expected: hook count unchanged after re-run.

- [ ] **Step 5: Verify `--remove`**

```bash
j-cli setup codex --local --remove && test -f .codex/hooks.json && echo "still exists" || echo "removed"
```

Expected: removed (or empty and pruned).

- [ ] **Step 6: Commit**

```bash
git add jupyter_jcli/commands/setup_cmd.py
git commit -m "feat(setup): add setup codex command with feature flag check"
```

---

### Task 5: Split tests and add Codex-specific tests

**Files:**
- Rename: `tests/test_setup_cmd.py` → `tests/test_setup_claude.py`
- Create: `tests/test_setup_codex.py`
- Create: `tests/test_hooks_codex.py`

- [ ] **Step 1: Rename test file and update internal references**

```bash
cd tests
git mv test_setup_cmd.py test_setup_claude.py
```

Update `test_setup_claude.py`:
- Module docstring line 1: `"""Tests for j-cli setup claude."""`
- `_invoke` helper (line 36): update `["setup", "claude"]` — unchanged, already correct
- All path references `.claude/settings*.json` — unchanged, already correct
- All `_remove_claude_hooks` → `_remove_managed_hooks` references (there may be none in tests; tests use the public API via CLI)

- [ ] **Step 2: Create `tests/test_setup_codex.py`**

```python
"""Tests for j-cli setup codex."""

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from jupyter_jcli.cli import main


def _invoke(runner: CliRunner, args: list[str]):
    return runner.invoke(main, ["setup", "codex"] + args, catch_exceptions=False)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _count_hooks(settings: dict) -> int:
    """Return total hook entries across all events."""
    total = 0
    for event_list in settings.get("hooks", {}).values():
        for block in event_list:
            total += len(block.get("hooks", []))
    return total


class TestCodexScopeRouting:
    def test_local_is_default(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = _invoke(runner, [])
        assert result.exit_code == 0
        target = tmp_path / ".codex" / "hooks.local.json"
        assert target.exists()

    def test_project_writes_settings_json(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = _invoke(runner, ["--project"])
        assert result.exit_code == 0
        target = tmp_path / ".codex" / "hooks.json"
        assert target.exists()

    def test_user_writes_home(self, tmp_path, monkeypatch):
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        monkeypatch.setenv("HOME", str(tmp_path))
        runner = CliRunner()
        result = _invoke(runner, ["--user"])
        assert result.exit_code == 0
        assert (codex_dir / "hooks.json").exists()


class TestCodexInstall:
    def test_creates_parent_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        codex_dir = tmp_path / ".codex"
        assert not codex_dir.exists()
        runner = CliRunner()
        _invoke(runner, ["--local"])
        assert codex_dir.is_dir()

    def test_writes_four_guards_no_notebook_edit(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        _invoke(runner, ["--local"])
        settings = _read_json(tmp_path / ".codex" / "hooks.local.json")
        count = _count_hooks(settings)
        assert count == 4

        managed_vals = set()
        for event_list in settings.get("hooks", {}).values():
            for block in event_list:
                for entry in block.get("hooks", []):
                    managed_vals.add(entry.get("_jcli_managed"))
        assert "notebook-edit-guard" not in managed_vals

    def test_commands_have_platform_flag(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        _invoke(runner, ["--local"])
        settings = _read_json(tmp_path / ".codex" / "hooks.local.json")
        for event_list in settings.get("hooks", {}).values():
            for block in event_list:
                for entry in block.get("hooks", []):
                    assert " --platform codex" in entry.get("command", "")

    def test_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        _invoke(runner, ["--local"])
        _invoke(runner, ["--local"])
        settings = _read_json(tmp_path / ".codex" / "hooks.local.json")
        assert _count_hooks(settings) == 4

    def test_warns_when_config_toml_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = _invoke(runner, ["--local"])
        assert result.exit_code == 0
        # Should warn about missing config.toml
        assert "codex_hooks" in result.stderr

    def test_warns_when_feature_flag_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            "[features]\nsome_other_flag = true\n", encoding="utf-8"
        )
        runner = CliRunner()
        result = _invoke(runner, ["--local"])
        assert result.exit_code == 0
        assert "codex_hooks" in result.stderr

    def test_no_warning_when_feature_flag_present(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            "[features]\ncodex_hooks = true\n", encoding="utf-8"
        )
        runner = CliRunner()
        result = _invoke(runner, ["--local"])
        assert result.exit_code == 0
        assert "codex_hooks" not in result.stderr


class TestCodexRemove:
    def test_remove_cleans_all_entries(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        _invoke(runner, ["--local"])
        target = tmp_path / ".codex" / "hooks.local.json"
        assert target.exists()
        _invoke(runner, ["--local", "--remove"])
        # File should be deleted (empty after remove)
        assert not target.exists()

    def test_remove_noop_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = _invoke(runner, ["--local", "--remove"])
        assert result.exit_code == 0
        assert "does not exist" in result.stdout or "Nothing to remove" in result.stdout

    def test_remove_preserves_non_managed_hooks(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        existing = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "my-custom-hook",
                                "_custom": "keep-me",
                            }
                        ],
                    }
                ]
            }
        }
        (codex_dir / "hooks.local.json").write_text(
            json.dumps(existing), encoding="utf-8"
        )
        runner = CliRunner()
        _invoke(runner, ["--local", "--remove"])
        # File should still exist with the non-managed hook
        settings = _read_json(codex_dir / "hooks.local.json")
        assert _count_hooks(settings) == 1


class TestCodexJsonOutput:
    def test_install_json_output(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            main, ["--json", "setup", "codex", "--local"], catch_exceptions=False
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["status"] == "ok"

    def test_remove_json_output(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        _invoke(runner, ["--local"])
        result = runner.invoke(
            main, ["--json", "setup", "codex", "--local", "--remove"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["status"] == "ok"
```

- [ ] **Step 3: Create `tests/test_hooks_codex.py`**

```python
"""Tests for hook handlers with --platform codex (apply_patch input parsing)."""

import json
import sys
from io import StringIO
from pathlib import Path

import pytest
from click.testing import CliRunner

from jupyter_jcli.commands.hooks_cmd import (
    _extract_bash_command_codex,
    _extract_file_paths_codex,
    _parse_codex_apply_patch_file_paths,
)
from jupyter_jcli.cli import main


class TestExtractBashCommandCodex:
    def test_string_command(self):
        payload = {"tool_input": {"command": "echo hello"}}
        assert _extract_bash_command_codex(payload) == "echo hello"

    def test_array_with_dash_c(self):
        payload = {
            "tool_input": {
                "command": ["bash", "-c", "jupyter nbconvert --execute foo.ipynb"]
            }
        }
        assert (
            _extract_bash_command_codex(payload)
            == "jupyter nbconvert --execute foo.ipynb"
        )

    def test_array_without_dash_c(self):
        payload = {"tool_input": {"command": ["python", "script.py"]}}
        assert _extract_bash_command_codex(payload) == "script.py"

    def test_empty_payload(self):
        assert _extract_bash_command_codex({}) == ""

    def test_missing_command(self):
        assert _extract_bash_command_codex({"tool_input": {}}) == ""


class TestParseCodexApplyPatchFilePaths:
    def test_single_update_file(self):
        text = "*** Begin Patch\n*** Update File: foo.py\n@@ ... @@\n- old\n+ new\n*** End Patch"
        result = _parse_codex_apply_patch_file_paths(text)
        assert result == ["foo.py"]

    def test_multiple_files(self):
        text = (
            "*** Begin Patch\n"
            "*** Update File: foo.py\n@@ ... @@\n- old\n+ new\n"
            "*** Add File: bar.py\n+print('hello')\n"
            "*** End Patch"
        )
        result = _parse_codex_apply_patch_file_paths(text)
        assert result == ["foo.py", "bar.py"]

    def test_delete_file(self):
        text = "*** Begin Patch\n*** Delete File: stale.py\n*** End Patch"
        result = _parse_codex_apply_patch_file_paths(text)
        assert result == ["stale.py"]

    def test_content_line_not_matched(self):
        """Line with + prefix is content, not a directive."""
        text = (
            "*** Begin Patch\n"
            "*** Update File: real.py\n@@ ... @@\n"
            " *** Update File: fake.py\n"  # space prefix = context line
            "+*** Add File: also_fake.py\n"  # + prefix = added line
            "*** End Patch"
        )
        result = _parse_codex_apply_patch_file_paths(text)
        assert result == ["real.py"]


class TestExtractFilePathsCodex:
    def test_array_command(self):
        payload = {
            "tool_input": {
                "command": [
                    "apply_patch",
                    "*** Update File: foo.py\n@@ -1 +1 @@\n- old\n+ new\n",
                ]
            }
        }
        result = _extract_file_paths_codex(payload)
        assert result == ["foo.py"]

    def test_string_command_fallback(self):
        payload = {
            "tool_input": {
                "command": "*** Update File: bar.py\n@@ -1 +1 @@\n- old\n+ new\n"
            }
        }
        result = _extract_file_paths_codex(payload)
        assert result == ["bar.py"]

    def test_no_command(self):
        assert _extract_file_paths_codex({}) == []


class TestCodexNotebookExecGuard:
    def test_denies_nbconvert_with_codex_bash_array(self):
        runner = CliRunner()
        payload = json.dumps({
            "tool_name": "Bash",
            "tool_input": {
                "command": ["bash", "-c", "jupyter nbconvert --execute foo.ipynb"]
            },
            "hook_event_name": "PreToolUse",
            "model": "gpt-5",
            "permission_mode": "default",
            "session_id": "test",
            "tool_use_id": "t1",
            "transcript_path": None,
            "cwd": "/tmp",
            "turn_id": "t1",
        })
        result = runner.invoke(
            main,
            ["_hooks", "notebook-exec-guard", "--platform", "codex"],
            input=payload,
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
```

- [ ] **Step 4: Run all tests**

```bash
python -m pytest tests/test_setup_claude.py tests/test_setup_codex.py tests/test_hooks_codex.py -v --timeout=120
```

Expected: all tests pass.

- [ ] **Step 5: Run full test suite (regression)**

```bash
python -m pytest tests/ -v --timeout=180
```

Expected: all existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add tests/
git commit -m "test: split test_setup_cmd, add test_setup_codex and test_hooks_codex"
```

---

### Task 6: Update SKILL.md

**Files:**
- Modify: `skills/j-cli/SKILL.md`

- [ ] **Step 1: Add `setup codex` section in SKILL.md**

After the existing "One-time Claude Code hook install" section, add a parallel section for Codex. Read the current SKILL.md first to find the exact insertion point.

Find the line after the `setup claude` docs (around line 23, after the "What the hooks install" bullet list ends). Insert:

```markdown
## One-time Codex hook install

Run this once per project to prevent Codex from falling back to `jupyter nbconvert --execute` (or `papermill` / `runipy`) instead of j-cli:

```bash
j-cli setup codex --local    # writes .codex/hooks.local.json (gitignored, this machine only)
# or:
j-cli setup codex --project  # writes .codex/hooks.json       (committed, team-shared)
# or:
j-cli setup codex --user     # writes ~/.codex/hooks.json     (global, all projects)
```

The command is idempotent — re-running updates the hook in place without duplicating it.

**Prerequisites:**
Codex hooks require `[features]\ncodex_hooks = true` in `.codex/config.toml`.  `setup codex` checks for this and warns if missing.

**What the hooks install:**

- **`notebook-exec-guard`** (Bash, hard deny) — blocks `jupyter nbconvert --execute`, `papermill`, `runipy`, and `ipython <notebook>.ipynb`.
- **`python-run-guard`** (Bash, soft deny) — fires when a shell command targets a `.py` file that has a paired `.ipynb`.
- **`pair-drift-guard-pre`** (PreToolUse, apply_patch) — detects drift before an `apply_patch` edit touches a paired `.py` file.
- **`pair-drift-guard-post`** (PostToolUse, apply_patch) — after `apply_patch`, silently syncs the other side of the pair when possible.

> **Note:** `notebook-edit-guard` is not installed for Codex because Codex has no `NotebookEdit` tool; file edits go through `apply_patch` instead.
```

- [ ] **Step 2: Commit**

```bash
git add skills/j-cli/SKILL.md
git commit -m "docs: document setup codex command in j-cli skill"
```

---

### Final Verification

- [ ] **Run full test suite**

```bash
python -m pytest tests/ -v --timeout=180
```

- [ ] **Manual smoke test**

```bash
# Install
cd /tmp && rm -rf codex_smoke && mkdir codex_smoke && cd codex_smoke
j-cli setup codex --local
cat .codex/hooks.json | python3 -m json.tool | head -30

# Remove
j-cli setup codex --local --remove
ls .codex/ 2>/dev/null || echo "cleaned up"
```

