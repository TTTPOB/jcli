"""Output processing: extract images to temp files, strip ANSI codes."""

import base64
import os
import re
import tempfile
from pathlib import Path

from jupyter_jcli._enums import OutputType

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
_SUMMARY_TEXT_LIMIT = 4_000


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


def summarize_outputs(
    raw_outputs: list[dict], *, text_limit: int = _SUMMARY_TEXT_LIMIT
) -> list[dict]:
    """Build a bounded execution summary without writing payload files."""
    results = []
    for output in raw_outputs:
        try:
            output_type = OutputType(output.get("output_type"))
        except (ValueError, TypeError):
            continue

        if output_type == OutputType.STREAM:
            text = output.get("text", "")
            if isinstance(text, list):
                text = "".join(text)
            results.append(
                {
                    "type": OutputType.STREAM,
                    "name": output.get("name", "stdout"),
                    "text": _bounded_text(strip_ansi(str(text)), text_limit),
                }
            )
        elif output_type in (OutputType.DISPLAY_DATA, OutputType.EXECUTE_RESULT):
            data = output.get("data", {})
            if not isinstance(data, dict):
                results.append({"type": output_type, "keys": []})
            elif "image/png" in data:
                results.append({"type": OutputType.IMAGE, "mime": "image/png"})
            elif "image/jpeg" in data:
                results.append({"type": OutputType.IMAGE, "mime": "image/jpeg"})
            elif "text/html" in data:
                html = data["text/html"]
                if isinstance(html, list):
                    html = "".join(html)
                results.append(
                    {
                        "type": OutputType.HTML,
                        "html": _bounded_text(str(html), text_limit),
                    }
                )
            elif "text/plain" in data:
                plain = data["text/plain"]
                if isinstance(plain, list):
                    plain = "".join(plain)
                results.append(
                    {
                        "type": OutputType.EXECUTE_RESULT,
                        "text": _bounded_text(strip_ansi(str(plain)), text_limit),
                    }
                )
            else:
                results.append({"type": output_type, "keys": list(data.keys())})
        elif output_type == OutputType.ERROR:
            traceback = output.get("traceback", [])
            if isinstance(traceback, list):
                traceback = [
                    _bounded_text(strip_ansi(str(line)), text_limit)
                    for line in traceback
                ]
            results.append(
                {
                    "type": OutputType.ERROR,
                    "ename": output.get("ename", ""),
                    "evalue": _bounded_text(str(output.get("evalue", "")), text_limit),
                    "traceback": traceback,
                }
            )
    return results


def _bounded_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


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
    return "\n".join(parts)
