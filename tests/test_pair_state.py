"""Tests for shared py/notebook pair state."""

from jupyter_jcli.formats.model import Cell, ParsedFile
from jupyter_jcli.pair_state import KernelConflict, PairState, merge_kernel


def _state(kernel: str | None, source: str = "x = 1") -> PairState:
    return PairState.from_parsed(
        ParsedFile(kernel_name=kernel, cells=[Cell(0, "code", source)]),
        include_cell_ids=False,
    )


def test_pair_state_ignores_kernel_description_and_cell_metadata():
    left = _state("env")
    right = _state("env")
    left.kernel_info = left.kernel_info.__class__("env", "Left", "python")
    right.kernel_info = right.kernel_info.__class__("env", "Right", "julia")
    left.cells[0].node.metadata["tags"] = ["left"]

    assert left == right


def test_canonical_text_contains_kernel_but_cell_text_does_not():
    state = _state("env")

    assert "name: env" in state.canonical_text()
    assert "name: env" not in state.cell_text()


def test_kernel_three_way_merge_selects_changed_side_description():
    base = _state("base")
    py = _state("base")
    notebook = _state("new")
    notebook.kernel_info = notebook.kernel_info.__class__("new", "New Kernel", "julia")

    name, info = merge_kernel(base, py, notebook)

    assert name == "new"
    assert info == notebook.kernel_info


def test_kernel_three_way_merge_supports_removal():
    merged = merge_kernel(_state("base"), _state(None), _state("base"))

    assert merged == (None, None)


def test_kernel_three_way_merge_reports_structured_conflict():
    merged = merge_kernel(_state("base"), _state("py"), _state("notebook"))

    assert merged == KernelConflict(base="base", py="py", notebook="notebook")
