import threading
import time
from dataclasses import replace

import pytest

from readme_batch.batches import BatchManager, BatchState, CommitState, PreviewState, StalePreviewError
from readme_batch.models import CommitInfo, FileSnapshot, Repository
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
    def __init__(self, contents=None, update_errors=None, read_gate=None):
        self.contents = contents or {}
        self.update_errors = update_errors or {}
        self.read_gate = read_gate
        self.updates = []
        self.reads = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def list_repositories(self):
        return list(self.contents)

    def read_readme(self, repo):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.reads.append(repo.full_name)
        try:
            if self.read_gate:
                self.read_gate.wait(2)
            value = self.contents[repo.full_name]
            if isinstance(value, Exception):
                raise value
            return FileSnapshot("README.md", repo.default_branch, value, f"sha-{repo.full_name}")
        finally:
            with self.lock:
                self.active -= 1

    def update_readme(self, repo, snapshot, new_content, message):
        self.updates.append((repo.full_name, snapshot.sha, new_content, message))
        errors = self.update_errors.get(repo.full_name, [])
        if errors:
            error = errors.pop(0)
            raise error
        return CommitInfo(f"commit-{repo.full_name}", f"https://example/{repo.full_name}")


def wait_for(manager, batch_id, states, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        batch = manager.get_batch(batch_id)
        if batch.state in states:
            return batch
        time.sleep(0.01)
    raise AssertionError(f"batch did not reach {states}: {manager.get_batch(batch_id)}")


def test_preview_generates_diff_and_skips_without_updating():
    adapter = FakeAdapter(
        {
            "owner/repo-1": "before old after",
            "owner/repo-2": "nothing to change",
            "owner/repo-3": "old",
            "owner/repo-4": PlatformError(ErrorKind.NOT_FOUND, "missing"),
            "owner/repo-5": PlatformError(ErrorKind.PERMISSION, "denied"),
        }
    )
    completed = []
    manager = BatchManager(adapter, on_complete=completed.append)

    batch_id = manager.start_preview(
        [repository(n) for n in range(1, 6)], "old", "old", "docs: update"
    )
    batch = wait_for(manager, batch_id, {BatchState.READY})

    assert [item.preview_state for item in batch.items] == [
        PreviewState.UNCHANGED,
        PreviewState.NO_MATCH,
        PreviewState.UNCHANGED,
        PreviewState.NO_MATCH,
        PreviewState.READ_FAILED,
    ]
    assert adapter.updates == []
    assert completed == [batch]
    assert completed[0].items[0].diff == ""


def test_preview_marks_actual_replacement_ready_with_unified_diff():
    adapter = FakeAdapter({"owner/repo-1": "before\nold\nafter"})
    manager = BatchManager(adapter)

    batch_id = manager.start_preview([repository(1)], "old", "new", "docs: update")
    item = wait_for(manager, batch_id, {BatchState.READY}).items[0]

    assert item.preview_state is PreviewState.READY
    assert item.match_count == 1
    assert "-old" in item.diff
    assert "+new" in item.diff


def test_commit_uses_stored_message_only_for_ready_items():
    adapter = FakeAdapter({"owner/repo-1": "old", "owner/repo-2": "none"})
    manager = BatchManager(adapter)
    batch_id = manager.start_preview([repository(1), repository(2)], "old", "new", "docs: batch")
    wait_for(manager, batch_id, {BatchState.READY})

    manager.start_commit(batch_id)
    batch = wait_for(manager, batch_id, {BatchState.COMPLETE})

    assert adapter.updates == [("owner/repo-1", "sha-owner/repo-1", "new", "docs: batch")]
    assert batch.items[0].commit_state is CommitState.SUCCESS
    assert batch.items[0].commit_info.sha == "commit-owner/repo-1"
    with pytest.raises(StalePreviewError):
        manager.start_commit(batch_id)
    assert len(adapter.updates) == 1


def test_invalidation_prevents_commit():
    adapter = FakeAdapter({"owner/repo-1": "old"})
    manager = BatchManager(adapter)
    batch_id = manager.start_preview([repository(1)], "old", "new", "docs: batch")
    wait_for(manager, batch_id, {BatchState.READY})

    manager.invalidate()

    with pytest.raises(StalePreviewError):
        manager.start_commit(batch_id)
    assert adapter.updates == []


def test_conflict_requires_new_preview_instead_of_automatic_retry():
    adapter = FakeAdapter(
        {"owner/repo-1": "old"},
        {"owner/repo-1": [PlatformError(ErrorKind.CONFLICT, "conflict")]},
    )
    manager = BatchManager(adapter)
    batch_id = manager.start_preview([repository(1)], "old", "new", "docs: batch")
    wait_for(manager, batch_id, {BatchState.READY})

    manager.start_commit(batch_id)
    batch = wait_for(manager, batch_id, {BatchState.COMPLETE})

    assert batch.items[0].commit_state is CommitState.CONFLICT
    assert len(adapter.updates) == 1
    with pytest.raises(StalePreviewError):
        manager.start_retry(batch_id, "owner/repo-1")
    assert adapter.reads == ["owner/repo-1"]
    assert len(adapter.updates) == 1


def test_temporary_errors_stop_after_max_attempts_and_backoff():
    adapter = FakeAdapter(
        {"owner/repo-1": "old"},
        {"owner/repo-1": [PlatformError(ErrorKind.TEMPORARY, "temporary")] * 3},
    )
    sleeps = []
    manager = BatchManager(adapter, max_attempts=3, sleeper=sleeps.append)
    batch_id = manager.start_preview([repository(1)], "old", "new", "docs: batch")
    wait_for(manager, batch_id, {BatchState.READY})

    manager.start_commit(batch_id)
    batch = wait_for(manager, batch_id, {BatchState.COMPLETE})

    assert batch.items[0].commit_state is CommitState.FAILED
    assert len(adapter.updates) == 3
    assert sleeps == [0.25, 0.5]


def test_retry_only_repeats_one_failed_update_with_same_sha():
    adapter = FakeAdapter(
        {"owner/repo-1": "old", "owner/repo-2": "old"},
        {"owner/repo-1": [PlatformError(ErrorKind.TEMPORARY, "temporary")] * 2},
    )
    manager = BatchManager(adapter, max_attempts=2, sleeper=lambda _: None)
    batch_id = manager.start_preview([repository(1), repository(2)], "old", "new", "docs: batch")
    wait_for(manager, batch_id, {BatchState.READY})
    manager.start_commit(batch_id)
    wait_for(manager, batch_id, {BatchState.COMPLETE})

    manager.start_retry(batch_id, "owner/repo-1")
    batch = wait_for(manager, batch_id, {BatchState.COMPLETE})

    assert batch.items[0].commit_state is CommitState.SUCCESS
    assert len(adapter.updates) == 4
    assert adapter.updates[-1][0] == "owner/repo-1"
    assert adapter.updates[-1][1] == "sha-owner/repo-1"
    assert sum(update[0] == "owner/repo-2" for update in adapter.updates) == 1


def test_read_failure_retry_uses_stored_replacement_rule():
    adapter = FakeAdapter({"owner/repo-1": PlatformError(ErrorKind.TEMPORARY, "temporary")})
    manager = BatchManager(adapter, max_attempts=1)
    batch_id = manager.start_preview([repository(1)], "old", "new", "docs: batch")
    wait_for(manager, batch_id, {BatchState.READY})
    adapter.contents["owner/repo-1"] = "old"

    manager.start_retry(batch_id, "owner/repo-1")
    batch = wait_for(manager, batch_id, {BatchState.READY})

    assert batch.items[0].preview_state is PreviewState.READY
    assert batch.items[0].new_content == "new"


def test_concurrency_is_limited_and_one_failure_does_not_cancel_others():
    gate = threading.Event()
    contents = {f"owner/repo-{n}": "old" for n in range(1, 8)}
    contents["owner/repo-4"] = RuntimeError("broken")
    adapter = FakeAdapter(contents, read_gate=gate)
    manager = BatchManager(adapter, max_workers=3)

    batch_id = manager.start_preview([repository(n) for n in range(1, 8)], "old", "new", "docs")
    deadline = time.monotonic() + 2
    while adapter.max_active < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    gate.set()
    batch = wait_for(manager, batch_id, {BatchState.READY})

    assert adapter.max_active == 3
    assert len(batch.items) == 7
    assert batch.items[3].preview_state is PreviewState.READ_FAILED
    assert sum(item.preview_state is PreviewState.READY for item in batch.items) == 6
