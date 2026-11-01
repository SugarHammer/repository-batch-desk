import difflib
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

from readme_batch.execution import RepositoryExecutor
from readme_batch.models import CommitInfo, FileSnapshot, Repository
from readme_batch.platforms.base import ErrorKind, PlatformError, RepositoryPlatform
from readme_batch.replace import replace_literal


class BatchState(str, Enum):
    PREVIEWING = "previewing"
    READY = "ready"
    COMMITTING = "committing"
    COMPLETE = "complete"
    STALE = "stale"


class PreviewState(str, Enum):
    READY = "ready"
    NO_MATCH = "no_match"
    UNCHANGED = "unchanged"
    READ_FAILED = "read_failed"


class CommitState(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    CONFLICT = "conflict"


class StalePreviewError(Exception):
    pass


@dataclass(frozen=True)
class BatchItem:
    repository: Repository
    snapshot: FileSnapshot | None = None
    new_content: str | None = None
    match_count: int = 0
    diff: str = ""
    preview_state: PreviewState | None = None
    error: str | None = None
    commit_state: CommitState | None = None
    commit_info: CommitInfo | None = None
    reconciled: bool = False


@dataclass(frozen=True)
class Batch:
    id: str
    revision: int
    state: BatchState
    items: tuple[BatchItem, ...]
    commit_message: str
    created_at: datetime


class BatchManager:
    def __init__(
        self,
        adapter: RepositoryPlatform,
        max_workers: int = 3,
        max_attempts: int = 3,
        sleeper: Callable[[float], None] = time.sleep,
        on_complete: Callable[[Batch], object] | None = None,
    ):
        self._adapter = adapter
        self._on_complete = on_complete
        self._executor = RepositoryExecutor(max_workers, max_attempts, sleeper)
        self._lock = threading.Lock()
        self._batches: dict[str, Batch] = {}
        self._rules: dict[str, tuple[str, str]] = {}
        self._retryable_updates: set[tuple[str, str]] = set()
        self._revision = 0

    def start_preview(
        self,
        repositories: list[Repository],
        old: str,
        new: str,
        commit_message: str,
    ) -> str:
        if not repositories:
            raise ValueError("at least one repository must be selected")
        if not old:
            raise ValueError("old text must not be empty")
        if not commit_message.strip():
            raise ValueError("commit message must not be empty")
        batch_id = uuid.uuid4().hex
        with self._lock:
            batch = Batch(
                id=batch_id,
                revision=self._revision,
                state=BatchState.PREVIEWING,
                items=tuple(BatchItem(repository=repo) for repo in repositories),
                commit_message=commit_message,
                created_at=datetime.now(timezone.utc),
            )
            self._batches[batch_id] = batch
            self._rules[batch_id] = (old, new)
        self._submit_read(batch_id, repositories, old, new)
        return batch_id

    def start_commit(self, batch_id: str) -> None:
        with self._lock:
            batch = self._require_current(batch_id)
            if batch.state is not BatchState.READY:
                raise StalePreviewError("preview is not ready for commit")
            items = [
                item
                for item in batch.items
                if item.preview_state is PreviewState.READY and item.commit_state is None
            ]
            if not items:
                raise StalePreviewError("no pending repository updates")
            self._batches[batch_id] = replace(batch, state=BatchState.COMMITTING)
        self._submit_update(batch_id, [item.repository for item in items])

    def start_retry(self, batch_id: str, full_name: str) -> None:
        with self._lock:
            batch = self._require_current(batch_id)
            if batch.state in {BatchState.PREVIEWING, BatchState.COMMITTING}:
                raise StalePreviewError("batch is still running")
            item = self._find_item(batch, full_name)
            if item.preview_state is PreviewState.READ_FAILED:
                old, new = self._rules[batch_id]
                self._batches[batch_id] = replace(batch, state=BatchState.PREVIEWING)
                self._replace_item(batch_id, full_name, preview_state=None, error=None)
                operation = (self._submit_read, batch_id, [item.repository], old, new)
            elif (
                item.commit_state is CommitState.FAILED
                and (batch_id, full_name) in self._retryable_updates
            ):
                self._batches[batch_id] = replace(batch, state=BatchState.COMMITTING)
                self._replace_item(batch_id, full_name, commit_state=None, error=None)
                operation = (self._submit_update, batch_id, [item.repository])
            else:
                raise StalePreviewError("this repository requires a new preview")
        operation[0](*operation[1:])

    def get_batch(self, batch_id: str) -> Batch:
        with self._lock:
            return self._batches[batch_id]

    def invalidate(self) -> None:
        with self._lock:
            self._revision += 1
            for batch_id, batch in list(self._batches.items()):
                if batch.state is not BatchState.COMMITTING:
                    self._batches[batch_id] = replace(batch, state=BatchState.STALE)

    def _submit_read(self, batch_id: str, repositories: list[Repository], old: str, new: str) -> None:
        def failed(repo: Repository, error: PlatformError, retryable: bool) -> None:
            state = PreviewState.NO_MATCH if error.kind is ErrorKind.NOT_FOUND else PreviewState.READ_FAILED
            self._finish_preview(batch_id, repo.full_name, preview_state=state, error=str(error))

        self._executor.submit(
            repositories,
            lambda repo: self._read_preview(repo, old, new),
            lambda repo, preview: self._finish_preview(batch_id, repo.full_name, **preview),
            failed,
        )

    def _read_preview(self, repo: Repository, old: str, new: str) -> dict:
        snapshot = self._adapter.read_readme(repo)
        result = replace_literal(snapshot.content, old, new)
        diff = "\n".join(
            difflib.unified_diff(
                snapshot.content.splitlines(),
                result.content.splitlines(),
                fromfile=f"{repo.full_name}/README.md (before)",
                tofile=f"{repo.full_name}/README.md (after)",
                lineterm="",
            )
        )
        if result.match_count == 0:
            state = PreviewState.NO_MATCH
        elif result.content == snapshot.content:
            state = PreviewState.UNCHANGED
        else:
            state = PreviewState.READY
        return {
            "snapshot": snapshot,
            "new_content": result.content,
            "match_count": result.match_count,
            "diff": diff,
            "preview_state": state,
            "error": None,
        }

    def _submit_update(self, batch_id: str, repositories: list[Repository]) -> None:
        def succeeded(repo: Repository, result: tuple[CommitInfo | None, bool]) -> None:
            commit, reconciled = result
            self._retryable_updates.discard((batch_id, repo.full_name))
            self._finish_commit(
                batch_id,
                repo.full_name,
                commit_state=CommitState.SUCCESS,
                commit_info=commit,
                reconciled=reconciled,
                error=None,
            )

        def failed(repo: Repository, error: PlatformError, retryable: bool) -> None:
            key = (batch_id, repo.full_name)
            if retryable:
                self._retryable_updates.add(key)
            else:
                self._retryable_updates.discard(key)
            state = CommitState.CONFLICT if error.kind is ErrorKind.CONFLICT else CommitState.FAILED
            self._finish_commit(batch_id, repo.full_name, commit_state=state, error=str(error))

        self._executor.submit(
            repositories,
            lambda repo: self._update_readme(batch_id, repo),
            succeeded,
            failed,
        )

    def _update_readme(self, batch_id: str, repo: Repository) -> tuple[CommitInfo | None, bool]:
        batch = self.get_batch(batch_id)
        item = self._find_item(batch, repo.full_name)
        try:
            commit = self._adapter.update_readme(
                repo, item.snapshot, item.new_content, batch.commit_message
            )
            return commit, False
        except PlatformError as error:
            uncertain = error.kind in {
                ErrorKind.TEMPORARY,
                ErrorKind.RATE_LIMIT,
                ErrorKind.CONFLICT,
            }
            if uncertain and self._remote_matches(repo, item.new_content):
                return None, True
            raise

    def _remote_matches(self, repo: Repository, expected_content: str) -> bool:
        try:
            return self._adapter.read_readme(repo).content == expected_content
        except PlatformError:
            return False

    def _finish_preview(self, batch_id: str, full_name: str, **changes) -> None:
        completed = None
        with self._lock:
            item = self._replace_item(batch_id, full_name, **changes)
            batch = self._batches[batch_id]
            if item.preview_state is PreviewState.READ_FAILED or item.preview_state is not PreviewState.READY:
                self._retryable_updates.discard((batch_id, full_name))
            if batch.state is BatchState.PREVIEWING and all(
                entry.preview_state is not None for entry in batch.items
            ):
                completed = replace(batch, state=BatchState.READY)
                self._batches[batch_id] = completed
        if completed is not None:
            self._notify(completed)

    def _finish_commit(self, batch_id: str, full_name: str, **changes) -> None:
        completed = None
        with self._lock:
            self._replace_item(batch_id, full_name, **changes)
            batch = self._batches[batch_id]
            pending = [
                item
                for item in batch.items
                if item.preview_state is PreviewState.READY and item.commit_state is None
            ]
            if batch.state is BatchState.COMMITTING and not pending:
                completed = replace(batch, state=BatchState.COMPLETE)
                self._batches[batch_id] = completed
        if completed is not None:
            self._notify(completed)

    def _replace_item(self, batch_id: str, full_name: str, **changes) -> BatchItem:
        batch = self._batches[batch_id]
        index = next(i for i, item in enumerate(batch.items) if item.repository.full_name == full_name)
        item = replace(batch.items[index], **changes)
        items = list(batch.items)
        items[index] = item
        self._batches[batch_id] = replace(batch, items=tuple(items))
        return item

    def _require_current(self, batch_id: str) -> Batch:
        batch = self._batches[batch_id]
        if batch.revision != self._revision or batch.state is BatchState.STALE:
            raise StalePreviewError("preview is stale")
        return batch

    @staticmethod
    def _find_item(batch: Batch, full_name: str) -> BatchItem:
        return next(item for item in batch.items if item.repository.full_name == full_name)

    def _notify(self, batch: Batch) -> None:
        if self._on_complete is not None:
            self._on_complete(batch)
