"""End-to-end convergence tests for the shared pair synchronization flow."""

import subprocess

import nbformat

from jupyter_jcli.diff import Conflict, InSync, check_drift
from jupyter_jcli.formats import percent
from jupyter_jcli.pairing import synchronize_pair


def _init_repo(path):
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)


def _py_text(name="esm_ml", display="ML", language="python", extra=""):
    return (
        "# ---\n# jupyter:\n#   kernelspec:\n"
        f"#     display_name: {display}\n#     language: {language}\n"
        f"#     name: {name}\n{extra}# ---\n\n# %%\nx = 1\n"
    )


def _notebook(name="esm_default", display="Default", language="python"):
    cell = nbformat.v4.new_code_cell("x = 1")
    cell.outputs = [nbformat.v4.new_output("stream", name="stdout", text="1\n")]
    cell.metadata["tags"] = ["local-cell"]
    notebook = nbformat.v4.new_notebook(cells=[cell])
    notebook.metadata.update(
        {
            "kernelspec": {
                "name": name,
                "display_name": display,
                "language": language,
            },
            "custom": {"shared": True},
            "vscode": {"side": "notebook"},
        }
    )
    return notebook


def test_existing_conversion_then_cell_sync_converges_and_is_idempotent(tmp_path):
    _init_repo(tmp_path)
    py_path = tmp_path / "nb.py"
    ipynb_path = tmp_path / "nb.ipynb"
    py_path.write_text(
        _py_text(extra="#   custom:\n#     shared: true\n"), encoding="utf-8"
    )
    notebook = _notebook()
    original_id = notebook.cells[0].id
    nbformat.write(notebook, str(ipynb_path))

    converted = synchronize_pair(py_path, ipynb_path, authoritative="py")

    assert converted.ipynb_changed is True
    converted_nb = nbformat.read(str(ipynb_path), as_version=4)
    assert converted_nb.metadata["kernelspec"]["name"] == "esm_ml"
    assert converted_nb.cells[0].outputs[0].text == "1\n"
    assert converted_nb.cells[0].metadata["tags"] == ["local-cell"]
    assert converted_nb.cells[0].id == original_id
    assert converted_nb.metadata["vscode"] == {"side": "notebook"}
    assert isinstance(check_drift(py_path, ipynb_path), InSync)

    py_path.write_text(
        py_path.read_text(encoding="utf-8") + "\n# %%\ny = 2\n",
        encoding="utf-8",
    )
    synced = synchronize_pair(py_path, ipynb_path)
    assert synced.ipynb_changed is True
    assert [cell.source for cell in nbformat.read(ipynb_path, as_version=4).cells] == [
        "x = 1",
        "y = 2",
    ]
    assert isinstance(check_drift(py_path, ipynb_path), InSync)

    before = (py_path.read_bytes(), ipynb_path.read_bytes())
    repeated = synchronize_pair(py_path, ipynb_path)
    assert repeated.py_changed is False
    assert repeated.ipynb_changed is False
    assert (py_path.read_bytes(), ipynb_path.read_bytes()) == before


def test_kernel_configuration_changes_sync_in_both_directions(tmp_path):
    _init_repo(tmp_path)
    py_path = tmp_path / "nb.py"
    ipynb_path = tmp_path / "nb.ipynb"
    py_path.write_text(_py_text(), encoding="utf-8")
    nbformat.write(_notebook("esm_ml", "ML", "python"), str(ipynb_path))
    synchronize_pair(py_path, ipynb_path, authoritative="py")

    notebook = nbformat.read(ipynb_path, as_version=4)
    notebook.metadata["kernelspec"] = {
        "name": "esm_ml",
        "display_name": "ML Julia",
        "language": "julia",
    }
    nbformat.write(notebook, ipynb_path)
    to_py = synchronize_pair(py_path, ipynb_path)
    assert to_py.py_changed is True
    assert percent.load(py_path).notebook.metadata["kernelspec"]["language"] == "julia"

    text = py_path.read_text(encoding="utf-8").replace("ML Julia", "ML Display")
    py_path.write_text(text, encoding="utf-8")
    to_notebook = synchronize_pair(py_path, ipynb_path)
    assert to_notebook.ipynb_changed is True
    assert (
        nbformat.read(ipynb_path, as_version=4).metadata["kernelspec"]["display_name"]
        == "ML Display"
    )


def test_both_kernel_changes_report_atomic_conflict(tmp_path):
    _init_repo(tmp_path)
    py_path = tmp_path / "nb.py"
    ipynb_path = tmp_path / "nb.ipynb"
    py_path.write_text(_py_text(), encoding="utf-8")
    nbformat.write(_notebook("esm_ml", "ML", "python"), str(ipynb_path))
    synchronize_pair(py_path, ipynb_path, authoritative="py")

    py_path.write_text(
        py_path.read_text(encoding="utf-8").replace("esm_ml", "py_kernel"),
        encoding="utf-8",
    )
    notebook = nbformat.read(ipynb_path, as_version=4)
    notebook.metadata["kernelspec"] = {
        "name": "nb_kernel",
        "display_name": "Notebook Kernel",
        "language": "julia",
    }
    nbformat.write(notebook, ipynb_path)

    result = check_drift(py_path, ipynb_path)
    assert isinstance(result, Conflict)
    assert result.conflict_indices == []
    assert result.kernel_conflict is not None
    assert result.kernel_conflict.display_path == "metadata.kernel"
    assert "py_kernel" in result.diff_text
    assert "nb_kernel" in result.diff_text


def test_excluded_metadata_remains_local_without_drift(tmp_path):
    _init_repo(tmp_path)
    py_path = tmp_path / "nb.py"
    ipynb_path = tmp_path / "nb.ipynb"
    py_path.write_text(
        _py_text(extra="#   vscode:\n#     side: python\n"), encoding="utf-8"
    )
    notebook = _notebook("esm_ml", "ML", "python")
    notebook.metadata["language_info"] = {"name": "python", "version": "3.12"}
    nbformat.write(notebook, str(ipynb_path))

    synchronize_pair(py_path, ipynb_path, authoritative="py")

    assert percent.load(py_path).notebook.metadata["vscode"] == {"side": "python"}
    updated = nbformat.read(ipynb_path, as_version=4)
    assert updated.metadata["vscode"] == {"side": "notebook"}
    assert isinstance(check_drift(py_path, ipynb_path), InSync)


def test_header_key_order_and_layout_do_not_drift(tmp_path):
    py_path = tmp_path / "nb.py"
    ipynb_path = tmp_path / "nb.ipynb"
    py_path.write_text(
        "# ---\n# jupyter: {custom: {value: null}, kernelspec: {language: python, "
        "name: env, display_name: Env}}\n# ---\n\n# %%\nx = 1\n",
        encoding="utf-8",
    )
    notebook = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("x = 1")])
    notebook.metadata.update(
        {
            "kernelspec": {
                "display_name": "Env",
                "name": "env",
                "language": "python",
            },
            "custom": {"value": None},
        }
    )
    nbformat.write(notebook, ipynb_path)

    result = check_drift(py_path, ipynb_path)
    assert isinstance(result, InSync)
