"""Shared semantic state for py:percent / notebook pairs."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from jupyter_jcli.formats import percent
from jupyter_jcli.formats.model import Cell, ParsedFile
from jupyter_jcli.metadata import json_compatible_copy, json_equal

# These paths are local/runtime state, not pair-shared notebook configuration.
_EXCLUDED_TOP_LEVEL = frozenset({"widgets", "jupytext", "vscode", "colab"})
_KERNEL_METADATA_KEYS = frozenset({"kernelspec", "language_info"})
_MISSING = object()


@dataclass(eq=False)
class PairState:
    """Cell sources and explicitly projected notebook-level metadata."""

    cells: list[Cell]
    metadata: dict[str, Any]
    include_cell_ids: bool = True

    @classmethod
    def from_parsed(
        cls, parsed: ParsedFile, *, include_cell_ids: bool = True
    ) -> PairState:
        """Project a parsed representation onto pair-shared semantics."""
        return cls(
            cells=[deepcopy(cell) for cell in parsed.cells],
            metadata=project_shared_metadata(parsed.notebook.metadata),
            include_cell_ids=include_cell_ids,
        )

    @property
    def kernel_name(self) -> str | None:
        """Return the shared kernelspec name, when representable as text."""
        value = self.metadata.get("kernelspec", {}).get("name")
        return str(value) if value is not None else None

    def cell_text(self) -> str:
        """Return deterministic metadata-free canonical text for cell merging."""
        parsed = ParsedFile(cells=[deepcopy(cell) for cell in self.cells])
        return percent.dumps(
            parsed,
            include_cell_ids=self.include_cell_ids,
            assign_missing_ids=False,
        )

    def canonical_text(self) -> str:
        """Return deterministic baseline text for the complete shared state."""
        parsed = ParsedFile(cells=[deepcopy(cell) for cell in self.cells])
        parsed.notebook.metadata = deepcopy(self.metadata)
        return percent.dumps(
            parsed,
            include_cell_ids=self.include_cell_ids,
            assign_missing_ids=False,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PairState):
            return NotImplemented
        return json_equal(self.metadata, other.metadata) and (
            self.cell_text() == other.cell_text()
        )


@dataclass(frozen=True)
class MetadataConflict:
    """A three-way conflict at one shared metadata path."""

    path: tuple[str, ...]
    base: object
    py: object
    notebook: object

    @property
    def display_path(self) -> str:
        return "metadata." + ".".join(self.path)


def project_shared_metadata(metadata: dict) -> dict[str, Any]:
    """Remove the centralized set of local/runtime metadata paths."""
    projected = {
        key: json_compatible_copy(value)
        for key, value in metadata.items()
        if key not in _EXCLUDED_TOP_LEVEL and isinstance(key, str)
    }
    invalid_keys = [key for key in metadata if not isinstance(key, str)]
    if invalid_keys:
        raise TypeError("metadata mapping keys must be strings")
    language_info = projected.get("language_info")
    if isinstance(language_info, dict):
        language_info.pop("version", None)
        if not language_info:
            projected.pop("language_info", None)
    return projected


def metadata_with_local_fields(current: dict, shared: dict[str, Any]) -> dict[str, Any]:
    """Overlay shared metadata while retaining valid representation-local fields."""
    result = deepcopy(shared)
    old_kernel = project_shared_metadata(current).get("kernelspec", {})
    new_kernel = shared.get("kernelspec", {})
    old_name = old_kernel.get("name") if isinstance(old_kernel, dict) else None
    new_name = new_kernel.get("name") if isinstance(new_kernel, dict) else None
    kernel_changed = not json_equal(old_name, new_name)

    for key in _EXCLUDED_TOP_LEVEL:
        if key in current and not (kernel_changed and key == "widgets"):
            result[key] = deepcopy(current[key])
    current_language = current.get("language_info")
    target_language = result.get("language_info")
    if (
        not kernel_changed
        and isinstance(current_language, dict)
        and "version" in current_language
        and isinstance(target_language, dict)
        and isinstance(target_language.get("name"), str)
        and target_language["name"]
    ):
        target_language["version"] = deepcopy(current_language["version"])
    return result


def merge_metadata(
    base: dict[str, Any], py: dict[str, Any], notebook: dict[str, Any]
) -> tuple[dict[str, Any] | None, list[MetadataConflict]]:
    """Three-way merge shared metadata with one atomic kernel configuration."""
    base = deepcopy(base)
    py = deepcopy(py)
    notebook = deepcopy(notebook)
    conflicts: list[MetadataConflict] = []
    merged: dict[str, Any] = {}

    base_kernel = {key: base.pop(key) for key in _KERNEL_METADATA_KEYS if key in base}
    py_kernel = {key: py.pop(key) for key in _KERNEL_METADATA_KEYS if key in py}
    nb_kernel = {
        key: notebook.pop(key) for key in _KERNEL_METADATA_KEYS if key in notebook
    }
    kernel = _merge_atomic(("kernel",), base_kernel, py_kernel, nb_kernel, conflicts)
    if kernel is not _MISSING:
        merged.update(kernel)

    ordinary = _merge_value((), base, py, notebook, conflicts)
    if ordinary is not _MISSING:
        merged.update(ordinary)
    return (None if conflicts else merged), conflicts


def _copy_value(value):
    return _MISSING if value is _MISSING else deepcopy(value)


def _merge_atomic(path, base, py, notebook, conflicts):
    if json_equal(py, notebook):
        return _copy_value(py)
    if json_equal(py, base):
        return _copy_value(notebook)
    if json_equal(notebook, base):
        return _copy_value(py)
    conflicts.append(
        MetadataConflict(
            path=path,
            base=_display_value(base),
            py=_display_value(py),
            notebook=_display_value(notebook),
        )
    )
    return _MISSING


def _merge_value(path, base, py, notebook, conflicts):
    if json_equal(py, notebook):
        return _copy_value(py)
    if json_equal(py, base):
        return _copy_value(notebook)
    if json_equal(notebook, base):
        return _copy_value(py)
    if all(
        value is _MISSING or isinstance(value, dict) for value in (base, py, notebook)
    ):
        merged = {}
        keys = set()
        for value in (base, py, notebook):
            if isinstance(value, dict):
                keys.update(value)
        for key in sorted(keys, key=str):
            value = _merge_value(
                (*path, str(key)),
                base.get(key, _MISSING) if isinstance(base, dict) else _MISSING,
                py.get(key, _MISSING) if isinstance(py, dict) else _MISSING,
                notebook.get(key, _MISSING) if isinstance(notebook, dict) else _MISSING,
                conflicts,
            )
            if value is not _MISSING:
                merged[key] = value
        return merged
    conflicts.append(
        MetadataConflict(
            path=path,
            base=_display_value(base),
            py=_display_value(py),
            notebook=_display_value(notebook),
        )
    )
    return _MISSING


def _display_value(value):
    return "<missing>" if value is _MISSING else deepcopy(value)
