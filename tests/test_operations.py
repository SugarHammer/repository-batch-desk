import threading
import time

import pytest

from readme_batch.models import Repository
from readme_batch.operations import (
    OperationItemState,
    OperationManager,
    OperationState,
    StaleOperationError,
)
from readme_batch.platforms.base import ErrorKind, PlatformError


def repository(number: int) -> Repository:
    return Repository(
        "owner",
        f"repo-{number}",
        f"owner/repo-{number}",
        "main",
        f"https://gitee.com/owner/repo-{number}",
        display_name=f"Repo {number}",
    )


class FakeAdapter:
    def __init__(self, settings_errors=None, label_errors=None, gate=None):
        self.settings_errors = settings_errors or {}
        self.label_errors = label_errors or {}
        self.gate = gate
        self.settings_calls = []
        self.label_calls = []

    def update_repository_settings(self, repo, display_name, private):
        self.settings_calls.append((repo.full_name, display_name, private))
        if self.gate:
            self.gate.wait(2)
        errors = self.settings_errors.get(repo.full_name, [])
        if errors:
            raise errors.pop(0)

    def add_project_labels(self, repo, labels):
        self.label_calls.append((repo.full_name, labels))
        if self.gate:
            self.gate.wait(2)
        errors = self.label_errors.get(repo.full_name, [])
        if errors:
            raise errors.pop(0)


def wait_for_state(manager, operation_id, state, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        operation = manager.get_operation(operation_id)
        if operation.state is state:
            return operation
        time.sleep(0.01)
    raise AssertionError(f"operation did not reach {state}: {operation}")


def test_settings_task_uses_immutable_name_and_visibility_snapshot():
    adapter = FakeAdapter()
    manager = OperationManager(adapter, max_workers=1)
    names = {"owner/repo-1": "New name"}

    operation_id = manager.start_settings([repository(1)], names, "private")
    names["owner/repo-1"] = "Changed later"
    operation = wait_for_state(manager, operation_id, OperationState.COMPLETE)

    assert adapter.settings_calls == [("owner/repo-1", "New name", True)]
    assert operation.items[0].state is OperationItemState.SUCCESS


def test_labels_task_trims_and_stably_deduplicates_labels():
    adapter = FakeAdapter()
    manager = OperationManager(adapter, max_workers=1)

    operation_id = manager.start_labels(
        [repository(1)], [" Java ", "Spring Boot", "Java", "", "Spring Boot"]
    )
    wait_for_state(manager, operation_id, OperationState.COMPLETE)

    assert adapter.label_calls == [("owner/repo-1", ("Java", "Spring Boot"))]


def test_partial_failures_have_independent_retryability():
    adapter = FakeAdapter(
        settings_errors={
            "owner/repo-2": [PlatformError(ErrorKind.TEMPORARY, "temporary")],
            "owner/repo-3": [PlatformError(ErrorKind.PERMISSION, "denied")],
        }
    )
    manager = OperationManager(adapter, max_workers=1, max_attempts=1)

    operation_id = manager.start_settings(
        [repository(1), repository(2), repository(3)], {}, "private"
    )
    operation = wait_for_state(manager, operation_id, OperationState.COMPLETE)

    assert [item.state for item in operation.items] == [
        OperationItemState.SUCCESS,
        OperationItemState.FAILED,
        OperationItemState.FAILED,
    ]
    assert [item.retryable for item in operation.items] == [False, True, False]
    assert len(adapter.settings_calls) == 3


def test_manual_retry_only_repeats_one_failed_repository():
    adapter = FakeAdapter(
        settings_errors={"owner/repo-2": [PlatformError(ErrorKind.TEMPORARY, "temporary")]}
    )
    manager = OperationManager(adapter, max_workers=1, max_attempts=1, sleeper=lambda _: None)
    operation_id = manager.start_settings([repository(1), repository(2)], {}, "private")
    wait_for_state(manager, operation_id, OperationState.COMPLETE)

    manager.start_retry(operation_id, "owner/repo-2")
    operation = wait_for_state(manager, operation_id, OperationState.COMPLETE)

    assert all(item.state is OperationItemState.SUCCESS for item in operation.items)
    assert sum(call[0] == "owner/repo-1" for call in adapter.settings_calls) == 1
    assert sum(call[0] == "owner/repo-2" for call in adapter.settings_calls) == 2


def test_retry_rejects_running_unknown_and_non_retryable_operations():
    gate = threading.Event()
    running_adapter = FakeAdapter(gate=gate)
    running_manager = OperationManager(running_adapter, max_workers=1)
    running_id = running_manager.start_labels([repository(1)], ["Java"])
    with pytest.raises(StaleOperationError, match="still running"):
        running_manager.start_retry(running_id, "owner/repo-1")
    with pytest.raises(KeyError):
        running_manager.start_retry("missing", "owner/repo-1")
    gate.set()
    wait_for_state(running_manager, running_id, OperationState.COMPLETE)

    denied_adapter = FakeAdapter(
        label_errors={"owner/repo-1": [PlatformError(ErrorKind.PERMISSION, "denied")]}
    )
    denied_manager = OperationManager(denied_adapter, max_workers=1)
    denied_id = denied_manager.start_labels([repository(1)], ["Java"])
    wait_for_state(denied_manager, denied_id, OperationState.COMPLETE)
    with pytest.raises(StaleOperationError, match="cannot be retried"):
        denied_manager.start_retry(denied_id, "owner/repo-1")


def test_auth_failure_stops_queued_operations():
    adapter = FakeAdapter(
        label_errors={"owner/repo-1": [PlatformError(ErrorKind.AUTH, "unauthorized")]}
    )
    manager = OperationManager(adapter, max_workers=1, max_attempts=1)

    operation_id = manager.start_labels(
        [repository(number) for number in range(1, 6)], ["Java"]
    )
    operation = wait_for_state(manager, operation_id, OperationState.COMPLETE)

    assert adapter.label_calls == [("owner/repo-1", ("Java",))]
    assert all(item.state is OperationItemState.FAILED for item in operation.items)
    assert all(item.error for item in operation.items)
