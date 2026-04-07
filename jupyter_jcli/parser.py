"""Parse py:percent and .ipynb files into cells."""

from dataclasses import dataclass, field
from pathlib import Path
import re

import nbformat


@dataclass
class Cell:
    """A single cell parsed from a file."""
    index: int
    cell_type: str  # "code" or "markdown"
    source: str


@dataclass
class ParsedFile:
    """Parsed file with cells and metadata."""
    kernel_name: str | None
    cells: list[Cell] = field(default_factory=list)
    source_path: str = ""
    paired_ipynb: str | None = None


def parse_cell_spec(spec: str, num_cells: int) -> list[int]:
    """Parse a cell spec string into a list of cell indices.

    Supported formats:
        "3"     -> [3]
        "3:7"   -> [3, 4, 5, 6]
        "3:"    -> [3, 4, ..., num_cells-1]
        ":5"    -> [0, 1, 2, 3, 4]
    """
    spec = spec.strip()
    if ":" in spec:
        parts = spec.split(":", 1)
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if parts[1] else num_cells
        return list(range(start, min(end, num_cells)))
    return [int(spec)]


def find_paired_ipynb(py_path: Path) -> Path | None:
    """Find the paired .ipynb for a .py file.

    foo.py -> foo.ipynb
    foo.dummy.py -> foo.ipynb
    """
    stem = py_path.stem
    # Handle .dummy.py pattern
    if stem.endswith(".dummy"):
        stem = stem[: -len(".dummy")]
    ipynb_path = py_path.parent / f"{stem}.ipynb"
    return ipynb_path if ipynb_path.exists() else None


def parse_py_percent(path: str) -> ParsedFile:
    """Parse a py:percent format file into cells.

    Extracts kernel name from YAML front matter and splits on # %% markers.
    """
    text = Path(path).read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    kernel_name = None
    content_start = 0

    # Extract YAML front matter between # --- markers
    if lines and lines[0].strip() == "# ---":
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "# ---":
                front_matter = "".join(lines[1:i])
                # Simple extraction of kernelspec name
                match = re.search(r"name:\s*(\S+)", front_matter)
                if match:
                    # Take the last match (kernelspec.name, not jupytext name)
                    for m in re.finditer(r"name:\s*(\S+)", front_matter):
                        kernel_name = m.group(1)
                content_start = i + 1
                break

    # Split remaining content on # %% markers
    cells: list[Cell] = []
    current_lines: list[str] = []
    current_type = "code"
    cell_index = 0

    for line in lines[content_start:]:
        stripped = line.rstrip()
        if stripped.startswith("# %%"):
            # Save previous cell if it has content
            if current_lines:
                source = "".join(current_lines).strip()
                if source:
                    cells.append(Cell(index=cell_index, cell_type=current_type, source=source))
                    cell_index += 1

            # Determine cell type
            if "[markdown]" in stripped.lower():
                current_type = "markdown"
            else:
                current_type = "code"
            current_lines = []
        else:
            current_lines.append(line)

    # Don't forget the last cell
    if current_lines:
        source = "".join(current_lines).strip()
        if source:
            cells.append(Cell(index=cell_index, cell_type=current_type, source=source))

    # Strip leading comment markers from markdown cells
    for cell in cells:
        if cell.cell_type == "markdown":
            cell.source = re.sub(r"^# ?", "", cell.source, flags=re.MULTILINE)

    py_path = Path(path)
    return ParsedFile(
        kernel_name=kernel_name,
        cells=cells,
        source_path=path,
        paired_ipynb=str(p) if (p := find_paired_ipynb(py_path)) else None,
    )


def parse_ipynb(path: str) -> ParsedFile:
    """Parse a .ipynb file into cells."""
    nb = nbformat.read(path, as_version=4)
    kernel_name = nb.metadata.get("kernelspec", {}).get("name")

    cells = []
    for i, cell in enumerate(nb.cells):
        cells.append(Cell(
            index=i,
            cell_type=cell.cell_type,
            source=cell.source,
        ))

    return ParsedFile(
        kernel_name=kernel_name,
        cells=cells,
        source_path=path,
        paired_ipynb=path,  # ipynb writes back to itself
    )


def parse_file(path: str) -> ParsedFile:
    """Parse a file (auto-detect format by extension)."""
    if path.endswith(".ipynb"):
        return parse_ipynb(path)
    return parse_py_percent(path)
