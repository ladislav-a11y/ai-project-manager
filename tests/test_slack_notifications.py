from types import SimpleNamespace

from ai_project_manager.slack_notifications import (
    SlackLifecycleNotifier,
    build_lifecycle_message,
    emit_lifecycle,
)


def _project():
    return SimpleNamespace(
        name="P3.02 — cw dekoder",
        trello_card_url="https://trello.com/c/example",
        provider="claude",
        status=SimpleNamespace(value="testing"),
    )


def test_lifecycle_messages_contain_card_and_audit_result():
    project = _project()
    assert "začíná práce na kartě" in build_lifecycle_message("work_started", project)
    assert "začíná audit karty" in build_lifecycle_message("audit_started", project)
    message = build_lifecycle_message("audit_finished", project, {"result": "rejected"})
    assert "audit ukončen" in message
    assert "REJECTED" in message
    assert "https://trello.com/c/example" in message


def test_notifier_uses_injected_post_function_without_network():
    sent = []
    notifier = SlackLifecycleNotifier(post_fn=sent.append)
    emit_lifecycle(notifier, "audit_finished", _project(), result="accepted")
    assert len(sent) == 1
    assert "ACCEPTED" in sent[0]


def test_notifier_failure_is_best_effort():
    def failing(*args):
        raise RuntimeError("Slack down")

    # The scheduler-facing wrapper absorbs notifier failures.
    emit_lifecycle(failing, "work_started", _project())
