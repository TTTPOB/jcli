"""CLI selectors, pair discovery, and format auto-detection."""

from pathlib import Path

from jupyter_jcli.formats import ipynb, percent
from jupyter_jcli.formats.model import ParsedFile


def parse_cell_spec(spec: str, num_cells: int) -> list[int]:
    """Parse a cell index or slice expression."""
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
    """Compute the paired .ipynb path for a .py file."""
    stem = py_path.stem
    stem = stem.removesuffix(".dummy")
    return py_path.parent / f"{stem}.ipynb"


def find_paired_ipynb(py_path: Path) -> Path | None:
    """Find the paired .ipynb for a .py file."""
    if py_path.suffix != ".py":
        return None
    ipynb_path = ipynb_path_for_py(py_path)
    return ipynb_path if ipynb_path.exists() else None


def find_pair(path: Path) -> Path | None:
    """Find the paired file for a .py or .ipynb path."""
    if path.suffix == ".ipynb":
        stem = path.stem
        dummy = path.parent / f"{stem}.dummy.py"
        if dummy.exists():
            return dummy
        py_path = path.parent / f"{stem}.py"
        return py_path if py_path.exists() else None
    return find_paired_ipynb(path)


def parse_file(path: str) -> ParsedFile:
    """Parse a supported file and attach pair discovery information."""
    if path.endswith(".ipynb"):
        return ipynb.load(path)
    parsed = percent.load(path)
    py_path = Path(path)
    paired = find_paired_ipynb(py_path)
    parsed.paired_ipynb = str(paired) if paired else None
    return parsed
