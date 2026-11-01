import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

from readme_batch.models import Repository
from readme_batch.platforms.base import ErrorKind, PlatformError

T = TypeVar("T")


class RepositoryExecutor:
    def __init__(
        self,
        max_workers: int = 3,
        max_attempts: int = 3,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._max_attempts = max_attempts
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._auth_failed = False

    def submit(
        self,
        repositories: list[Repository],
        operation: Callable[[Repository], T],
        on_success: Callable[[Repository, T], None],
        on_failure: Callable[[Repository, PlatformError, bool], None],
        on_start: Callable[[Repository], None] | None = None,
    ) -> None:
        for repository in repositories:
            self._executor.submit(
                self._run,
                repository,
                operation,
                on_success,
                on_failure,
                on_start,
            )

    def _run(
        self,
        repository: Repository,
        operation: Callable[[Repository], T],
        on_success: Callable[[Repository, T], None],
        on_failure: Callable[[Repository, PlatformError, bool], None],
        on_start: Callable[[Repository], None] | None,
    ) -> None:
        if self._authentication_failed():
            on_failure(
                repository,
                PlatformError(ErrorKind.AUTH, "Authentication failed; queued request stopped"),
                False,
            )
            return
        if on_start is not None:
            on_start(repository)
        for attempt in range(self._max_attempts):
            try:
                result = operation(repository)
                on_success(repository, result)
                return
            except PlatformError as error:
                if error.kind is ErrorKind.AUTH:
                    with self._lock:
                        self._auth_failed = True
                retryable = error.kind in {ErrorKind.TEMPORARY, ErrorKind.RATE_LIMIT}
                if retryable and attempt + 1 < self._max_attempts:
                    delay = error.retry_after if error.retry_after is not None else 0.25 * (2**attempt)
                    self._sleeper(delay)
                    continue
                on_failure(repository, error, retryable)
                return
            except Exception:
                on_failure(
                    repository,
                    PlatformError(ErrorKind.REQUEST, "Repository operation failed"),
                    False,
                )
                return

    def _authentication_failed(self) -> bool:
        with self._lock:
            return self._auth_failed
