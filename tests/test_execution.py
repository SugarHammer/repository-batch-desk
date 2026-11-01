import threading
import time

from readme_batch.execution import RepositoryExecutor
from readme_batch.models import Repository
from readme_batch.platforms.base import ErrorKind, PlatformError


def repository(number: int) -> Repository:
    return Repository(
        "owner",
        f"repo-{number}",
        f"owner/repo-{number}",
        "main",
        f"https://gitee.com/owner/repo-{number}",
    )


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


def test_executor_limits_concurrency_and_completes_each_repository_once():
    gate = threading.Event()
    lock = threading.Lock()
    active = 0
    max_active = 0
    succeeded = []

    def operation(repo):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            gate.wait(2)
            return repo.full_name
        finally:
            with lock:
                active -= 1

    executor = RepositoryExecutor(max_workers=3)
    executor.submit(
        [repository(number) for number in range(1, 8)],
        operation,
        lambda repo, result: succeeded.append((repo.full_name, result)),
        lambda repo, error, retryable: None,
    )
    wait_for(lambda: max_active == 3, timeout=2)
    gate.set()
    wait_for(lambda: len(succeeded) == 7)

    assert max_active == 3
    assert sorted(succeeded) == [(f"owner/repo-{n}", f"owner/repo-{n}") for n in range(1, 8)]


def test_temporary_error_uses_backoff_and_is_retryable():
    attempts = []
    sleeps = []
    failures = []

    def operation(repo):
        attempts.append(repo.full_name)
        raise PlatformError(ErrorKind.TEMPORARY, "temporary")

    executor = RepositoryExecutor(max_workers=1, max_attempts=3, sleeper=sleeps.append)
    executor.submit(
        [repository(1)],
        operation,
        lambda repo, result: None,
        lambda repo, error, retryable: failures.append((error, retryable)),
    )
    wait_for(lambda: len(failures) == 1)

    assert attempts == ["owner/repo-1"] * 3
    assert sleeps == [0.25, 0.5]
    assert failures[0][0].kind is ErrorKind.TEMPORARY
    assert failures[0][1] is True


def test_rate_limit_retry_after_overrides_backoff():
    sleeps = []
    failures = []

    def operation(repo):
        raise PlatformError(ErrorKind.RATE_LIMIT, "limited", retry_after=1.5)

    executor = RepositoryExecutor(max_workers=1, max_attempts=2, sleeper=sleeps.append)
    executor.submit(
        [repository(1)],
        operation,
        lambda repo, result: None,
        lambda repo, error, retryable: failures.append(error),
    )
    wait_for(lambda: len(failures) == 1)

    assert sleeps == [1.5]
    assert failures[0].retry_after == 1.5


def test_auth_failure_stops_queued_operations():
    calls = []
    starts = []
    failures = []

    def operation(repo):
        calls.append(repo.full_name)
        raise PlatformError(ErrorKind.AUTH, "unauthorized")

    executor = RepositoryExecutor(max_workers=1, max_attempts=1)
    executor.submit(
        [repository(number) for number in range(1, 6)],
        operation,
        lambda repo, result: None,
        lambda repo, error, retryable: failures.append((repo.full_name, error, retryable)),
        on_start=lambda repo: starts.append(repo.full_name),
    )
    wait_for(lambda: len(failures) == 5)

    assert calls == ["owner/repo-1"]
    assert starts == ["owner/repo-1"]
    assert all(error.kind is ErrorKind.AUTH for _, error, _ in failures)
    assert all(not retryable for _, _, retryable in failures)


def test_unexpected_error_is_safely_mapped():
    failures = []

    def operation(repo):
        raise RuntimeError("sensitive implementation detail")

    executor = RepositoryExecutor(max_workers=1)
    executor.submit(
        [repository(1)],
        operation,
        lambda repo, result: None,
        lambda repo, error, retryable: failures.append((error, retryable)),
    )
    wait_for(lambda: len(failures) == 1)

    error, retryable = failures[0]
    assert error.kind is ErrorKind.REQUEST
    assert str(error) == "Repository operation failed"
    assert "sensitive implementation detail" not in str(error)
    assert retryable is False
