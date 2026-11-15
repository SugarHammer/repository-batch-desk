import threading
import time

import pytest

from readme_batch.deletions import (
    DeletionItemState,
    DeletionManager,
    DeletionState,
    StaleDeletionError,
)
from readme_batch.models import RemoteFile, Repository
from readme_batch.platforms.base import ErrorKind, PlatformError


def repository(number: int) -> Repository:
    return Repository(
        "owner",
        f"repo-{number}",
        f"owner/repo-{number}",
        "main",
        f"https://gitee.com/owner/repo-{number}",
    )


class FakeAdapter:
    def __init__(self, outcomes=None, gate=None):
        self.outcomes = outcomes or {}
        self.gate = gate
        self.read_calls = []

    def read_file(self, repo, path):
        self.read_calls.append((repo.full_name, path))
        if self.gate:
            self.gate.wait(2)
        outcome = self.outcomes.get(repo.full_name, RemoteFile(path, "main", f"sha-{repo.name}"))
        if isinstance(outcome, list):
            outcome = outcome.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def wait_for_state(manager, operation_id, state, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        operation = manager.get_preview(operation_id)
        if operation.state is state:
            return operation
        time.sleep(0.01)
    raise AssertionError(f"deletion preview did not reach {state}: {operation}")


def test_preview_distinguishes_ready_missing_conflict_and_read_failure():
    adapter = FakeAdapter(
        outcomes={
            "owner/repo-2": PlatformError(ErrorKind.NOT_FOUND, "missing"),
            "owner/repo-3": PlatformError(ErrorKind.CONFLICT, "directory"),
            "owner/repo-4": PlatformError(ErrorKind.TEMPORARY, "temporary"),
        }
    )
    manager = DeletionManager(adapter, max_workers=1, max_attempts=1)

    preview_id = manager.start_preview(
        [repository(number) for number in range(1, 5)], "images/abc.png", "chore: delete"
    )
    preview = wait_for_state(manager, preview_id, DeletionState.READY)

    assert [item.state for item in preview.items] == [
        DeletionItemState.READY,
        DeletionItemState.MISSING,
        DeletionItemState.CONFLICT,
        DeletionItemState.FAILED,
    ]
    assert preview.items[0].snapshot == RemoteFile("images/abc.png", "main", "sha-repo-1")
    assert preview.items[3].retryable is True
    assert all(call[1] == "images/abc.png" for call in adapter.read_calls)


def test_confirm_uses_only_ready_snapshots_and_cannot_be_submitted_twice():
    adapter = FakeAdapter(
        outcomes={"owner/repo-2": PlatformError(ErrorKind.NOT_FOUND, "missing")}
    )
    manager = DeletionManager(adapter, max_workers=1)

    preview_id = manager.start_preview(
        [repository(1), repository(2)], "abc.png", "chore: original"
    )
    wait_for_state(manager, preview_id, DeletionState.READY)

    repositories, snapshots, message = manager.start_delete(preview_id)

    assert [repo.full_name for repo in repositories] == ["owner/repo-1"]
    assert snapshots == {"owner/repo-1": RemoteFile("abc.png", "main", "sha-repo-1")}
    assert message == "chore: original"
    with pytest.raises(StaleDeletionError):
        manager.start_delete(preview_id)


def test_invalidate_makes_preview_stale_without_deleting():
    adapter = FakeAdapter()
    manager = DeletionManager(adapter, max_workers=1)
    preview_id = manager.start_preview([repository(1)], "abc.png", "chore: delete")
    wait_for_state(manager, preview_id, DeletionState.READY)

    manager.invalidate()

    with pytest.raises(StaleDeletionError):
        manager.start_delete(preview_id)


def test_retry_repeats_only_retryable_read_failure():
    adapter = FakeAdapter(
        outcomes={
            "owner/repo-2": [
                PlatformError(ErrorKind.TEMPORARY, "temporary"),
                RemoteFile("abc.png", "main", "sha-repo-2"),
            ]
        }
    )
    manager = DeletionManager(adapter, max_workers=1, max_attempts=1, sleeper=lambda _: None)
    preview_id = manager.start_preview(
        [repository(1), repository(2)], "abc.png", "chore: delete"
    )
    wait_for_state(manager, preview_id, DeletionState.READY)

    manager.start_retry(preview_id, "owner/repo-2")
    preview = wait_for_state(manager, preview_id, DeletionState.READY)

    assert all(item.state is DeletionItemState.READY for item in preview.items)
    assert [call[0] for call in adapter.read_calls].count("owner/repo-1") == 1
    assert [call[0] for call in adapter.read_calls].count("owner/repo-2") == 2


def test_retry_rejects_running_missing_and_non_retryable_previews():
    gate = threading.Event()
    running_manager = DeletionManager(FakeAdapter(gate=gate), max_workers=1)
    running_id = running_manager.start_preview([repository(1)], "abc.png", "chore: delete")
    with pytest.raises(StaleDeletionError, match="still running"):
        running_manager.start_retry(running_id, "owner/repo-1")
    with pytest.raises(KeyError):
        running_manager.start_retry("missing", "owner/repo-1")
    gate.set()
    wait_for_state(running_manager, running_id, DeletionState.READY)

    denied_manager = DeletionManager(
        FakeAdapter(
            outcomes={"owner/repo-1": PlatformError(ErrorKind.PERMISSION, "denied")}
        ),
        max_workers=1,
    )
    denied_id = denied_manager.start_preview([repository(1)], "abc.png", "chore: delete")
    wait_for_state(denied_manager, denied_id, DeletionState.READY)
    with pytest.raises(StaleDeletionError, match="cannot be retried"):
        denied_manager.start_retry(denied_id, "owner/repo-1")
