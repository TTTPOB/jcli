"""Source normalization shared by percent serialization and execution checks."""


def normalize_cell_source(source: str) -> str:
    """Apply percent-format trailing blank-line normalization only."""
    lines = source.splitlines()
    while lines and (not lines[-1] or lines[-1].isspace()):
        lines.pop()
    return "\n".join(lines)
