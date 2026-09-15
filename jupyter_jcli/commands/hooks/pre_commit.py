"""Pre-commit orchestration for py/ipynb pair synchronization."""

import fnmatch
import subprocess
import sys

from jupyter_jcli._enums import DriftStatus
from jupyter_jcli.gitutil import resolve_git_root

from .decision import HookOutcome
from .pair_drift import (
    _diff_section,
    _prepare_merged_py,
)


def _run_git_add(repo_root, path) -> None:
    """Stage *path* and propagate git failures to the hook caller."""
    try:
        result = subprocess.run(
            ["git", "add", str(path)],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(repo_root),
        )
    except (OSError, FileNotFoundError) as exc:
        raise RuntimeError(f"git add failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "git add failed").strip()
        raise RuntimeError(f"git add failed for {path}: {detail}")


def _run_pre_commit_pair_sync(include_globs: tuple[str, ...]) -> HookOutcome:
    # ------------------------------------------------------------------
    # Step 1: locate repo root. A directory outside git is a normal noop;
    # an unavailable or failing git executable is an operation failure.
    # ------------------------------------------------------------------
    root_result = resolve_git_root()
    if root_result.error is not None:
        return HookOutcome.failure(f"git repository lookup failed: {root_result.error}")
    repo_root = root_result.root
    if repo_root is None:
        print("pre-commit-pair-sync: not in a git repo, skipping", file=sys.stderr)
        return HookOutcome.success()

    # ------------------------------------------------------------------
    # Step 2: staged files
    # ------------------------------------------------------------------
    try:
        diff = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(repo_root),
        )
    except (OSError, FileNotFoundError) as exc:
        return HookOutcome.failure(f"could not list staged files: {exc}")
    if diff.returncode != 0:
        detail = (diff.stderr or diff.stdout or "git diff failed").strip()
        return HookOutcome.failure(f"could not list staged files: {detail}")
    staged_rel = [p for p in diff.stdout.splitlines() if p.strip()]

    # ------------------------------------------------------------------
    # Step 3: block staged .ipynb
    # ------------------------------------------------------------------
    staged_ipynb = [p for p in staged_rel if p.endswith(".ipynb")]
    if staged_ipynb:
        print(
            "pre-commit-pair-sync: staged .ipynb files found — "
            "unstage them and commit the .py pair instead:",
            file=sys.stderr,
        )
        for p in staged_ipynb:
            print(f"  {p}", file=sys.stderr)
        print("  Hint: git restore --staged <file>.ipynb", file=sys.stderr)
        return HookOutcome.failure("staged .ipynb files require manual unstaging")

    # ------------------------------------------------------------------
    # Step 4: filter staged .py files
    # ------------------------------------------------------------------
    staged_py_rel = [p for p in staged_rel if p.endswith(".py")]
    if include_globs:
        staged_py_rel = [
            p
            for p in staged_py_rel
            if any(fnmatch.fnmatch(p, g) for g in include_globs)
        ]

    # ------------------------------------------------------------------
    # Step 5: process each candidate
    # ------------------------------------------------------------------
    from jupyter_jcli.parser import find_pair

    updated_py: list[str] = []
    updated_ipynb: list[str] = []
    conflicts: list[tuple[str, str, list[int], str]] = []
    drifts: list[tuple[str, str, str]] = []

    for rel_path in staged_py_rel:
        py_path = repo_root / rel_path

        pair = find_pair(py_path)
        if pair is None:
            continue
        ipynb_path = pair

        # Initial sync: .py missing on disk but .ipynb exists
        if not py_path.exists() and ipynb_path.exists():
            try:
                from jupyter_jcli.formats import ipynb, percent

                parsed_nb = ipynb.load(ipynb_path)
                py_text = percent.dumps(parsed_nb)
                py_path.parent.mkdir(parents=True, exist_ok=True)
                py_path.write_text(py_text, encoding="utf-8")
                _run_git_add(repo_root, py_path)
            except Exception as exc:  # noqa: BLE001
                return HookOutcome.failure(
                    f"could not initially sync {py_path.name}: {exc}"
                )
            updated_py.append(rel_path)
            print(
                f"pre-commit-pair-sync: initial sync "
                f"{py_path.name} from {ipynb_path.name}",
                file=sys.stderr,
            )
            continue

        if not py_path.exists() or not ipynb_path.exists():
            continue

        # Drift check (fail-closed for decode/format errors)
        try:
            from jupyter_jcli.diff import check_drift

            result = check_drift(py_path, ipynb_path, strict_baseline=True)
        except Exception as exc:  # noqa: BLE001
            return HookOutcome.failure(
                f"error checking {py_path.name}/{ipynb_path.name}: {exc}"
            )

        if result.status == DriftStatus.IN_SYNC:
            continue

        if result.status == DriftStatus.MERGED:
            if result.py_needs_update:
                try:
                    merged_text, _ = _prepare_merged_py(py_path, result.merged_cells)
                    py_path.write_text(merged_text, encoding="utf-8")
                    _run_git_add(repo_root, py_path)
                except Exception as exc:  # noqa: BLE001
                    return HookOutcome.failure(
                        f"could not synchronize {py_path.name}: {exc}"
                    )
                updated_py.append(rel_path)
            if result.ipynb_needs_update:
                try:
                    from jupyter_jcli.pairing import update_ipynb_sources

                    update_ipynb_sources(ipynb_path, result.merged_cells)
                    try:
                        ipynb_rel = str(ipynb_path.relative_to(repo_root))
                    except ValueError:
                        ipynb_rel = str(ipynb_path)
                    updated_ipynb.append(ipynb_rel)
                except Exception as exc:  # noqa: BLE001
                    return HookOutcome.failure(
                        f"could not synchronize {ipynb_path.name}: {exc}"
                    )
            continue

        if result.status == DriftStatus.CONFLICT:
            try:
                ipynb_rel = str(ipynb_path.relative_to(repo_root))
            except ValueError:
                ipynb_rel = str(ipynb_path)
            conflicts.append(
                (rel_path, ipynb_rel, result.conflict_indices, result.diff_text)
            )
            continue

        if result.status == DriftStatus.DRIFT_ONLY:
            try:
                ipynb_rel = str(ipynb_path.relative_to(repo_root))
            except ValueError:
                ipynb_rel = str(ipynb_path)
            drifts.append((rel_path, ipynb_rel, result.diff_text))

    # ------------------------------------------------------------------
    # Step 6: report and exit
    # ------------------------------------------------------------------
    if conflicts:
        print(
            "pre-commit-pair-sync: merge conflicts — "
            "resolve manually or pick a side via j-cli convert:",
            file=sys.stderr,
        )
        for py_rel, ipynb_rel, indices, diff_text in conflicts:
            idx_str = ", ".join(str(i) for i in indices)
            print(
                f"  {py_rel} ↔ {ipynb_rel}  [conflict cells: {idx_str}]",
                file=sys.stderr,
            )
            if diff_text:
                print(_diff_section(diff_text, py_rel).lstrip("\n"), file=sys.stderr)
        print(
            "  j-cli convert ipynb-to-py <nb.ipynb> <nb.py>  "
            "OR  j-cli convert py-to-ipynb <nb.py> <nb.ipynb>",
            file=sys.stderr,
        )
        return HookOutcome.failure("merge conflicts require manual resolution")

    if drifts:
        print(
            "pre-commit-pair-sync: no git base to auto-merge; "
            "pick a side via j-cli convert:",
            file=sys.stderr,
        )
        for py_rel, ipynb_rel, diff_text in drifts:
            print(f"  {py_rel} ↔ {ipynb_rel}", file=sys.stderr)
            if diff_text:
                print(_diff_section(diff_text, py_rel).lstrip("\n"), file=sys.stderr)
        print(
            "  j-cli convert ipynb-to-py <nb.ipynb> <nb.py>  "
            "OR  j-cli convert py-to-ipynb <nb.py> <nb.ipynb>",
            file=sys.stderr,
        )
        return HookOutcome.failure("unmerged pair drift requires manual resolution")

    if updated_py:
        print(
            f"pre-commit-pair-sync: auto-synced .py: {', '.join(updated_py)}",
            file=sys.stderr,
        )
    if updated_ipynb:
        print(
            f"pre-commit-pair-sync: auto-synced .ipynb (not staged): "
            f"{', '.join(updated_ipynb)}",
            file=sys.stderr,
        )
    return HookOutcome.success()
