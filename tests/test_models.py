import pytest

from ai_project_manager.models import (
    GitHubRef,
    GoogleDriveRef,
    ProjectRecord,
    ProjectStatus,
)


def test_project_record_holds_all_required_fields():
    project = ProjectRecord(
        name="Demo",
        priority=3,
        status=ProjectStatus.IN_PROGRESS,
        main_task="Build the thing",
        open_feedback=["button is misaligned"],
        next_step="Fix the layout",
        orchestrator_ready_task="Fix layout in Header.tsx",
        last_output="Layout patch applied",
        checkpoint={"step": 2},
        blocked_by=None,
        github_repo=GitHubRef(repo_url="https://github.com/acme/demo"),
        google_drive_ref=GoogleDriveRef(url="https://drive.google.com/folder/xyz"),
    )

    assert project.name == "Demo"
    assert project.priority == 3
    assert project.status == ProjectStatus.IN_PROGRESS
    assert project.main_task == "Build the thing"
    assert project.open_feedback == ["button is misaligned"]
    assert project.next_step == "Fix the layout"
    assert project.orchestrator_ready_task == "Fix layout in Header.tsx"
    assert project.last_output == "Layout patch applied"
    assert project.checkpoint == {"step": 2}
    assert project.blocked_by is None
    assert project.github_repo.repo_url == "https://github.com/acme/demo"
    assert project.google_drive_ref.url == "https://drive.google.com/folder/xyz"
    assert project.is_blocked is False


@pytest.mark.parametrize("priority", [-1, 6, 10])
def test_priority_out_of_range_rejected(priority):
    with pytest.raises(ValueError):
        ProjectRecord(name="Demo", priority=priority)


@pytest.mark.parametrize("priority", [0, 1, 2, 3, 4, 5])
def test_priority_in_range_accepted(priority):
    ProjectRecord(name="Demo", priority=priority)


def test_blocked_by_marks_project_blocked():
    project = ProjectRecord(name="Demo", blocked_by="waiting for API keys")
    assert project.is_blocked is True


def test_status_blocked_marks_project_blocked_even_without_blocked_by():
    project = ProjectRecord(name="Demo", status=ProjectStatus.BLOCKED)
    assert project.is_blocked is True


def test_to_dict_from_dict_round_trip():
    project = ProjectRecord(
        name="Demo",
        priority=5,
        status=ProjectStatus.READY,
        main_task="Ship it",
        open_feedback=["typo on homepage"],
        next_step="Deploy",
        checkpoint={"phase": "final"},
        github_repo=GitHubRef(repo_url="https://github.com/acme/demo", default_branch="main"),
        google_drive_ref=GoogleDriveRef(url="https://drive.google.com/folder/xyz", label="specs"),
        trello_card_id="card-1",
    )

    restored = ProjectRecord.from_dict(project.to_dict())

    assert restored == project


def test_from_dict_accepts_string_status():
    project = ProjectRecord.from_dict({"name": "Demo", "status": "blocked", "priority": 1})
    assert project.status == ProjectStatus.BLOCKED
