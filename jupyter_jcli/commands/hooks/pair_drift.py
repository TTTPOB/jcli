"""Py/ipynb pair drift checks and synchronization for agent hooks."""

import sys
from dataclasses import dataclass
from pathlib import Path

from jupyter_jcli._enums import DriftStatus

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


def _persist_baseline_for_hook(py_path: Path, canonical_text: str) -> None:
    """Persist a baseline, distinguishing no repository from Git failure."""
    from jupyter_jcli import pair_baseline
    from jupyter_jcli.gitutil import resolve_git_root

    root_result = resolve_git_root(py_path.parent)
    if root_result.error is not None:
        raise RuntimeError(f"baseline Git lookup failed: {root_result.error}")
    if root_result.root is None:
        return
    if not pair_baseline.write_baseline(py_path, canonical_text):
        raise RuntimeError(
            "pair synchronized but baseline persistence failed; baseline was not advanced"
        )


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
        from jupyter_jcli.diff import check_drift

        result = check_drift(py_path, ipynb_path, strict_baseline=True)
    except UnicodeDecodeError as exc:
        if logger is not None:
            logger.record_exception(exc)
        raise RuntimeError("non-UTF-8 content prevented pair drift checking") from exc
    except Exception as exc:
        if logger is not None:
            logger.record_exception(exc)
        raise RuntimeError(f"pair drift check failed: {exc}") from exc

    if result.status == DriftStatus.IN_SYNC:
        return None

    if result.status == DriftStatus.CONFLICT:
        idx_str = ", ".join(str(i) for i in result.conflict_indices)
        return (
            f"Pre-existing conflict between `{py_path.name}` and `{ipynb_path.name}` "
            f"at cell(s) [{idx_str}] — both sides have been edited (e.g. by a human "
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

    if result.status == DriftStatus.DRIFT_ONLY:
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

    if result.status == DriftStatus.MERGED:
        return _apply_merge_and_decide(path, py_path, ipynb_path, result, logger=logger)

    return None


def _prepare_merged_py(py_path: Path, merged_cells, logger=None) -> tuple[str, str]:
    try:
        from jupyter_jcli.formats import percent
        from jupyter_jcli.formats.model import ParsedFile

        py_parsed = percent.load(py_path)
        include_cell_ids = bool(py_parsed.stable_cell_ids) or any(
            cell.cell_id is not None for cell in merged_cells
        )
        merged_parsed = ParsedFile(
            kernel_name=py_parsed.kernel_name,
            cells=merged_cells,
            source_path=py_parsed.source_path,
            front_matter_raw=py_parsed.front_matter_raw,
        )
        merged_text = percent.dumps(
            merged_parsed,
            include_cell_ids=include_cell_ids,
            assign_missing_ids=include_cell_ids,
        )
        return merged_text, percent.canonicalize(
            merged_text,
            include_cell_ids=None if include_cell_ids else False,
        )
    except Exception as exc:
        if logger is not None:
            logger.record_exception(exc)
        raise RuntimeError(f"could not prepare merged py text: {exc}") from exc


def _apply_merge_and_decide(
    target: Path,
    py_path: Path,
    ipynb_path: Path,
    result,  # DriftResult
    logger=None,
) -> str | None:
    """Write merged content and emit allow/deny based on which file changed."""
    from jupyter_jcli.pairing import update_ipynb_sources

    try:
        target_before = target.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"could not read edited source {target}: {exc}") from exc

    wrote_target = False
    merged_py_text: str | None = None
    canonical_merged_py: str | None = None
    failures: list[str] = []

    if result.py_needs_update:
        try:
            if py_path == target and py_path.read_bytes() != target_before:
                raise RuntimeError("edited source changed while the guard was running")
            if merged_py_text is None:
                merged_py_text, canonical_merged_py = _prepare_merged_py(
                    py_path, result.merged_cells, logger
                )
            py_path.write_text(merged_py_text, encoding="utf-8")
            if py_path == target:
                wrote_target = True
            else:
                print(
                    f"pair-drift-guard-pre: auto-synced {py_path.name} with merged content",
                    file=sys.stderr,
                )
        except Exception as exc:  # noqa: BLE001
            if logger is not None:
                logger.record_exception(exc)
            failures.append(f"{py_path.name}: {exc}")

    if result.ipynb_needs_update:
        try:
            if ipynb_path == target and ipynb_path.read_bytes() != target_before:
                raise RuntimeError("edited source changed while the guard was running")
            update_ipynb_sources(ipynb_path, result.merged_cells)
            if ipynb_path == target:
                wrote_target = True
            else:
                print(
                    f"pair-drift-guard-pre: auto-synced {ipynb_path.name} with merged content",
                    file=sys.stderr,
                )
        except Exception as exc:  # noqa: BLE001
            if logger is not None:
                logger.record_exception(exc)
            failures.append(f"{ipynb_path.name}: {exc}")

    if failures:
        raise RuntimeError(
            "source file was not fully synchronized; paired file remains unsynced: "
            + "; ".join(failures)
        )

    if canonical_merged_py is None:
        _, canonical_merged_py = _prepare_merged_py(
            py_path, result.merged_cells, logger
        )

    _persist_baseline_for_hook(py_path, canonical_merged_py)

    if wrote_target:
        other = ipynb_path if target == py_path else py_path
        return (
            f"Someone else edited the paired `{other.name}` before your edit — the "
            f"changes have been auto-merged into `{target.name}`. Re-read `{target.name}` "
            "so your next Edit sees the updated content. "
            "(This drift existed before your tool call; you did not cause it.)"
        )
    return None


def _post_drift_notice(drift_reason: str) -> PostDriftNotice:
    """Rewrap a drift reason as a visible post-edit diagnostic."""
    context = (
        "Paired notebook drift detected after edit — the other side may "
        "have been modified by a human or another agent.\n\n"
        f"{drift_reason}\n\n"
        "Run `j-cli convert` to reconcile before further edits."
    )
    return PostDriftNotice(context, HookOutcome.failure(context))


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

    try:
        from jupyter_jcli.diff import check_drift

        result = check_drift(py_path, ipynb_path, strict_baseline=True)
    except UnicodeDecodeError as exc:
        if logger is not None:
            logger.record_exception(exc)
        raise RuntimeError("non-UTF-8 content prevented pair drift checking") from exc
    except Exception as exc:
        if logger is not None:
            logger.record_exception(exc)
        raise RuntimeError(f"pair drift check failed: {exc}") from exc

    if result.status == DriftStatus.IN_SYNC:
        return None

    if result.status == DriftStatus.MERGED:
        context = _sync_pair_after_edit(
            path, py_path, ipynb_path, result, logger=logger
        )
        return PostDriftNotice(context, HookOutcome.success()) if context else None

    if result.status == DriftStatus.CONFLICT:
        idx_str = ", ".join(str(i) for i in result.conflict_indices)
        other = ipynb_path if path == py_path else py_path
        drift_reason = (
            f"Your edit to `{path.name}` and an independent edit to `{other.name}` "
            f"both changed cell(s) [{idx_str}] — the changes collide and cannot be "
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

    if result.status == DriftStatus.DRIFT_ONLY:
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
    result,  # DriftResult
    logger=None,
) -> str | None:
    """Write the merge result to the OTHER side (not the one the agent just edited)."""
    from jupyter_jcli import pair_baseline
    from jupyter_jcli.pairing import update_ipynb_sources

    # Read before write_baseline advances the sticky pair-sync reference.
    old_baseline_text = pair_baseline.read_baseline(py_path, strict=True)
    both_sides_need_update = result.ipynb_needs_update and result.py_needs_update
    merged_py_text: str | None = None
    canonical_merged_py: str | None = None
    summary_text: str | None = None
    failures: list[str] = []

    if result.ipynb_needs_update:
        if ipynb_path == edited and not both_sides_need_update:
            failures.append(
                f"{ipynb_path.name}: edited source requires a canonical update"
            )
        else:
            try:
                update_ipynb_sources(ipynb_path, result.merged_cells)
            except Exception as exc:  # noqa: BLE001
                if logger is not None:
                    logger.record_exception(exc)
                failures.append(f"{ipynb_path.name}: could not write: {exc}")

    if result.py_needs_update:
        if py_path == edited and not both_sides_need_update:
            failures.append(
                f"{py_path.name}: edited source requires a canonical update"
            )
        else:
            try:
                if merged_py_text is None:
                    merged_py_text, canonical_merged_py = _prepare_merged_py(
                        py_path, result.merged_cells, logger
                    )
                py_path.write_text(merged_py_text, encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                if logger is not None:
                    logger.record_exception(exc)
                failures.append(f"{py_path.name}: could not write: {exc}")

    if failures:
        raise RuntimeError(
            "source file was modified, paired file remains unsynced: "
            + "; ".join(failures)
        )

    if canonical_merged_py is None:
        _, canonical_merged_py = _prepare_merged_py(
            py_path, result.merged_cells, logger
        )

    try:
        _persist_baseline_for_hook(py_path, canonical_merged_py)
    except RuntimeError as exc:
        raise RuntimeError(
            f"source file was modified and pair synchronized, but {exc}"
        ) from exc

    if old_baseline_text is not None:
        try:
            from jupyter_jcli.diff import diff_cells
            from jupyter_jcli.formats.percent import loads
            from jupyter_jcli.summ import build_summary_data, format_summary_human

            baseline = loads(old_baseline_text, source_path=str(py_path))
            current = loads(canonical_merged_py, source_path=str(py_path))
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
