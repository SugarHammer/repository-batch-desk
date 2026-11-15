import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum

from readme_batch.execution import RepositoryExecutor
from readme_batch.models import RemoteFile, Repository
from readme_batch.platforms.base import ErrorKind, PlatformError, RepositoryPlatform


class DeletionState(str, Enum):
    PREVIEWING = "previewing"
    READY = "ready"
    SUBMITTED = "submitted"
    STALE = "stale"


class DeletionItemState(str, Enum):
    READY = "ready"
    MISSING = "missing"
    FAILED = "failed"
    CONFLICT = "conflict"


class StaleDeletionError(Exception):
    pass


@dataclass(frozen=True)
class DeletionItem:
    repository: Repository
    state: DeletionItemState | None = None
    snapshot: RemoteFile | None = None
    error: str | None = None
    retryable: bool = False


@dataclass(frozen=True)
class DeletionPreview:
    id: str
    revision: int
    state: DeletionState
    items: tuple[DeletionItem, ...]
    path: str
    message: str
    created_at: datetime


class DeletionManager:
    def __init__(
        self,
        adapter: RepositoryPlatform,
        max_workers: int = 3,
        max_attempts: int = 3,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self._adapter = adapter
        self._executor = RepositoryExecutor(max_workers, max_attempts, sleeper)
        self._lock = threading.Lock()
        self._previews: dict[str, DeletionPreview] = {}
        self._revision = 0

    def start_preview(
        self, repositories: list[Repository], path: str, message: str
    ) -> str:
        preview_id = uuid.uuid4().hex
        with self._lock:
            self._previews[preview_id] = DeletionPreview(
                id=preview_id,
                revision=self._revision,
                state=DeletionState.PREVIEWING,
                items=tuple(DeletionItem(repository=repo) for repo in repositories),
                path=path,
                message=message,
                created_at=datetime.now(timezone.utc),
            )
        self._submit(preview_id, repositories)
        return preview_id

    def get_preview(self, preview_id: str) -> DeletionPreview:
        with self._lock:
            return self._previews[preview_id]

    def start_delete(
        self, preview_id: str
    ) -> tuple[list[Repository], dict[str, RemoteFile], str]:
        with self._lock:
            preview = self._require_current(preview_id)
            if preview.state is not DeletionState.READY:
                raise StaleDeletionError("deletion preview is not ready")
            items = [item for item in preview.items if item.state is DeletionItemState.READY]
            if not items:
                raise StaleDeletionError("no files are ready for deletion")
            self._previews[preview_id] = replace(preview, state=DeletionState.SUBMITTED)
            return (
                [item.repository for item in items],
                {item.repository.full_name: item.snapshot for item in items},
                preview.message,
            )

    def start_retry(self, preview_id: str, full_name: str) -> None:
        with self._lock:
            preview = self._require_current(preview_id)
            if preview.state is DeletionState.PREVIEWING:
                raise StaleDeletionError("deletion preview is still running")
            if preview.state is not DeletionState.READY:
                raise StaleDeletionError("deletion preview cannot be retried")
            item = self._find_item(preview, full_name)
            if item.state is not DeletionItemState.FAILED or not item.retryable:
                raise StaleDeletionError("this repository cannot be retried")
            self._previews[preview_id] = replace(preview, state=DeletionState.PREVIEWING)
            self._replace_item(
                preview_id,
                full_name,
                state=None,
                snapshot=None,
                error=None,
                retryable=False,
            )
        self._submit(preview_id, [item.repository])

    def invalidate(self) -> None:
        with self._lock:
            self._revision += 1
            for preview_id, preview in list(self._previews.items()):
                if preview.state is not DeletionState.SUBMITTED:
                    self._previews[preview_id] = replace(preview, state=DeletionState.STALE)

    def _submit(self, preview_id: str, repositories: list[Repository]) -> None:
        def succeeded(repo: Repository, snapshot: RemoteFile) -> None:
            self._finish_item(
                preview_id,
                repo.full_name,
                state=DeletionItemState.READY,
                snapshot=snapshot,
                error=None,
                retryable=False,
            )

        def failed(repo: Repository, error: PlatformError, retryable: bool) -> None:
            state = {
                ErrorKind.NOT_FOUND: DeletionItemState.MISSING,
                ErrorKind.CONFLICT: DeletionItemState.CONFLICT,
            }.get(error.kind, DeletionItemState.FAILED)
            self._finish_item(
                preview_id,
                repo.full_name,
                state=state,
                snapshot=None,
                error=str(error),
                retryable=retryable,
            )

        self._executor.submit(
            repositories,
            lambda repo, attempt: self._adapter.read_file(repo, self._previews[preview_id].path),
            succeeded,
            failed,
        )

    def _finish_item(self, preview_id: str, full_name: str, **changes) -> None:
        with self._lock:
            self._replace_item(preview_id, full_name, **changes)
            preview = self._previews[preview_id]
            if preview.state is DeletionState.PREVIEWING and all(
                item.state is not None for item in preview.items
            ):
                self._previews[preview_id] = replace(preview, state=DeletionState.READY)

    def _replace_item(self, preview_id: str, full_name: str, **changes) -> None:
        preview = self._previews[preview_id]
        items = list(preview.items)
        index = next(
            index for index, item in enumerate(items) if item.repository.full_name == full_name
        )
        items[index] = replace(items[index], **changes)
        self._previews[preview_id] = replace(preview, items=tuple(items))

    def _require_current(self, preview_id: str) -> DeletionPreview:
        preview = self._previews[preview_id]
        if preview.revision != self._revision or preview.state is DeletionState.STALE:
            raise StaleDeletionError("deletion preview is stale")
        return preview

    @staticmethod
    def _find_item(preview: DeletionPreview, full_name: str) -> DeletionItem:
        item = next(
            (item for item in preview.items if item.repository.full_name == full_name),
            None,
        )
        if item is None:
            raise StaleDeletionError("repository is not part of this deletion preview")
        return item
