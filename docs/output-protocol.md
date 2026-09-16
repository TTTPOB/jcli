# Output protocol v1

The shared Python reader is the source of truth for notebook output indexing, MIME selection, text paging, and transport limits. Adapters should call this API or the JSON CLI instead of parsing `.ipynb` themselves.

This protocol reads saved output. It does not promise that a saved output came from the latest version of the source file, and reading never executes a cell. Callers that require fresh results must execute the relevant source first.

## Public API

```python
from jupyter_jcli.outputs import list_outputs, read_output
from jupyter_jcli.outputs.notebook import (
    list_notebook_outputs,
    read_notebook_output,
    resolve_notebook_cell,
)

list_outputs(outputs, *, source, max_transport_bytes=MAX_TRANSPORT_BYTES)
read_output(
    outputs,
    output_index,
    *,
    source,
    mime_type=None,
    supported_mime_types=None,
    offset=0,
    limit=DEFAULT_TEXT_LIMIT,
    max_transport_bytes=MAX_TRANSPORT_BYTES,
)
resolve_notebook_cell(file_path, cell_index)
list_notebook_outputs(file_path, cell_index, *, max_transport_bytes=...)
read_notebook_output(
    file_path,
    cell_index,
    output_index,
    *,
    mime_type=None,
    supported_mime_types=None,
    offset=0,
    limit=DEFAULT_TEXT_LIMIT,
    max_transport_bytes=...,
)
```

`output_index` is the actual zero-based index in `cell.outputs`. A multi-MIME bundle remains one output. Stream and error outputs keep their own structures. `display_data` and `execute_result` return one `selected` representation while listing every available MIME type.

## CLI

```bash
j-cli -j notebook outputs analysis.ipynb --cell 4
j-cli -j notebook output analysis.ipynb --cell 4 --output 1
j-cli -j notebook output analysis.py --cell 4 --output 2 --mime text/html
j-cli -j notebook output analysis.ipynb --cell 4 --output 0 --offset 100000 --limit 100000
j-cli -j notebook output analysis.ipynb --cell 4 --output 1 \
  --supported-mime image/png --supported-mime text/html
```

Adapters should repeat `--supported-mime` for every MIME type they can present. Omitting the option keeps the default core behavior. The option filters automatic selection and explicit `--mime`: an explicitly requested representation excluded by the capability set fails with `MIME_NOT_SUPPORTED` rather than falling back.

The file cell index is always the physical zero-based index in the supplied file. An `.ipynb` is direct. A `.py` cell is mapped only by a unique shared stable ID or a unique, non-conflicting `(cell type, source)` match. Position and similarity guesses are rejected with `CELL_MAPPING_UNRELIABLE`. Reads never synchronize pairs, write baselines, execute cells, create caches, or create `.j-cli`. There is no background indexing daemon.

## Saved-output locations

Notebook-backed execution writes outputs to the target `.ipynb` before formatting the command response. Read those outputs with `notebook outputs` / `notebook output`, or with the adapter tool described below. A `.py` request resolves its paired notebook only through the reliable mapping rules above.

Inline execution and plain `.py` execution have no notebook to write. Short text-only results stay inline and create no output directory. Rich output or more than 4,000 text characters is saved under the command's real current working directory:

```text
<cwd>/.j-cli/outputs/<run-id>/manifest.json
```

The execution response includes the absolute `output_manifest` path. Raster payloads are files in the same run directory; use `j-cli output show MANIFEST` to list the run, then add `--output INDEX` and optionally `--mime`, `--offset`, or `--limit` to read one output. `.j-cli` is retained workspace data, not a permanent archive. `j-cli setup git` adds `**/.j-cli/` to its managed ignore block.

Completed inline runs are cleaned opportunistically after a new persisted run. The defaults are 7 days and the newest 50 runs; a run is eligible when either limit is exceeded. Configure the limits with `JCLI_OUTPUT_RETENTION_DAYS` and `JCLI_OUTPUT_MAX_RUNS`, or run cleanup explicitly:

```bash
j-cli output clean --dry-run
j-cli output clean --days 7 --max-runs 50
```

Cleanup has no daemon. It deletes only complete, recognized run groups and retains incomplete, unknown, linked, or otherwise uncertain entries.

## Adapter tool

Claude Code, Codex, DSH, and OpenCode expose one tool named `read_notebook_output`. One call either lists a cell's saved outputs (omit `output_index`) or reads one physical output (provide `output_index`); optional `mime_type`, `offset`, and `limit` select a representation or page text. The tool is read-only and does not write a cache.

Claude Code and Codex use the `jcli-notebook-output` stdio MCP server installed by their setup commands and require the `jupyter-jcli[mcp]` extra. Project setup passes the project root explicitly. User setup depends on roots supplied by the MCP client and fails with `ROOTS_REQUIRED` when none are available. Paths outside trusted roots fail instead of being read. Removing any host integration removes only managed configuration and never deletes notebooks or saved output data.

DSH and OpenCode use native adapters with the same one-tool contract. Contract and local adapter tests cover all four integrations; external end-to-end runs in the four host applications are not claimed here and remain a separate integration check. The shared MCP contract tests currently use MCP Python SDK 1.30.0 as a validation version, not as a statement of the minimum supported host version.

## MIME rules

Without `mime_type`, selection is deterministic:

1. `image/png`, `image/jpeg`, `image/webp`, `image/gif`
2. `text/html`
3. `text/markdown`
4. `text/plain`
5. `application/json`, then lexically sorted `+json` types
6. `image/svg+xml` source text

Adapters may pass `supported_mime_types`; unsupported representations are skipped during automatic selection. An explicit MIME request is exact and fails instead of falling back. Unknown MIME types remain visible in directories.

Raster images use strict base64 decoding and lightweight PNG/JPEG/WebP/GIF signature checks. No imaging dependency is required. HTML is returned as original text, JSON stays structured, and SVG is returned as original source text; none of these representations is rasterized. Structured JSON is never truncated into invalid JSON.

## Envelope and limits

Every success response includes `schema_version: 1`, `status: "ok"`, and notebook provenance. MIME bundle reads include:

```json
{
  "schema_version": 1,
  "status": "ok",
  "source": {
    "kind": "notebook",
    "path": "/work/analysis.ipynb",
    "cell_index": 4,
    "output_index": 1,
    "mapping": "direct"
  },
  "output_type": "display_data",
  "available_mime_types": ["image/png", "text/plain"],
  "metadata": {},
  "selected": {
    "mime_type": "image/png",
    "encoding": "base64",
    "bytes": 8,
    "data": "iVBORw0KGgo="
  }
}
```

Text uses UTF-8 data plus `offset`, `returned_characters`, `total_characters`, `truncated`, and optional `next_offset`. Offsets and limits count Unicode characters. The default page is 100,000 characters. `bytes` is the UTF-8 size of returned text, compact JSON size for a JSON MIME value, or decoded raster size.

The compact serialized response limit is 32 MiB (`MAX_TRANSPORT_BYTES`). The JSON CLI uses the same compact UTF-8 serialization counted by the core, so adapters can apply this limit without compensating for pretty-print whitespace. Over-limit images and structured JSON fail with `OUTPUT_TOO_LARGE`; they are never silently truncated. Text callers should request a smaller page.

Protocol fixtures live at `tests/fixtures/outputs/mixed_outputs.json`. They are intended for Python and adapter contract tests.

## Error codes

- `FILE_NOT_FOUND`, `FILE_TYPE_UNSUPPORTED`, `NOTEBOOK_PAIR_NOT_FOUND`
- `NOTEBOOK_READ_FAILED`, `CELL_NOT_FOUND`, `CELL_MAPPING_UNRELIABLE`
- `OUTPUT_NOT_FOUND`, `OUTPUT_TYPE_UNSUPPORTED`, `OUTPUT_DATA_INVALID`
- `MIME_NOT_FOUND`, `MIME_NOT_SUPPORTED`, `MIME_NOT_APPLICABLE`
- `INVALID_WINDOW`, `WINDOW_NOT_SUPPORTED`
- `INVALID_TRANSPORT_LIMIT`, `OUTPUT_TOO_LARGE`

Library calls raise `OutputProtocolError` with `code` and `message`. CLI commands convert it to the repository's standard structured error response.
