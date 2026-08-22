from ai_project_manager.guard import OrchestratorGuard


def test_first_denial_does_not_halt():
    guard = OrchestratorGuard(max_repeats=2)
    halted = guard.record_denial("Demo", "permission denied: rm -rf")
    assert halted is False
    assert guard.should_halt("Demo") is False


def test_repeated_identical_denials_eventually_halt():
    guard = OrchestratorGuard(max_repeats=2)
    signature = "test command blocked: pytest -k flaky"

    results = [guard.record_denial("Demo", signature) for _ in range(3)]

    assert results == [False, False, True]
    assert guard.should_halt("Demo") is True


def test_different_signatures_do_not_accumulate():
    guard = OrchestratorGuard(max_repeats=2)
    assert guard.record_denial("Demo", "denied: A") is False
    assert guard.record_denial("Demo", "denied: B") is False
    assert guard.record_denial("Demo", "denied: C") is False
    assert guard.should_halt("Demo") is False


def test_reset_clears_the_streak():
    guard = OrchestratorGuard(max_repeats=1)
    guard.record_denial("Demo", "denied: A")
    guard.reset("Demo")

    assert guard.record_denial("Demo", "denied: A") is False


def test_guard_tracks_projects_independently():
    guard = OrchestratorGuard(max_repeats=1)
    guard.record_denial("A", "denied: X")
    guard.record_denial("A", "denied: X")

    assert guard.should_halt("A") is True
    assert guard.should_halt("B") is False
