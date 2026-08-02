"""Parser, emitter, and canonicalizer for the py:percent format."""

import re
from pathlib import Path
from uuid import uuid4

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
    kernel_name = None
    kernel_display_name = None
    kernel_language = None
    front_matter_raw: str | None = None
    content_start = 0

    if lines and lines[0].strip() == "# ---":
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "# ---":
                front_matter = "".join(lines[1:i])
                front_matter_raw = "".join(lines[0 : i + 1])
                match = re.search(r"^#\s+name:\s*(\S+)", front_matter, re.MULTILINE)
                if match:
                    kernel_name = match.group(1)
                match = re.search(
                    r"^#\s+display_name:\s*(.+?)\s*$", front_matter, re.MULTILINE
                )
                if match:
                    kernel_display_name = match.group(1)
                match = re.search(r"^#\s+language:\s*(\S+)", front_matter, re.MULTILINE)
                if match:
                    kernel_language = match.group(1)
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

    return ParsedFile(
        kernel_name=kernel_name,
        cells=cells,
        source_path=source_path,
        front_matter_raw=front_matter_raw,
        is_py_percent=is_py_percent,
        kernel_display_name=kernel_display_name,
        kernel_language=kernel_language,
        stable_cell_ids=stable_cell_ids,
    )


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
        parts.append(parsed.front_matter_raw.rstrip("\r\n"))
        parts.append("\n\n" if cells else "\n")
    elif parsed.kernel_name is not None:
        parts.extend(["# ---\n", "# jupyter:\n", "#   kernelspec:\n"])
        if parsed.kernel_display_name is not None:
            parts.append(f"#     display_name: {parsed.kernel_display_name}\n")
        if parsed.kernel_language is not None:
            parts.append(f"#     language: {parsed.kernel_language}\n")
        parts.extend(
            [
                f"#     name: {parsed.kernel_name}\n",
                "# ---\n",
                "\n" if cells else "",
            ]
        )

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


def canonicalize(text: str, *, include_cell_ids: bool | None = None) -> str:
    """Normalize py:percent text through a parser and emitter round trip."""
    parsed = loads(text)
    if not parsed.is_py_percent:
        return text
    parsed.front_matter_raw = None
    parsed.kernel_display_name = None
    parsed.kernel_language = None
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
