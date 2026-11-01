from enum import Enum
from typing import Protocol

from readme_batch.models import CommitInfo, FileSnapshot, Repository


class ErrorKind(Enum):
    AUTH = "auth"
    PERMISSION = "permission"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RATE_LIMIT = "rate_limit"
    TEMPORARY = "temporary"
    REQUEST = "request"


class PlatformError(Exception):
    def __init__(self, kind: ErrorKind, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


class RepositoryPlatform(Protocol):
    def list_repositories(self) -> list[Repository]: ...

    def read_readme(self, repository: Repository) -> FileSnapshot: ...

    def update_readme(
        self,
        repository: Repository,
        snapshot: FileSnapshot,
        new_content: str,
        message: str,
    ) -> CommitInfo: ...
