"""Tests for applying shared state to both pair representations."""

import nbformat

from jupyter_jcli.formats import percent
from jupyter_jcli.formats.model import Cell
from jupyter_jcli.pair_state import KernelInfo, PairState
from jupyter_jcli.pairing import (
    apply_pair_state_to_ipynb,
    apply_pair_state_to_python,
)


def _state(name, *, display=None, language=None, source="x = 2"):
    return PairState(
        cells=[Cell(0, "code", source)],
        kernel_name=name,
        kernel_info=(KernelInfo(name, display, language) if name is not None else None),
        include_cell_ids=False,
    )


def test_python_apply_updates_managed_header_and_preserves_other_yaml(tmp_path):
    path = tmp_path / "nb.py"
    path.write_text(
        "# ---\n# jupyter:\n#   custom: keep\n#   kernelspec:\n"
        "#     display_name: Old\n#     language: python\n#     name: old\n"
        "#     resource_dir: keep\n# ---\n\n# %%\nx = 1\n",
        encoding="utf-8",
    )

    changed = apply_pair_state_to_python(
        path, _state("new", display="New", language="julia")
    )

    text = path.read_text(encoding="utf-8")
    assert changed is True
    assert "#   custom: keep" in text
    assert "#     resource_dir: keep" in text
    assert "display_name: New" in text
    assert "language: julia" in text
    assert "name: new" in text
    assert percent.loads(text).kernel_name == "new"


def test_python_apply_removes_kernel_without_dropping_raw_header(tmp_path):
    path = tmp_path / "nb.py"
    path.write_text(
        "# ---\n# jupyter:\n#   custom: keep\n#   kernelspec:\n"
        "#     name: old\n# ---\n\n# %%\nx = 1\n",
        encoding="utf-8",
    )

    apply_pair_state_to_python(path, _state(None))

    text = path.read_text(encoding="utf-8")
    assert "custom: keep" in text
    assert "name: old" not in text
    assert percent.loads(text).kernel_name is None


def test_notebook_apply_changes_kernel_and_preserves_unrelated_data(tmp_path):
    path = tmp_path / "nb.ipynb"
    cell = nbformat.v4.new_code_cell("x = 1")
    cell.outputs = [nbformat.v4.new_output("stream", name="stdout", text="1\n")]
    cell.metadata["tags"] = ["keep"]
    nb = nbformat.v4.new_notebook(cells=[cell])
    nb.metadata["custom"] = {"keep": True}
    nb.metadata["kernelspec"] = {
        "name": "old",
        "display_name": "Old",
        "language": "python",
    }
    nbformat.write(nb, str(path))

    changed = apply_pair_state_to_ipynb(
        path, _state("new", display="New", language="julia")
    )

    updated = nbformat.read(str(path), as_version=4)
    assert changed is True
    assert updated.metadata["custom"] == {"keep": True}
    assert updated.metadata["kernelspec"] == {
        "name": "new",
        "display_name": "New",
        "language": "julia",
    }
    assert updated.cells[0].outputs[0].text == "1\n"
    assert updated.cells[0].metadata["tags"] == ["keep"]


def test_notebook_apply_uses_non_python_fallback_and_supports_removal(tmp_path):
    path = tmp_path / "nb.ipynb"
    nb = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("x = 1")])
    nbformat.write(nb, str(path))

    apply_pair_state_to_ipynb(path, _state("unknown"))
    updated = nbformat.read(str(path), as_version=4)
    assert updated.metadata["kernelspec"] == {
        "name": "unknown",
        "display_name": "unknown",
    }

    apply_pair_state_to_ipynb(path, _state(None))
    updated = nbformat.read(str(path), as_version=4)
    assert "kernelspec" not in updated.metadata
