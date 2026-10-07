"""Lightweight native hook entry point; import the CLI only when needed."""

import io
import json
import sys

from .pair_guard import needs_pair_guard


def main() -> None:
    """Replay stdin to the existing guard in this process, preserving its protocol."""
    args = sys.argv[1:]
    known_options = args[1:] in ([], ["--platform", "claude"], ["--platform", "codex"])
    if (
        args
        and args[0] in {"pair-drift-guard-pre", "pair-drift-guard-post"}
        and known_options
    ):
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            pass
        else:
            phase = "pre" if args[0].endswith("-pre") else "post"
            if not needs_pair_guard(payload, phase):
                return
        sys.stdin = io.StringIO(raw)

    from .cli import main as cli_main

    cli_main(args=["_hooks", *args], prog_name="j-cli-hook")


if __name__ == "__main__":
    main()
