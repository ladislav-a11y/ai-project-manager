from pathlib import Path
from uuid import uuid4
import shutil

import pytest

from ai_project_manager.test_artifact_paths import (
    RepoLocalArtifactRootError,
    default_test_artifact_root,
    ensure_not_repo_local,
    resolve_test_artifact_root,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


def pytest_configure(config):
    """Never let pytest basetemp land inside this checkout.

    A ``.pytest-basetemp-*`` directory in the repo tree is exactly the kind
    of disposable, tool-owned artifact WORKFLOW.md's fail-closed test
    artifact lifecycle forbids - it can pick up read-only git objects
    (created by tests that themselves exercise ``git init``) that a later
    plain ``rm``/``shutil.rmtree`` cannot remove without extra privileges,
    permanently littering the checkout. Basetemp is redirected to a fixed
    external, user-writable root (see ``test_artifact_paths.py``) instead of
    the machine's shared temp dir, since that shared root can itself be
    inaccessible to the process and turn otherwise-passing tests into setup
    errors.

    An explicit ``--basetemp`` is honored only when it does not resolve
    inside this checkout; a repo-local explicit value fails closed with a
    clear usage error instead of silently creating the artifact this hook
    exists to prevent.
    """
    if config.option.basetemp is not None:
        try:
            ensure_not_repo_local(
                Path(config.option.basetemp), _REPO_ROOT, source="--basetemp"
            )
        except RepoLocalArtifactRootError as exc:
            raise pytest.UsageError(str(exc)) from exc
        # An explicit, external --basetemp belongs to the caller and must
        # never be deleted by this suite.
        return

    root = resolve_test_artifact_root(checkout_root=_REPO_ROOT)
    config._ai_pm_test_artifact_root = root
    config.option.basetemp = str(root / f".pytest-basetemp-{uuid4().hex}")
    # Remember ownership: only our own freshly computed default is ever
    # auto-deleted at session finish (see pytest_sessionfinish below).
    config._ai_pm_owned_basetemp = True


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    """Remove only the fresh basetemp created by this completed test run."""
    config = session.config
    if not getattr(config, "_ai_pm_owned_basetemp", False):
        return
    path = Path(config.option.basetemp)
    expected_parent = Path(
        getattr(config, "_ai_pm_test_artifact_root", default_test_artifact_root())
    ).resolve()
    try:
        if (
            path.parent.resolve() == expected_parent
            and path.name.startswith(".pytest-basetemp-")
            and not path.is_symlink()
        ):
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        # Cleanup must never replace the test result with a teardown failure.
        pass


@pytest.fixture(autouse=True)
def _isolate_provider_model_catalog(monkeypatch):
    """Do not let the production launcher leak provider models into tests.

    Individual tests deliberately configure a smaller provider set.  The
    persistent launcher may export a catalog for several providers, and
    ``load_config`` intentionally rejects model entries for providers that are
    not enabled.  Keeping that strict production validation while clearing
    the ambient launcher value makes the suite deterministic when it is run
    by ai-orchestrator as the project's real test command.
    """
    monkeypatch.delenv("AI_PM_PROVIDER_MODELS", raising=False)


@pytest.fixture(autouse=True)
def _isolate_provider_state_path(monkeypatch):
    """Never load the live scheduler's provider-limit cache in tests."""
    monkeypatch.delenv("AI_PM_PROVIDER_STATE_PATH", raising=False)
