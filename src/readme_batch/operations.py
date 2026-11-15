import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum

from readme_batch.execution import RepositoryExecutor
from readme_batch.models import (
    CommitInfo,
    DeleteResult,
    RemoteFile,
    Repository,
    UploadFile,
    UploadResult,
)
from readme_batch.platforms.base import ErrorKind, PlatformError, RepositoryPlatform


class OperationKind(str, Enum):
    SETTINGS = "settings"
    LABELS = "labels"
    UPLOAD = "upload"
    DELETE = "delete"


class OperationState(str, Enum):
    RUNNING = "running"
    COMPLETE = "complete"


class OperationItemState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CONFLICT = "conflict"


class StaleOperationError(Exception):
    pass


@dataclass(frozen=True)
class OperationItem:
    repository: Repository
    state: OperationItemState = OperationItemState.PENDING
    error: str | None = None
    retryable: bool = False
    commit_info: CommitInfo | None = None
    reconciled: bool = False


@dataclass(frozen=True)
class Operation:
    id: str
    kind: OperationKind
    revision: int
    state: OperationState
    items: tuple[OperationItem, ...]
    created_at: datetime


class OperationManager:
    def __init__(
        self,
        adapter: RepositoryPlatform,
        max_workers: int = 3,
        max_attempts: int = 3,
        sleeper: Callable[[float], None] = time.sleep,
        on_complete: Callable[[Operation], object] | None = None,
        max_retained_uploads: int = 4,
    ):
        self._adapter = adapter
        self._executor = RepositoryExecutor(max_workers, max_attempts, sleeper)
        self._on_complete = on_complete
        self._lock = threading.Lock()
        self._operations: dict[str, Operation] = {}
        self._settings: dict[str, dict[str, tuple[str | None, bool | None]]] = {}
        self._labels: dict[str, tuple[str, ...]] = {}
        self._uploads: dict[str, tuple[tuple[UploadFile, ...], bool, str]] = {}
        self._deletions: dict[str, tuple[dict[str, RemoteFile], str]] = {}
        self._max_retained_uploads = max_retained_uploads
        self._revision = 0
        self._accepting = True

    def start_settings(
        self,
        repositories: list[Repository],
        names: dict[str, str | None],
        visibility: str,
    ) -> str:
        private = None if visibility == "keep" else visibility == "private"
        settings = {
            repo.full_name: (
                names[repo.full_name].strip() if names.get(repo.full_name) is not None else None,
                private,
            )
            for repo in repositories
        }
        operation_id = self._create(OperationKind.SETTINGS, repositories)
        self._settings[operation_id] = settings
        self._submit(operation_id, repositories)
        return operation_id

    def start_labels(self, repositories: list[Repository], labels: list[str]) -> str:
        normalized = tuple(dict.fromkeys(label.strip() for label in labels if label.strip()))
        operation_id = self._create(OperationKind.LABELS, repositories)
        self._labels[operation_id] = normalized
        self._submit(operation_id, repositories)
        return operation_id

    def start_upload(
        self,
        repositories: list[Repository],
        files: list[UploadFile] | tuple[UploadFile, ...],
        overwrite: bool,
        message: str,
    ) -> str:
        snapshot = tuple(UploadFile(file.path, bytes(file.content)) for file in files)
        operation_id = self._create(OperationKind.UPLOAD, repositories)
        self._uploads[operation_id] = (snapshot, overwrite, message)
        self._submit(operation_id, repositories)
        return operation_id

    def start_delete(
        self,
        repositories: list[Repository],
        snapshots: dict[str, RemoteFile],
        message: str,
    ) -> str:
        snapshot = dict(snapshots)
        operation_id = self._create(OperationKind.DELETE, repositories)
        self._deletions[operation_id] = (snapshot, message)
        self._submit(operation_id, repositories)
        return operation_id

    def get_operation(self, operation_id: str) -> Operation:
        with self._lock:
            return self._operations[operation_id]

    def start_retry(self, operation_id: str, full_name: str) -> None:
        with self._lock:
            operation = self._require_current(operation_id)
            if operation.state is OperationState.RUNNING:
                raise StaleOperationError("operation is still running")
            item = self._find_item(operation, full_name)
            if item.state is not OperationItemState.FAILED or not item.retryable:
                raise StaleOperationError("this repository cannot be retried")
            self._operations[operation_id] = replace(operation, state=OperationState.RUNNING)
            self._replace_item(
                operation_id,
                full_name,
                state=OperationItemState.PENDING,
                error=None,
                retryable=False,
            )
        self._submit(operation_id, [item.repository], reconcile_before=True)

    def invalidate(self) -> None:
        with self._lock:
            self._revision += 1
            self._accepting = False

    def _create(self, kind: OperationKind, repositories: list[Repository]) -> str:
        operation_id = uuid.uuid4().hex
        with self._lock:
            if not self._accepting:
                raise StaleOperationError("operation manager is stale")
            self._operations[operation_id] = Operation(
                id=operation_id,
                kind=kind,
                revision=self._revision,
                state=OperationState.RUNNING,
                items=tuple(OperationItem(repository=repo) for repo in repositories),
                created_at=datetime.now(timezone.utc),
            )
        return operation_id

    def _submit(
        self,
        operation_id: str,
        repositories: list[Repository],
        reconcile_before: bool = False,
    ) -> None:
        def started(repo: Repository) -> None:
            self._finish_item(
                operation_id,
                repo.full_name,
                state=OperationItemState.RUNNING,
                error=None,
                retryable=False,
                require_pending=True,
            )

        def succeeded(repo: Repository, result: UploadResult | DeleteResult | None) -> None:
            self._finish_item(
                operation_id,
                repo.full_name,
                state=OperationItemState.SUCCESS,
                error=None,
                retryable=False,
                commit_info=result.commit_info if result else None,
                reconciled=result.reconciled if result else False,
            )

        def failed(repo: Repository, error: PlatformError, retryable: bool) -> None:
            state = (
                OperationItemState.CONFLICT
                if error.kind is ErrorKind.CONFLICT
                else OperationItemState.FAILED
            )
            self._finish_item(
                operation_id,
                repo.full_name,
                state=state,
                error=str(error),
                retryable=retryable,
            )

        with self._lock:
            if not self._accepting:
                raise StaleOperationError("operation manager is stale")
            self._executor.submit(
                repositories,
                lambda repo, attempt: self._perform(
                    operation_id, repo, attempt, reconcile_before
                ),
                succeeded,
                failed,
                on_start=started,
            )

    def _perform(
        self,
        operation_id: str,
        repo: Repository,
        attempt: int,
        reconcile_before: bool,
    ) -> UploadResult | DeleteResult | None:
        operation = self.get_operation(operation_id)
        if operation.kind is OperationKind.SETTINGS:
            display_name, private = self._settings[operation_id][repo.full_name]
            self._adapter.update_repository_settings(repo, display_name, private)
        elif operation.kind is OperationKind.LABELS:
            self._adapter.add_repository_topics(repo, self._labels[operation_id])
        elif operation.kind is OperationKind.UPLOAD:
            files, overwrite, message = self._uploads[operation_id]
            return self._adapter.upload_files(
                repo,
                files,
                overwrite,
                message,
                reconcile_before=reconcile_before or attempt > 0,
            )
        else:
            snapshots, message = self._deletions[operation_id]
            return self._adapter.delete_file(
                repo,
                snapshots[repo.full_name],
                message,
                reconcile_before=reconcile_before or attempt > 0,
            )
        return None

    def _finish_item(
        self,
        operation_id: str,
        full_name: str,
        require_pending: bool = False,
        **changes,
    ) -> None:
        completed = None
        with self._lock:
            operation = self._operations[operation_id]
            current = self._find_item(operation, full_name)
            if require_pending and current.state is not OperationItemState.PENDING:
                return
            self._replace_item(operation_id, full_name, **changes)
            operation = self._operations[operation_id]
            if operation.state is OperationState.RUNNING and all(
                item.state in {
                    OperationItemState.SUCCESS,
                    OperationItemState.FAILED,
                    OperationItemState.CONFLICT,
                }
                for item in operation.items
            ):
                completed = replace(operation, state=OperationState.COMPLETE)
                self._operations[operation_id] = completed
                if completed.kind is OperationKind.UPLOAD:
                    if not any(item.retryable for item in completed.items):
                        self._uploads.pop(operation_id, None)
                    self._trim_upload_snapshots()
        if completed is not None and self._on_complete is not None:
            self._on_complete(completed)

    def _trim_upload_snapshots(self) -> None:
        while len(self._uploads) > self._max_retained_uploads:
            oldest = next(
                (
                    operation_id
                    for operation_id in self._uploads
                    if self._operations[operation_id].state is OperationState.COMPLETE
                ),
                None,
            )
            if oldest is None:
                return
            self._uploads.pop(oldest)
            operation = self._operations[oldest]
            self._operations[oldest] = replace(
                operation,
                items=tuple(replace(item, retryable=False) for item in operation.items),
            )

    def _replace_item(self, operation_id: str, full_name: str, **changes) -> None:
        operation = self._operations[operation_id]
        items = list(operation.items)
        index = next(
            index for index, item in enumerate(items) if item.repository.full_name == full_name
        )
        items[index] = replace(items[index], **changes)
        self._operations[operation_id] = replace(operation, items=tuple(items))

    def _require_current(self, operation_id: str) -> Operation:
        operation = self._operations[operation_id]
        if operation.revision != self._revision:
            raise StaleOperationError("operation is stale")
        return operation

    @staticmethod
    def _find_item(operation: Operation, full_name: str) -> OperationItem:
        item = next(
            (item for item in operation.items if item.repository.full_name == full_name),
            None,
        )
        if item is None:
            raise StaleOperationError("repository is not part of this operation")
        return item
