"""Write execution outputs back to .ipynb files."""

from pathlib import Path

import nbformat


def convert_to_nbformat_outputs(raw_outputs: list[dict]) -> list:
    """Convert raw kernel outputs to nbformat output objects."""
    nb_outputs = []
    for output in raw_outputs:
        output_type = output.get("output_type")

        if output_type == "stream":
            text = output.get("text", "")
            if isinstance(text, list):
                text = "".join(text)
            nb_outputs.append(nbformat.v4.new_output(
                output_type="stream",
                name=output.get("name", "stdout"),
                text=str(text),
            ))

        elif output_type == "execute_result":
            nb_outputs.append(nbformat.v4.new_output(
                output_type="execute_result",
                data=output.get("data", {}),
                metadata=output.get("metadata", {}),
                execution_count=output.get("execution_count"),
            ))

        elif output_type == "display_data":
            nb_outputs.append(nbformat.v4.new_output(
                output_type="display_data",
                data=output.get("data", {}),
                metadata=output.get("metadata", {}),
            ))

        elif output_type == "error":
            nb_outputs.append(nbformat.v4.new_output(
                output_type="error",
                ename=output.get("ename", ""),
                evalue=output.get("evalue", ""),
                traceback=output.get("traceback", []),
            ))

    return nb_outputs


def write_outputs_to_notebook(
    ipynb_path: str,
    cell_results: list[dict],
) -> str | None:
    """Write execution outputs back to a .ipynb file.

    Args:
        ipynb_path: Path to the .ipynb file.
        cell_results: List of dicts with keys:
            - cell_index: int
            - raw_outputs: list of raw kernel output dicts
            - execution_count: int or None

    Returns:
        Path to the updated notebook, or None if file doesn't exist.
    """
    path = Path(ipynb_path)
    if not path.exists():
        return None

    nb = nbformat.read(path, as_version=4)

    for result in cell_results:
        idx = result["cell_index"]
        if idx < 0 or idx >= len(nb.cells):
            continue
        cell = nb.cells[idx]
        if cell.cell_type != "code":
            continue

        cell.outputs = convert_to_nbformat_outputs(result["raw_outputs"])
        if result.get("execution_count") is not None:
            cell.execution_count = result["execution_count"]

    # Clean transient fields that cause nbformat validation errors
    for cell in nb.cells:
        if cell.cell_type == "code" and hasattr(cell, "outputs"):
            for output in cell.outputs:
                if isinstance(output, dict) and "transient" in output:
                    del output["transient"]

    nbformat.write(nb, path)
    return str(path)
