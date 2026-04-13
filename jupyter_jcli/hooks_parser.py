"""Shell command parser backed by tree-sitter-bash.

Replaces the regex-based command matching in hook guards with a proper
AST walk.  Key improvements over the regex approach:

* Commands inside quoted strings, heredoc bodies, and comments are
  never extracted — no false positives from ``echo "python foo.py"``.
* Leading env-var assignments (``FOO=bar python foo.py``) are captured
  in ``SimpleCommand.assigns`` and stripped from the name/args.
* Runner wrappers (uv, pixi, conda, poetry, env, nohup, …) are peeled
  by ``unwrap_runner`` regardless of how many flag/value pairs precede
  the real command.
* Multi-statement inputs (``a; b``, ``a && b``, ``a | b``) yield one
  ``SimpleCommand`` per statement — ``--execute`` in a later ``echo``
  cannot bleed into an earlier ``nbconvert`` match.

Public API
----------
SimpleCommand          – parsed representation of a simple shell command
iter_simple_commands() – walk AST, return every SimpleCommand in order
unwrap_runner()        – peel runner-wrapper prefixes
WRAPPER_NAMES          – frozenset of known wrapper names

Lazy import
-----------
tree-sitter and tree-sitter-bash are imported inside ``_get_parser()``,
which is ``lru_cache``-decorated and only called when a guard subcommand
actually runs.  Importing this module at the top of another module is
safe — it will not load tree-sitter until ``iter_simple_commands()`` or
``unwrap_runner()`` is first called.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------

@dataclass
class SimpleCommand:
    """A parsed simple shell command."""

    name: str
    """Command name, quote-stripped (e.g. ``python``, ``uv``)."""

    args: tuple[str, ...]
    """Positional arguments after the command name, quote-stripped."""

    assigns: dict[str, str]
    """Leading env-var assignments: ``FOO=bar python`` → ``{"FOO": "bar"}``."""

    raw: str
    """Raw source text of this command node (for diagnostic messages)."""


WRAPPER_NAMES: frozenset[str] = frozenset({
    "uv", "pixi", "poetry", "conda",
    "env", "nohup", "exec", "time", "nice",
})


# ---------------------------------------------------------------------------
# Internal: lazy tree-sitter initialisation
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _get_parser():
    """Return ``(Parser, Language)`` — imported and cached on first call."""
    import tree_sitter_bash as tsbash  # type: ignore[import]
    from tree_sitter import Language, Parser  # type: ignore[import]

    lang = Language(tsbash.language())
    return Parser(lang), lang


# ---------------------------------------------------------------------------
# Internal: AST helpers
# ---------------------------------------------------------------------------

# Node types where descent stops — commands inside these contexts are ignored.
_SKIP_DESCENT: frozenset[str] = frozenset({
    "string",
    "raw_string",
    "ansi_c_string",
    "heredoc_body",
    "comment",
    "command_substitution",
    "process_substitution",
})

# Argument child types that represent I/O redirects, not actual arguments.
_REDIRECT_TYPES: frozenset[str] = frozenset({
    "file_redirect",
    "heredoc_redirect",
    "herestring_redirect",
    "stderr_redirect",
    "stdin_redirect",
    "stdout_redirect",
    "append_redirect",
})


def _extract_text(node, source: bytes) -> str:
    """Return the plain-text content of *node*, stripping one layer of outer quotes."""
    raw = source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
    t = node.type
    if t == "string":        # "..."
        return raw[1:-1] if len(raw) >= 2 else raw
    if t == "raw_string":    # '...'
        return raw[1:-1] if len(raw) >= 2 else raw
    if t == "ansi_c_string": # $'...'
        return raw[2:-1] if len(raw) >= 3 else raw
    return raw


def _command_name_text(node, source: bytes) -> str:
    """Extract the name string from a ``command_name`` AST node."""
    # command_name wraps exactly one _literal child (word, string, …).
    nc = node.named_children
    if nc:
        return _extract_text(nc[0], source)
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _build_simple_command(node, source: bytes) -> SimpleCommand | None:
    """Build a :class:`SimpleCommand` from a ``command`` AST node."""
    raw = source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = _command_name_text(name_node, source)

    assigns: dict[str, str] = {}
    args: list[str] = []

    for child in node.named_children:
        ct = child.type
        if ct == "command_name":
            continue  # already handled via field lookup
        if ct == "variable_assignment":
            name_c = child.child_by_field_name("name")
            val_c = child.child_by_field_name("value")
            if name_c is not None:
                k = source[name_c.start_byte:name_c.end_byte].decode(
                    "utf-8", errors="replace"
                )
                v = _extract_text(val_c, source) if val_c is not None else ""
                assigns[k] = v
        elif ct not in _REDIRECT_TYPES:
            args.append(_extract_text(child, source))

    return SimpleCommand(name=name, args=tuple(args), assigns=assigns, raw=raw)


def _collect(node, source: bytes, results: list[SimpleCommand]) -> None:
    """Depth-first walk; append a :class:`SimpleCommand` for every ``command`` node."""
    t = node.type
    if t in _SKIP_DESCENT:
        return
    if t == "command":
        sc = _build_simple_command(node, source)
        if sc is not None:
            results.append(sc)
        # A command's children are its arguments/redirects, not sub-commands.
        return
    # Use named_children to skip anonymous tokens (';', '|', '&&', etc.).
    for child in node.named_children:
        _collect(child, source, results)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def iter_simple_commands(command_line: str) -> list[SimpleCommand]:
    """Parse *command_line* and return every simple command in execution order.

    Commands inside string literals, heredoc bodies, comments, command
    substitutions, and process substitutions are excluded.
    """
    if not command_line:
        return []
    parser, _lang = _get_parser()
    src = command_line.encode("utf-8")
    tree = parser.parse(src)
    results: list[SimpleCommand] = []
    _collect(tree.root_node, src, results)
    return results


# ---------------------------------------------------------------------------
# Wrapper peeling
# ---------------------------------------------------------------------------

# Flags that consume the *next* token as their value, per wrapper.
_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "uv":    frozenset({"-p", "--python", "--with", "--with-editable",
                        "--from", "--index", "--extra-index-url"}),
    "pixi":  frozenset({"-e", "--environment", "--manifest-path"}),
    "conda": frozenset({"-n", "--name", "-p", "--prefix"}),
    "nice":  frozenset({"-n", "--adjustment"}),
    "env":   frozenset({"-u", "--unset", "-C", "--chdir"}),
}

# Wrappers that require a "run" subcommand verb before the payload.
_RUN_SUBCOMMAND: frozenset[str] = frozenset({"uv", "pixi", "conda", "poetry"})


def unwrap_runner(sc: SimpleCommand) -> SimpleCommand:
    """Peel runner-wrapper prefixes and return the innermost command.

    Handles ``uv run``, ``pixi run``, ``conda run``, ``poetry run``,
    ``env``, ``nohup``, ``exec``, ``time``, ``nice``, plus arbitrary
    leading env-var assignments mixed into the flag list.

    Returns *sc* unchanged when it is not a recognised wrapper or when
    the expected ``run`` subcommand is absent.
    """
    if sc.name not in WRAPPER_NAMES:
        return sc

    args = list(sc.args)
    inherited_assigns = dict(sc.assigns)

    # Consume the "run" subcommand verb for applicable wrappers.
    if sc.name in _RUN_SUBCOMMAND:
        if not args or args[0] != "run":
            return sc  # not the expected form
        args = args[1:]

    # Skip flags (and their values) and env assignments; stop at the command.
    value_flags = _VALUE_FLAGS.get(sc.name, frozenset())
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if a.startswith("-"):
            i += 1
            if a in value_flags and i < len(args):
                i += 1  # skip the flag's value token
            continue
        if "=" in a:  # inline env assignment: FOO=bar
            k, _, v = a.partition("=")
            inherited_assigns[k] = v
            i += 1
            continue
        break  # found the command token

    if i >= len(args):
        return sc  # nothing left after flags

    inner = SimpleCommand(
        name=args[i],
        args=tuple(args[i + 1:]),
        assigns=inherited_assigns,
        raw=sc.raw,
    )
    # Recurse for stacked wrappers (e.g. ``env nohup python foo.py``).
    return unwrap_runner(inner) if inner.name in WRAPPER_NAMES else inner


# ---------------------------------------------------------------------------
# Guard helpers shared by multiple hook commands
# ---------------------------------------------------------------------------

def extract_script_target(sc: SimpleCommand) -> str | None:
    """Return the ``.py`` script path from *sc*, or ``None`` if not applicable.

    Handles:

    * ``python foo.py`` / ``python3 foo.py`` / ``python3.12 foo.py``
      Flags (``-u``, ``-W``, …) are skipped; the first non-flag positional
      arg ending in ``.py`` is the target.
    * ``./foo.py`` — direct shebang execution via the command name itself.
    """
    if re.fullmatch(r"python\d*(?:\.\d+)?", sc.name):
        for a in sc.args:
            if a.startswith("-"):
                continue  # skip interpreter flags
            if a.endswith(".py"):
                return a
            break  # first positional was not a .py path
        return None
    if sc.name.startswith("./") and sc.name.endswith(".py"):
        return sc.name
    return None
