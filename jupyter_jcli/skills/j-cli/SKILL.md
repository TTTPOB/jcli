---
name: j-cli
description: Use this skill whenever the user wants to execute code on a Jupyter server, manage Jupyter sessions or kernels, create, inspect, edit, convert, or run notebooks and py:percent files, or interact with Jupyter Lab from the command line. Also use for checking server health, inspecting kernel variables, searching notebook content, reading saved results, and writing execution outputs back to notebooks.
---

# j-cli — Jupyter workflows for agents

Use j-cli to work with notebook files and live Jupyter kernels. Select a workflow
by the user's current goal, not by loading the entire command reference.

## Load only the current workflow

Read only the workflow needed for the current step. Do not preload every linked
file. When a task enters another workflow, load that file at that point. Resolve
links relative to this skill directory, not the user's working directory.

| Current goal | Read | Scope |
|---|---|---|
| Create notebook content | [create](workflows/create.md) | New py:percent notebook, metadata, initial pair |
| Inspect or search without executing | [view](workflows/view.md) | Source summaries, complete cells, saved outputs, manifests |
| Modify existing notebook cells | [edit](workflows/edit.md) | Text round-trip, stable IDs, hook synchronization, drift |
| Run code or selected cells | [exec](workflows/exec.md) | Display modes, writeback, timeout, execution errors |
| Explicitly convert file formats | [convert](workflows/convert.md) | ipynb ↔ py:percent, cell-ID preparation |
| Manage live kernel state | [session](workflows/session.md) | Sessions, kernel selection, interrupt/restart, variables |
| Set up integration or resolve connection/startup issues | [deployment](DEPLOYMENT.md) | Installation, host configuration, server startup |

Examples of staged loading:

- **Read existing results:** load view only; do not execute cells.
- **Edit and then run a cell:** load edit first, then exec when ready to run.
  Load session only if session selection or management is needed.
- **Create a notebook without running it:** load create only; no live kernel needed.
- **Convert a file:** load convert only; no server needed.

## Shared rules

- Assume j-cli and host integration are configured. Read deployment instructions
  only when setup, connection, or server startup is actually needed.
- Do not require healthcheck or a session for file-only creation, viewing,
  editing, or conversion.
- Never edit raw `.ipynb` JSON. Edit its py:percent representation and synchronize
  the pair. Preserve existing cell IDs and heed pair-drift diagnostics.
- Prefer default human output when reading results as an agent. Use `--json`
  (`-j`) only when a script or tool must parse the response programmatically.
- Read saved outputs or the returned `output_manifest` when summaries are
  truncated; do not rerun code merely to recover an existing result.
- Reuse an appropriate session when possible. Clean up disposable sessions you
  created; do not restart or kill a reused user session unless the task calls for it.

Keep command details and examples in the relevant workflow. Do not read unrelated
workflows just because they are linked here.
