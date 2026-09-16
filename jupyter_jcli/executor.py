"""Output processing: extract images to temp files, strip ANSI codes."""

import base64
import os
import re
import tempfile
from pathlib import Path

from jupyter_jcli._enums import OutputType

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
SUMMARY_TEXT_LIMIT = 4_000
SUMMARY_ITEM_LIMIT = 20


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return _ANSI_ESCAPE_RE.sub("", text)


def save_base64_image(data: str, suffix: str = ".png") -> str:
    """Decode base64 image data and save to a temp file. Returns path."""
    img_bytes = base64.b64decode(data)
    fd, path = tempfile.mkstemp(prefix="jcli_", suffix=suffix)
    os.close(fd)
    Path(path).write_bytes(img_bytes)
    return path


def process_outputs(raw_outputs: list[dict]) -> list[dict]:
    """Process kernel outputs into normalized dicts.

    Images are saved to temp files and referenced by path.
    """
    results = []
    for output in raw_outputs:
        try:
            output_type = OutputType(output.get("output_type"))
        except (ValueError, TypeError):
            continue  # skip unknown output types

        if output_type == OutputType.STREAM:
            text = output.get("text", "")
            if isinstance(text, list):
                text = "".join(text)
            results.append(
                {
                    "type": OutputType.STREAM,
                    "name": output.get("name", "stdout"),
                    "text": strip_ansi(str(text)),
                }
            )

        elif output_type in (OutputType.DISPLAY_DATA, OutputType.EXECUTE_RESULT):
            data = output.get("data", {})
            # Check image types first
            if "image/png" in data:
                path = save_base64_image(data["image/png"], suffix=".png")
                results.append(
                    {"type": OutputType.IMAGE, "path": path, "mime": "image/png"}
                )
            elif "image/jpeg" in data:
                path = save_base64_image(data["image/jpeg"], suffix=".jpg")
                results.append(
                    {"type": OutputType.IMAGE, "path": path, "mime": "image/jpeg"}
                )
            elif "text/html" in data:
                results.append({"type": OutputType.HTML, "html": data["text/html"]})
            elif "text/plain" in data:
                plain = data["text/plain"]
                if isinstance(plain, list):
                    plain = "".join(plain)
                results.append(
                    {
                        "type": OutputType.EXECUTE_RESULT,
                        "text": strip_ansi(str(plain)),
                    }
                )
            else:
                results.append(
                    {
                        "type": output_type,
                        "keys": list(data.keys()),
                    }
                )

        elif output_type == OutputType.ERROR:
            traceback = output.get("traceback", [])
            if isinstance(traceback, list):
                traceback = [strip_ansi(str(line)) for line in traceback]
            results.append(
                {
                    "type": OutputType.ERROR,
                    "ename": output.get("ename", ""),
                    "evalue": output.get("evalue", ""),
                    "traceback": traceback,
                }
            )

    return results


class _SummaryBudget:
    def __init__(self, text_limit: int, item_limit: int) -> None:
        self.text_remaining = text_limit
        self.items_remaining = item_limit
        self.truncated = False
        self.omitted_items = 0

    def item(self) -> bool:
        if self.items_remaining <= 0:
            self.truncated = True
            self.omitted_items += 1
            return False
        self.items_remaining -= 1
        return True

    def text(self, value: str) -> str:
        if len(value) <= self.text_remaining:
            self.text_remaining -= len(value)
            return value
        selected = value[: self.text_remaining]
        self.text_remaining = 0
        self.truncated = True
        return selected + "...[truncated]"


def summarize_outputs(
    raw_outputs: list[dict],
    *,
    text_limit: int = SUMMARY_TEXT_LIMIT,
    item_limit: int = SUMMARY_ITEM_LIMIT,
    full_output_location: str | None = None,
) -> list[dict]:
    """Build one globally bounded execution summary without writing files."""
    budget = _SummaryBudget(text_limit, item_limit)
    results = []
    for output_index, output in enumerate(raw_outputs):
        if not budget.item():
            budget.omitted_items += len(raw_outputs) - output_index - 1
            break
        try:
            output_type = OutputType(output.get("output_type"))
        except (ValueError, TypeError):
            budget.truncated = True
            budget.omitted_items += 1
            continue

        if output_type == OutputType.STREAM:
            results.append(
                {
                    "type": OutputType.STREAM,
                    "name": budget.text(str(output.get("name", "stdout"))),
                    "text": budget.text(
                        strip_ansi(_text_value(output.get("text", "")))
                    ),
                }
            )
        elif output_type in (OutputType.DISPLAY_DATA, OutputType.EXECUTE_RESULT):
            data = output.get("data", {})
            if not isinstance(data, dict):
                results.append({"type": output_type, "keys": []})
            elif "image/png" in data or "image/jpeg" in data:
                mime = "image/png" if "image/png" in data else "image/jpeg"
                summary = {"type": OutputType.IMAGE, "mime": mime}
                reference = data[mime]
                if isinstance(reference, dict) and reference.get("type") == "file":
                    summary["path"] = reference.get("path")
                results.append(summary)
            elif "text/html" in data:
                results.append(
                    {
                        "type": OutputType.HTML,
                        "html": budget.text(_text_value(data["text/html"])),
                    }
                )
            elif "text/plain" in data:
                results.append(
                    {
                        "type": OutputType.EXECUTE_RESULT,
                        "text": budget.text(
                            strip_ansi(_text_value(data["text/plain"]))
                        ),
                    }
                )
            else:
                keys = []
                for key in data:
                    if not budget.item():
                        break
                    keys.append(budget.text(str(key)))
                results.append({"type": output_type, "keys": keys})
        elif output_type == OutputType.ERROR:
            traceback = output.get("traceback", [])
            lines = []
            if isinstance(traceback, list):
                for line in traceback:
                    if not budget.item():
                        break
                    lines.append(budget.text(strip_ansi(str(line))))
            else:
                budget.truncated = True
                budget.omitted_items += 1
            results.append(
                {
                    "type": OutputType.ERROR,
                    "ename": budget.text(str(output.get("ename", ""))),
                    "evalue": budget.text(str(output.get("evalue", ""))),
                    "traceback": lines,
                }
            )

    if budget.truncated:
        notice = {
            "type": "summary_notice",
            "truncated": True,
            "omitted_items": budget.omitted_items,
            "message": (
                "Execution summary was truncated; full outputs remain persisted."
                if full_output_location is not None
                else "Execution summary was truncated; additional output was omitted."
            ),
        }
        if full_output_location is not None:
            notice["complete_outputs"] = full_output_location
        results.append(notice)
    return results


def summary_fits(
    raw_outputs: list[dict],
    *,
    text_limit: int = SUMMARY_TEXT_LIMIT,
    item_limit: int = SUMMARY_ITEM_LIMIT,
) -> bool:
    """Return whether the complete lightweight summary fits shared budgets."""
    summary = summarize_outputs(
        raw_outputs, text_limit=text_limit, item_limit=item_limit
    )
    return not summary or summary[-1].get("type") != "summary_notice"


def _text_value(value) -> str:
    if isinstance(value, list):
        return "".join(str(part) for part in value)
    return str(value)


def format_outputs_human(outputs: list[dict]) -> str:
    """Format processed outputs for human-readable display."""
    parts = []
    for o in outputs:
        if o["type"] == OutputType.STREAM or o["type"] == OutputType.EXECUTE_RESULT:
            parts.append(o["text"])
        elif o["type"] == OutputType.IMAGE:
            if "path" in o:
                parts.append(f"[image saved: {o['path']}]")
            else:
                parts.append(f"[image output: {o['mime']}]")
        elif o["type"] == OutputType.HTML:
            parts.append("[HTML output]")
        elif o["type"] == OutputType.ERROR:
            parts.append(f"{o['ename']}: {o['evalue']}")
            if o.get("traceback"):
                parts.append("\n".join(o["traceback"]))
        elif o["type"] == "summary_notice":
            notice = o["message"]
            if o.get("complete_outputs"):
                notice += f" Full outputs: {o['complete_outputs']}"
            parts.append(notice)
    return "\n".join(parts)
