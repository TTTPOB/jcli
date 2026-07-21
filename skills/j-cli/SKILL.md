---
name: j-cli
description: Use this skill whenever the user wants to execute code on a Jupyter server, manage Jupyter sessions or kernels, inspect or run notebook cells, or interact with Jupyter Lab from the command line. Triggers include mentions of Jupyter, notebooks, kernels, ipynb files, or requests to run Python/R code on a remote server. Also use when the user wants to check Jupyter server health, create/list/kill sessions, interrupt/restart kernels, summarize or show notebook source without execution, write execution outputs back to notebooks, inspect kernel variables, search notebook content with ripgrep, or edit a notebook by editing its py:percent pair.
---

# j-cli — Jupyter CLI for LLM Agents

## Overview

j-cli is a CLI tool that lets you operate Jupyter Lab servers. Use it to execute code in kernels, manage sessions, and write outputs back to notebooks. Always use `--json` (`-j`) flag when you need to parse the output programmatically.

## One-time Claude Code hook install

Run this once per project to prevent Claude from falling back to `jupyter nbconvert --execute` (or `papermill` / `runipy`) instead of j-cli:

```bash
j-cli setup claude --local    # writes .claude/settings.local.json (gitignored, this machine only)
# or:
j-cli setup claude --project  # writes .claude/settings.json       (committed, team-shared)
# or:
j-cli setup claude --user     # writes ~/.claude/settings.json     (global, all projects)
```

The command is idempotent — re-running updates the hook in place without duplicating it.

**What the hooks install:**

- **`notebook-exec-guard`** (Bash, hard deny) — blocks `jupyter nbconvert --execute`, `papermill`, `runipy`, and `ipython <notebook>.ipynb`. These tools bypass j-cli and lose kernel state.
- **`python-run-guard`** (Bash, soft deny) — fires when a command like `python foo.py`, `uv run python foo.py`, `pixi run python foo.py`, or `./foo.py` targets a `.py` file that has a paired `.ipynb` next to it. The guard surfaces a "reconsider" message explaining that running the file as a script discards kernel state and py/ipynb pair sync. The agent is expected to use `j-cli session` + `j-cli exec` instead. Commands on ordinary scripts (no paired `.ipynb`) are never intercepted.
- **`pair-drift-guard`** **(PreToolUse, Edit/Write)** — detects drift that was already present before your edit (e.g. a human teammate edited the `.ipynb` in JupyterLab). Uses `git merge-file` 3-way merge (handles cell insertions, deletions, and non-overlapping edits); asks you to re-read the target file after auto-merge, or explains the conflict and what to inspect before picking a side. `.ipynb` is by design gitignored; `.py` history is the only merge baseline.
- **`pair-drift-guard-post`** **(PostToolUse, Edit/Write)** — after your own Edit/Write, silently syncs your change to the pair's other side when `git merge-file` produces no conflicts. With a git baseline, its context includes a cell summary where `~` marks edits, `+` marks inserts, and `- old:N` marks deleted baseline cells. The summary prioritizes changed cells within 16-cell and 8,000-character limits; when it reports omissions, run the included `j-cli notebook summary` command.
- **`notebook-edit-guard`** **(PreToolUse, NotebookEdit)** — hard-denies direct `NotebookEdit` calls; always use the py:percent round-trip instead.

## One-time Codex hook install

Run this once per project to prevent Codex from falling back to `jupyter nbconvert --execute` (or `papermill` / `runipy`) instead of j-cli:

```bash
j-cli setup codex             # writes .codex/hooks.json (default)
# or:
j-cli setup codex --project   # same as default
# or:
j-cli setup codex --user      # writes ~/.codex/hooks.json (global, all projects)
```

The command is idempotent — re-running updates the hook in place without duplicating it.

**Prerequisites:** Codex hooks require `[features]\ncodex_hooks = true` in `.codex/config.toml`. `setup codex` checks for this and warns if missing.

**What the hooks install:**

- **`notebook-exec-guard`** (Bash, hard deny) — blocks `jupyter nbconvert --execute`, `papermill`, `runipy`, and `ipython <notebook>.ipynb`.
- **`python-run-guard`** (Bash, soft deny) — fires when a shell command targets a `.py` file that has a paired `.ipynb`.
- **`pair-drift-guard-pre`** (PreToolUse, apply_patch) — detects drift before an `apply_patch` edit touches a paired `.py` file.
- **`pair-drift-guard-post`** (PostToolUse, apply_patch) — after `apply_patch`, syncs the other side of the pair when possible and includes the same baseline-backed cell markers in its context. Multi-file context is limited to 16,000 characters and reports how many additional file contexts it omitted.

> **Note:** `notebook-edit-guard` is not installed for Codex because Codex has no `NotebookEdit` tool; file edits go through `apply_patch` instead.

## One-time OpenCode hook install

Run this once per project to install the j-cli OpenCode plugin:

```bash
j-cli setup opencode             # writes .opencode/plugins/jcli.js (default)
# or:
j-cli setup opencode --project   # same as default
# or:
j-cli setup opencode --user      # writes ~/.config/opencode/plugins/jcli.js
```

OpenCode loads the plugin at startup. The plugin applies the execution guards to `bash`, the pair drift guards to `edit`, `write`, and `apply_patch`, and appends post-edit synchronization notices to tool output. Set `JCLI_BIN` before starting OpenCode if `j-cli` is not available on its `PATH`.

Do not install both project and user copies. OpenCode loads both plugin directories and would invoke both copies.

## Installing the git pre-commit hook

Run once per repository to keep `.py` / `.ipynb` pairs in sync at commit time:

```bash
j-cli setup git                             # default --project scope
j-cli setup git --project                   # .githooks/pre-commit + core.hooksPath
j-cli setup git --local                     # .git/hooks/pre-commit (this clone only)
j-cli setup git --include 'src/*'           # only watch .py files under src/
j-cli setup git --include 'a/*' --include 'b/*'   # multiple globs (OR logic)
```

**What the installer does:**

- Writes a bash shim at the hook path that delegates to `j-cli _hooks pre-commit-pair-sync`
- `--project` (default): stores the hook under `.githooks/pre-commit` and sets
  `git config --local core.hooksPath .githooks`
- `--local`: writes directly to `.git/hooks/pre-commit`; does not touch `core.hooksPath`
- Injects a managed block into `.gitignore` so `*.ipynb` files are never accidentally committed:

```
# >>> jcli managed (git hooks) >>>
*.ipynb
# <<< jcli managed (git hooks) <<<
```

The installer is idempotent — re-running updates the hook shim and `.gitignore` block in place.

**Hook behaviour at commit time:**

| Situation | Result |
|-----------|--------|
| `.ipynb` staged | Blocked — unstage it, commit only the `.py` pair |
| Pair in sync | Silently allowed |
| One side changed (auto-merge possible) | `git merge-file` 3-way merge; merged content written back; `.py` re-staged if updated |
| Both sides changed the **same** cell | Commit blocked — conflict markers printed; resolve manually |
| `.py` not yet committed — no baseline + any drift | Commit blocked — 2-way diff printed; pick a side first, then commit |

When a conflict or drift is detected, the hook prints a diff (3-way conflict markers or unified diff) and suggests:

```bash
j-cli convert ipynb-to-py <nb.ipynb> <nb.py>   # take ipynb as truth
j-cli convert py-to-ipynb <nb.py> <nb.ipynb>    # take py as truth
```

## Starting the Jupyter server

Before connecting, check whether the server is already running:

```bash
j-cli healthcheck > /dev/null 2>&1 && echo "running" || echo "not running"
```

If the server is **already running**, skip to the Connection section.

If it is **not running**, launch it as a fully detached process so it survives after this session ends:

```bash
nohup bash -c "$(j-cli serve-cmd --serve-backend lab)" \
  > /tmp/jupyter_$(date +%Y%m%d_%H%M%S)_$$.log 2>&1 & disown
```

How this works:
- `$(j-cli serve-cmd --serve-backend lab)` — captures the launch command (token is never inlined; the output contains the literal `$JCLI_JUPYTER_SERVER_TOKEN` reference)
- `bash -c "..."` — the inner bash expands `$JCLI_JUPYTER_SERVER_TOKEN` from the environment
- `nohup … & disown` — detaches the process from this session; it survives after Claude exits
- Log file includes a timestamp and the launching shell's PID for easy identification

After launching, wait a moment and confirm the server is up:

```bash
j-cli healthcheck
```

`--serve-backend` must be one of `lab`, `server`, or `notebook`.

## Prerequisites

Before using j-cli, check if it is installed:

```bash
command -v j-cli > /dev/null && echo "installed" || echo "not installed"
```

If not installed, install it with:

```bash
uv tool install jupyter-jcli
j-cli --version
```

Note: the PyPI package name is `jupyter-jcli`, the binary name is `j-cli`.

## Connection

Before running any j-cli command, check if the environment variables are already set:

```bash
[ -n "$JCLI_JUPYTER_SERVER_URL" ] && echo "URL: set" || echo "URL: unset"
[ -n "$JCLI_JUPYTER_SERVER_TOKEN" ] && echo "TOKEN: set" || echo "TOKEN: unset"
```

- If both are set, proceed directly — do not re-export them.
- If either is unset, ask the user for the missing value(s), then export:

```bash
export JCLI_JUPYTER_SERVER_URL=http://localhost:8888
export JCLI_JUPYTER_SERVER_TOKEN=<token>
```

You can also pass them as flags per-command: `-s <url>` and `-t <token>`.

## Workflow

A typical workflow follows these steps:

1. **Check connectivity** — run `j-cli healthcheck`; if it fails the server is not running — start it first (see *Starting the Jupyter server* above)
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
j-cli -j kernelspec inspect-file analysis.py
# Output: {"path": "analysis.py", "kernel_name": "ir", "kernel_display_name": null, "kernel_language": null}

# 3. Create a session with the detected kernel
j-cli -j session create --kernel ir --name analysis
# Output (JSON): {"session_id": "abc-123", "kernel_id": "def-456", "kernel_name": "ir"}

# 4. Execute inline code (use the session_id from step 3)
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
j-cli -j healthcheck
# JSON: {"status": "ok", "version": "2.14.2", "kernels_running": 1}
```

### `kernelspec list`

List available kernel specifications on the server.

```bash
j-cli kernelspec list
j-cli -j kernelspec list
```

### `session create`

Create a new session. Returns the session_id needed for all subsequent commands.

```bash
j-cli session create --kernel python3
j-cli session create --kernel python3 --name my-analysis
j-cli -j session create --kernel python3
# JSON: {"session_id": "...", "kernel_id": "...", "kernel_name": "python3"}
```

### `session list`

List all active sessions with their kernel state. By default fetches a short variable preview for each idle kernel (VARS column).

```bash
j-cli session list            # includes VARS column (default)
j-cli session list --no-vars  # faster, skips variable fetch
j-cli session list --vars     # force fetch even when >10 sessions

j-cli -j session list
# JSON: {"sessions": [{"session_id": "...", "kernel_id": "...", "kernel_name": "python3",
#   "kernel_state": "idle", "name": "...",
#   "vars_preview": {"names": ["x", "df"], "total": 2}}]}
```

A hint line in human output points at `j-cli vars <SESSION_ID>` for the full variable list.

### `session kill`

Delete a session and shut down its kernel.

```bash
j-cli session kill <session_id>
```

### `kernel interrupt`

Interrupt a running kernel (e.g., stuck execution).

```bash
j-cli kernel interrupt <session_id>
```

### `kernel restart`

Restart a kernel (clears all state).

```bash
j-cli kernel restart <session_id>
```

### `notebook summary` and `notebook show`

For an existing notebook or py:percent file, use `summary -> show -> exec`: locate
relevant cells, read their complete source, then execute only the cells the task
requires. `summary` and `show` do not execute code or display stored outputs.

Python summaries report `imports`, `defines`, `writes`, and qualified `calls`
extracted from the AST, plus an original source preview. Cells containing IPython
syntax still report the preview when AST parsing fails.

```bash
j-cli notebook summary analysis.py
j-cli notebook show analysis.py --cell 4
j-cli notebook show analysis.py --cell 3:7
j-cli exec <session_id> --file analysis.py --cell 4
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
j-cli vars <session_id>
j-cli -j vars <session_id>
# JSON: {"session_id": "...", "source": "dap", "variables": [{"name": "x", "type": "int", "value": "42", "variables_reference": 0}]}

# Inspect a single variable
j-cli vars <session_id> --name x
j-cli -j vars <session_id> --name x

# Rich inspection (MIME-typed data; DAP kernels only, e.g. ipykernel)
j-cli vars <session_id> --name df --rich

# Longer timeout (default 10s)
j-cli vars <session_id> --timeout 20
```

**Source**: `"dap"` when the kernel supports the Jupyter debug protocol (e.g. ipykernel); `"fallback"` when a shell-channel snippet is used instead.

**Ordering caveat**: variables appear in first-definition order (CPython insertion order). Re-assigning does NOT move a variable to the end. Do NOT infer "most recently modified" from position.

**No mtime**: the protocol provides no per-variable last-modified timestamp. If you need to know which cells ran, use `exec` to track state yourself or restart the kernel and re-run.

### `exec`

Execute code in a kernel session. This is the most important command.

**Inline code:**
```bash
j-cli exec <session_id> --code "print('hello')"
j-cli exec <session_id> -c "import pandas as pd; df = pd.read_csv('data.csv'); df.describe()"
j-cli exec <session_id> --code $'df.head()\ndf.describe()' --display-mode all
```

**From a file:**
```bash
# All code cells from a notebook (omit --cell to run everything)
j-cli exec <session_id> --file notebook.ipynb

# Single cell (0-indexed)
j-cli exec <session_id> --file notebook.ipynb --cell 3

# Multiple consecutive cells via range
j-cli exec <session_id> --file notebook.ipynb --cell 0:5    # cells 0,1,2,3,4
j-cli exec <session_id> --file notebook.ipynb --cell 3:     # cell 3 to end
j-cli exec <session_id> --file notebook.ipynb --cell :3      # cells 0,1,2

# From py:percent file
j-cli exec <session_id> --file script.py --cell 0

# Display every top-level expression in each selected cell
j-cli exec <session_id> --file script.py --display-mode all
```

Each cell in the range is executed sequentially. After a cell finishes, j-cli immediately prints that cell's output and writes that cell's outputs back to the target notebook when writeback applies. Human output uses `--- cell N ---` separators.

Inline code and file execution default to `--display-mode last_expr`, matching VS Code
notebook behavior: only the final expression is displayed. Use `all` when every top-level
table or figure expression should be displayed. Use `last_expr_or_assign` when a final
assignment should also produce output. The accepted modes are `last_expr`, `all`,
`last_expr_or_assign`, `last`, and `none`.

**Timeout** (default: 10s per cell; when set, it is one total budget shared across selected cells):
```bash
j-cli exec <session_id> --code "long_computation()" --timeout 600
```

When the deadline expires during a cell, j-cli sends an interrupt to the remote kernel and
continues consuming messages until that execution reports `idle`. It then exits with code 1
and reports `TIMEOUT`. The session, kernel process, and variables created before the interrupted
cell remain available. The interrupt raises `KeyboardInterrupt` in ordinary Python kernels, so
statements after the interruption point do not run unless user code catches that exception and
continues. j-cli still waits for the execution to report `idle` in that case.

If the interrupt request fails, j-cli reports `INTERRUPT_FAILED` instead of claiming that the
kernel returned to idle. Check `j-cli session list --no-vars`, then use
`j-cli kernel interrupt <session_id>` or `j-cli kernel restart <session_id>` as needed.

**JSON output** (for parsing results programmatically):
```bash
j-cli -j exec <session_id> --code "print('hello')"
# JSON: {"status": "ok", "outputs": [{"type": "stream", "stream_name": "stdout", "text": "hello\n"}]}

j-cli -j exec <session_id> --file notebook.ipynb --cell 0:3
# JSONL:
# {"status":"ok","cell":{"cell_index":0,"outputs":[...],"execution_count":1},"notebook_updated":"notebook.ipynb"}
# {"status":"ok","cell":{"cell_index":1,"outputs":[...],"execution_count":2},"notebook_updated":"notebook.ipynb"}
# {"status":"ok","summary":{"cells_executed":2,"notebook_updated":"notebook.ipynb"}}
```

When you are an LLM/agent reading the output yourself, prefer the default human mode. Do not use `--json` just because you are a machine; JSON/JSONL mode is for scripts or tools that need to parse output programmatically.

## Notebook Writeback

When executing from a file, j-cli automatically writes each completed cell's outputs back to the paired `.ipynb`:

- `notebook.ipynb` → outputs written back to itself
- `analysis.py` (py:percent) → outputs written to `analysis.ipynb`; **created automatically if it does not exist**
- `analysis.dummy.py` (py:percent) → outputs written to `analysis.ipynb`; created automatically if absent
- `script.py` (plain, no `# %%` markers or front matter) → outputs printed to stdout only, no `.ipynb` created

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

# %%
import matplotlib.pyplot as plt
import numpy as np

# %%
x = np.linspace(0, 10, 100)
fig, ax = plt.subplots()
ax.plot(x, np.sin(x))
fig

# %% [markdown]
# ## Results
# The plot above shows a sine wave.
```

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
#    Cell markers: # %% (code), # %% [markdown], # %% [raw]

# 3. Write edited sources back — outputs/metadata in the .ipynb are untouched
j-cli convert py-to-ipynb analysis.py analysis.ipynb
```

If a paired `.py` already exists (same stem), you can go directly to step 2 and then step 3.

The `j-cli convert py-to-ipynb` command detects whether the `.ipynb` already exists:
- **Exists** → source-only update (outputs, execution counts, metadata preserved)
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
- Save the `session_id` from `session create` — you need it for every subsequent command.
- Use `--cell` to run specific cells instead of entire notebooks when debugging.
- If execution hangs, use `kernel interrupt` followed by retry.
- If kernel state is corrupted, use `kernel restart` (this clears all variables).
- Images in execution output are automatically extracted to temp files with paths included in the output.
- Clean up sessions with `session kill` when done to free server resources.
