---
name: j-cli
description: Use this skill whenever the user wants to execute code on a Jupyter server, manage Jupyter sessions or kernels, inspect or run notebook cells, or interact with Jupyter Lab from the command line. Triggers include mentions of Jupyter, notebooks, kernels, ipynb files, or requests to run Python/R code on a remote server. Also use when the user wants to check Jupyter server health, create/list/kill sessions, interrupt/restart kernels, summarize or show notebook source without execution, write execution outputs back to notebooks, inspect kernel variables, search notebook content with ripgrep, or edit a notebook by editing its py:percent pair.
---

# j-cli — Jupyter CLI for LLM Agents

## Overview

j-cli is a CLI tool that lets you operate Jupyter Lab servers. Use it to execute code in kernels, manage sessions, and write outputs back to notebooks. Always use `--json` (`-j`) flag when you need to parse the output programmatically.

## Deployment

Assume j-cli and its host integration are already configured. Only when setup,
connection, or server startup is actually required, read
[DEPLOYMENT.md](DEPLOYMENT.md). Do not load it for normal notebook work.

## Workflow

A typical workflow follows these steps:

1. **Check connectivity** — run `j-cli healthcheck`; if it fails because the server is not running, read [DEPLOYMENT.md](DEPLOYMENT.md) before starting it
2. **Detect kernel spec** — if the user provides a `.py` or `.ipynb` file, inspect the file metadata through the CLI:
   ```bash
   j-cli -j kernelspec inspect-file analysis.py
   ```
   Use `kernel_name` as the `--kernel` value when creating the session. If `kernel_name` is `null`, do not guess blindly. First inspect the paired notebook if one exists, then infer from project environment files (`pixi.toml`, `pyproject.toml` / `uv.lock`, Conda environment files) and `j-cli -j kernelspec list` whether there is one promising kernel. If there is no clear single match, ask the user to specify the kernel.
3. **Create a session** — use the detected or clearly inferred kernel spec
4. **Execute code** — run inline code or cells from files
5. **Clean up** — kill the session when done

### Step-by-step Example

```bash
# 1. Healthcheck
j-cli healthcheck
# Output: OK  Jupyter server v2.14.2  0 kernel(s) running

# 2. Detect kernel spec from the file
j-cli kernelspec inspect-file analysis.py

# 3. Create a session with the detected kernel
j-cli session create --kernel ir --name analysis

# 4. Execute inline code (use the session_selector from step 3)
j-cli exec abc-123 --code "print(1 + 1)"

# 5. Execute cells from a notebook
j-cli exec abc-123 --file analysis.ipynb --cell 0:5

# 6. Execute from a py:percent file (outputs auto-written to paired .ipynb)
j-cli exec abc-123 --file analysis.py

# 7. Clean up
j-cli session kill abc-123
```

## Commands Reference

### `healthcheck`

Check server connectivity and running kernel count.

```bash
j-cli healthcheck
```

### `kernelspec list`

List available kernel specifications on the server.

```bash
j-cli kernelspec list
j-cli -j kernelspec list
```

### `session create`

Create a new session. JSON output returns both the full `session_id` and the shortest unique `session_selector`. Commands accept the full ID, short selector, or exact unique session name.

```bash
j-cli session create --kernel python3
j-cli session create --kernel python3 --name my-analysis
j-cli session create --kernel python3
```

### `session list`

List all active sessions with their kernel state. By default fetches a short variable preview for each idle kernel (VARS column).

```bash
j-cli session list            # includes VARS column (default)
j-cli session list --no-vars  # faster, skips variable fetch
j-cli session list --vars     # force fetch even when >10 sessions

j-cli -j session list
# JSON: {"sessions": [{"session_id": "...", "session_selector": "abc", "kernel_id": "...", "kernel_name": "python3",
#   "kernel_state": "idle", "name": "...",
#   "vars_preview": {"names": ["x", "df"], "total": 2}}]}
```

Human output shows the shortest unique session ID prefix, with at least three characters. Commands accept a full ID, the displayed short ID, or an exact unique session name. A selector matching multiple sessions exits without choosing one. A hint line points at `j-cli vars <SESSION_SELECTOR>` for the full variable list.

### `session kill`

Delete a session and shut down its kernel.

```bash
j-cli session kill <session_selector>
```

### `kernel interrupt`

Interrupt a running kernel (e.g., stuck execution).

```bash
j-cli kernel interrupt <session_selector>
```

### `kernel restart`

Restart a kernel (clears all state).

```bash
j-cli kernel restart <session_selector>
```

JSON success responses from `kernel interrupt` and `kernel restart` include
`session_id`, `session_selector`, and `kernel_id`. Human output identifies the
session with the same short selector.

### `notebook summary` and `notebook show`

For an existing notebook or py:percent file, use `summary -> show -> exec`: locate
relevant cells, read their complete source, then execute only the cells the task
requires. `summary` and `show` do not execute code or display stored outputs.

Summaries show complete source for short cells. Longer Python cells report
`imports`, `defines`, `writes`, and qualified `calls` extracted from the AST, plus
an original source preview. Cells containing IPython syntax still report the
preview when AST parsing fails.

```bash
j-cli notebook summary analysis.py
j-cli notebook show analysis.py --cell 4
j-cli notebook show analysis.py --cell 3:7
j-cli exec <session_selector> --file analysis.py --cell 4
j-cli -j notebook summary analysis.ipynb
```

`show --cell` accepts the same 0-indexed specs as `exec`: `3`, `3:7`, `3:`, and
`:5`. Ranges are half-open; negative indices, descending ranges, and specs with
multiple colons are invalid. `show` prints code, markdown, and raw cells without
executing them.

### `vars`

Inspect kernel variables. Use after `exec` to check what's defined and what values variables hold.

```bash
# List all global variables (NAME / TYPE / VALUE table)
j-cli vars <session_selector>
# j-cli -j vars <session_selector>
# JSON: {"session_id": "...", "session_selector": "abc", "source": "dap", "variables": [{"name": "x", "type": "int", "value": "42", "variables_reference": 0}]}

# Inspect a single variable
j-cli vars <session_selector> --name x
# j-cli -j vars <session_selector> --name x

# Rich inspection (MIME-typed data; DAP kernels only, e.g. ipykernel)
j-cli vars <session_selector> --name df --rich

# Longer timeout (default 10s)
j-cli vars <session_selector> --timeout 20
```

**Source**: `"dap"` when the kernel supports the Jupyter debug protocol (e.g. ipykernel); `"fallback"` when a shell-channel snippet is used instead.

**Ordering caveat**: variables appear in first-definition order (CPython insertion order). Re-assigning does NOT move a variable to the end. Do NOT infer "most recently modified" from position.

**No mtime**: the protocol provides no per-variable last-modified timestamp. If you need to know which cells ran, use `exec` to track state yourself or restart the kernel and re-run.

### `exec`

Execute code in a kernel session. This is the most important command.

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

Inline code and file execution default to `--display-mode last_expr`, matching VS Code
notebook behavior: only the final expression is displayed. Use `all` when every top-level
table or figure expression should be displayed. Use `last_expr_or_assign` when a final
assignment should also produce output. The accepted modes are `last_expr`, `all`,
`last_expr_or_assign`, `last`, and `none`.

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

**JSON output** (for parsing results programmatically):
```bash
j-cli -j exec <session_selector> --code "print('hello')"
# JSON: {"status": "ok", "outputs": [{"type": "stream", "stream_name": "stdout", "text": "hello\n"}]}

j-cli -j exec <session_selector> --file notebook.ipynb --cell 0:3
# JSONL:
# {"status":"ok","cell":{"cell_index":0,"outputs":[...],"execution_count":1},"notebook_updated":"notebook.ipynb"}
# {"status":"ok","cell":{"cell_index":1,"outputs":[...],"execution_count":2},"notebook_updated":"notebook.ipynb"}
# {"status":"ok","summary":{"cells_executed":2,"notebook_updated":"notebook.ipynb"}}
```

A successful file run ends with the summary object. If a cell fails, stdout ends with that cell's `status: "error"` event, j-cli omits the summary, and it writes the structured `EXECUTION_ERROR` object to stderr.

When you are an LLM/agent reading the output yourself, prefer the default human mode. Do not use `--json` just because you think you are a machine (coding agent); JSON/JSONL mode is for scripts or tools that need to parse output programmatically like jq. Display summaries have total and per-entry limits; if a response reports omitted entries, use the saved notebook or `output_manifest` as the complete result.

## Reading Saved Output

For notebook-backed execution, j-cli saves the output to `.ipynb` before formatting the response. Read it without executing again:

```bash
# omit --output to list physical outputs first
j-cli -j notebook outputs analysis.ipynb --cell 4
j-cli -j notebook output analysis.ipynb --cell 4 --output 1
j-cli -j notebook output analysis.ipynb --cell 4 --output 1 --mime text/html

# a py:percent path resolves to its paired notebook only through a reliable match
j-cli -j notebook output analysis.py --cell 4 --output 1
```

Indexes are zero-based and physical. File-execution JSON uses `cell_index` for the executed source-file position and `notebook_cell_index` for the actual saved notebook position when different. A `.py` cell maps only by a unique stable ID or unique, non-conflicting cell type and source; positional and similarity guesses fail.

Saved output may be older than the current source. Reading does not execute, synchronize pairs, write a baseline, create a cache, or start a daemon. HTML and SVG remain original source text, JSON remains structured, and only existing raster MIME representations are images.

Claude Code, Codex, DSH, and OpenCode expose the same single `read_notebook_output` call. Omit `output_index` to list and provide it to read, with optional `mime_type`, `offset`, and `limit`. Prefer this tool inside a configured host; use the CLI directly otherwise.

Inline code and plain `.py` files have no notebook target. Short text-only results stay inline and create no `.j-cli`; rich output or more than 4,000 text characters is stored under the real cwd and returned as an absolute `output_manifest`:

```bash
j-cli -j output show /absolute/path/to/manifest.json
j-cli -j output show /absolute/path/to/manifest.json --output 0
j-cli output clean --dry-run --days 7 --max-runs 50
```

The default cleanup limits are 7 days and the newest 50 runs. Override them with `JCLI_OUTPUT_RETENTION_DAYS` and `JCLI_OUTPUT_MAX_RUNS`. Cleanup is opportunistic or explicit, never a background daemon, and `.j-cli` is working data rather than permanent archival storage.

## Notebook Writeback

When executing from a file, j-cli automatically writes each completed cell's outputs back to the paired `.ipynb` before formatting that cell's display response. If display formatting then fails, the diagnostic states that the notebook output was already saved:

- `notebook.ipynb` → outputs written back to itself
- `analysis.py` (py:percent) → outputs written to `analysis.ipynb`; **created automatically if it does not exist**
- `analysis.dummy.py` (py:percent) → outputs written to `analysis.ipynb`; created automatically if absent
- `script.py` (plain, no `# %%` markers or front matter) → short text stays inline; rich or oversized output gets an `output_manifest`; no `.ipynb` created

A py:percent file is one that has at least one `# %%` cell marker or a `# ---` YAML front matter block. Plain scripts without these markers are not treated as notebooks.

This keeps notebooks in sync with their execution results and lets you create a new notebook pair in a single `j-cli exec` call — no separate `j-cli convert py-to-ipynb` step required.

## Searching notebook content with ripgrep

Use `rg` with the `--pre` flag and the bundled preprocessor to search inside `.ipynb` files:

```bash
# Search all notebooks for a pattern
rg --pre skills/j-cli/scripts/rg_ipynb_preprocessor.py 'pattern' .

# Search only .ipynb files
rg --pre skills/j-cli/scripts/rg_ipynb_preprocessor.py -g '*.ipynb' 'pattern' .

# The preprocessor renders each notebook as plain text: cell sources and outputs
# Binary outputs (images, PDFs) are replaced with a size notice
```

The preprocessor is at `skills/j-cli/scripts/rg_ipynb_preprocessor.py` and has no
external dependencies.

## Py:Percent Format

j-cli supports py:percent format — plain Python files with `# %%` cell markers:

```python
# ---
# jupyter:
#   kernelspec:
#     name: python3
# ---

# %% id="imports"
import matplotlib.pyplot as plt
import numpy as np

# %% id="plot"
x = np.linspace(0, 10, 100)
fig, ax = plt.subplots()
ax.plot(x, np.sin(x))
fig

# %% [markdown] id="results"
# ## Results
# The plot above shows a sine wave.
```

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

`j-cli exec --file` displays the final expression in each code cell by default. Leave a
table or figure as the final bare expression, such as `df` or `fig`, to display it without
an explicit `display(...)` or `plt.show()` call. Pass `--display-mode all` when the cell
contains multiple expressions that should produce outputs.

### Editing via py:percent round-trip

**Never edit `.ipynb` files directly** — use the py:percent round-trip to edit notebook
cells safely without losing outputs:

```bash
# 1. Convert notebook to py:percent (outputs are preserved in the .ipynb)
j-cli convert ipynb-to-py analysis.ipynb analysis.py

# 2. Edit analysis.py using normal text tools (Edit tool, etc.)
#    Preserve id="..." on existing cell markers
#    Cell markers: # %% (code), # %% [markdown], # %% [raw]

# 3. Write edited sources back; preserve outputs (default)
# if a coding agent are the editor, it should trigger the hook and auto sync so no need 
# to run this command manually
j-cli convert py-to-ipynb analysis.py analysis.ipynb
```

If a paired `.py` already exists (same stem), you can go directly to step 2 and then step 3.

The `j-cli convert py-to-ipynb` command detects whether the `.ipynb` already exists:
- **Exists** → source-only update (outputs and execution counts preserved by default)
- **Does not exist** → new notebook created from the py cells

> **Policy**: The `NotebookEdit` tool is disabled by the `notebook-edit-guard` hook
> installed via `j-cli setup claude`. Always go through the py:percent round-trip instead.

### Drift guards at a glance

`.ipynb` is gitignored by design — only `.py` history is the merge baseline.

| Who triggers | Hook | When | Meaning | Next step |
|---|---|---|---|---|
| Agent (pre-edit) | `pair-drift-guard` | Pre Edit/Write/apply_patch | Drift already existed before your call | Read the message; if auto-merged, re-read the target file; if conflict, inspect and pick a side |
| Agent (post-edit) | `pair-drift-guard-post` | Post Edit/Write/apply_patch | Your edit may have diverged the pair | Read `~` edited, `+` inserted, and `- old:N` deleted markers after an auto-sync with a git baseline. Follow any omission hint with `j-cli notebook summary`. If warned: pick a side with `j-cli convert` |
| Agent | `notebook-edit-guard` | Pre NotebookEdit | Hard deny; use py:percent round-trip | Follow the three-step convert workflow above |

## Error Handling

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

## Tips for Agents

- Always use `-j` (JSON mode) when you need to parse output — it gives structured, machine-readable results.
- Save the `session_selector` from `session create` for subsequent commands; keep `session_id` when you need the stable full identifier.
- Use `--cell` to run specific cells instead of entire notebooks when debugging.
- If execution hangs, use `kernel interrupt` followed by retry.
- If kernel state is corrupted, use `kernel restart` (this clears all variables).
- For notebook-backed images, use `read_notebook_output` or `j-cli notebook output`; for inline/plain-script rich output, follow the returned `output_manifest` path.
- Clean up sessions with `session kill` when done to free server resources.
