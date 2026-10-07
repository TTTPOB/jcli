"""Standard-library-only pair guard prefilter and shared path extraction."""

import errno
import os
import re
from pathlib import Path

_DIRECTIVE = re.compile(
    r"^(?:\*{3}|\*{2}_) (?:Update|Add|Delete) File: |^(?:\*{3}|\*{2}_) Move to: "
)


def patch_paths(patch_text: str) -> list[str]:
    """Read all file directives, including move targets and legacy markers."""
    return [
        line[match.end() :].strip()
        for line in patch_text.splitlines()
        if (match := _DIRECTIVE.match(line))
    ]


def normalize_path(file_path: str, payload: dict) -> str:
    """Resolve tool paths against the tool/session cwd, not the plugin cwd."""
    cwd = payload.get("cwd") or os.getcwd()
    workdir = payload.get("tool_input", {}).get("workdir") or "."
    return os.path.abspath(os.path.join(cwd, workdir, file_path))


def needs_pair_guard(payload: object, phase: str) -> bool:
    """Skip only proven unrelated paths; uncertainty belongs to the old guard."""
    if not isinstance(payload, dict):
        return True
    tool = payload.get("tool_name")
    args = payload.get("tool_input")
    if not isinstance(tool, str) or not isinstance(args, dict):
        return True
    if "cwd" in payload and not isinstance(payload["cwd"], str):
        return True
    if "workdir" in args and not isinstance(args["workdir"], str):
        return True
    if "file_path" in args and not isinstance(args["file_path"], str):
        return True
    if "command" in args and not (
        isinstance(args["command"], str)
        or (
            isinstance(args["command"], list)
            and all(isinstance(item, str) for item in args["command"])
        )
    ):
        return True
    if tool == "apply_patch":
        command = args.get("command")
        if isinstance(command, list):
            if len(command) != 2 or command[0] != "apply_patch":
                return True
            command = command[1]
        if not isinstance(command, str):
            return True
        paths = patch_paths(command)
        if not paths:
            return True
    elif tool in {"Edit", "Write", "edit", "write"}:
        paths = [args.get("file_path")]
    else:
        return True
    for value in paths:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            return True
        try:
            path = Path(normalize_path(value, payload))
        except (OSError, ValueError):
            return True
        if phase == "pre" and path.suffix == ".ipynb":
            return True
        if path.suffix != ".py":
            continue
        try:
            path.with_suffix(".ipynb").stat()
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
                continue
            return True
        except ValueError:
            return True
        return True
    return False
