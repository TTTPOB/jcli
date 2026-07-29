"""Tests for py:percent and ipynb serialization."""

import ast

import nbformat
import pytest
from IPython.core.inputtransformer2 import TransformerManager

from jupyter_jcli._enums import OutputPolicy
from jupyter_jcli.formats.ipynb import from_node as parsed_from_ipynb
from jupyter_jcli.formats.ipynb import to_node as create_ipynb_from_parsed
from jupyter_jcli.formats.model import Cell, ParsedFile
from jupyter_jcli.formats.percent import dumps as emit_py_percent
from jupyter_jcli.formats.percent import loads as parse_py_percent_text
from jupyter_jcli.pairing import update_ipynb_sources

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cells(*pairs: tuple[str, str]) -> list[Cell]:
    """Build a Cell list from (cell_type, source) pairs."""
    return [Cell(index=i, cell_type=t, source=s) for i, (t, s) in enumerate(pairs)]


def _parsed(
    kernel: str | None, *pairs: tuple[str, str], fmr: str | None = None
) -> ParsedFile:
    return ParsedFile(
        kernel_name=kernel,
        cells=_cells(*pairs),
        front_matter_raw=fmr,
    )


# ---------------------------------------------------------------------------
# emit_py_percent — round-trip stability
# ---------------------------------------------------------------------------


class TestEmitPyPercent:
    def test_roundtrip_code_cell(self):
        source = "x = 1\ny = 2"
        parsed = _parsed("python3", ("code", source))
        text = emit_py_percent(parsed)
        parsed2 = parse_py_percent_text(text)
        assert len(parsed2.cells) == 1
        assert parsed2.cells[0].cell_type == "code"
        assert parsed2.cells[0].source == source

    @pytest.mark.parametrize(
        ("source", "commented"),
        [
            (
                "%load_ext autoreload\n%autoreload 2",
                "# %load_ext autoreload\n# %autoreload 2",
            ),
            ("!ls -al", "# !ls -al"),
            ("object?", "# object?"),
            ('%%bash\necho "hello"', '# echo "hello"'),
            ("files = !ls", "# files = !ls"),
            ("timing = %timeit value + 1", "# timing = %timeit value + 1"),
            ("%timeit foo( \\\n    1)", "# %timeit foo( \\\n#     1)"),
        ],
    )
    def test_roundtrip_comments_ipython_magics(self, source, commented):
        text = emit_py_percent(_parsed("python3", ("code", source)))

        assert commented in text
        parsed = parse_py_percent_text(text)
        assert len(parsed.cells) == 1
        assert parsed.cells[0].source == source

    @pytest.mark.parametrize(
        "magic",
        [
            "%%capture",
            "%%debug",
            "%%file output.py",
            "%%prun",
            "%%pypy",
            "%%python",
            "%%python2",
            "%%python3",
            "%%time",
            "%%timeit",
            "%%writefile output.py",
        ],
    )
    def test_python_body_cell_magic_keeps_body_parseable(self, magic):
        source = f"{magic}\nvalue = sum(range(10))"
        text = emit_py_percent(_parsed("python3", ("code", source)))

        assert f"# {magic}\nvalue = sum(range(10))" in text
        compile(text, "notebook.py", "exec")
        assert parse_py_percent_text(text).cells[0].source == source

    def test_writefile_with_non_python_body_comments_entire_cell(self):
        source = "%%writefile notes.txt\nplain text"
        text = emit_py_percent(_parsed("python3", ("code", source)))

        assert "# %%writefile notes.txt" in text
        assert "# jupyter-jcli: commented cell magic body" in text
        assert "# plain text" in text
        compile(text, "notebook.py", "exec")
        assert parse_py_percent_text(text).cells[0].source == source

    def test_cell_magic_without_body_roundtrips(self):
        source = "%%bash"
        text = emit_py_percent(_parsed("python3", ("code", source)))

        compile(text, "notebook.py", "exec")
        assert parse_py_percent_text(text).cells[0].source == source

    def test_cell_magic_after_blank_line_is_restored(self):
        text = emit_py_percent(_parsed("python3", ("code", "\n%%bash\necho hi")))

        compile(text, "notebook.py", "exec")
        assert parse_py_percent_text(text).cells[0].source == "%%bash\necho hi"

    def test_indented_magic_adds_and_removes_parseable_placeholder(self):
        source = "if ready:\n    %timeit value + 1"
        text = emit_py_percent(_parsed("python3", ("code", source)))

        assert "    pass  # jupyter-jcli: IPython magic placeholder" in text
        assert "    # %timeit value + 1" in text
        compile(text, "notebook.py", "exec")
        assert parse_py_percent_text(text).cells[0].source == source

    def test_user_placeholder_without_magic_is_preserved(self):
        source = "if ready:\n    pass  # jupyter-jcli: IPython magic placeholder\n    value = 1"
        text = emit_py_percent(_parsed("python3", ("code", source)))

        assert parse_py_percent_text(text).cells[0].source == source

    @pytest.mark.parametrize(
        "source",
        [
            "%time expression",
            "!find . -name '*.py'",
            "?value",
            "value?",
            "value??",
            ",print value other",
            ";print value other",
            "/print value other",
            "value.attribute?",
            "value[-1]?",
            "%time?",
            "x = %time expression",
            "x, y = %some_magic args",
            'files = !find . -name "*.py"',
            "files = !!find . -name '*.py'",
            "a, *rest = !command",
            "x: object = %time expression",
            "target.attribute = %time expression",
            "target[0] = !command",
            "(\n    x,\n    y,\n) = %some_magic args",
            "x = %time foo( \\\n    1)",
        ],
    )
    def test_matches_ipython_assignment_grammar(self, source):
        transformer = TransformerManager()
        assert transformer.transform_cell(source) != source + "\n"

        text = emit_py_percent(_parsed("python3", ("code", source)))

        ast.parse(text)
        assert parse_py_percent_text(text).cells[0].source == source

    @pytest.mark.parametrize(
        "source",
        [
            "(x := %time expression)",
            "print(%time func())",
            "items.append(!ls)",
            "foo + %time bar",
            "x = 1; %time func()",
            "x = y = %time expression",
            "x += %time expression",
            "x = %123",
            "= !command",
            "value?  # comment",
            'value["key"]?',
        ],
    )
    def test_leaves_syntax_ipython_does_not_transform(self, source):
        transformer = TransformerManager()
        assert transformer.transform_cell(source) == source + "\n"

        text = emit_py_percent(_parsed("python3", ("code", source)))

        assert source in text
        assert parse_py_percent_text(text).cells[0].source == source

    def test_roundtrip_preserves_commented_magic(self):
        source = "# %load_ext autoreload"
        text = emit_py_percent(_parsed("python3", ("code", source)))

        assert "# # %load_ext autoreload" in text
        assert parse_py_percent_text(text).cells[0].source == source

    def test_does_not_comment_magic_inside_multiline_string(self):
        source = 'value = """\n%not_a_magic\n"""'
        text = emit_py_percent(_parsed("python3", ("code", source)))

        assert source in text
        assert parse_py_percent_text(text).cells[0].source == source

    def test_roundtrip_markdown_cell(self):
        source = "## Title\n\nSome text"
        parsed = _parsed("python3", ("markdown", source))
        text = emit_py_percent(parsed)
        parsed2 = parse_py_percent_text(text)
        assert len(parsed2.cells) == 1
        assert parsed2.cells[0].cell_type == "markdown"
        assert parsed2.cells[0].source == source

    def test_roundtrip_mixed_cells(self):
        cells = [
            ("code", "import numpy as np"),
            ("markdown", "## Analysis"),
            ("code", "np.array([1, 2, 3])"),
        ]
        parsed = _parsed("python3", *cells)
        text = emit_py_percent(parsed)
        parsed2 = parse_py_percent_text(text)
        assert len(parsed2.cells) == 3
        for orig, reparsed in zip(cells, parsed2.cells):
            assert reparsed.cell_type == orig[0]
            assert reparsed.source == orig[1]

    def test_markdown_line_prefix(self):
        source = "## Title\nSome text"
        parsed = _parsed(None, ("markdown", source))
        text = emit_py_percent(parsed)
        lines = [line for line in text.splitlines() if line.startswith("#")]
        # All markdown body lines should be prefixed with "# "
        body_lines = [line for line in lines if not line.startswith("# %%")]
        for line in body_lines:
            assert line.startswith("# ") or line == "#"

    def test_empty_markdown_line_becomes_bare_hash(self):
        # Empty line in markdown source should emit "#" (not "# ")
        source = "line1\n\nline2"
        parsed = _parsed(None, ("markdown", source))
        text = emit_py_percent(parsed)
        parsed2 = parse_py_percent_text(text)
        assert parsed2.cells[0].source == source

    def test_front_matter_preserved(self):
        # front_matter_raw always ends with \n (splitlines keepends=True)
        fmr = "# ---\n# jupyter:\n#   kernelspec:\n#     name: ir\n# ---\n"
        parsed = _parsed("ir", ("code", "print(1)"), fmr=fmr)
        text = emit_py_percent(parsed)
        assert text.startswith(fmr)
        parsed2 = parse_py_percent_text(text)
        assert parsed2.front_matter_raw == fmr
        assert parsed2.kernel_name == "ir"

    def test_synthesized_header_from_kernel_name(self):
        parsed = _parsed("python3", ("code", "x = 1"))
        text = emit_py_percent(parsed)
        assert "# ---" in text
        assert "python3" in text
        parsed2 = parse_py_percent_text(text)
        assert parsed2.kernel_name == "python3"

    def test_synthesized_header_includes_display_name_and_language(self):
        """When ParsedFile carries display_name and language, they appear in the
        synthesized header in alphabetical order (display_name, language, name)."""
        from jupyter_jcli.formats.model import ParsedFile

        parsed = ParsedFile(
            kernel_name="ir",
            kernel_display_name="R 4.2",
            kernel_language="R",
            cells=[Cell(0, "code", "1 + 1")],
        )
        text = emit_py_percent(parsed)
        assert "display_name: R 4.2" in text
        assert "language: R" in text
        assert "name: ir" in text
        # alphabetical order: display_name before language before name
        assert (
            text.index("display_name") < text.index("language") < text.index("name: ir")
        )
        # round-trip
        parsed2 = parse_py_percent_text(text)
        assert parsed2.kernel_name == "ir"
        assert parsed2.kernel_display_name == "R 4.2"
        assert parsed2.kernel_language == "R"

    def test_no_header_when_no_kernel(self):
        parsed = _parsed(None, ("code", "x = 1"))
        text = emit_py_percent(parsed)
        assert not text.startswith("# ---")
        parsed2 = parse_py_percent_text(text)
        assert len(parsed2.cells) == 1

    def test_roundtrips_empty_cells(self):
        parsed = _parsed(
            "python3",
            ("code", ""),
            ("markdown", ""),
            ("raw", ""),
            ("code", "x = 1"),
        )
        text = emit_py_percent(parsed)
        parsed2 = parse_py_percent_text(text)
        assert [(cell.cell_type, cell.source) for cell in parsed2.cells] == [
            ("code", ""),
            ("markdown", ""),
            ("raw", ""),
            ("code", "x = 1"),
        ]

    def test_marker_only_represents_one_empty_cell(self):
        parsed = parse_py_percent_text("# %%\n")

        assert len(parsed.cells) == 1
        assert parsed.cells[0].cell_type == "code"
        assert parsed.cells[0].source == ""

    def test_whitespace_only_source_normalizes_without_dropping_cell(self):
        parsed = parse_py_percent_text(
            emit_py_percent(_parsed(None, ("code", " \n\t")))
        )

        assert len(parsed.cells) == 1
        assert parsed.cells[0].source == ""

    def test_raw_cell_roundtrip(self):
        source = "raw content"
        parsed = _parsed(None, ("raw", source))
        text = emit_py_percent(parsed)
        assert "# %% [raw]" in text
        parsed2 = parse_py_percent_text(text)
        assert parsed2.cells[0].cell_type == "raw"
        assert parsed2.cells[0].source == source


# ---------------------------------------------------------------------------
# update_ipynb_sources — source-only update, outputs/metadata preserved
# ---------------------------------------------------------------------------


def _make_ipynb(cells: list[tuple[str, str, list]]) -> nbformat.NotebookNode:
    """Create a notebook from (cell_type, source, outputs) triples."""
    nb = nbformat.v4.new_notebook()
    for cell_type, source, outputs in cells:
        if cell_type == "code":
            cell = nbformat.v4.new_code_cell(source)
            cell.outputs = [
                nbformat.v4.new_output(output_type="stream", name="stdout", text=o)
                for o in outputs
            ]
            cell.execution_count = 1 if outputs else None
        else:
            cell = nbformat.v4.new_markdown_cell(source)
        nb.cells.append(cell)
    return nb


class TestUpdateIpynbSources:
    def test_preserves_empty_aligned_cell(self, tmp_path):
        nb = _make_ipynb([("code", "", ["output\n"]), ("code", "x = 1", [])])
        nb.cells[0].metadata["tags"] = ["empty"]
        cell_id = nb.cells[0].id
        p = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(p))

        update_ipynb_sources(p, _cells(("code", ""), ("code", "x = 1")))

        cell = nbformat.read(str(p), as_version=4).cells[0]
        assert cell.source == ""
        assert cell.id == cell_id
        assert cell.metadata["tags"] == ["empty"]
        assert cell.outputs[0]["text"] == "output\n"

    def test_preserves_aligned_cell_identity_and_metadata(self, tmp_path):
        nb = _make_ipynb([("code", "x = 1", ["1\n"])])
        nb.cells[0].metadata["tags"] = ["parameters"]
        cell_id = nb.cells[0].id
        p = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(p))

        update_ipynb_sources(p, [Cell(0, "code", "x = 10")])

        cell = nbformat.read(str(p), as_version=4).cells[0]
        assert cell.id == cell_id
        assert cell.metadata["tags"] == ["parameters"]
        assert cell.outputs[0]["text"] == "1\n"

    def test_updates_source_preserves_outputs_on_unchanged_cell(self, tmp_path):
        """Cells whose source is unchanged keep their outputs (hash match)."""
        nb = _make_ipynb(
            [
                ("code", "x = 1", ["1\n"]),
                ("code", "y = 2", ["2\n"]),
            ]
        )
        p = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(p))

        # Same sources -> outputs should be preserved
        new_cells = [
            Cell(0, "code", "x = 1"),
            Cell(1, "code", "y = 2"),
        ]
        update_ipynb_sources(p, new_cells)

        nb2 = nbformat.read(str(p), as_version=4)
        assert nb2.cells[0].source == "x = 1"
        assert nb2.cells[1].source == "y = 2"
        assert nb2.cells[0].execution_count == 1
        assert nb2.cells[1].execution_count == 1
        assert nb2.cells[0].outputs[0]["text"] == "1\n"
        assert nb2.cells[1].outputs[0]["text"] == "2\n"

    def test_changed_cell_preserves_outputs_by_default(self, tmp_path):
        nb = _make_ipynb(
            [
                ("code", "x = 1", ["1\n"]),
                ("code", "y = 2", ["2\n"]),
            ]
        )
        p = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(p))

        new_cells = [
            Cell(0, "code", "x = 10"),  # changed
            Cell(1, "code", "y = 2"),  # unchanged
        ]
        update_ipynb_sources(p, new_cells)

        nb2 = nbformat.read(str(p), as_version=4)
        assert nb2.cells[0].source == "x = 10"
        assert nb2.cells[0].outputs[0]["text"] == "1\n"
        assert nb2.cells[0].execution_count == 1
        assert nb2.cells[1].source == "y = 2"
        assert nb2.cells[1].outputs[0]["text"] == "2\n"

    def test_clear_edited_outputs_removes_changed_cell_outputs(self, tmp_path):
        nb = _make_ipynb([("code", "x = 1", ["1\n"])])
        p = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(p))

        update_ipynb_sources(
            p,
            [Cell(0, "code", "x = 10")],
            output_policy=OutputPolicy.CLEAR_EDITED,
        )

        cell = nbformat.read(str(p), as_version=4).cells[0]
        assert cell.outputs == []
        assert cell.execution_count is None

    def test_supports_count_change_insert(self, tmp_path):
        """Inserting a new cell (count grows) works without error."""
        nb = _make_ipynb([("code", "x = 1", ["1\n"])])
        p = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(p))

        new_cells = [
            Cell(0, "code", "x = 1"),  # same -> outputs preserved
            Cell(1, "code", "y = 2"),  # new cell
        ]
        update_ipynb_sources(p, new_cells)

        nb2 = nbformat.read(str(p), as_version=4)
        assert len(nb2.cells) == 2
        assert nb2.cells[0].source == "x = 1"
        assert nb2.cells[0].outputs[0]["text"] == "1\n"
        assert nb2.cells[1].source == "y = 2"
        assert nb2.cells[1].outputs == []

    def test_insert_before_existing_cell_does_not_copy_its_outputs(self, tmp_path):
        nb = _make_ipynb([("code", "x = 1", ["1\n"])])
        p = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(p))

        update_ipynb_sources(
            p,
            [Cell(0, "code", "inserted = True"), Cell(1, "code", "x = 1")],
        )

        nb2 = nbformat.read(str(p), as_version=4)
        assert nb2.cells[0].outputs == []
        assert nb2.cells[1].outputs[0]["text"] == "1\n"

    def test_insert_before_edited_cell_preserves_aligned_output(self, tmp_path):
        nb = _make_ipynb([("code", "x = 1", ["x\n"]), ("code", "y = 2", ["y\n"])])
        p = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(p))

        update_ipynb_sources(
            p,
            [
                Cell(0, "code", "inserted = True"),
                Cell(1, "code", "x = 10"),
                Cell(2, "code", "y = 2"),
            ],
        )

        nb2 = nbformat.read(str(p), as_version=4)
        assert nb2.cells[0].outputs == []
        assert nb2.cells[1].outputs[0]["text"] == "x\n"
        assert nb2.cells[2].outputs[0]["text"] == "y\n"

    def test_delete_before_edited_cell_preserves_aligned_output(self, tmp_path):
        nb = _make_ipynb(
            [
                ("code", "a = 1", ["a\n"]),
                ("code", "remove = True", ["remove\n"]),
                ("code", "c = 3", ["c\n"]),
            ]
        )
        p = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(p))

        update_ipynb_sources(
            p,
            [Cell(0, "code", "a = 1"), Cell(1, "code", "c = 30")],
        )

        nb2 = nbformat.read(str(p), as_version=4)
        assert nb2.cells[0].outputs[0]["text"] == "a\n"
        assert nb2.cells[1].outputs[0]["text"] == "c\n"

    def test_duplicate_sources_preserve_distinct_outputs(self, tmp_path):
        nb = _make_ipynb(
            [("code", "value", ["first\n"]), ("code", "value", ["second\n"])]
        )
        p = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(p))

        update_ipynb_sources(p, [Cell(0, "code", "value"), Cell(1, "code", "value")])

        nb2 = nbformat.read(str(p), as_version=4)
        assert nb2.cells[0].outputs[0]["text"] == "first\n"
        assert nb2.cells[1].outputs[0]["text"] == "second\n"

    def test_repeated_cells_use_shifted_unique_anchor(self, tmp_path):
        old_sources = ["anchor_start", *(["repeat"] * 99), "anchor_end"]
        nb = _make_ipynb(
            [
                ("code", source, [f"out-{index}\n"])
                for index, source in enumerate(old_sources)
            ]
        )
        p = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(p))
        new_sources = ["brand_new", "anchor_start", *(["repeat"] * 98), "anchor_end"]

        update_ipynb_sources(
            p,
            [Cell(index, "code", source) for index, source in enumerate(new_sources)],
        )

        nb2 = nbformat.read(str(p), as_version=4)
        assert nb2.cells[0].outputs == []
        assert nb2.cells[1].outputs[0]["text"] == "out-0\n"
        assert nb2.cells[-1].outputs[0]["text"] == "out-100\n"

    def test_clear_edited_preserves_large_repeated_equal_cells_after_insert(
        self, tmp_path
    ):
        nb = _make_ipynb(
            [("code", "repeat", [f"out-{index}\n"]) for index in range(200)]
        )
        p = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(p))
        new_cells = [Cell(0, "code", "inserted")]
        new_cells.extend(Cell(index + 1, "code", "repeat") for index in range(200))

        update_ipynb_sources(p, new_cells, output_policy=OutputPolicy.CLEAR_EDITED)

        nb2 = nbformat.read(str(p), as_version=4)
        assert nb2.cells[0].outputs == []
        assert nb2.cells[1].outputs[0]["text"] == "out-0\n"
        assert nb2.cells[-1].outputs[0]["text"] == "out-199\n"

    def test_large_fallback_handles_two_insertions_before_edits(self, tmp_path):
        nb = _make_ipynb(
            [("code", f"value_{index} = 0", [f"out-{index}\n"]) for index in range(101)]
        )
        p = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(p))
        new_cells = [Cell(0, "code", "first_insert"), Cell(1, "code", "second_insert")]
        new_cells.extend(
            Cell(index + 2, "code", f"value_{index} = 1") for index in range(101)
        )

        update_ipynb_sources(p, new_cells)

        nb2 = nbformat.read(str(p), as_version=4)
        assert nb2.cells[0].outputs == []
        assert nb2.cells[1].outputs == []
        assert nb2.cells[2].outputs[0]["text"] == "out-0\n"
        assert nb2.cells[-1].outputs[0]["text"] == "out-100\n"

    def test_large_fallback_compares_long_single_line_suffixes(self, tmp_path):
        prefix = "x" * 512
        old_sources = [prefix + f"-old-{index:03}" for index in range(51)]
        nb = _make_ipynb(
            [
                ("code", source, [f"out-{index}\n"])
                for index, source in enumerate(old_sources)
            ]
        )
        p = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(p))
        new_cells = [
            Cell(0, "code", prefix + "-insert-a"),
            Cell(1, "code", prefix + "-insert-b"),
        ]
        new_cells.extend(
            Cell(index + 2, "code", source + "-edited")
            for index, source in enumerate(old_sources)
        )

        update_ipynb_sources(p, new_cells)

        nb2 = nbformat.read(str(p), as_version=4)
        assert nb2.cells[0].outputs == []
        assert nb2.cells[1].outputs == []
        assert nb2.cells[2].outputs[0]["text"] == "out-0\n"
        assert nb2.cells[-1].outputs[0]["text"] == "out-50\n"

    def test_alignment_compares_middle_lines_with_shared_boundaries(self, tmp_path):
        boundary = "x" * 512
        nb = _make_ipynb(
            [
                ("code", f"{boundary}\nold_a\n{boundary}", ["a\n"]),
                ("code", f"{boundary}\nold_b\n{boundary}", ["b\n"]),
            ]
        )
        p = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(p))

        update_ipynb_sources(
            p,
            [
                Cell(0, "code", f"{boundary}\nnew_a\n{boundary}"),
                Cell(1, "code", f"{boundary}\ninserted\n{boundary}"),
                Cell(2, "code", f"{boundary}\nnew_b\n{boundary}"),
            ],
        )

        nb2 = nbformat.read(str(p), as_version=4)
        assert nb2.cells[0].outputs[0]["text"] == "a\n"
        assert nb2.cells[1].outputs == []
        assert nb2.cells[2].outputs[0]["text"] == "b\n"

    def test_alignment_resolves_unsampled_middle_line_collision(self, tmp_path):
        def source(marker):
            return "\n".join(
                marker if index == 100 else f"common-{index:03}" for index in range(200)
            )

        nb = _make_ipynb(
            [
                ("code", source("old-A"), ["a\n"]),
                ("code", source("old-B"), ["b\n"]),
            ]
        )
        p = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(p))

        update_ipynb_sources(
            p,
            [
                Cell(0, "code", source("new-A")),
                Cell(1, "code", source("inserted")),
                Cell(2, "code", source("new-B")),
            ],
        )

        nb2 = nbformat.read(str(p), as_version=4)
        assert nb2.cells[0].outputs[0]["text"] == "a\n"
        assert nb2.cells[1].outputs == []
        assert nb2.cells[2].outputs[0]["text"] == "b\n"

    def test_large_fallback_uses_net_shift_beyond_lookahead(self, tmp_path):
        nb = _make_ipynb(
            [("code", f"value_{index} = 0", [f"out-{index}\n"]) for index in range(51)]
        )
        p = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(p))
        new_cells = [
            Cell(index, "code", f"insert_{index} = True") for index in range(9)
        ]
        new_cells.extend(
            Cell(index + 9, "code", f"value_{index} = 1") for index in range(51)
        )

        update_ipynb_sources(p, new_cells)

        nb2 = nbformat.read(str(p), as_version=4)
        assert all(cell.outputs == [] for cell in nb2.cells[:9])
        assert nb2.cells[9].outputs[0]["text"] == "out-0\n"
        assert nb2.cells[-1].outputs[0]["text"] == "out-50\n"

    def test_supports_count_change_delete(self, tmp_path):
        """Deleting a cell (count shrinks) works without error."""
        nb = _make_ipynb(
            [
                ("code", "x = 1", ["1\n"]),
                ("code", "y = 2", ["2\n"]),
            ]
        )
        p = tmp_path / "nb.ipynb"
        nbformat.write(nb, str(p))

        new_cells = [Cell(0, "code", "x = 1")]  # only first cell remains
        update_ipynb_sources(p, new_cells)

        nb2 = nbformat.read(str(p), as_version=4)
        assert len(nb2.cells) == 1
        assert nb2.cells[0].source == "x = 1"
        assert nb2.cells[0].outputs[0]["text"] == "1\n"


# ---------------------------------------------------------------------------
# create_ipynb_from_parsed
# ---------------------------------------------------------------------------


class TestCreateIpynbFromParsed:
    def test_wraps_complete_nbformat_notebook(self):
        notebook = _make_ipynb([("code", "x = 1", ["1\n"])])
        notebook.metadata["custom"] = {"owner": "jcli"}
        notebook.cells[0].metadata["tags"] = ["keep"]
        parsed = parsed_from_ipynb(notebook)

        notebook.cells[0].source = "x = 2"
        emitted = create_ipynb_from_parsed(parsed)

        assert parsed.cells[0].source == "x = 2"
        assert emitted.metadata["custom"] == {"owner": "jcli"}
        assert emitted.cells[0].metadata["tags"] == ["keep"]
        assert emitted.cells[0].outputs[0]["text"] == "1\n"
        assert emitted.cells[0].id == notebook.cells[0].id

    def test_creates_notebook_with_cells(self, tmp_path):
        parsed = _parsed("python3", ("code", "x = 1"), ("markdown", "## Title"))
        nb = create_ipynb_from_parsed(parsed)
        assert len(nb.cells) == 2
        assert nb.cells[0].cell_type == "code"
        assert nb.cells[0].source == "x = 1"
        assert nb.cells[1].cell_type == "markdown"
        assert nb.cells[1].source == "## Title"

    def test_sets_kernelspec_metadata(self):
        parsed = _parsed("ir", ("code", "1 + 1"))
        nb = create_ipynb_from_parsed(parsed)
        assert nb.metadata["kernelspec"]["name"] == "ir"

    def test_no_kernelspec_when_kernel_is_none(self):
        parsed = _parsed(None, ("code", "x = 1"))
        nb = create_ipynb_from_parsed(parsed)
        assert "kernelspec" not in nb.metadata

    def test_preserves_empty_cells(self):
        parsed = _parsed(None, ("code", ""), ("code", "x = 1"))
        nb = create_ipynb_from_parsed(parsed)
        assert [cell.source for cell in nb.cells] == ["", "x = 1"]

    def test_writable_with_nbformat(self, tmp_path):
        parsed = _parsed("python3", ("code", "x = 1"))
        nb = create_ipynb_from_parsed(parsed)
        p = tmp_path / "out.ipynb"
        nbformat.write(nb, str(p))
        nb2 = nbformat.read(str(p), as_version=4)
        assert nb2.cells[0].source == "x = 1"
