# Output protocol v1

The shared Python reader is the source of truth for notebook output indexing, MIME selection, text paging, and transport limits. Adapters should call this API or the JSON CLI instead of parsing `.ipynb` themselves.

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

The file cell index is always the physical zero-based index in the supplied file. An `.ipynb` is direct. A `.py` cell is mapped only by a unique shared stable ID or a unique, non-conflicting `(cell type, source)` match. Position and similarity guesses are rejected with `CELL_MAPPING_UNRELIABLE`. Reads never synchronize pairs, write baselines, execute cells, or create `.j-cli`.

## MIME rules

Without `mime_type`, selection is deterministic:

1. `image/png`, `image/jpeg`, `image/webp`, `image/gif`
2. `text/html`
3. `text/markdown`
4. `text/plain`
5. `application/json`, then lexically sorted `+json` types
6. `image/svg+xml` source text

Adapters may pass `supported_mime_types`; unsupported representations are skipped during automatic selection. An explicit MIME request is exact and fails instead of falling back. Unknown MIME types remain visible in directories.

Raster images use strict base64 decoding and lightweight PNG/JPEG/WebP/GIF signature checks. No imaging dependency is required. Text and HTML are returned unchanged. JSON stays structured and is never truncated into invalid JSON.

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
