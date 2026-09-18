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

The pair projection intentionally excludes cell metadata, outputs, execution
counts, and the explicit local/runtime notebook metadata paths described below.

## Core Model

Both `.py` and `.ipynb` inputs become a `ParsedFile` containing ordered `Cell`
objects and notebook-level metadata. A cell carries an index, type, source, and
optional source line range. A parsed Python file also retains raw front matter
and the `is_py_percent` classification.

Pair synchronization projects this representation into a `PairState`. The
shared state contains cell type/source/stable-ID semantics and notebook-level
metadata after explicit local/runtime exclusions. Cell metadata, outputs, and
execution counts remain representation-local and are not shared.

## Format Recognition

j-cli classifies Python text as py:percent when either condition holds:

- the file starts with a closed `# ---` front matter block; or
- the file contains a line matching a `# %%` cell marker.

A plain Python script still parses as one code cell, but
`is_py_percent` remains false. This distinction controls notebook creation:
executing a py:percent file can create a paired notebook, while executing a
plain script cannot.

Front matter must start on the first line and have a closing `# ---` delimiter.
The parser uses YAML to read the `jupyter` mapping as notebook metadata and keeps
the raw block as a formatting template. Fields outside `jupyter` are Python
header data: synchronization preserves them but does not copy them into notebook
metadata. When shared metadata changes, the emitter structurally replaces the
`jupyter` subtree, so stale raw text cannot override the target state. An
unclosed block does not count as front matter.

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

The emitter removes trailing blank and whitespace-only lines from every cell.
It preserves blank lines inside a cell and spaces on the final nonblank line,
including the two spaces that create a Markdown hard line break. It writes one
blank line between cells. The final cell has no following blank line, and every
nonempty emitted file ends with exactly one `\n`.

The parser removes one trailing blank separator from a cell, including the EOF
separator written by older j-cli versions, and the physical line ending after
a cell's final body line. It preserves leading blank lines, internal blank
lines, and spaces on the final nonblank line. An explicit cell marker preserves
an empty cell and its type; whitespace-only source normalizes to an empty
string. Empty files and front matter without a cell marker do not create a cell.
Source line ranges describe non-empty py:percent cells only; plain scripts do
not expose those ranges.

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

`ipynb-to-py` reads cell type, source, stable cell ID, and shared notebook
metadata. The emitter writes the metadata under the commented `jupyter` mapping
and omits execution outputs and cell metadata. Raw front matter preservation
applies to py:percent parse-and-emit paths, not to notebook input.

### Python to Notebook

`convert assign-ids FILE.py` fills missing marker IDs in place. When the
canonical paired notebook exists, j-cli aligns its cells with the Python cells
and reuses nonconflicting notebook IDs. It generates IDs for remaining cells.
The command leaves a file unchanged when every cell already has an ID and
rejects plain Python without py:percent markers or front matter.

When the target notebook does not exist, `py-to-ipynb` creates one from the
parsed cells and shared metadata. A kernelspec with a name but no display name
uses the name as the minimal nbformat-compatible display fallback. j-cli does
not infer a language or consult locally installed kernels.

`py-to-ipynb` accepts files where all cells have persistent IDs or where no
cells have persistent IDs.

When the target exists, j-cli applies the same target `PairState` to both
representations. Shared notebook metadata is replaced, including deletions.
Aligned cells reuse old notebook cell objects, preserving their IDs, cell
metadata, outputs, and execution counts according to the output policy.

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

Drift comparison projects both sides to `PairState`. Its deterministic baseline
encoding sorts metadata keys, so YAML key order and header layout do not create
drift. Shared metadata value changes do create drift, including changes to
`kernelspec.display_name`, `kernelspec.language`, custom fields, explicit nulls,
and deletions. Cell canonicalization still normalizes markers and spacing,
removes trailing blank lines, and preserves empty cells. Plain scripts pass
through the standalone canonicalizer unchanged.

The shared metadata projection excludes exactly these local/runtime paths:

- `metadata.language_info.version`;
- `metadata.widgets`;
- `metadata.jupytext`;
- `metadata.vscode`;
- `metadata.colab`.

Each representation retains its own excluded values. When the kernelspec name
changes, j-cli drops the old `language_info.version` and notebook widget state
instead of attaching stale runtime data to the new kernel. Other metadata is
shared by default. The complete `kernelspec` plus shared `language_info` form one
atomic kernel configuration during merge; this prevents a name from one branch
being combined with a language description from another branch.

Canonicalization preserves the presence or absence of IDs in legacy text. When
an ID-enabled tracked file contains new cells without IDs, synchronization
reuses aligned notebook IDs where possible, assigns remaining IDs once, and
writes them to both sides. For a legacy Python file, drift comparison suppresses
notebook IDs and keeps the previous content-based form.

For a canonical pair in a Git worktree, j-cli obtains the Python baseline from
the newer of:

- the Python file in `HEAD`;
- a sticky baseline under `refs/jcli/pair-sync/` written by a successful sync.

j-cli performs the established diff3 text merge only on kernel-free canonical
cell text. Shared metadata uses a recursive mapping three-way merge; missing and
explicit null are distinct, while lists and scalar values merge as whole values.
Metadata conflicts report their path and base/Python/notebook values. The atomic
kernel configuration reports a dedicated `metadata.kernel` conflict. A
conflict-free result is one complete target `PairState`.
Reading a baseline does not modify Git refs. An existing sticky baseline wins
when its timestamp equals the Python file's latest commit in `HEAD`. A strictly
newer commit makes the sticky baseline eligible for explicit garbage collection;
reading alone leaves the ref in place. Switching to an older `HEAD` can therefore
select that retained sticky baseline again. The pre-edit hook bootstraps a missing
baseline from the canonical in-sync result without rereading either source.

Without a baseline, equal shared state is in sync. Different state reports
drift and does not choose a winning side. Conflicts distinguish cell indices
from structured metadata paths.

Conversion, pre/post edit hooks, and pre-commit use one synchronization flow:
select a target state, apply it with the requested output policy, re-read both
projections, require convergence, and only then store the deterministic shared
state baseline. Reports reflect actual byte changes. A second synchronization
is therefore idempotent. Canonical exports to non-pair paths do not advance the
managed pair baseline. Git refs support synchronization state but do not add
files to normal branch history.

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
- byte-for-byte preservation of outer cell whitespace;
- preservation of arbitrary notebook cell metadata;
- complete coverage of IPython input transformations;
- source-aligned execution writeback;
- automatic conflict resolution without a common baseline.

Changes that broaden any of these guarantees must update the common model,
round-trip tests, canonicalization rules, and drift behavior together.
