"""Py/ipynb pair drift checks and synchronization for agent hooks."""

import sys
from dataclasses import dataclass
from pathlib import Path

from jupyter_jcli.diff import Conflict, DriftOnly, InSync, Merged

from .decision import HookOutcome

_MAX_DIFF_CHARS = 6000
_HOOK_SUMMARY_MAX_CELLS = 16
_HOOK_SUMMARY_MAX_CHARS = 8000
_HOOK_CONTEXT_MAX_CHARS = 16000


@dataclass(frozen=True)
class PostDriftNotice:
    """PostToolUse context plus its non-blocking process outcome."""

    context: str
    outcome: HookOutcome


def _run_pre_drift_check(path: Path, logger=None) -> str | None:
    """Run drift check for PreToolUse and return a deny reason if action is needed."""
    from jupyter_jcli.parser import find_pair

    pair = find_pair(path)
    if pair is None:
        return None

    if path.suffix == ".ipynb":
        py_path, ipynb_path = pair, path
    else:
        py_path, ipynb_path = path, pair

    if not py_path.exists() or not ipynb_path.exists():
        return None

    try:
        from jupyter_jcli.pairing import synchronize_pair

        sync = synchronize_pair(py_path, ipynb_path, strict_baseline=True)
        result = sync.drift
    except UnicodeDecodeError as exc:
        if logger is not None:
            logger.record_exception(exc)
        raise RuntimeError("non-UTF-8 content prevented pair drift checking") from exc
    except Exception as exc:
        if logger is not None:
            logger.record_exception(exc)
        raise RuntimeError(f"pair drift check failed: {exc}") from exc

    if isinstance(result, InSync):
        return None

    if isinstance(result, Conflict):
        scope = _format_conflicts(result)
        return (
            f"Pre-existing conflict between `{py_path.name}` and `{ipynb_path.name}` "
            f"at {scope} — both sides have been edited (e.g. by a human "
            "user in JupyterLab and via py:percent) since the last commit of `.py`, "
            "and the edits collide on the same cell(s). This drift existed before "
            "your tool call.\n\n"
            f"Before resolving, run `git diff -- {py_path.name}` to see what changed "
            f"on the `.py` side, and open `{ipynb_path.name}` (or jupyter-lab) to "
            "inspect the other side. Then pick a direction:\n"
            f"  j-cli convert ipynb-to-py {ipynb_path.name} {py_path.name}"
            "   # takes ipynb's cells; discards .py's edits\n"
            f"  j-cli convert py-to-ipynb {py_path.name} {ipynb_path.name}"
            "   # takes .py's cells; discards ipynb's edits"
            + _diff_section(result.diff_text, py_path.name)
        )

    if isinstance(result, DriftOnly):
        return (
            f"`{py_path.name}` is not yet committed, so jcli has no baseline to "
            f"auto-merge the pair. Current sources of `{py_path.name}` and "
            f"`{ipynb_path.name}` differ. This state existed before your tool call.\n\n"
            "This usually happens right after creating a new notebook (common "
            "`j-cli exec` flow: create `.py`, exec to generate `.ipynb` with outputs; "
            "the two can drift in whitespace/cell count before the first commit).\n\n"
            "Before picking a side:\n"
            f"  1. Run `git log --oneline -- {py_path.name}` to confirm `.py` really "
            "is new (no HEAD).\n"
            "  2. Run `git status` and check who/what wrote each side most recently.\n"
            f"  3. If `{ipynb_path.name}` has exec outputs you want to keep, take "
            f"`{ipynb_path.name}` as truth; otherwise take `{py_path.name}`.\n\n"
            "Then, once you've decided:\n"
            f"  j-cli convert ipynb-to-py {ipynb_path.name} {py_path.name}"
            "   # overwrites .py\n"
            f"  j-cli convert py-to-ipynb {py_path.name} {ipynb_path.name}"
            "   # overwrites .ipynb sources (outputs preserved)"
            + _diff_section(result.diff_text, py_path.name)
        )

    if isinstance(result, Merged):
        return _apply_merge_and_decide(path, py_path, ipynb_path, sync)

    return None


def _apply_merge_and_decide(
    target: Path, py_path: Path, ipynb_path: Path, sync
) -> str | None:
    """Report the actual changes made by the shared synchronization flow."""
    wrote_target = sync.py_changed if target == py_path else sync.ipynb_changed
    if not wrote_target:
        changed_other = sync.ipynb_changed if target == py_path else sync.py_changed
        if changed_other:
            other = ipynb_path if target == py_path else py_path
            print(
                f"pair-drift-guard-pre: auto-synced {other.name} with merged content",
                file=sys.stderr,
            )
        return None
    other = ipynb_path if target == py_path else py_path
    return (
        f"Someone else edited the paired `{other.name}` before your edit — the "
        f"changes have been auto-merged into `{target.name}`. Re-read `{target.name}` "
        "so your next Edit sees the updated content. "
        "(This drift existed before your tool call; you did not cause it.)"
    )


def _post_drift_notice(drift_reason: str) -> PostDriftNotice:
    """Rewrap a drift reason as a visible post-edit diagnostic."""
    context = (
        "Paired notebook drift detected after edit — the other side may "
        "have been modified by a human or another agent.\n\n"
        f"{drift_reason}\n\n"
        "Run `j-cli convert` to reconcile before further edits."
    )
    return PostDriftNotice(context, HookOutcome.failure(context))


def _format_conflicts(result: Conflict) -> str:
    parts = []
    if result.conflict_indices:
        parts.append("cell(s) [" + ", ".join(map(str, result.conflict_indices)) + "]")
    if result.metadata_conflicts:
        parts.append(
            "metadata path(s) ["
            + ", ".join(conflict.display_path for conflict in result.metadata_conflicts)
            + "]"
        )
    return " and ".join(parts) or "unknown shared state"


def _diff_section(diff_text: str, py_name: str = "") -> str:
    """Format diff_text for appending to a hook reason (truncated to _MAX_DIFF_CHARS)."""
    if not diff_text:
        return ""
    if len(diff_text) > _MAX_DIFF_CHARS:
        hint = (
            f"\n... (truncated; run: git diff -- {py_name})"
            if py_name
            else "\n... (truncated)"
        )
        diff_text = diff_text[:_MAX_DIFF_CHARS] + hint
    return "\n\n" + diff_text


def _merge_post_contexts(
    contexts: list[str], max_chars: int = _HOOK_CONTEXT_MAX_CHARS
) -> str:
    separator = "\n\n---\n\n"
    merged = separator.join(contexts)
    if len(merged) <= max_chars:
        return merged

    included: list[str] = []
    for context in contexts:
        candidate = separator.join([*included, context])
        omitted = len(contexts) - len(included) - 1
        suffix = f"\n\n... ({omitted} additional file contexts omitted)"
        if len(candidate) + len(suffix) > max_chars:
            break
        included.append(context)

    omitted = len(contexts) - len(included)
    suffix = f"... ({omitted} additional file contexts omitted)"
    if not included:
        available = max(0, max_chars - len(suffix) - 2)
        return f"{contexts[0][:available]}\n\n{suffix}"[:max_chars]
    return f"{separator.join(included)}\n\n{suffix}"


def _run_post_drift_check(path: Path, logger=None) -> PostDriftNotice | None:
    """Run drift check after an agent edit and return a typed notice if needed."""
    from jupyter_jcli.parser import find_pair

    pair = find_pair(path)
    if pair is None:
        return None

    if path.suffix == ".ipynb":
        py_path, ipynb_path = pair, path
    else:
        py_path, ipynb_path = path, pair

    if not py_path.exists() or not ipynb_path.exists():
        return None

    from jupyter_jcli import pair_baseline

    old_baseline_text = pair_baseline.read_baseline(py_path, strict=True)
    try:
        from jupyter_jcli.pairing import synchronize_pair

        sync = synchronize_pair(
            py_path,
            ipynb_path,
            strict_baseline=True,
            protected_path=path,
        )
        result = sync.drift
    except UnicodeDecodeError as exc:
        if logger is not None:
            logger.record_exception(exc)
        raise RuntimeError("non-UTF-8 content prevented pair drift checking") from exc
    except Exception as exc:
        if logger is not None:
            logger.record_exception(exc)
        raise RuntimeError(f"pair drift check failed: {exc}") from exc

    if isinstance(result, InSync):
        return None

    if isinstance(result, Merged):
        context = _sync_pair_after_edit(
            path,
            py_path,
            ipynb_path,
            sync,
            old_baseline_text,
            logger=logger,
        )
        return PostDriftNotice(context, HookOutcome.success()) if context else None

    if isinstance(result, Conflict):
        scope = _format_conflicts(result)
        other = ipynb_path if path == py_path else py_path
        drift_reason = (
            f"Your edit to `{path.name}` and an independent edit to `{other.name}` "
            f"both changed {scope} — the changes collide and cannot be "
            "auto-merged. (The edit to `"
            + other.name
            + "` may have arrived concurrently or was already present before your edit.)\n\n"
            f"Run `git diff -- {py_path.name}` to see the `.py` side, open "
            f"`{other.name}` to inspect the other side, then pick a direction:\n"
            f"  j-cli convert ipynb-to-py {ipynb_path.name} {py_path.name}"
            "   # take ipynb; discard .py edits on those cells\n"
            f"  j-cli convert py-to-ipynb {py_path.name} {ipynb_path.name}"
            "   # take .py; discard ipynb edits on those cells"
            + _diff_section(result.diff_text, py_path.name)
        )
        return _post_drift_notice(drift_reason)

    if isinstance(result, DriftOnly):
        if path == py_path:
            convert_hint = (
                f"  j-cli convert py-to-ipynb {py_path.name} {ipynb_path.name}"
            )
        else:
            convert_hint = (
                f"  j-cli convert ipynb-to-py {ipynb_path.name} {py_path.name}"
            )
        drift_reason = (
            f"Pair is drifted and `{py_path.name}` has no git baseline, so jcli "
            "can't auto-merge. Since you just edited "
            f"`{path.name}`, if that represents your current intent run:\n"
            f"{convert_hint}\n"
            "Be aware this overwrites the other file's independent content."
            + _diff_section(result.diff_text, py_path.name)
        )
        return _post_drift_notice(drift_reason)

    return None


def _sync_pair_after_edit(
    edited: Path,
    py_path: Path,
    ipynb_path: Path,
    sync,
    old_baseline_text: str | None,
    logger=None,
) -> str | None:
    """Describe the actual write performed by the shared synchronization flow."""
    other_changed = sync.ipynb_changed if edited == py_path else sync.py_changed
    if not other_changed or sync.state is None:
        return None

    summary_text: str | None = None
    if old_baseline_text is not None:
        try:
            from jupyter_jcli.diff import diff_cells
            from jupyter_jcli.formats.percent import loads
            from jupyter_jcli.summ import build_summary_data, format_summary_human

            baseline = loads(old_baseline_text, source_path=str(py_path))
            current = loads(sync.state.canonical_text(), source_path=str(py_path))
            summary_text = format_summary_human(
                build_summary_data(current, diff_cells(baseline, current)),
                max_cells=_HOOK_SUMMARY_MAX_CELLS,
                max_chars=_HOOK_SUMMARY_MAX_CHARS,
            )
        except Exception as exc:  # noqa: BLE001
            if logger is not None:
                logger.record_exception(exc)
    other = ipynb_path if edited == py_path else py_path
    context = (
        f"Auto-synced your edit in `{edited.name}` to `{other.name}`. "
        "Pair is now in sync."
    )
    return f"{context}\n\n{summary_text}" if summary_text is not None else context
