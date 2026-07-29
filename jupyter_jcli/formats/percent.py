"""Parser, emitter, and canonicalizer for the py:percent format."""

import ast
import io
import re
import tokenize
from pathlib import Path
from uuid import uuid4

from jupyter_jcli._enums import CellType
from jupyter_jcli.formats.model import Cell, ParsedFile

_CELL_MARKER_RE = re.compile(r"^# %%(?:\s|$)")
_CELL_ID_OPTION_RE = re.compile(r'(?:^|\s)id=(?:"([^"]*)"|(\S+))(?=\s|$)')
_VALID_CELL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_COMMENTED_CELL_MAGIC_BODY = "# jupyter-jcli: commented cell magic body"
_MAGIC_PLACEHOLDER = "pass  # jupyter-jcli: IPython magic placeholder"
_HELP_END_RE = re.compile(
    r"(%{0,2}(?!\d)[\w*]+(?:\.(?!\d)[\w*]+|\[-?[0-9]+\])*)(\?\??)$"
)
_PYTHON_BODY_CELL_MAGICS = {
    "%%capture",
    "%%debug",
    "%%file",
    "%%prun",
    "%%pypy",
    "%%python",
    "%%python2",
    "%%python3",
    "%%time",
    "%%timeit",
    "%%writefile",
}


def _tokens_by_logical_line(source: str) -> list[list[tokenize.TokenInfo]]:
    groups: list[list[tokenize.TokenInfo]] = [[]]
    paren_level = 0
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for token in tokens:
            groups[-1].append(token)
            if token.type == tokenize.NEWLINE or (
                token.type == tokenize.NL and paren_level <= 0
            ):
                groups.append([])
            elif token.string in {"(", "[", "{"}:
                paren_level += 1
            elif token.string in {")", "]", "}"} and paren_level > 0:
                paren_level -= 1
    except (IndentationError, SyntaxError, tokenize.TokenError):
        pass
    return [group for group in groups if group]


def _significant_tokens(tokens: list[tokenize.TokenInfo]) -> list[tokenize.TokenInfo]:
    ignored = {
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.NEWLINE,
        tokenize.NL,
        tokenize.ENDMARKER,
        tokenize.COMMENT,
    }
    return [
        token
        for token in tokens
        if token.type not in ignored and not token.string.isspace()
    ]


def _is_supported_magic(tokens: list[tokenize.TokenInfo], source: str) -> bool:
    # This subset mirrors IPython's token transformers without importing IPython.
    if tokens[0].string in {"%", "!", "?", ",", ";", "/"}:
        return True

    if tokens[-1].string == "?" and _HELP_END_RE.search(source.rstrip()):
        return True

    paren_level = 0
    for index, token in enumerate(tokens):
        if token.string in {"(", "[", "{"}:
            paren_level += 1
        elif token.string in {")", "]", "}"}:
            paren_level = max(0, paren_level - 1)
        elif token.string == "=" and paren_level == 0:
            if index + 1 >= len(tokens):
                return False
            rhs = tokens[index + 1]
            if rhs.string == "!":
                return index > 0
            return (
                rhs.string == "%"
                and index + 2 < len(tokens)
                and tokens[index + 2].type == tokenize.NAME
            )
    return False


def _find_first_magic_range(lines: list[str]) -> tuple[int, int] | None:
    for logical_line in _tokens_by_logical_line("".join(lines)):
        tokens = _significant_tokens(logical_line)
        if not tokens:
            continue

        start = min(token.start[0] for token in logical_line) - 1
        end = max(token.end[0] for token in logical_line) - 1
        if _is_supported_magic(tokens, "".join(lines[start : end + 1])):
            return start, end
    return None


def _magic_ranges(source: str) -> list[tuple[int, int]]:
    shadow = source.splitlines(keepends=True)
    ranges: list[tuple[int, int]] = []
    for _ in range(500):
        found = _find_first_magic_range(shadow)
        if found is None:
            return ranges
        start, end = found
        ranges.append(found)
        indent = shadow[start][: len(shadow[start]) - len(shadow[start].lstrip())]
        shadow[start] = f"{indent}pass\n"
        for index in range(start + 1, end + 1):
            shadow[index] = "\n"
    raise RuntimeError("IPython magic detection did not converge")


def _uncomment_once(line: str) -> str:
    unindented = line.lstrip()
    indent = line[: len(line) - len(unindented)]
    if unindented.startswith("# "):
        return indent + unindented[2:]
    if unindented.startswith("#"):
        return indent + unindented[1:]
    return line


def _fully_uncomment(line: str) -> str:
    previous = line
    while previous.lstrip().startswith("#"):
        current = _uncomment_once(previous)
        if current == previous:
            break
        previous = current
    return previous


def _encoded_magic_ranges(lines: list[str]) -> list[tuple[int, int]]:
    probe = [
        _fully_uncomment(line) if line.lstrip().startswith("#") else line
        for line in lines
    ]
    return [
        (start, end)
        for start, end in _magic_ranges("".join(probe))
        if all(lines[index].lstrip().startswith("#") for index in range(start, end + 1))
    ]


def _uncomment_magic_lines(lines: list[str]) -> list[str]:
    for start, end in _encoded_magic_ranges(lines):
        for index in range(start, end + 1):
            lines[index] = _uncomment_once(lines[index])
    return lines


def _transform_ipython_magics(source: str, *, comment: bool) -> str:
    lines = source.splitlines(keepends=True)
    if not comment:
        encoded_ranges = _encoded_magic_ranges(lines)
        for index in range(len(lines) - 2, -1, -1):
            if lines[index].strip() == _MAGIC_PLACEHOLDER and any(
                start == index + 1 for start, _ in encoded_ranges
            ):
                lines.pop(index)

    first_content_index = next(
        (index for index, line in enumerate(lines) if line.strip()), None
    )
    first_content = (
        lines[first_content_index] if first_content_index is not None else ""
    )
    if comment:
        cell_magic_match = re.match(r"^\s*(%%{1,2}[A-Za-z]+)", first_content)
    else:
        cell_magic_match = re.match(r"^\s*# (%%{1,2}[A-Za-z]+)", first_content)

    commented_body_marker = (
        not comment
        and first_content_index is not None
        and first_content_index + 1 < len(lines)
        and lines[first_content_index + 1].rstrip() == _COMMENTED_CELL_MAGIC_BODY
    )
    if commented_body_marker:
        marker_index = first_content_index + 1
        marker_was_last_line = marker_index == len(lines) - 1
        lines.pop(marker_index)
        if marker_was_last_line:
            lines[first_content_index] = _without_final_line_ending(
                lines[first_content_index]
            )

    comment_entire_cell = False
    if cell_magic_match:
        if comment:
            body = "".join(lines[first_content_index + 1 :])
            python_body = cell_magic_match.group(1) in _PYTHON_BODY_CELL_MAGICS
            try:
                ast.parse(body)
            except SyntaxError:
                python_body = False
            comment_entire_cell = not python_body
        else:
            comment_entire_cell = commented_body_marker

    if comment_entire_cell:
        if not comment:
            return "".join(_uncomment_once(line) for line in lines)
        target_ranges = [(0, len(lines) - 1)]
    elif comment:
        target_ranges = _magic_ranges("".join(lines)) + _encoded_magic_ranges(lines)
        target_ranges.sort()
    else:
        return "".join(_uncomment_magic_lines(lines))

    placeholder_indices: list[int] = []
    for start, end in target_ranges:
        for index in range(start, end + 1):
            line = lines[index]
            if not line.strip():
                continue
            if index > start:
                lines[index] = f"# {line}"
            else:
                unindented = line.lstrip()
                indent = line[: len(line) - len(unindented)]
                lines[index] = f"{indent}# {unindented}"
        indent = lines[start][: len(lines[start]) - len(lines[start].lstrip())]
        if indent and not comment_entire_cell:
            placeholder_indices.append(start)

    if comment and comment_entire_cell and first_content_index is not None:
        if not lines[first_content_index].endswith(("\n", "\r")):
            lines[first_content_index] += "\n"
        lines.insert(first_content_index + 1, _COMMENTED_CELL_MAGIC_BODY + "\n")
    for index in reversed(placeholder_indices):
        indent = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
        lines.insert(index, f"{indent}{_MAGIC_PLACEHOLDER}\n")

    return "".join(lines)


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
            source = _transform_ipython_magics(source, comment=False)
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
                _transform_ipython_magics(cell.source, comment=True)
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
