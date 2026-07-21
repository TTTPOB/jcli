"""Parse py:percent and .ipynb files into cells."""

import ast
from dataclasses import dataclass, field
import io
from pathlib import Path
import re
from textwrap import dedent
import tokenize

from IPython.core.inputtransformer2 import TransformerManager
import nbformat

from jupyter_jcli._enums import CellType


_CELL_MARKER_RE = re.compile(r"^# %%(?:\s|$)")
_COMMENTED_CELL_MAGIC_BODY = "# jupyter-jcli: commented cell magic body"
_MAGIC_PLACEHOLDER = "pass  # jupyter-jcli: IPython magic placeholder"
_IPYTHON_TRANSFORMER = TransformerManager()
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


def _is_ipython_syntax(source: str) -> bool:
    candidate = dedent(source)
    if not candidate.endswith("\n"):
        candidate += "\n"
    try:
        return _IPYTHON_TRANSFORMER.transform_cell(candidate) != candidate
    except (IndentationError, SyntaxError, tokenize.TokenError):
        return False


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
            elif (
                token.string
                in {
                    ")",
                    "]",
                    "}",
                }
                and paren_level > 0
            ):
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


def _find_first_magic_range(lines: list[str]) -> tuple[int, int] | None:
    for logical_line in _tokens_by_logical_line("".join(lines)):
        tokens = _significant_tokens(logical_line)
        if not tokens:
            continue

        special = tokens[0].string in {"%", "!", "?"} or tokens[-1].string == "?"
        paren_level = 0
        for index, token in enumerate(tokens):
            if token.string in {"(", "[", "{"}:
                paren_level += 1
            elif token.string in {
                ")",
                "]",
                "}",
            }:
                paren_level = max(0, paren_level - 1)
            elif token.string == "=" and paren_level == 0 and index + 1 < len(tokens):
                rhs = tokens[index + 1]
                special = rhs.string in {"%", "!"}
                break

        if not special:
            continue

        start = min(token.start[0] for token in logical_line) - 1
        end = max(token.end[0] for token in logical_line) - 1
        if _is_ipython_syntax("".join(lines[start : end + 1])):
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


def _uncomment_magic_lines(lines: list[str]) -> list[str]:
    for start, end in _encoded_magic_ranges(lines):
        for index in range(start, end + 1):
            lines[index] = _uncomment_once(lines[index])
    return lines


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


def _commented_magic_ranges(lines: list[str]) -> list[tuple[int, int]]:
    return _encoded_magic_ranges(lines)


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
        (index for index, line in enumerate(lines) if line.strip()),
        None,
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
        lines.pop(first_content_index + 1)

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
        target_ranges = _magic_ranges("".join(lines)) + _commented_magic_ranges(lines)
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


def comment_ipython_magics(source: str) -> str:
    """Comment IPython magic commands for parseable py:percent output."""
    return _transform_ipython_magics(source, comment=True)


def uncomment_ipython_magics(source: str) -> str:
    """Restore IPython magic commands from py:percent comments."""
    return _transform_ipython_magics(source, comment=False)


@dataclass
class Cell:
    """A single cell parsed from a file."""

    index: int
    cell_type: CellType  # CellType.CODE, MARKDOWN, or RAW
    source: str

    def __post_init__(self) -> None:
        self.cell_type = CellType(self.cell_type)


@dataclass
class ParsedFile:
    """Parsed file with cells and metadata."""

    kernel_name: str | None
    cells: list[Cell] = field(default_factory=list)
    source_path: str = ""
    paired_ipynb: str | None = None
    front_matter_raw: str | None = None  # raw text including both # --- delimiters
    is_py_percent: bool = False  # True if file has front matter or # %% markers
    kernel_display_name: str | None = None  # kernelspec display_name (e.g. "Python 3")
    kernel_language: str | None = None  # kernelspec language (e.g. "python")


def parse_cell_spec(spec: str, num_cells: int) -> list[int]:
    """Parse a cell spec string into a list of cell indices.

    Supported formats:
        "3"     -> [3]
        "3:7"   -> [3, 4, 5, 6]
        "3:"    -> [3, 4, ..., num_cells-1]
        ":5"    -> [0, 1, 2, 3, 4]
    """
    spec = spec.strip()
    if num_cells < 0 or spec.count(":") > 1:
        raise ValueError(f"Invalid cell spec: {spec}")
    if ":" in spec:
        parts = spec.split(":")
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if parts[1] else num_cells
        if start < 0 or end < 0 or (parts[1] and start > end):
            raise ValueError(f"Invalid cell range: {spec}")
        return list(range(start, min(end, num_cells)))
    index = int(spec)
    if index < 0:
        raise ValueError(f"Invalid cell index: {spec}")
    return [index]


def ipynb_path_for_py(py_path: Path) -> Path:
    """Compute the paired .ipynb path for a .py file (no existence check).

    foo.py -> foo.ipynb
    foo.dummy.py -> foo.ipynb
    """
    stem = py_path.stem
    # Handle .dummy.py pattern
    if stem.endswith(".dummy"):
        stem = stem[: -len(".dummy")]
    return py_path.parent / f"{stem}.ipynb"


def find_paired_ipynb(py_path: Path) -> Path | None:
    """Find the paired .ipynb for a .py file.

    foo.py -> foo.ipynb
    foo.dummy.py -> foo.ipynb
    """
    ipynb_path = ipynb_path_for_py(py_path)
    return ipynb_path if ipynb_path.exists() else None


def find_pair(path: Path) -> Path | None:
    """Find the paired file for a .py or .ipynb path.

    .py / .dummy.py -> .ipynb  (via find_paired_ipynb)
    .ipynb -> .dummy.py (preferred) or .py
    """
    if path.suffix == ".ipynb":
        stem = path.stem
        dummy = path.parent / f"{stem}.dummy.py"
        if dummy.exists():
            return dummy
        py = path.parent / f"{stem}.py"
        if py.exists():
            return py
        return None
    return find_paired_ipynb(path)


def parse_py_percent_text(text: str, source_path: str = "") -> ParsedFile:
    """Parse py:percent format text into cells (no file I/O).

    Extracts kernel name from YAML front matter and splits on # %% markers.
    """
    lines = text.splitlines(keepends=True)

    kernel_name = None
    kernel_display_name = None
    kernel_language = None
    front_matter_raw: str | None = None
    content_start = 0

    # Extract YAML front matter between # --- markers
    if lines and lines[0].strip() == "# ---":
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "# ---":
                front_matter = "".join(lines[1:i])
                # Store raw block including both # --- delimiters
                front_matter_raw = "".join(lines[0 : i + 1])
                # Extract kernelspec fields (anchored to line start to avoid
                # matching "name:" as a substring of "display_name:")
                m = re.search(r"^#\s+name:\s*(\S+)", front_matter, re.MULTILINE)
                if m:
                    kernel_name = m.group(1)
                m = re.search(
                    r"^#\s+display_name:\s*(.+?)\s*$", front_matter, re.MULTILINE
                )
                if m:
                    kernel_display_name = m.group(1)
                m = re.search(r"^#\s+language:\s*(\S+)", front_matter, re.MULTILINE)
                if m:
                    kernel_language = m.group(1)
                content_start = i + 1
                break

    # Split remaining content on # %% markers
    cells: list[Cell] = []
    current_lines: list[str] = []
    current_type = CellType.CODE
    cell_index = 0
    found_percent_marker = False

    for line in lines[content_start:]:
        stripped = line.rstrip()
        if _CELL_MARKER_RE.match(stripped):
            found_percent_marker = True
            # Save previous cell if it has content
            if current_lines:
                source = "".join(current_lines).strip()
                if source:
                    cells.append(
                        Cell(index=cell_index, cell_type=current_type, source=source)
                    )
                    cell_index += 1

            # Determine cell type from marker tag
            tag = stripped[4:].strip().lower()
            if "[markdown]" in tag:
                current_type = CellType.MARKDOWN
            elif "[raw]" in tag:
                current_type = CellType.RAW
            else:
                current_type = CellType.CODE
            current_lines = []
        else:
            current_lines.append(line)

    # Don't forget the last cell
    if current_lines:
        source = "".join(current_lines).strip()
        if source:
            cells.append(Cell(index=cell_index, cell_type=current_type, source=source))

    # Strip leading comment markers from markdown and raw cells
    for cell in cells:
        if cell.cell_type in (CellType.MARKDOWN, CellType.RAW):
            cell.source = re.sub(r"^# ?", "", cell.source, flags=re.MULTILINE)
        elif cell.cell_type == CellType.CODE:
            cell.source = uncomment_ipython_magics(cell.source).strip()

    return ParsedFile(
        kernel_name=kernel_name,
        cells=cells,
        source_path=source_path,
        front_matter_raw=front_matter_raw,
        is_py_percent=front_matter_raw is not None or found_percent_marker,
        kernel_display_name=kernel_display_name,
        kernel_language=kernel_language,
    )


def parse_py_percent(path: str) -> ParsedFile:
    """Parse a py:percent format file into cells.

    Extracts kernel name from YAML front matter and splits on # %% markers.
    """
    text = Path(path).read_text(encoding="utf-8")
    parsed = parse_py_percent_text(text, source_path=path)
    py_path = Path(path)
    parsed.paired_ipynb = str(p) if (p := find_paired_ipynb(py_path)) else None
    return parsed


def parse_ipynb(path: str) -> ParsedFile:
    """Parse a .ipynb file into cells."""
    nb = nbformat.read(path, as_version=4)
    ks = nb.metadata.get("kernelspec", {})
    kernel_name = ks.get("name")
    kernel_display_name = ks.get("display_name") or None
    kernel_language = ks.get("language") or None

    cells = []
    for i, cell in enumerate(nb.cells):
        cells.append(
            Cell(
                index=i,
                cell_type=cell.cell_type,
                source=cell.source,
            )
        )

    return ParsedFile(
        kernel_name=kernel_name,
        cells=cells,
        source_path=path,
        paired_ipynb=path,  # ipynb writes back to itself
        kernel_display_name=kernel_display_name,
        kernel_language=kernel_language,
    )


def parse_file(path: str) -> ParsedFile:
    """Parse a file (auto-detect format by extension)."""
    if path.endswith(".ipynb"):
        return parse_ipynb(path)
    return parse_py_percent(path)
