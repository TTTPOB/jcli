# Edit an existing notebook

1. Locate the relevant cells and read their complete source. Load [view](view.md) only if you need source discovery or search commands.
2. If a paired py:percent file already exists, edit it directly. Otherwise, convert the notebook to text first as shown below.
3. Preserve existing cell IDs. End cells that should display a table or figure with a bare `df` or `fig` expression by default; do not add `fig.show()`, `plt.show()`, or `display(fig)` merely to render the final figure.
4. Let the configured hook synchronize changes; use manual conversion only when the hook is not available. Inspect any drift diagnostic before continuing.
5. Do not execute just to save edits. If execution is requested, load [exec](exec.md) at that stage.

## Text round-trip

**Never edit `.ipynb` files directly** — use the py:percent round-trip to edit notebook
cells safely without losing outputs:

```bash
# 1. Convert notebook to py:percent (outputs are preserved in the .ipynb)
j-cli convert ipynb-to-py analysis.ipynb analysis.py

# 2. Edit analysis.py using normal text tools (Edit tool, etc.)
#    Preserve id="..." on existing cell markers
#    Cell markers: # %% (code), # %% [markdown], # %% [raw]

# 3. Write edited sources back; preserve outputs (default)
# Run manually only when no synchronization hook is configured
j-cli convert py-to-ipynb analysis.py analysis.ipynb
```

If a paired `.py` already exists (same stem), you can go directly to step 2 and then step 3.

The `j-cli convert py-to-ipynb` command detects whether the `.ipynb` already exists:
- **Exists** → source-only update (outputs and execution counts preserved by default)
- **Does not exist** → new notebook created from the py cells

> **Policy**: The `NotebookEdit` tool is disabled by the `notebook-edit-guard` hook
> installed via `j-cli setup claude`. Always go through the py:percent round-trip instead.

Hook synchronization assigns IDs to newly inserted cells. Preserve existing IDs when moving or editing cells. For explicit conversion of mixed-ID files, load [convert](convert.md).

## Handle pair drift

`.ipynb` is gitignored by design — only `.py` history is the merge baseline.

| Who triggers | Hook | When | Meaning | Next step |
|---|---|---|---|---|
| Agent (pre-edit) | `pair-drift-guard` | Pre Edit/Write/apply_patch | Drift already existed before your call | Read the message; if auto-merged, re-read the target file; if conflict, inspect and pick a side |
| Agent (post-edit) | `pair-drift-guard-post` | Post Edit/Write/apply_patch | Your edit may have diverged the pair | Read `~` edited, `+` inserted, and `- old:N` deleted markers after an auto-sync with a git baseline. Follow any omission hint with `j-cli notebook summary`. If warned: pick a side with `j-cli convert` |
| Agent | `notebook-edit-guard` | Pre NotebookEdit | Hard deny; use py:percent round-trip | Follow the three-step convert workflow above |
