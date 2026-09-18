# Output workflow migration

j-cli now separates notebook-backed output from inline/plain-script output. Consumers should migrate from treating every image as a temporary extracted file.

## Notebook-backed execution

`j-cli exec --file analysis.ipynb` and py:percent execution save each completed cell to the target notebook before formatting the response. JSON output confirms a successful writeback with:

- `notebook_updated: true`
- `cell_index`: physical index in the executed source file
- `notebook_cell_index`: physical index in the saved notebook when it differs from the source index

Read saved outputs back through the originally requested `.py` or `.ipynb` path; j-cli resolves that request to the canonical notebook cell. The display summary may describe an image, but it is no longer an extracted temporary-file API. To retrieve the payload, first list the saved cell outputs, then read the selected physical output:

```bash
j-cli -j notebook outputs analysis.py --cell 4
j-cli -j notebook output analysis.py --cell 4 --output 1
```

For a `.py` path, j-cli follows the paired `.ipynb` only when a unique stable cell ID or unique non-conflicting content match proves the mapping. Positional and similarity guesses fail with `CELL_MAPPING_UNRELIABLE`.

Saved output is not a freshness guarantee. Editing source does not re-execute the notebook; execute the relevant cell before reading when current results are required.

## Inline and plain-script execution

Short text-only output remains inline and creates no output directory. Rich output or more than 4,000 text characters is persisted under the command's actual cwd, and JSON includes an absolute `output_manifest`:

```text
<cwd>/.j-cli/outputs/<run-id>/manifest.json
```

Use the manifest instead of retaining an old temporary image path:

```bash
j-cli -j output show "$MANIFEST"
j-cli -j output show "$MANIFEST" --output 0
```

Raster files referenced by a manifest live in that run directory. HTML is returned as original HTML text, JSON as structured data, and SVG as original SVG source; j-cli does not rasterize them.

`.j-cli` is workspace data, not permanent archival storage. Complete managed runs are cleaned opportunistically after persistence, by default when older than 7 days or outside the newest 50 runs. Preview or override cleanup with:

```bash
j-cli output clean --dry-run --days 7 --max-runs 50
```

The equivalent environment settings are `JCLI_OUTPUT_RETENTION_DAYS` and `JCLI_OUTPUT_MAX_RUNS`. Cleanup has no background daemon and retains unknown or uncertain entries. `j-cli setup git` adds `**/.j-cli/` to its managed ignore block.

## Agent integrations

Claude Code, Codex, DSH, and OpenCode expose one `read_notebook_output` call. Omitting `output_index` lists the cell's saved outputs; providing it reads one output, with optional `mime_type`, `offset`, and `limit`.

Claude Code and Codex use the `jcli-notebook-output` MCP server and need `jupyter-jcli[mcp]`. Project setup passes an explicit project root. User setup relies on client-provided roots and returns `ROOTS_REQUIRED` if the client provides none. DSH and OpenCode provide the same contract through native adapters. Every adapter is read-only: it does not execute cells, modify notebooks, create a cache, or run a background service.

Removing a managed integration removes configuration only; it does not delete notebook or `.j-cli` output data.

The four adapter contracts and local tests are covered in this repository. This does not claim an end-to-end verification in each external host application; that host integration check is separate. The shared MCP tests currently validate against MCP Python SDK 1.30.0, which is not a claimed minimum host version.
