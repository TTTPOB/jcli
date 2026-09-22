"""Focused tests for bundled j-cli skill installation."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

import jupyter_jcli.commands.setup.skill as skill_module
from jupyter_jcli.commands.setup.skill import (
    SkillConflictError,
    SkillResourceError,
    install_skill,
    preflight_install_skill,
    preflight_remove_skill,
    remove_skill,
)


@pytest.fixture
def bundled_skill(tmp_path, monkeypatch):
    source = tmp_path / "bundled-j-cli"
    (source / "scripts").mkdir(parents=True)
    (source / "workflows").mkdir()
    (source / "SKILL.md").write_text("---\nname: j-cli\n---\n", encoding="utf-8")
    (source / "scripts" / "search.py").write_text(
        "#!/usr/bin/env python3\nprint('v1')\n", encoding="utf-8"
    )
    (source / "workflows" / "view.md").write_text("view v1\n", encoding="utf-8")
    monkeypatch.setattr(skill_module, "_skill_resource", lambda: source)
    return source


def _marker(target: Path) -> dict[str, object]:
    return json.loads((target / ".j-cli-managed.json").read_text(encoding="utf-8"))


def test_install_is_offline_complete_and_idempotent(tmp_path, bundled_skill):
    target = tmp_path / "agent" / "skills" / "j-cli"

    preflight_install_skill(target)
    assert not target.exists()
    assert install_skill(target) is True

    assert (target / "SKILL.md").read_bytes() == (
        bundled_skill / "SKILL.md"
    ).read_bytes()
    assert (target / "workflows" / "view.md").read_bytes() == (
        bundled_skill / "workflows" / "view.md"
    ).read_bytes()
    assert stat.S_IMODE((target / "scripts" / "search.py").stat().st_mode) == 0o755
    marker = _marker(target)
    assert marker["schema"] == 1
    assert marker["source"] == "jupyter-jcli:j-cli"
    assert set(marker["files"]) == {
        "SKILL.md",
        "scripts/search.py",
        "workflows/view.md",
    }

    before = {
        path.relative_to(target): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }
    preflight_install_skill(target)
    assert install_skill(target) is False
    after = {
        path.relative_to(target): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_install_updates_managed_files_and_preserves_unrelated_files(
    tmp_path, bundled_skill
):
    target = tmp_path / "j-cli"
    assert install_skill(target) is True
    unrelated = target / "notes.txt"
    unrelated.write_text("keep me\n", encoding="utf-8")

    (bundled_skill / "workflows" / "view.md").write_text("view v2\n", encoding="utf-8")
    (bundled_skill / "scripts" / "search.py").unlink()
    (bundled_skill / "workflows" / "edit.md").write_text("edit v1\n", encoding="utf-8")

    preflight_install_skill(target)
    assert (target / "workflows" / "view.md").read_text(encoding="utf-8") == "view v1\n"
    assert install_skill(target) is True

    assert (target / "workflows" / "view.md").read_text(encoding="utf-8") == "view v2\n"
    assert (target / "workflows" / "edit.md").read_text(encoding="utf-8") == "edit v1\n"
    assert not (target / "scripts" / "search.py").exists()
    assert unrelated.read_text(encoding="utf-8") == "keep me\n"
    assert set(_marker(target)["files"]) == {
        "SKILL.md",
        "workflows/edit.md",
        "workflows/view.md",
    }


def test_install_rejects_unmanaged_target_without_modifying_it(tmp_path, bundled_skill):
    target = tmp_path / "j-cli"
    target.mkdir()
    owned = target / "SKILL.md"
    owned.write_text("user owned\n", encoding="utf-8")

    with pytest.raises(SkillConflictError, match="not managed"):
        preflight_install_skill(target)
    with pytest.raises(SkillConflictError, match="not managed"):
        install_skill(target)

    assert owned.read_text(encoding="utf-8") == "user owned\n"
    assert set(target.iterdir()) == {owned}


def test_preflight_rejects_non_directory_existing_ancestor(tmp_path, bundled_skill):
    blocking_file = tmp_path / "agent-home"
    blocking_file.write_text("keep me\n", encoding="utf-8")
    target = blocking_file / "skills" / "j-cli"

    with pytest.raises(SkillConflictError, match="ancestor is not a directory"):
        preflight_install_skill(target)
    with pytest.raises(SkillConflictError, match="ancestor is not a directory"):
        preflight_remove_skill(target)

    assert blocking_file.read_text(encoding="utf-8") == "keep me\n"


def test_update_and_remove_reject_modified_managed_file(tmp_path, bundled_skill):
    target = tmp_path / "j-cli"
    assert install_skill(target) is True
    modified = target / "workflows" / "view.md"
    modified.write_text("user edit\n", encoding="utf-8")

    with pytest.raises(SkillConflictError, match="modified or removed"):
        preflight_install_skill(target)
    with pytest.raises(SkillConflictError, match="modified or removed"):
        preflight_remove_skill(target)
    with pytest.raises(SkillConflictError, match="modified or removed"):
        remove_skill(target)

    assert modified.read_text(encoding="utf-8") == "user edit\n"
    assert (target / ".j-cli-managed.json").exists()


def test_install_and_remove_reject_symlink_targets(tmp_path, bundled_skill):
    actual = tmp_path / "actual"
    actual.mkdir()
    target = tmp_path / "j-cli"
    target.symlink_to(actual, target_is_directory=True)

    with pytest.raises(SkillConflictError, match="symbolic link"):
        install_skill(target)
    with pytest.raises(SkillConflictError, match="symbolic link"):
        remove_skill(target)
    assert not list(actual.iterdir())


def test_remove_preserves_unrelated_files_and_is_idempotent_without_them(
    tmp_path, bundled_skill
):
    target = tmp_path / "j-cli"
    assert install_skill(target) is True
    unrelated = target / "notes.txt"
    unrelated.write_text("keep me\n", encoding="utf-8")

    preflight_remove_skill(target)
    assert (target / "SKILL.md").exists()
    assert remove_skill(target) is True

    assert target.is_dir()
    assert list(target.iterdir()) == [unrelated]
    assert unrelated.read_text(encoding="utf-8") == "keep me\n"

    clean_target = tmp_path / "clean-j-cli"
    assert install_skill(clean_target) is True
    assert remove_skill(clean_target) is True
    assert not clean_target.exists()
    preflight_remove_skill(clean_target)
    assert remove_skill(clean_target) is False


def test_missing_bundled_resource_fails_before_creating_target(tmp_path, monkeypatch):
    missing = tmp_path / "missing"
    monkeypatch.setattr(skill_module, "_skill_resource", lambda: missing)
    target = tmp_path / "j-cli"

    with pytest.raises(SkillResourceError, match="missing or unreadable"):
        install_skill(target)
    assert not target.exists()


@pytest.mark.parametrize("marker_text", ["not json", "{}", '{"schema": 99}'])
def test_invalid_marker_does_not_modify_installation(
    tmp_path, bundled_skill, marker_text
):
    target = tmp_path / "j-cli"
    install_skill(target)
    marker = target / ".j-cli-managed.json"
    marker.write_text(marker_text, encoding="utf-8")
    before = (target / "SKILL.md").read_bytes()

    for operation in (install_skill, remove_skill):
        with pytest.raises(SkillConflictError, match="marker is invalid"):
            operation(target)
        assert (target / "SKILL.md").read_bytes() == before
        assert marker.read_text(encoding="utf-8") == marker_text


def test_update_does_not_overwrite_user_file_added_at_new_resource_path(
    tmp_path, bundled_skill
):
    target = tmp_path / "j-cli"
    install_skill(target)
    user_file = target / "workflows" / "edit.md"
    user_file.write_text("my notes\n", encoding="utf-8")
    (bundled_skill / "workflows" / "edit.md").write_text(
        "new workflow\n", encoding="utf-8"
    )
    before_marker = (target / ".j-cli-managed.json").read_bytes()

    with pytest.raises(SkillConflictError, match="unmanaged entry"):
        install_skill(target)
    assert user_file.read_text(encoding="utf-8") == "my notes\n"
    assert (target / ".j-cli-managed.json").read_bytes() == before_marker


def test_skill_has_one_importable_source_inside_the_python_package():
    root = Path(__file__).parents[1]
    packaged_skill = root / "jupyter_jcli" / "skills" / "j-cli"

    assert (packaged_skill / "SKILL.md").is_file()
    assert (packaged_skill / "scripts" / "rg_ipynb_preprocessor.py").is_file()
    assert not (root / "skills" / "j-cli").exists()

    resource_files = skill_module._load_resource_files()
    assert resource_files["SKILL.md"] == (packaged_skill / "SKILL.md").read_bytes()
    assert "scripts/rg_ipynb_preprocessor.py" in resource_files
