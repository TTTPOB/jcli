"""Format-independent notebook document model."""

from dataclasses import dataclass, field

from jupyter_jcli._enums import CellType


@dataclass
class Cell:
    """A single cell parsed from a file."""

    index: int
    cell_type: CellType
    source: str
    source_start_line: int | None = None
    source_end_line: int | None = None

    def __post_init__(self) -> None:
        self.cell_type = CellType(self.cell_type)


@dataclass
class ParsedFile:
    """Parsed file with cells and metadata."""

    kernel_name: str | None
    cells: list[Cell] = field(default_factory=list)
    source_path: str = ""
    paired_ipynb: str | None = None
    front_matter_raw: str | None = None
    is_py_percent: bool = False
    kernel_display_name: str | None = None
    kernel_language: str | None = None
