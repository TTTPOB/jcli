# Convert notebook formats explicitly

Use this workflow for explicit format conversion or cell-ID preparation. Ordinary editing should use [edit](edit.md), which includes its minimal round-trip commands. Conversion does not require a server or execution.

## Notebook to editable text

```bash
j-cli convert ipynb-to-py analysis.ipynb analysis.py
```

Outputs remain in the notebook. Edit the py:percent file, never raw notebook JSON.

## Editable text to notebook

1. Preserve existing cell IDs. If the file mixes cells with and without IDs, prepare IDs as described below.
2. Run conversion explicitly only when needed; do not duplicate an automatic hook synchronization.

```bash
j-cli convert py-to-ipynb analysis.py analysis.ipynb
```

The `j-cli convert py-to-ipynb` command detects whether the `.ipynb` already exists:
- **Exists** → source-only update (outputs and execution counts preserved by default)
- **Does not exist** → new notebook created from the py cells

## Prepare cell IDs

Cell markers may carry a stable nbformat ID as `id="..."`. Preserve the ID when
editing or moving an existing cell. Hook synchronization will assign an ID to a
newly inserted cell, so you don't have to assign it manually.

Fill missing IDs before explicit conversion (meaning if you are running this
manually instead of through the hook, and you know the file is having a mix of
cells with and without IDs). The command reuses IDs from an
aligned paired notebook and generates IDs for cells without a pair match:

```bash
j-cli convert assign-ids analysis.py
```

j-cli comments IPython magic commands in py:percent files so Python tools can
parse them, then restores the commands when syncing to `.ipynb`. Python-body
cell magics such as `%%timeit` and `%%writefile` keep their body as Python code;
other cell magics are commented through the end of the cell.

## Pair naming

- `analysis.py` (py:percent) pairs with `analysis.ipynb`.
- `analysis.dummy.py` (py:percent) pairs with `analysis.ipynb`.
- A py:percent file has at least one `# %%` cell marker or a `# ---` YAML front matter block.
- Plain scripts without these markers are not treated as notebooks during execution.
