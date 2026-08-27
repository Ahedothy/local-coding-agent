from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.workspace import Workspace, WorkspaceSecurityError


def make_workspace(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path)


def test_relative_path_inside_workspace_is_allowed(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    target = tmp_path / "src" / "main.py"
    target.parent.mkdir()
    target.write_text("print('ok')", encoding="utf-8")

    assert workspace.resolve_path("src/main.py") == target.resolve()


def test_parent_traversal_is_rejected(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)

    with pytest.raises(WorkspaceSecurityError):
        workspace.resolve_path("../outside.txt")


def test_absolute_path_outside_workspace_is_rejected(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    outside = tmp_path.parent / "outside.txt"

    with pytest.raises(WorkspaceSecurityError):
        workspace.resolve_path(outside)


def test_git_metadata_is_rejected(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    (tmp_path / ".git").mkdir()

    with pytest.raises(WorkspaceSecurityError):
        workspace.resolve_path(".git/config")


def test_existing_file_and_directory_validation(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    file_path = tmp_path / "file.txt"
    file_path.write_text("content", encoding="utf-8")

    assert workspace.ensure_readable_file("file.txt") == file_path.resolve()
    assert workspace.resolve_cwd() == tmp_path.resolve()

    with pytest.raises(FileNotFoundError):
        workspace.ensure_readable_file("missing.txt")

    with pytest.raises(WorkspaceSecurityError):
        workspace.resolve_cwd("file.txt")


def test_future_write_target_is_allowed_only_inside_workspace(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)

    assert workspace.ensure_writable_file("new.txt") == (tmp_path / "new.txt").resolve()

    with pytest.raises(WorkspaceSecurityError):
        workspace.ensure_writable_file("../new.txt")


def test_symlink_escape_is_rejected_when_supported(tmp_path: Path) -> None:
    outside_dir = tmp_path.parent / "workspace-outside"
    outside_dir.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks is not permitted on this platform")

    workspace = make_workspace(tmp_path)
    with pytest.raises(WorkspaceSecurityError):
        workspace.resolve_path("linked/secret.txt")


def test_cwd_outside_workspace_is_rejected(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)

    with pytest.raises(WorkspaceSecurityError):
        workspace.resolve_cwd(tmp_path.parent)
