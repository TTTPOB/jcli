"""Shared enum definitions for jupyter_jcli.

All enums use ``class X(str, Enum)`` so members are simultaneously str instances.
This means ``json.dumps``, f-strings, and nbformat API calls all work without
``.value`` — the serialized output is identical to the raw string.

Enums that are constrained by external protocols (nbformat, Jupyter REST API,
Claude Code hooks) carry a note in their docstring. Changing their values requires
synchronizing with those protocols.
"""

from __future__ import annotations

from enum import Enum


class DriftStatus(str, Enum):
    """Status of a py/ipynb pair drift check.

    Produced by ``check_drift()`` and consumed by hook handlers.
    """
    IN_SYNC = "in_sync"
    MERGED = "merged"
    CONFLICT = "conflict"
    DRIFT_ONLY = "drift_only"


class CellType(str, Enum):
    """Cell type as stored in .ipynb / py:percent files."""
    CODE = "code"
    MARKDOWN = "markdown"
    RAW = "raw"


class OutputType(str, Enum):
    """Kernel output type.

    Values must match nbformat cell output types exactly.
    Source: https://nbformat.readthedocs.io/en/latest/format_description.html
    """
    STREAM = "stream"
    EXECUTE_RESULT = "execute_result"
    DISPLAY_DATA = "display_data"
    ERROR = "error"
    # Derived types used internally after processing (not raw nbformat types)
    IMAGE = "image"
    HTML = "html"


class MergeMode(str, Enum):
    """How a DriftResult with status=MERGED was produced."""
    THREE_WAY = "three_way"
    PY_WINS_NO_BASE = "py_wins_no_base"


class ResponseStatus(str, Enum):
    """Status field emitted in JSON responses by all j-cli commands."""
    OK = "ok"
    NOOP = "noop"
    ERROR = "error"
