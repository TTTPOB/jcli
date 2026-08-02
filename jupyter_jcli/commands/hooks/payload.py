"""Platform-specific payload extraction for agent hook handlers."""

import re


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
        # Scan for '-c' flag position rather than assuming fixed index.
        try:
            c_idx = cmd.index("-c")
            if c_idx + 1 < len(cmd):
                return cmd[c_idx + 1] or ""
        except ValueError:
            pass
        # Fallback: return the last non-empty element.
        return cmd[-1] if cmd else ""
    return cmd or ""


def _extract_file_path_claude(payload: dict) -> str:
    """Claude Code: Edit/Write tools have file_path in tool_input."""
    return payload.get("tool_input", {}).get("file_path", "") or ""


def _extract_file_paths_codex(payload: dict) -> list[str]:
    """Codex: apply_patch tool_input.command = ['apply_patch', '<patch_text>'].

    Only extracts when tool_name is 'apply_patch' to avoid false positives
    from Bash commands that happen to contain patch-like markers.
    """
    if payload.get("tool_name", "") != "apply_patch":
        return []
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
      change_move: "*** Move to: " filename LF
      change_line: ("+" | "-" | " ") /(.+)/ LF

    The parser accepts "*** " markers, but the model may still produce the
    older "**_ " format from the legacy system prompt, so both are matched.

    Content lines always have + / - / space prefix at column 0, so directive
    markers at column 0 cannot be confused with file content.  ^ anchor
    enforces this guarantee.
    """
    _MARKER = r"\*{3}|\*{2}_"  # both "*** " and "**_ " (legacy prompt)
    _DIRECTIVE = (
        rf"^(?:{_MARKER}) (?:Update|Add|Delete) File: |^(?:{_MARKER}) Move to: "
    )
    paths: list[str] = []
    for line in patch_text.splitlines():
        m = re.match(_DIRECTIVE, line)
        if not m:
            continue
        paths.append(line[m.end() :].strip())
    return paths
