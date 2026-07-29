# Py:Percent Design

## Status

This document describes the py:percent behavior implemented by j-cli today. It
records the constraints that conversion, execution, and pair synchronization
must preserve. It is not a proposal for full Jupytext compatibility.

## Purpose

j-cli uses a py:percent file as the text-editable representation of a notebook.
The paired `.ipynb` remains the runtime representation that stores outputs and
notebook metadata. This split supports normal Python editing and Git workflows
without giving up notebook execution results.

The design favors:

- deterministic Python text for review and merging;
- source synchronization without discarding useful notebook outputs;
- a small parser and emitter with no runtime dependency on Jupytext or IPython;
- explicit handling of drift when both sides of a pair have changed.

The format conversion is intentionally lossy outside the source, cell type,
kernel identity, and output fields described below.

## Core Model

Both `.py` and `.ipynb` inputs become a `ParsedFile` containing ordered `Cell`
objects. A cell carries an index, type, source, and optional source line range.
A parsed file carries kernel identity, pair information, raw front matter, and
the `is_py_percent` classification.

The common model gives conversion, execution, drift checks, and notebook
summaries one representation. It does not model arbitrary notebook or cell
metadata.

## Format Recognition

j-cli classifies Python text as py:percent when either condition holds:

- the file starts with a closed `# ---` front matter block; or
- the file contains a line matching a `# %%` cell marker.

A plain Python script still parses as one code cell, but
`is_py_percent` remains false. This distinction controls notebook creation:
executing a py:percent file can create a paired notebook, while executing a
plain script cannot.

Front matter must start on the first line and have a closing `# ---` delimiter.
The parser preserves the complete raw block for a py-to-py round trip. It does
not parse general YAML. It extracts only these kernelspec fields from commented
lines:

- `name`;
- `display_name`;
- `language`.

An unclosed block does not count as front matter.

## Cells and Markers

`# %%` starts a code cell. A marker containing `[markdown]` starts a markdown
cell, and one containing `[raw]` starts a raw cell. The optional `id` marker
option maps to the top-level nbformat cell ID, not cell metadata. Other marker
annotations do not become cell metadata and default to a code cell unless they
include one of those type tags.

The emitter uses canonical markers:

```python
# %% id="a1b2c3d4"
print("code")

# %% [markdown] id="report-title"
# Markdown text

# %% [raw] id="raw_data"
# Raw text
```

Cell IDs contain 1 to 64 letters, digits, hyphens, or underscores and must be
unique within a notebook. The parser accepts quoted and unquoted values. The
emitter uses quoted values, retains the first occurrence of a duplicate ID, and
assigns a new ID to later occurrences. Once any marker in a file has an ID,
drift synchronization assigns IDs to newly added marker-only cells and writes
them to both sides.

Markdown and raw source lines receive one comment prefix in Python text. The
parser removes one prefix when reconstructing the cell. A blank line inside
such a cell emits as a bare `#`.

The parser trims outer whitespace from cell source. An explicit cell marker
preserves an empty cell and its type; whitespace-only source normalizes to an
empty string. Empty files and front matter without a cell marker do not create
a cell. Source line ranges describe non-empty py:percent cells only; plain
scripts do not expose those ranges.

Legacy files without IDs remain valid and continue to use content alignment.
Tags, attachments, and arbitrary per-cell metadata remain outside the text
format.

## IPython Syntax

Notebook code can contain IPython syntax that Python tooling cannot parse.
When j-cli emits py:percent text, it comments supported magic, shell, and help
forms. Parsing the text restores those forms before execution or notebook
creation.

The transformer uses Python tokenization and a small grammar modeled on the
relevant IPython token transformations. It does not import IPython and does not
claim complete IPython syntax coverage. It avoids transforming magic-like text
inside strings.

For recognized Python-body cell magics such as `%%timeit` and `%%writefile`,
j-cli comments the magic line and leaves the body active when that body parses
as Python. For other cell magics, j-cli comments through the end of the cell and
records an internal marker that allows restoration. Indented magic may require
a temporary `pass` placeholder to keep the emitted file syntactically valid;
the reverse transformation removes that placeholder.

The encoded Python file is designed to remain parseable, not to produce the
same result when run directly with the Python interpreter. j-cli restores the
original IPython syntax before sending code to a kernel.

## Pair Naming

Pair discovery uses fixed names in the same directory:

| Python path | Notebook path |
| --- | --- |
| `name.py` | `name.ipynb` |
| `name.dummy.py` | `name.ipynb` |

When resolving from `name.ipynb`, j-cli prefers `name.dummy.py` if it exists,
then falls back to `name.py`. It does not search other directories or infer a
pair from notebook metadata.

The `.dummy.py` form lets a repository keep a Python representation without
claiming that direct Python execution has notebook semantics.

## Conversion

### Notebook to Python

`ipynb-to-py` reads cell type, source, stable cell ID, and the notebook
kernelspec. The emitter writes py:percent text, omits execution outputs, and
synthesizes a minimal commented kernelspec block when the notebook declares a
kernel name.
Raw front matter preservation applies to py:percent parse-and-emit paths, not
to notebook input.

Notebook metadata outside kernelspec and all cell metadata are omitted.

### Python to Notebook

When the target notebook does not exist, `py-to-ipynb` creates one from the
parsed cells. It writes kernelspec metadata when a kernel name is available;
missing display name and language values default to the kernel name and
`python`.

`py-to-ipynb` accepts files where all cells have persistent IDs or where no
cells have persistent IDs.

When the target exists, j-cli replaces its cell list from the parsed cells.
Notebook-level metadata remains in place. Aligned cells reuse old notebook cell
objects, preserving their IDs, metadata, outputs, and execution counts according
to the output policy.

Canonical pair conversions refresh the pair baseline. Conversions to an
explicit noncanonical output path act as exports and do not refresh it.

## Output Preservation

Updating an existing notebook first aligns unique IDs in relative order, then
falls back to type and source within unmatched regions. The alignment handles
unchanged, edited, inserted, and deleted cells, including repeated source. It
uses bounded fallbacks for large replacement regions so alignment does not
require unbounded quadratic work. Notebook writeback also maps unique IDs
directly, preserving cell state across a reorder even though textual diff
represents a move as deletion plus insertion.

The `--outputs` policy controls aligned code cells:

| Policy | Existing aligned outputs |
| --- | --- |
| `preserve` | Preserve outputs and execution count, including edited source |
| `clear-edited` | Clear them on cells classified as edited |
| `clear-all` | Clear them on every code cell |

New code cells always start without outputs. Markdown and raw cells do not
carry execution outputs.

New cells without a matching ID or content anchor do not inherit old metadata.

## Canonical Text and Drift

Drift comparison first converts both sides to canonical py:percent text. The
canonicalizer parses and re-emits cells, normalizes markers and spacing,
preserves empty cells, and synthesizes front matter from the kernel name only.
It removes raw front matter, display name, and language from comparison so
equivalent kernel identities do not create metadata-only drift. Plain scripts
pass through unchanged.

Canonicalization preserves the presence or absence of IDs in legacy text. When
an ID-enabled tracked file contains new cells without IDs, drift synchronization
assigns IDs once and requests Python writeback. For a legacy Python file, drift
comparison suppresses notebook IDs and keeps the previous content-based form.

For a canonical pair in a Git worktree, j-cli obtains the Python baseline from
the newer of:

- the Python file in `HEAD`;
- a sticky baseline under `refs/jcli/pair-sync/` written by a successful sync.

j-cli then performs a diff3 text merge with canonical baseline text, current
Python text, and current notebook text emitted as py:percent. Text merging
preserves insertions and deletions better than position-only cell merging. A
successful result is parsed back into cells before either side is updated.

Without a baseline, equal canonical text is in sync. Different text reports
drift and does not choose a winning side. A conflicting three-way merge returns
diff3 conflict text and the cell indices that contain conflict markers.

The merge operates on canonical text, not independently on cell objects. Git
refs support synchronization state, but they do not add files to the user's
normal branch history.

## Execution and Writeback

File execution selects code cells and runs them sequentially. With no explicit
timeout, each cell receives a ten-second timeout. An explicit timeout becomes
one wall-clock budget shared by all selected cells.

Before execution, a py:percent `.py` file without an existing pair creates its
canonical `.ipynb`. This includes files recognized through front matter alone.
A plain Python script does not create a notebook.

After each cell completes, j-cli writes its raw kernel outputs and execution
count to the paired notebook, then emits the cell result. If a later cell fails,
the completed cells remain written. A cell that returns an error output is also
written before execution stops. JSON file execution emits one JSON object per
completed cell and a final summary only after full success.

Execution writeback addresses the notebook cell at the parsed cell index. It
does not run source alignment at that point. The pair must therefore be source
synchronized before execution; empty-cell differences or concurrent structural
edits can invalidate index correspondence.

Writeback supports stream, display data, execute result, and error outputs.
It removes transient fields that cannot be persisted as valid notebook output.

## Behavioral Boundaries

The current design does not promise:

- full Jupytext syntax or metadata compatibility;
- lossless empty-cell or whitespace round trips;
- preservation of arbitrary notebook cell metadata;
- complete coverage of IPython input transformations;
- source-aligned execution writeback;
- automatic conflict resolution without a common baseline.

Changes that broaden any of these guarantees must update the common model,
round-trip tests, canonicalization rules, and drift behavior together.
