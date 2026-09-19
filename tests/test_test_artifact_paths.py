from pathlib import Path

import pytest

from ai_project_manager.test_artifact_paths import (
    RepoLocalArtifactRootError,
    default_test_artifact_root,
    ensure_not_repo_local,
    is_inside_checkout,
    resolve_test_artifact_root,
)


def test_default_root_uses_local_app_data_on_windows():
    """Unset AI_PM_TEST_ARTIFACT_ROOT resolves to the fixed per-user
    external root under %LOCALAPPDATA%, never a machine-shared or
    repo-relative location."""
    env = {"LOCALAPPDATA": r"C:\Users\demo\AppData\Local"}

    root = default_test_artifact_root(env)

    assert root == Path(r"C:\Users\demo\AppData\Local\AIProjectManager\pytest")


def test_default_root_honors_explicit_override():
    env = {
        "LOCALAPPDATA": r"C:\Users\demo\AppData\Local",
        "AI_PM_TEST_ARTIFACT_ROOT": r"D:\somewhere\else",
    }

    root = default_test_artifact_root(env)

    assert root == Path(r"D:\somewhere\else")


def test_default_root_falls_back_without_local_app_data():
    """A non-Windows/unset-LOCALAPPDATA environment still resolves to a
    stable external root, never the current/checkout directory."""
    root = default_test_artifact_root({})

    assert root == Path.home() / ".cache" / "ai-project-manager" / "pytest"


def test_resolve_root_falls_back_from_unwritable_local_app_data(tmp_path):
    blocked_parent = tmp_path / "blocked"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    env = {
        "LOCALAPPDATA": str(blocked_parent),
        "TEMP": str(tmp_path / "writable-temp"),
    }
    resolved = resolve_test_artifact_root(env, checkout_root=tmp_path / "checkout")

    assert resolved == Path(env["TEMP"]) / "AIProjectManager" / "pytest"


def test_is_inside_checkout_detects_repo_local_paths(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    inside = checkout / ".pytest-basetemp-abc"
    outside = tmp_path / "elsewhere" / ".pytest-basetemp-abc"

    assert is_inside_checkout(inside, checkout) is True
    assert is_inside_checkout(checkout, checkout) is True
    assert is_inside_checkout(outside, checkout) is False


def test_ensure_not_repo_local_rejects_a_repo_local_basetemp(tmp_path):
    """The explicit failure path a caller (conftest.py) turns into a hard
    pytest usage error - a repo-local --basetemp must never silently
    proceed and create a .pytest-basetemp-* artifact inside the checkout."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    repo_local = checkout / ".pytest-basetemp-xyz"

    with pytest.raises(RepoLocalArtifactRootError) as excinfo:
        ensure_not_repo_local(repo_local, checkout, source="--basetemp")

    assert "--basetemp" in str(excinfo.value)
    assert str(repo_local) in str(excinfo.value)


def test_ensure_not_repo_local_accepts_an_external_path(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    external = tmp_path / "external" / ".pytest-basetemp-xyz"

    assert ensure_not_repo_local(external, checkout, source="--basetemp") == external


def test_repo_checkout_has_no_pytest_basetemp_artifacts_during_this_run():
    """conftest.py redirects basetemp entirely outside the checkout, so no
    .pytest-basetemp-* directory should ever exist in the repo root while
    this very test suite is running (see conftest.pytest_configure)."""
    repo_root = Path(__file__).resolve().parent.parent

    leftovers = [
        path for path in repo_root.iterdir()
        if path.is_dir() and path.name.startswith(".pytest-basetemp-")
    ]

    assert leftovers == []
