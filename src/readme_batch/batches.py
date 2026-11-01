import difflib
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

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
        self._max_attempts = max_attempts
        self._sleeper = sleeper
        self._on_complete = on_complete
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
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
        for repo in repositories:
            self._executor.submit(self._run_read, batch_id, repo, old, new)
        return batch_id

    def start_commit(self, batch_id: str) -> None:
        with self._lock:
            batch = self._require_current(batch_id)
            if batch.state is not BatchState.READY:
                raise StalePreviewError("preview is not ready for commit")
            self._batches[batch_id] = replace(batch, state=BatchState.COMMITTING)
            items = [item for item in batch.items if item.preview_state is PreviewState.READY]
        for item in items:
            self._executor.submit(self._run_update, batch_id, item.repository)

    def start_retry(self, batch_id: str, full_name: str) -> None:
        with self._lock:
            batch = self._require_current(batch_id)
            item = self._find_item(batch, full_name)
            if item.preview_state is PreviewState.READ_FAILED:
                old, new = self._rules[batch_id]
                self._batches[batch_id] = replace(batch, state=BatchState.PREVIEWING)
                self._replace_item(batch_id, full_name, preview_state=None, error=None)
                operation = (self._run_read, batch_id, item.repository, old, new)
            elif (
                item.commit_state is CommitState.FAILED
                and (batch_id, full_name) in self._retryable_updates
            ):
                self._batches[batch_id] = replace(batch, state=BatchState.COMMITTING)
                self._replace_item(batch_id, full_name, commit_state=None, error=None)
                operation = (self._run_update, batch_id, item.repository)
            else:
                raise StalePreviewError("this repository requires a new preview")
        self._executor.submit(*operation)

    def get_batch(self, batch_id: str) -> Batch:
        with self._lock:
            return self._batches[batch_id]

    def invalidate(self) -> None:
        with self._lock:
            self._revision += 1
            for batch_id, batch in list(self._batches.items()):
                if batch.state is not BatchState.COMMITTING:
                    self._batches[batch_id] = replace(batch, state=BatchState.STALE)

    def _run_read(self, batch_id: str, repo: Repository, old: str, new: str) -> None:
        try:
            snapshot = self._read_with_retries(repo)
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
            self._finish_preview(
                batch_id,
                repo.full_name,
                snapshot=snapshot,
                new_content=result.content,
                match_count=result.match_count,
                diff=diff,
                preview_state=state,
                error=None,
            )
        except PlatformError as error:
            state = PreviewState.NO_MATCH if error.kind is ErrorKind.NOT_FOUND else PreviewState.READ_FAILED
            self._finish_preview(batch_id, repo.full_name, preview_state=state, error=str(error))
        except Exception:
            self._finish_preview(
                batch_id,
                repo.full_name,
                preview_state=PreviewState.READ_FAILED,
                error="README could not be processed",
            )

    def _run_update(self, batch_id: str, repo: Repository) -> None:
        batch = self.get_batch(batch_id)
        item = self._find_item(batch, repo.full_name)
        for attempt in range(self._max_attempts):
            try:
                commit = self._adapter.update_readme(
                    repo, item.snapshot, item.new_content, batch.commit_message
                )
                self._retryable_updates.discard((batch_id, repo.full_name))
                self._finish_commit(
                    batch_id,
                    repo.full_name,
                    commit_state=CommitState.SUCCESS,
                    commit_info=commit,
                    error=None,
                )
                return
            except PlatformError as error:
                retryable = error.kind in {ErrorKind.TEMPORARY, ErrorKind.RATE_LIMIT}
                if retryable and attempt + 1 < self._max_attempts:
                    self._sleep_before_retry(attempt, error.retry_after)
                    continue
                state = CommitState.CONFLICT if error.kind is ErrorKind.CONFLICT else CommitState.FAILED
                if retryable:
                    self._retryable_updates.add((batch_id, repo.full_name))
                self._finish_commit(
                    batch_id,
                    repo.full_name,
                    commit_state=state,
                    error=str(error),
                )
                return
            except Exception:
                self._finish_commit(
                    batch_id,
                    repo.full_name,
                    commit_state=CommitState.FAILED,
                    error="README update failed",
                )
                return

    def _read_with_retries(self, repo: Repository) -> FileSnapshot:
        for attempt in range(self._max_attempts):
            try:
                return self._adapter.read_readme(repo)
            except PlatformError as error:
                retryable = error.kind in {ErrorKind.TEMPORARY, ErrorKind.RATE_LIMIT}
                if not retryable or attempt + 1 == self._max_attempts:
                    raise
                self._sleep_before_retry(attempt, error.retry_after)
        raise AssertionError("unreachable")

    def _sleep_before_retry(self, attempt: int, retry_after: float | None) -> None:
        self._sleeper(retry_after if retry_after is not None else 0.25 * (2**attempt))

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
