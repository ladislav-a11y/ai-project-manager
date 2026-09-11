from pathlib import Path
from uuid import uuid4
import shutil

import pytest


def pytest_configure(config):
    """Keep pytest temporary files inside this checkout on Windows.

    The machine's shared temporary root can be inaccessible to the process,
    and pytest's numbered-directory cleanup then turns otherwise passing
    tests into setup errors. A fresh directory per invocation avoids stale
    cleanup state while still honoring an explicit ``--basetemp`` supplied by
    the caller.
    """
    if config.option.basetemp is None:
        config.option.basetemp = str(
            Path.cwd() / f".pytest-basetemp-{uuid4().hex}"
        )
        # Remember ownership: an explicitly supplied --basetemp belongs to
        # the caller and must never be deleted by this suite.
        config._ai_pm_owned_basetemp = True


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    """Remove only the fresh basetemp created by this completed test run."""
    config = session.config
    if not getattr(config, "_ai_pm_owned_basetemp", False):
        return
    path = Path(config.option.basetemp)
    expected_parent = Path.cwd().resolve()
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
