"""Write execution outputs back to .ipynb files."""

from pathlib import Path

import nbformat

from jupyter_jcli._enums import CellType, OutputType


def convert_to_nbformat_outputs(raw_outputs: list[dict]) -> list:
    """Convert raw kernel outputs to nbformat output objects."""
    nb_outputs = []
    for output in raw_outputs:
        try:
            # Only the four canonical nbformat output types are valid here.
            # IMAGE and HTML are internal derived types — skip them.
            raw_type = output.get("output_type")
            output_type = OutputType(raw_type)
        except (ValueError, TypeError):
            continue  # skip unknown / derived output types

        if output_type == OutputType.STREAM:
            text = output.get("text", "")
            if isinstance(text, list):
                text = "".join(text)
            nb_outputs.append(nbformat.v4.new_output(
                output_type=OutputType.STREAM.value,
                name=output.get("name", "stdout"),
                text=str(text),
            ))

        elif output_type == OutputType.EXECUTE_RESULT:
            nb_outputs.append(nbformat.v4.new_output(
                output_type=OutputType.EXECUTE_RESULT.value,
                data=output.get("data", {}),
                metadata=output.get("metadata", {}),
                execution_count=output.get("execution_count"),
            ))

        elif output_type == OutputType.DISPLAY_DATA:
            nb_outputs.append(nbformat.v4.new_output(
                output_type=OutputType.DISPLAY_DATA.value,
                data=output.get("data", {}),
                metadata=output.get("metadata", {}),
            ))

        elif output_type == OutputType.ERROR:
            nb_outputs.append(nbformat.v4.new_output(
                output_type=OutputType.ERROR.value,
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
        if cell.cell_type != CellType.CODE:
            continue

        cell.outputs = convert_to_nbformat_outputs(result["raw_outputs"])
        if result.get("execution_count") is not None:
            cell.execution_count = result["execution_count"]

    # Clean transient fields that cause nbformat validation errors
    for cell in nb.cells:
        if cell.cell_type == CellType.CODE and hasattr(cell, "outputs"):
            for output in cell.outputs:
                if isinstance(output, dict) and "transient" in output:
                    del output["transient"]

    nbformat.write(nb, path)
    return str(path)
