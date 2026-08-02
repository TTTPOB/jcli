"""Encode and restore IPython magics in py:percent Python cells."""

import ast
import io
import re
import tokenize

_COMMENTED_CELL_MAGIC_BODY = "# jupyter-jcli: commented cell magic body"
_MAGIC_PLACEHOLDER = "pass  # jupyter-jcli: IPython magic placeholder"
_HELP_END_RE = re.compile(
    r"(%{0,2}(?!\d)[\w*]+(?:\.(?!\d)[\w*]+|\[-?[0-9]+\])*)(\?\??)$"
)
_PYTHON_BODY_CELL_MAGICS = {
    "%%capture",
    "%%debug",
    "%%file",
    "%%prun",
    "%%pypy",
    "%%python",
    "%%python2",
    "%%python3",
    "%%time",
    "%%timeit",
    "%%writefile",
}


def _tokens_by_logical_line(source: str) -> list[list[tokenize.TokenInfo]]:
    groups: list[list[tokenize.TokenInfo]] = [[]]
    paren_level = 0
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for token in tokens:
            groups[-1].append(token)
            if token.type == tokenize.NEWLINE or (
                token.type == tokenize.NL and paren_level <= 0
            ):
                groups.append([])
            elif token.string in {"(", "[", "{"}:
                paren_level += 1
            elif (
                token.string
                in {
                    ")",
                    "]",
                    "}",
                }
                and paren_level > 0
            ):
                paren_level -= 1
    except (IndentationError, SyntaxError, tokenize.TokenError):
        pass
    return [group for group in groups if group]


def _significant_tokens(tokens: list[tokenize.TokenInfo]) -> list[tokenize.TokenInfo]:
    ignored = {
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.NEWLINE,
        tokenize.NL,
        tokenize.ENDMARKER,
        tokenize.COMMENT,
    }
    return [
        token
        for token in tokens
        if token.type not in ignored and not token.string.isspace()
    ]


def _is_supported_magic(tokens: list[tokenize.TokenInfo], source: str) -> bool:
    # This subset mirrors IPython's token transformers without importing IPython.
    if tokens[0].string in {"%", "!", "?", ",", ";", "/"}:
        return True

    if tokens[-1].string == "?" and _HELP_END_RE.search(source.rstrip()):
        return True

    paren_level = 0
    for index, token in enumerate(tokens):
        if token.string in {"(", "[", "{"}:
            paren_level += 1
        elif token.string in {
            ")",
            "]",
            "}",
        }:
            paren_level = max(0, paren_level - 1)
        elif token.string == "=" and paren_level == 0:
            if index + 1 >= len(tokens):
                return False
            rhs = tokens[index + 1]
            if rhs.string == "!":
                return index > 0
            return (
                rhs.string == "%"
                and index + 2 < len(tokens)
                and tokens[index + 2].type == tokenize.NAME
            )
    return False


def _find_first_magic_range(lines: list[str]) -> tuple[int, int] | None:
    for logical_line in _tokens_by_logical_line("".join(lines)):
        tokens = _significant_tokens(logical_line)
        if not tokens:
            continue

        start = min(token.start[0] for token in logical_line) - 1
        end = max(token.end[0] for token in logical_line) - 1
        if _is_supported_magic(tokens, "".join(lines[start : end + 1])):
            return start, end
    return None


def _magic_ranges(source: str) -> list[tuple[int, int]]:
    shadow = source.splitlines(keepends=True)
    ranges: list[tuple[int, int]] = []
    for _ in range(500):
        found = _find_first_magic_range(shadow)
        if found is None:
            return ranges
        start, end = found
        ranges.append(found)
        indent = shadow[start][: len(shadow[start]) - len(shadow[start].lstrip())]
        shadow[start] = f"{indent}pass\n"
        for index in range(start + 1, end + 1):
            shadow[index] = "\n"
    raise RuntimeError("IPython magic detection did not converge")


def _uncomment_once(line: str) -> str:
    unindented = line.lstrip()
    indent = line[: len(line) - len(unindented)]
    if unindented.startswith("# "):
        return indent + unindented[2:]
    if unindented.startswith("#"):
        return indent + unindented[1:]
    return line


def _fully_uncomment(line: str) -> str:
    previous = line
    while previous.lstrip().startswith("#"):
        current = _uncomment_once(previous)
        if current == previous:
            break
        previous = current
    return previous


def _encoded_magic_ranges(lines: list[str]) -> list[tuple[int, int]]:
    probe = [
        _fully_uncomment(line) if line.lstrip().startswith("#") else line
        for line in lines
    ]
    return [
        (start, end)
        for start, end in _magic_ranges("".join(probe))
        if all(lines[index].lstrip().startswith("#") for index in range(start, end + 1))
    ]


def _uncomment_magic_lines(lines: list[str]) -> list[str]:
    for start, end in _encoded_magic_ranges(lines):
        for index in range(start, end + 1):
            lines[index] = _uncomment_once(lines[index])
    return lines


def _without_final_line_ending(text: str) -> str:
    if text.endswith("\r\n"):
        return text[:-2]
    if text.endswith(("\n", "\r")):
        return text[:-1]
    return text


def transform_ipython_magics(source: str, *, comment: bool) -> str:
    """Comment IPython magics for Python output or restore them on input."""
    lines = source.splitlines(keepends=True)
    if not comment:
        encoded_ranges = _encoded_magic_ranges(lines)
        for index in range(len(lines) - 2, -1, -1):
            if lines[index].strip() == _MAGIC_PLACEHOLDER and any(
                start == index + 1 for start, _ in encoded_ranges
            ):
                lines.pop(index)

    first_content_index = next(
        (index for index, line in enumerate(lines) if line.strip()), None
    )
    first_content = (
        lines[first_content_index] if first_content_index is not None else ""
    )
    if comment:
        cell_magic_match = re.match(r"^\s*(%%{1,2}[A-Za-z]+)", first_content)
    else:
        cell_magic_match = re.match(r"^\s*# (%%{1,2}[A-Za-z]+)", first_content)

    commented_body_marker = (
        not comment
        and first_content_index is not None
        and first_content_index + 1 < len(lines)
        and lines[first_content_index + 1].rstrip() == _COMMENTED_CELL_MAGIC_BODY
    )
    if commented_body_marker:
        marker_index = first_content_index + 1
        marker_was_last_line = marker_index == len(lines) - 1
        lines.pop(marker_index)
        if marker_was_last_line:
            lines[first_content_index] = _without_final_line_ending(
                lines[first_content_index]
            )

    comment_entire_cell = False
    if cell_magic_match:
        if comment:
            body = "".join(lines[first_content_index + 1 :])
            python_body = cell_magic_match.group(1) in _PYTHON_BODY_CELL_MAGICS
            try:
                ast.parse(body)
            except SyntaxError:
                python_body = False
            comment_entire_cell = not python_body
        else:
            comment_entire_cell = commented_body_marker

    if comment_entire_cell:
        if not comment:
            return "".join(_uncomment_once(line) for line in lines)
        target_ranges = [(0, len(lines) - 1)]
    elif comment:
        target_ranges = _magic_ranges("".join(lines)) + _encoded_magic_ranges(lines)
        target_ranges.sort()
    else:
        return "".join(_uncomment_magic_lines(lines))

    placeholder_indices: list[int] = []
    for start, end in target_ranges:
        for index in range(start, end + 1):
            line = lines[index]
            if not line.strip():
                continue
            if index > start:
                lines[index] = f"# {line}"
            else:
                unindented = line.lstrip()
                indent = line[: len(line) - len(unindented)]
                lines[index] = f"{indent}# {unindented}"
        indent = lines[start][: len(lines[start]) - len(lines[start].lstrip())]
        if indent and not comment_entire_cell:
            placeholder_indices.append(start)

    if comment and comment_entire_cell and first_content_index is not None:
        if not lines[first_content_index].endswith(("\n", "\r")):
            lines[first_content_index] += "\n"
        lines.insert(first_content_index + 1, _COMMENTED_CELL_MAGIC_BODY + "\n")
    for index in reversed(placeholder_indices):
        indent = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
        lines.insert(index, f"{indent}{_MAGIC_PLACEHOLDER}\n")

    return "".join(lines)
