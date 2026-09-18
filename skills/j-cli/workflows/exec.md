# Execute code and inspect results

## Contents

- [Execution workflow](#execution-workflow)
- [Choose display behavior](#choose-display-behavior)
- [Run inline code or selected cells](#run-inline-code-or-selected-cells)
- [Timeout and interruption](#timeout-and-interruption)
- [Output writeback](#output-writeback)
- [Machine-readable output](#machine-readable-output)
- [Errors](#errors)

## Execution workflow

1. For an existing notebook, locate relevant cells with `notebook summary`, then read complete source with `notebook show`. Load [view](view.md) if you need these inspection commands.
2. Use the intended session. Load [session](session.md) only if you need to find, create, or manage one.
3. Execute only the requested code or selected cells. Do not run an entire notebook when a smaller range suffices.
4. Read the returned outputs. For saved rich output or truncated summaries, load [view](view.md) rather than rerunning the cell.
5. Clean up disposable sessions you created when finished; do not kill a reused user session.

## Choose display behavior

Inline code and file execution default to `--display-mode last_expr`, matching VS Code
notebook behavior: only the final expression is displayed. Use `all` when every top-level
table or figure expression should be displayed. Use `last_expr_or_assign` when a final
assignment should also produce output. The accepted modes are `last_expr`, `all`,
`last_expr_or_assign`, `last`, and `none`.

**Default plotting convention:** Unless a different mode is requested, keep `last_expr` and end each plotting cell with the figure variable as a bare expression (`fig`). Do not add `fig.show()`, `plt.show()`, or `display(fig)` merely to render that final figure. Creating or assigning `fig` alone is not a substitute for the final `fig` expression. Apply the same convention to tables (`df`).

```python
fig, ax = plt.subplots()
ax.plot(x, y)
fig
```

Keep `fig` as the final statement in that cell, not necessarily the final line of the file. Use `--display-mode all` only when multiple top-level expressions should produce outputs; use `last_expr_or_assign` when output from a final assignment is intended.

## Run inline code or selected cells

**Inline code:**
```bash
j-cli exec <session_selector> --code "print('hello')"
j-cli exec <session_selector> -c "import pandas as pd; df = pd.read_csv('data.csv'); df.describe()"
j-cli exec <session_selector> --code $'df.head()\ndf.describe()' --display-mode all
```

**From a file:**
```bash
# All code cells from a notebook (omit --cell to run everything)
j-cli exec <session_selector> --file notebook.ipynb

# Single cell (0-indexed)
j-cli exec <session_selector> --file notebook.ipynb --cell 3

# Multiple consecutive cells via range
j-cli exec <session_selector> --file notebook.ipynb --cell 0:5    # cells 0,1,2,3,4
j-cli exec <session_selector> --file notebook.ipynb --cell 3:     # cell 3 to end
j-cli exec <session_selector> --file notebook.ipynb --cell :3      # cells 0,1,2

# From py:percent file
j-cli exec <session_selector> --file script.py --cell 0

# Display every top-level expression in each selected cell
j-cli exec <session_selector> --file script.py --display-mode all
```

Each cell in the range is executed sequentially. After a cell finishes, j-cli immediately prints that cell's output and writes that cell's outputs back to the target notebook when writeback applies. If a cell fails, j-cli writes back its error output, exits with code 1, and does not execute later cells. Human output uses `--- cell N ---` separators.

## Timeout and interruption

**Timeout** (default: 10s per cell; when set, it is one total budget shared across selected cells):
```bash
j-cli exec <session_selector> --code "long_computation()" --timeout 600
```

When the deadline expires during a cell, j-cli sends an interrupt to the remote kernel and
continues consuming messages until that execution reports `idle`. It then exits with code 1
and reports `TIMEOUT`. The session, kernel process, and variables created before the interrupted
cell remain available. The interrupt raises `KeyboardInterrupt` in ordinary Python kernels, so
statements after the interruption point do not run unless user code catches that exception and
continues. j-cli still waits for the execution to report `idle` in that case.

If the interrupt request fails, j-cli reports `INTERRUPT_FAILED` instead of claiming that the
kernel returned to idle. Check `j-cli session list --no-vars`, then use
`j-cli kernel interrupt <session_selector>` or `j-cli kernel restart <session_selector>` as needed.

## Output writeback

When executing from a file, j-cli automatically writes each completed cell's outputs back to the paired `.ipynb` before formatting that cell's display response. If display formatting then fails, the diagnostic states that the notebook output was already saved:

- `notebook.ipynb` → outputs written back to itself
- `analysis.py` (py:percent) → outputs written to `analysis.ipynb`; **created automatically if it does not exist**
- `analysis.dummy.py` (py:percent) → outputs written to `analysis.ipynb`; created automatically if absent
- `script.py` (plain, no `# %%` markers or front matter) → short text stays inline; rich or oversized output gets an `output_manifest`; no `.ipynb` created

A py:percent file is one that has at least one `# %%` cell marker or a `# ---` YAML front matter block. Plain scripts without these markers are not treated as notebooks.

This keeps notebooks in sync with their execution results and lets you create a new notebook pair in a single `j-cli exec` call — no separate `j-cli convert py-to-ipynb` step required.

Inline code has no notebook target. Short text-only results stay inline; rich output or more than 4,000 text characters is stored under the real cwd and returned as an absolute `output_manifest`. Read it using the [view](view.md) workflow.

## Machine-readable output

**JSON output** (for parsing results programmatically):
```bash
j-cli -j exec <session_selector> --code "print('hello')"
# JSON: {"status": "ok", "outputs": [{"type": "stream", "stream_name": "stdout", "text": "hello\n"}]}

j-cli -j exec <session_selector> --file notebook.ipynb --cell 0:3
# JSONL:
# {"status":"ok","cell":{"cell_index":0,"outputs":[...],"execution_count":1},"notebook_updated":true}
# {"status":"ok","cell":{"cell_index":1,"outputs":[...],"execution_count":2},"notebook_updated":true}
# {"status":"ok","summary":{"cells_executed":2,"notebook_updated":true}}
```

A successful file run ends with the summary object. If a cell fails, stdout ends with that cell's `status: "error"` event, j-cli omits the summary, and it writes the structured `EXECUTION_ERROR` object to stderr.

When you are an LLM/agent reading the output yourself, prefer the default human mode. Do not use `--json` just because you think you are a machine (coding agent); JSON/JSONL mode is for scripts or tools that need to parse output programmatically like jq. Display summaries have total and per-entry limits; if a response reports omitted entries, use the saved notebook or `output_manifest` as the complete result.

## Errors

Errors return structured error codes. In JSON mode:

```json
{"status": "error", "code": "SESSION_NOT_FOUND", "message": "..."}
{"status": "error", "code": "EXECUTION_ERROR", "message": "..."}
{"status": "error", "code": "TIMEOUT", "message": "Execution deadline expired; the kernel was interrupted and returned to idle"}
{"status": "error", "code": "INTERRUPT_FAILED", "message": "..."}
{"status": "error", "code": "CONNECTION_FAILED", "message": "..."}
{"status": "error", "code": "PARSE_ERROR", "message": "..."}
```

Error codes: `CONNECTION_FAILED`, `SESSION_NOT_FOUND`, `SESSION_CREATE_FAILED`, `KERNEL_NOT_FOUND`, `EXECUTION_ERROR`, `TIMEOUT`, `INTERRUPT_FAILED`, `PARSE_ERROR`.

All errors exit with code 1.
