"""Git-backed sticky baselines for py/ipynb pair drift detection."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from jupyter_jcli.gitutil import git_root, resolve_git_root

_REF_PREFIX = "refs/jcli/pair-sync/"
_SUBJECT_PREFIX = "jcli pair-sync baseline: "

_MISSING_REF_LOG_RE = re.compile(
    r"^(?:fatal: ambiguous argument '.+': unknown revision or path "
    r"not in the working tree\.|fatal: bad revision '.+')"
)
_MISSING_REF_SHOW_RE = re.compile(r"^fatal: invalid object name '.+'\.$")
_NO_HEAD_RE = re.compile(
    r"^fatal: bad revision 'HEAD'$|^fatal: ambiguous argument 'HEAD':"
)
_MISSING_HEAD_SHOW_RE = re.compile(
    r"^fatal: (?:bad revision 'HEAD'|invalid object name 'HEAD'\.|"
    r"ambiguous argument 'HEAD':|"
    r"path '.+' does not exist in 'HEAD'|"
    r"path '.+' exists on disk, but not in 'HEAD')$"
)


@dataclass(frozen=True)
class RefInfo:
    """Metadata about a stored pair-sync baseline ref."""

    refname: str
    subject: str
    rel_posix_path: str | None


def _git_root(path: Path, *, strict: bool = False) -> Path | None:
    cwd = path if path.is_dir() else path.parent
    if not strict:
        return git_root(cwd)
    result = resolve_git_root(cwd)
    if result.error is not None:
        raise RuntimeError(f"git repository lookup failed: {result.error}")
    return result.root


def _rel_posix_path(py_path: Path, repo_root: Path) -> str | None:
    try:
        return py_path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return None


def _ref_name(rel_posix_path: str) -> str:
    digest = hashlib.sha1(rel_posix_path.encode("utf-8")).hexdigest()
    return f"{_REF_PREFIX}{digest}"


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_COMMITTER_NAME"] = "jcli"
    env["GIT_COMMITTER_EMAIL"] = "jcli@local"
    env["GIT_AUTHOR_NAME"] = "jcli"
    env["GIT_AUTHOR_EMAIL"] = "jcli@local"
    return env


def _run_git(
    repo_root: Path,
    args: list[str],
    *,
    input_text: str | None = None,
    input_bytes: bytes | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    kwargs: dict[str, object] = {
        "cwd": str(repo_root),
        "capture_output": True,
    }
    command_env = os.environ.copy()
    if env is not None:
        command_env.update(env)
    command_env["LC_ALL"] = "C"
    command_env["LANG"] = "C"
    kwargs["env"] = command_env
    if input_text is not None:
        kwargs["text"] = True
        kwargs["input"] = input_text
    elif input_bytes is not None:
        kwargs["input"] = input_bytes
    else:
        kwargs["text"] = True
    return subprocess.run(["git", *args], check=False, **kwargs)


def _stderr_text(proc) -> str:
    stderr = getattr(proc, "stderr", "") or ""
    if isinstance(stderr, bytes):
        return stderr.decode("utf-8", errors="replace").strip()
    return str(stderr).strip()


def _is_expected_missing(proc, pattern: re.Pattern[str]) -> bool:
    """Recognize one documented Git missing-object diagnostic in strict mode."""
    if proc.returncode != 128:
        return False
    detail = _stderr_text(proc)
    return bool(pattern.match(detail))


def _commit_timestamp(
    repo_root: Path, ref_name: str, *, strict: bool = False
) -> int | None:
    try:
        proc = _run_git(repo_root, ["log", "-1", "--format=%ct", ref_name])
    except (OSError, FileNotFoundError) as exc:
        if strict:
            raise RuntimeError(f"git log failed for {ref_name}: {exc}") from exc
        return None
    if proc.returncode != 0:
        if strict and not _is_expected_missing(proc, _MISSING_REF_LOG_RE):
            raise RuntimeError(f"git log failed for {ref_name}: {_stderr_text(proc)}")
        return None
    stdout = proc.stdout.strip()
    if not stdout:
        if strict:
            raise RuntimeError(f"git log returned no timestamp for {ref_name}")
        return None
    try:
        return int(stdout)
    except ValueError as exc:
        if strict:
            raise RuntimeError(
                f"invalid git timestamp for {ref_name}: {stdout}"
            ) from exc
        return None


def _head_timestamp(
    repo_root: Path, rel_posix_path: str, *, strict: bool = False
) -> int | None:
    try:
        proc = _run_git(
            repo_root,
            ["log", "-1", "--format=%ct", "HEAD", "--", rel_posix_path],
        )
    except (OSError, FileNotFoundError) as exc:
        if strict:
            raise RuntimeError(f"git log failed for {rel_posix_path}: {exc}") from exc
        return None
    if proc.returncode != 0:
        if strict and not _is_expected_missing(proc, _NO_HEAD_RE):
            raise RuntimeError(
                f"git log failed for {rel_posix_path}: {_stderr_text(proc)}"
            )
        return None
    stdout = proc.stdout.strip()
    if not stdout:
        # A file with no matching commit is a normal baseline/GC state.
        return None
    try:
        return int(stdout)
    except ValueError as exc:
        if strict:
            raise RuntimeError(
                f"invalid git timestamp for {rel_posix_path}: {stdout}"
            ) from exc
        return None


def _resolve_ref_text(
    repo_root: Path, ref_name: str, *, strict: bool = False
) -> str | None:
    try:
        proc = _run_git(repo_root, ["show", f"{ref_name}:file"])
    except (OSError, FileNotFoundError) as exc:
        if strict:
            raise RuntimeError(f"git show failed for {ref_name}: {exc}") from exc
        return None
    if proc.returncode != 0:
        if strict and not _is_expected_missing(proc, _MISSING_REF_SHOW_RE):
            raise RuntimeError(f"git show failed for {ref_name}: {_stderr_text(proc)}")
        return None
    stdout = proc.stdout
    if isinstance(stdout, bytes):
        try:
            return stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            if strict:
                raise RuntimeError(f"baseline {ref_name} is not UTF-8") from exc
            return None
    return stdout


def _resolve_head_text(
    repo_root: Path, rel_posix_path: str, *, strict: bool = False
) -> str | None:
    try:
        proc = _run_git(repo_root, ["show", f"HEAD:{rel_posix_path}"])
    except (OSError, FileNotFoundError) as exc:
        if strict:
            raise RuntimeError(f"git show failed for {rel_posix_path}: {exc}") from exc
        return None
    if proc.returncode != 0:
        if strict and not _is_expected_missing(proc, _MISSING_HEAD_SHOW_RE):
            raise RuntimeError(
                f"git show failed for {rel_posix_path}: {_stderr_text(proc)}"
            )
        return None
    stdout = proc.stdout
    if isinstance(stdout, bytes):
        try:
            return stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            if strict:
                raise RuntimeError(f"HEAD {rel_posix_path} is not UTF-8") from exc
            return None
    return stdout


def _delete_ref(repo_root: Path, ref_name: str, *, strict: bool = False) -> bool:
    try:
        proc = _run_git(repo_root, ["update-ref", "-d", ref_name])
    except (OSError, FileNotFoundError) as exc:
        if strict:
            raise RuntimeError(f"git update-ref failed for {ref_name}: {exc}") from exc
        return False
    if proc.returncode != 0 and strict:
        raise RuntimeError(
            f"git update-ref failed for {ref_name}: {(proc.stderr or '').strip()}"
        )
    return proc.returncode == 0


def read_baseline(py_path: Path, *, strict: bool = False) -> str | None:
    """Read the freshest baseline text without modifying Git refs."""
    repo_root = _git_root(py_path, strict=strict)
    if repo_root is None:
        return None

    rel_posix_path = _rel_posix_path(py_path, repo_root)
    if rel_posix_path is None:
        return None

    ref_name = _ref_name(rel_posix_path)
    ref_ts = _commit_timestamp(repo_root, ref_name, strict=strict)
    head_ts = _head_timestamp(repo_root, rel_posix_path, strict=strict)

    if ref_ts is None and head_ts is None:
        return None

    if ref_ts is not None and (head_ts is None or ref_ts >= head_ts):
        ref_text = _resolve_ref_text(repo_root, ref_name, strict=strict)
        if ref_text is not None:
            return ref_text
        if head_ts is None:
            return None

    head_text = _resolve_head_text(repo_root, rel_posix_path, strict=strict)
    if head_text is None:
        return None

    return head_text


def write_baseline(py_path: Path, text: str) -> bool:
    """Store canonical py text as a sticky baseline ref for *py_path*."""
    repo_root = _git_root(py_path)
    if repo_root is None:
        return False

    rel_posix_path = _rel_posix_path(py_path, repo_root)
    if rel_posix_path is None:
        return False

    ref_name = _ref_name(rel_posix_path)
    env = _git_env()
    message = f"{_SUBJECT_PREFIX}{rel_posix_path}"

    try:
        blob = _run_git(
            repo_root,
            ["hash-object", "-w", "--stdin"],
            input_bytes=text.encode("utf-8"),
            env=env,
        )
        if blob.returncode != 0:
            raise RuntimeError(blob.stderr.decode("utf-8", errors="replace").strip())

        blob_sha = blob.stdout.decode("utf-8").strip()
        tree = _run_git(
            repo_root,
            ["mktree"],
            input_text=f"100644 blob {blob_sha}\tfile\n",
            env=env,
        )
        if tree.returncode != 0:
            raise RuntimeError(tree.stderr.strip())

        tree_sha = tree.stdout.strip()
        commit = _run_git(
            repo_root,
            ["commit-tree", tree_sha, "-m", message],
            env=env,
        )
        if commit.returncode != 0:
            raise RuntimeError(commit.stderr.strip())

        commit_sha = commit.stdout.strip()
        update = _run_git(
            repo_root,
            ["update-ref", ref_name, commit_sha],
            env=env,
        )
        if update.returncode != 0:
            raise RuntimeError(update.stderr.strip())
    except (OSError, RuntimeError, UnicodeError) as exc:
        print(
            f"pair-drift-guard: could not update baseline ref: {exc}",
            file=sys.stderr,
        )
        return False

    return True


def list_all_refs(repo_root: Path, *, strict: bool = False) -> list[RefInfo]:
    """List all jcli pair-sync refs under *repo_root*."""
    try:
        proc = _run_git(
            repo_root,
            ["for-each-ref", _REF_PREFIX, "--format=%(refname)\t%(contents:subject)"],
        )
    except (OSError, FileNotFoundError) as exc:
        if strict:
            raise RuntimeError(f"git for-each-ref failed: {exc}") from exc
        return []
    if proc.returncode != 0:
        if strict:
            raise RuntimeError(
                f"git for-each-ref failed: {(proc.stderr or '').strip()}"
            )
        return []

    refs: list[RefInfo] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        refname, _, subject = line.partition("\t")
        rel_posix_path = None
        if subject.startswith(_SUBJECT_PREFIX):
            rel_posix_path = subject[len(_SUBJECT_PREFIX) :]
        refs.append(
            RefInfo(
                refname=refname,
                subject=subject,
                rel_posix_path=rel_posix_path or None,
            )
        )
    return refs


def _head_file_exists(
    repo_root: Path, rel_posix_path: str, *, strict: bool = False
) -> bool:
    return _resolve_head_text(repo_root, rel_posix_path, strict=strict) is not None


def _classify_ref(
    repo_root: Path, ref_info: RefInfo, *, strict: bool = False
) -> tuple[str, str]:
    if not ref_info.rel_posix_path:
        return "orphan", "invalid-subject"

    worktree_path = repo_root / Path(ref_info.rel_posix_path)
    head_exists = _head_file_exists(repo_root, ref_info.rel_posix_path, strict=strict)
    if not worktree_path.exists() and not head_exists:
        return "orphan", "missing-path"

    ref_ts = _commit_timestamp(repo_root, ref_info.refname, strict=strict)
    head_ts = _head_timestamp(repo_root, ref_info.rel_posix_path, strict=strict)
    if head_exists and ref_ts is not None and head_ts is not None and head_ts > ref_ts:
        return "stale", "head-newer"

    return "keep", "active"


def gc_stale_refs(
    repo_root: Path, dry_run: bool, *, strict: bool = False
) -> tuple[int, int]:
    """Delete stale or orphaned pair-sync refs under *repo_root*."""
    removed = 0
    kept = 0

    for ref_info in list_all_refs(repo_root, strict=strict):
        status, _reason = _classify_ref(repo_root, ref_info, strict=strict)
        if status == "keep":
            kept += 1
            continue
        if not dry_run:
            deleted = _delete_ref(repo_root, ref_info.refname, strict=strict)
            if strict and not deleted:
                raise RuntimeError(f"git update-ref did not delete {ref_info.refname}")
        removed += 1

    return removed, kept
