# Manage sessions, kernels, and live variables

## Start or reuse a session

1. Run `j-cli healthcheck` when a live kernel is needed. If connection or startup fails, read [DEPLOYMENT.md](../DEPLOYMENT.md); do not load deployment instructions for routine work.
2. List sessions and reuse a suitable session when appropriate. Do not restart or kill a user's existing session just to obtain a clean environment.
3. When creating a session for a file, inspect its kernel metadata:

```bash
j-cli -j kernelspec inspect-file analysis.py
```

Use `kernel_name` as `--kernel`. If it is null, inspect the paired notebook, project environment files (`pixi.toml`, `pyproject.toml` / `uv.lock`, Conda files), and `j-cli -j kernelspec list`. If there is no clear single match, ask the user rather than guessing.

```bash
j-cli session create --kernel <detected_kernel_name> --name analysis
```

Save the returned `session_selector`; retain `session_id` when a stable identifier is needed. Commands accept the full ID, shortest unique selector, or exact unique session name. Ambiguous selectors fail without choosing a session.

## List sessions

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

## Interrupt, restart, or clean up

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

Kill disposable sessions you created when finished. Leave reused user sessions running unless cleanup was requested. Restart clears kernel state; use it only when needed and authorized by the task.

## Inspect live variables

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
