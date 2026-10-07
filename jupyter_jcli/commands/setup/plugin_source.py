"""Render shared host prefilter inline so deployed plugins stay standalone."""

import re
from importlib import resources


def inline_pair_guard(source: str) -> str:
    """Replace the local module import with its plain JavaScript implementation."""
    shared = (
        resources.files("jupyter_jcli")
        .joinpath("pair_guard.js")
        .read_text(encoding="utf-8")
        .replace(
            "export function needsPairGuardPayload", "function needsPairGuardPayload"
        )
    )
    return re.sub(
        r"^import \{ needsPairGuardPayload \} from ['\"]\./pair_guard\.js['\"]$",
        lambda _: shared.rstrip(),
        source,
        flags=re.MULTILINE,
    )
