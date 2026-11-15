from dataclasses import dataclass
from enum import Enum
from typing import Literal, Protocol

from readme_batch.models import (
    CommitInfo,
    DeleteResult,
    FileSnapshot,
    PlatformKind,
    RemoteFile,
    Repository,
    UploadFile,
    UploadResult,
)

__all__ = [
    "ErrorKind",
    "GITEE_CAPABILITIES",
    "GITCODE_CAPABILITIES",
    "GITHUB_CAPABILITIES",
    "PlatformCapabilities",
    "PlatformError",
    "PlatformKind",
    "RepositoryPlatform",
    "UploadMode",
]

UploadMode = Literal["single_commit", "unsupported"]


@dataclass(frozen=True)
class PlatformCapabilities:
    display_name: bool
    visibility: bool
    repository_topics: bool
    upload_mode: UploadMode
    file_deletion: bool = False


GITEE_CAPABILITIES = PlatformCapabilities(True, True, True, "single_commit", True)
GITHUB_CAPABILITIES = PlatformCapabilities(False, True, True, "single_commit")
GITCODE_CAPABILITIES = PlatformCapabilities(True, True, True, "unsupported")


class ErrorKind(Enum):
    AUTH = "auth"
    PERMISSION = "permission"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RATE_LIMIT = "rate_limit"
    TEMPORARY = "temporary"
    REQUEST = "request"
    UNSUPPORTED = "unsupported"


class PlatformError(Exception):
    def __init__(self, kind: ErrorKind, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


class RepositoryPlatform(Protocol):
    capabilities: PlatformCapabilities

    def list_repositories(self) -> list[Repository]: ...

    def read_readme(self, repository: Repository) -> FileSnapshot: ...

    def update_readme(
        self,
        repository: Repository,
        snapshot: FileSnapshot,
        new_content: str,
        message: str,
    ) -> CommitInfo: ...

    def update_repository_settings(
        self,
        repository: Repository,
        display_name: str | None,
        private: bool | None,
    ) -> None: ...

    def validate_topics(self, labels: tuple[str, ...]) -> None: ...

    def add_repository_topics(
        self,
        repository: Repository,
        labels: tuple[str, ...],
    ) -> None: ...

    def upload_files(
        self,
        repository: Repository,
        files: tuple[UploadFile, ...],
        overwrite: bool,
        message: str,
        reconcile_before: bool = False,
    ) -> UploadResult: ...

    def read_file(self, repository: Repository, path: str) -> RemoteFile: ...

    def delete_file(
        self,
        repository: Repository,
        snapshot: RemoteFile,
        message: str,
        reconcile_before: bool = False,
    ) -> DeleteResult: ...
