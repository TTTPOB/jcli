"""Git-backed sticky baselines for py/ipynb pair drift detection."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


_REF_PREFIX = "refs/jcli/pair-sync/"
_SUBJECT_PREFIX = "jcli pair-sync baseline: "


@dataclass(frozen=True)
class RefInfo:
    """Metadata about a stored pair-sync baseline ref."""

    refname: str
    subject: str
    rel_posix_path: str | None


def _git_root(path: Path) -> Path | None:
    cwd = path if path.is_dir() else path.parent
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(cwd),
        )
    except (OSError, FileNotFoundError):
        return None
    if top.returncode != 0:
        return None
    return Path(top.stdout.strip())


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
        "check": False,
    }
    if env is not None:
        kwargs["env"] = env
    if input_text is not None:
        kwargs["text"] = True
        kwargs["input"] = input_text
    elif input_bytes is not None:
        kwargs["input"] = input_bytes
    else:
        kwargs["text"] = True
    return subprocess.run(["git", *args], **kwargs)


def _commit_timestamp(repo_root: Path, ref_name: str) -> int | None:
    try:
        proc = _run_git(repo_root, ["log", "-1", "--format=%ct", ref_name])
    except (OSError, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    stdout = proc.stdout.strip()
    if not stdout:
        return None
    try:
        return int(stdout)
    except ValueError:
        return None


def _head_timestamp(repo_root: Path, rel_posix_path: str) -> int | None:
    try:
        proc = _run_git(
            repo_root,
            ["log", "-1", "--format=%ct", "HEAD", "--", rel_posix_path],
        )
    except (OSError, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    stdout = proc.stdout.strip()
    if not stdout:
        return None
    try:
        return int(stdout)
    except ValueError:
        return None


def _resolve_ref_text(repo_root: Path, ref_name: str) -> str | None:
    try:
        proc = _run_git(repo_root, ["show", f"{ref_name}:file"])
    except (OSError, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    stdout = proc.stdout
    if isinstance(stdout, bytes):
        try:
            return stdout.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return stdout


def _resolve_head_text(repo_root: Path, rel_posix_path: str) -> str | None:
    try:
        proc = _run_git(repo_root, ["show", f"HEAD:{rel_posix_path}"])
    except (OSError, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    stdout = proc.stdout
    if isinstance(stdout, bytes):
        try:
            return stdout.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return stdout


def _delete_ref(repo_root: Path, ref_name: str) -> bool:
    try:
        proc = _run_git(repo_root, ["update-ref", "-d", ref_name])
    except (OSError, FileNotFoundError):
        return False
    return proc.returncode == 0


def read_baseline(py_path: Path) -> str | None:
    """Return the freshest baseline text from sticky ref or HEAD."""
    repo_root = _git_root(py_path)
    if repo_root is None:
        return None

    rel_posix_path = _rel_posix_path(py_path, repo_root)
    if rel_posix_path is None:
        return None

    ref_name = _ref_name(rel_posix_path)
    ref_ts = _commit_timestamp(repo_root, ref_name)
    head_ts = _head_timestamp(repo_root, rel_posix_path)

    if ref_ts is None and head_ts is None:
        return None

    if ref_ts is not None and (head_ts is None or ref_ts >= head_ts):
        ref_text = _resolve_ref_text(repo_root, ref_name)
        if ref_text is not None:
            return ref_text
        if head_ts is None:
            return None

    head_text = _resolve_head_text(repo_root, rel_posix_path)
    if head_text is None:
        return None

    if ref_ts is not None and head_ts is not None and head_ts > ref_ts:
        try:
            _delete_ref(repo_root, ref_name)
        except Exception:
            pass
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
    except Exception as exc:
        print(
            f"pair-drift-guard: could not update baseline ref: {exc}",
            file=sys.stderr,
        )
        return False

    return True


def list_all_refs(repo_root: Path) -> list[RefInfo]:
    """List all jcli pair-sync refs under *repo_root*."""
    try:
        proc = _run_git(
            repo_root,
            ["for-each-ref", _REF_PREFIX, "--format=%(refname)\t%(contents:subject)"],
        )
    except (OSError, FileNotFoundError):
        return []
    if proc.returncode != 0:
        return []

    refs: list[RefInfo] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        refname, _, subject = line.partition("\t")
        rel_posix_path = None
        if subject.startswith(_SUBJECT_PREFIX):
            rel_posix_path = subject[len(_SUBJECT_PREFIX):]
        refs.append(
            RefInfo(
                refname=refname,
                subject=subject,
                rel_posix_path=rel_posix_path or None,
            )
        )
    return refs


def _head_file_exists(repo_root: Path, rel_posix_path: str) -> bool:
    return _resolve_head_text(repo_root, rel_posix_path) is not None


def _classify_ref(repo_root: Path, ref_info: RefInfo) -> tuple[str, str]:
    if not ref_info.rel_posix_path:
        return "orphan", "invalid-subject"

    worktree_path = repo_root / Path(ref_info.rel_posix_path)
    head_exists = _head_file_exists(repo_root, ref_info.rel_posix_path)
    if not worktree_path.exists() and not head_exists:
        return "orphan", "missing-path"

    ref_ts = _commit_timestamp(repo_root, ref_info.refname)
    head_ts = _head_timestamp(repo_root, ref_info.rel_posix_path)
    if head_exists and ref_ts is not None and head_ts is not None and head_ts >= ref_ts:
        return "stale", "head-newer-or-equal"

    return "keep", "active"


def gc_stale_refs(repo_root: Path, dry_run: bool) -> tuple[int, int]:
    """Delete stale or orphaned pair-sync refs under *repo_root*."""
    removed = 0
    kept = 0

    for ref_info in list_all_refs(repo_root):
        status, _reason = _classify_ref(repo_root, ref_info)
        if status == "keep":
            kept += 1
            continue
        if not dry_run:
            _delete_ref(repo_root, ref_info.refname)
        removed += 1

    return removed, kept
