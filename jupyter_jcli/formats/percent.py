"""Parser, emitter, and canonicalizer for the py:percent format."""

import math
import re
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import yaml

from jupyter_jcli._enums import CellType
from jupyter_jcli.formats.ipython_magics import transform_ipython_magics
from jupyter_jcli.formats.model import Cell, ParsedFile

_CELL_MARKER_RE = re.compile(r"^# %%(?:\s|$)")
_CELL_ID_OPTION_RE = re.compile(r'(?:^|\s)id=(?:"([^"]*)"|(\S+))(?=\s|$)')
_VALID_CELL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _without_final_line_ending(text: str) -> str:
    if text.endswith("\r\n"):
        return text[:-2]
    if text.endswith(("\n", "\r")):
        return text[:-1]
    return text


def _without_trailing_blank_lines(source: str) -> str:
    lines = source.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def loads(text: str, *, source_path: str = "") -> ParsedFile:
    """Parse py:percent text into the shared document model."""
    lines = text.splitlines(keepends=True)
    notebook_metadata: dict = {}
    front_matter_raw: str | None = None
    content_start = 0

    if lines and lines[0].strip() == "# ---":
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "# ---":
                front_matter = "".join(lines[1:i])
                front_matter_raw = "".join(lines[0 : i + 1])
                document = _load_front_matter_document(front_matter)
                raw_metadata = document.get("jupyter", {})
                if raw_metadata is None:
                    raw_metadata = {}
                if not isinstance(raw_metadata, dict):
                    raise ValueError("front matter jupyter field must be a mapping")
                notebook_metadata = deepcopy(raw_metadata)
                kernelspec = notebook_metadata.get("kernelspec", {})
                if kernelspec is None:
                    kernelspec = {}
                if not isinstance(kernelspec, dict):
                    raise ValueError("metadata.kernelspec must be a mapping")
                content_start = i + 1
                break

    cells: list[Cell] = []
    current_lines: list[str] = []
    current_type = CellType.CODE
    found_percent_marker = False
    current_start_line = content_start + 1
    current_cell_id: str | None = None
    stable_cell_ids: set[str] = set()

    def parse_cell_id(marker: str) -> str | None:
        matches = list(_CELL_ID_OPTION_RE.finditer(marker))
        if not matches:
            return None
        if len(matches) > 1:
            raise ValueError("Cell marker contains multiple id options")
        match = matches[0]
        cell_id = match.group(1) if match.group(1) is not None else match.group(2)
        if not _VALID_CELL_ID_RE.fullmatch(cell_id):
            raise ValueError(f"Invalid cell id: {cell_id!r}")
        if cell_id in stable_cell_ids:
            return None
        return cell_id

    def append_cell(
        raw_lines: list[str],
        start_line: int,
        *,
        preserve_empty: bool = False,
        has_separator: bool = False,
    ) -> None:
        source_lines = raw_lines.copy()
        if has_separator and source_lines and not source_lines[-1].strip():
            source_lines.pop()
        source = _without_final_line_ending("".join(source_lines))
        if not source.strip():
            if preserve_empty:
                cell = Cell(
                    index=len(cells),
                    cell_type=current_type,
                    source="",
                    has_stable_id=current_cell_id is not None,
                )
                if current_cell_id is not None:
                    cell.node.id = current_cell_id
                    stable_cell_ids.add(current_cell_id)
                cells.append(cell)
            return
        first_content_line = next(
            index for index, raw_line in enumerate(source_lines) if raw_line.strip()
        )
        last_content_line = next(
            index
            for index, raw_line in reversed(list(enumerate(source_lines)))
            if raw_line.strip()
        )
        if current_type in (CellType.MARKDOWN, CellType.RAW):
            source = re.sub(r"^# ?", "", source, flags=re.MULTILINE)
        else:
            source = transform_ipython_magics(source, comment=False)
        cell = Cell(
            index=len(cells),
            cell_type=current_type,
            source=source,
            source_start_line=start_line + first_content_line,
            source_end_line=start_line + last_content_line,
            has_stable_id=current_cell_id is not None,
        )
        if current_cell_id is not None:
            cell.node.id = current_cell_id
            stable_cell_ids.add(current_cell_id)
        cells.append(cell)

    for line_number, line in enumerate(lines[content_start:], content_start + 1):
        stripped = line.rstrip()
        if _CELL_MARKER_RE.match(stripped):
            append_cell(
                current_lines,
                current_start_line,
                preserve_empty=found_percent_marker,
                has_separator=True,
            )
            found_percent_marker = True
            tag = stripped[4:].strip().lower()
            current_cell_id = parse_cell_id(stripped[4:].strip())
            if "[markdown]" in tag:
                current_type = CellType.MARKDOWN
            elif "[raw]" in tag:
                current_type = CellType.RAW
            else:
                current_type = CellType.CODE
            current_lines = []
            current_start_line = line_number + 1
        else:
            current_lines.append(line)
    append_cell(
        current_lines,
        current_start_line,
        preserve_empty=found_percent_marker,
        has_separator=True,
    )

    is_py_percent = front_matter_raw is not None or found_percent_marker
    if not is_py_percent:
        for cell in cells:
            cell.source_start_line = None
            cell.source_end_line = None

    parsed = ParsedFile(
        cells=cells,
        source_path=source_path,
        front_matter_raw=front_matter_raw,
        is_py_percent=is_py_percent,
        stable_cell_ids=stable_cell_ids,
    )
    parsed.notebook.metadata.update(notebook_metadata)
    return parsed


def load(path: str | Path) -> ParsedFile:
    """Read and parse a py:percent file."""
    source_path = str(path)
    return loads(Path(path).read_text(encoding="utf-8"), source_path=source_path)


def dumps(
    parsed: ParsedFile,
    *,
    include_cell_ids: bool = True,
    assign_missing_ids: bool = True,
) -> str:
    """Serialize a document as py:percent text."""
    parts: list[str] = []
    cells = parsed.cells
    if parsed.front_matter_raw is not None:
        front_matter = _front_matter_for_metadata(
            parsed.front_matter_raw, parsed.notebook.metadata
        )
        if front_matter is not None:
            parts.append(front_matter.rstrip("\r\n"))
            parts.append("\n\n" if cells else "\n")
    elif parsed.notebook.metadata:
        parts.append(_dump_front_matter_document({"jupyter": parsed.notebook.metadata}))
        parts.append("\n" if cells else "")

    emitted_ids: set[str] = set()
    for index, cell in enumerate(cells):
        if index:
            parts.append("\n")
        cell_id = cell.cell_id
        if cell_id is not None and (
            not _VALID_CELL_ID_RE.fullmatch(cell_id) or cell_id in emitted_ids
        ):
            cell_id = None
        if include_cell_ids and cell_id is None and assign_missing_ids:
            cell_id = _new_unique_cell_id(parsed.stable_cell_ids | emitted_ids)
            cell.node.id = cell_id
            parsed.stable_cell_ids.add(cell_id)
        if cell_id is not None:
            emitted_ids.add(cell_id)
        id_option = f' id="{cell_id}"' if include_cell_ids and cell_id else ""
        if cell.cell_type == CellType.CODE:
            parts.append(f"# %%{id_option}\n")
            source = _without_trailing_blank_lines(
                transform_ipython_magics(cell.source, comment=True)
            )
            if source:
                parts.extend([source, "\n"])
        else:
            marker = (
                "markdown"
                if cell.cell_type == CellType.MARKDOWN
                else cell.cell_type.value
            )
            parts.append(f"# %% [{marker}]{id_option}\n")
            source = _without_trailing_blank_lines(cell.source)
            for line in source.splitlines():
                parts.append(f"# {line}\n" if line else "#\n")
    return "".join(parts)


def dump(parsed: ParsedFile, path: str | Path) -> None:
    """Serialize a document to a py:percent file."""
    Path(path).write_text(dumps(parsed), encoding="utf-8")


def update_front_matter_metadata(
    front_matter: str | None, metadata: dict
) -> str | None:
    """Replace the structured jupyter subtree and retain other header fields."""
    if front_matter is None:
        return None
    document = _front_matter_document(front_matter)
    if metadata:
        document["jupyter"] = deepcopy(metadata)
    else:
        document.pop("jupyter", None)
    return _dump_front_matter_document(document)


def _front_matter_for_metadata(front_matter: str, metadata: dict) -> str | None:
    """Keep an unchanged raw template or update its authoritative metadata."""
    document = _front_matter_document(front_matter)
    current = document.get("jupyter", {})
    if current is None:
        current = {}
    if not isinstance(current, dict):
        raise TypeError("front matter jupyter field must be a mapping")
    if current == metadata:
        return front_matter
    return update_front_matter_metadata(front_matter, metadata)


def _front_matter_document(front_matter: str) -> dict:
    return _load_front_matter_document(
        "".join(front_matter.splitlines(keepends=True)[1:-1])
    )


def _load_front_matter_document(commented_yaml: str) -> dict:
    lines = []
    for line in commented_yaml.splitlines():
        if not line.startswith("#"):
            raise ValueError("front matter lines must be comments")
        lines.append(line[2:] if line.startswith("# ") else line[1:])
    loaded = yaml.safe_load("\n".join(lines)) if lines else {}
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise TypeError("front matter must be a mapping")
    return loaded


def _dump_front_matter_document(document: dict) -> str:
    yaml_text = yaml.safe_dump(
        _to_plain(document),
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=True,
    )
    body = "".join(f"# {line}\n" if line else "#\n" for line in yaml_text.splitlines())
    return f"# ---\n{body}# ---\n"


def _to_plain(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("metadata mapping keys must be strings")
            result[key] = _to_plain(item)
        return result
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("metadata numbers must be finite")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"metadata value is not JSON-compatible: {type(value).__name__}")


def canonicalize(text: str, *, include_cell_ids: bool | None = None) -> str:
    """Normalize py:percent text through a parser and emitter round trip."""
    parsed = loads(text)
    if not parsed.is_py_percent:
        return text
    parsed.front_matter_raw = None
    return dumps(
        parsed,
        include_cell_ids=include_cell_ids is not False,
        assign_missing_ids=include_cell_ids is True,
    )


def _new_unique_cell_id(existing: set[str]) -> str:
    while True:
        cell_id = uuid4().hex[:8]
        if cell_id not in existing:
            return cell_id
