"""Tests for applying shared state to both pair representations."""

import nbformat

from jupyter_jcli.formats import percent
from jupyter_jcli.formats.model import Cell
from jupyter_jcli.pair_state import PairState
from jupyter_jcli.pairing import (
    apply_pair_state_to_ipynb,
    apply_pair_state_to_python,
)


def _state(metadata, source="x = 2"):
    return PairState(
        cells=[Cell(0, "code", source)],
        metadata=metadata,
        include_cell_ids=False,
    )


def test_python_apply_replaces_shared_metadata_and_preserves_outer_header(tmp_path):
    path = tmp_path / "nb.py"
    path.write_text(
        "# ---\n# project:\n#   owner: keep\n# jupyter:\n#   custom: old\n"
        "#   kernelspec:\n#     display_name: Old\n#     language: python\n"
        "#     name: old\n# ---\n\n# %%\nx = 1\n",
        encoding="utf-8",
    )

    changed = apply_pair_state_to_python(
        path,
        _state(
            {
                "custom": {"value": None},
                "kernelspec": {
                    "name": "new",
                    "display_name": "New",
                    "language": "julia",
                    "resource_dir": "shared",
                },
            }
        ),
    )

    parsed = percent.load(path)
    assert changed is True
    assert "owner: keep" in path.read_text(encoding="utf-8")
    assert parsed.notebook.metadata == {
        "custom": {"value": None},
        "kernelspec": {
            "name": "new",
            "display_name": "New",
            "language": "julia",
            "resource_dir": "shared",
        },
    }


def test_python_apply_removes_shared_kernel_and_keeps_excluded_metadata(tmp_path):
    path = tmp_path / "nb.py"
    path.write_text(
        "# ---\n# project: keep\n# jupyter:\n#   kernelspec:\n#     name: old\n"
        "#   vscode:\n#     local: true\n# ---\n\n# %%\nx = 1\n",
        encoding="utf-8",
    )

    apply_pair_state_to_python(path, _state({}))

    parsed = percent.load(path)
    assert "project: keep" in path.read_text(encoding="utf-8")
    assert "kernelspec" not in parsed.notebook.metadata
    assert parsed.notebook.metadata["vscode"] == {"local": True}


def test_notebook_apply_preserves_outputs_cell_metadata_and_local_metadata(tmp_path):
    path = tmp_path / "nb.ipynb"
    cell = nbformat.v4.new_code_cell("x = 1")
    cell.outputs = [nbformat.v4.new_output("stream", name="stdout", text="1\n")]
    cell.metadata["tags"] = ["keep"]
    nb = nbformat.v4.new_notebook(cells=[cell])
    nb.metadata.update(
        {
            "custom": {"old": True},
            "kernelspec": {"name": "old", "display_name": "Old"},
            "language_info": {"name": "python", "version": "3.12"},
            "vscode": {"local": True},
            "widgets": {"state": "stale"},
        }
    )
    nbformat.write(nb, str(path))

    changed = apply_pair_state_to_ipynb(
        path,
        _state(
            {
                "custom": {"new": None},
                "kernelspec": {"name": "julia", "display_name": "Julia"},
                "language_info": {"name": "julia"},
            }
        ),
    )

    updated = nbformat.read(str(path), as_version=4)
    assert changed is True
    assert updated.metadata == {
        "custom": {"new": None},
        "kernelspec": {"name": "julia", "display_name": "Julia"},
        "language_info": {"name": "julia"},
        "vscode": {"local": True},
    }
    assert updated.cells[0].outputs[0].text == "1\n"
    assert updated.cells[0].metadata["tags"] == ["keep"]


def test_notebook_apply_preserves_runtime_version_when_kernel_is_unchanged(tmp_path):
    path = tmp_path / "nb.ipynb"
    nb = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("x = 1")])
    nb.metadata.update(
        {
            "kernelspec": {"name": "env", "display_name": "Old"},
            "language_info": {"name": "python", "version": "3.12"},
        }
    )
    nbformat.write(nb, str(path))

    apply_pair_state_to_ipynb(
        path,
        _state(
            {
                "kernelspec": {"name": "env", "display_name": "New"},
                "language_info": {"name": "python"},
            }
        ),
    )

    updated = nbformat.read(str(path), as_version=4)
    assert updated.metadata["kernelspec"]["display_name"] == "New"
    assert updated.metadata["language_info"]["version"] == "3.12"
