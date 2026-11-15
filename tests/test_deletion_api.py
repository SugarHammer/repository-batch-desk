import time

import pytest
from fastapi.testclient import TestClient

from readme_batch.app import create_app
from readme_batch.models import DeleteResult, CommitInfo, PlatformKind, RemoteFile, Repository
from readme_batch.platforms.base import (
    GITEE_CAPABILITIES,
    GITCODE_CAPABILITIES,
    GITHUB_CAPABILITIES,
    ErrorKind,
    PlatformError,
)

TOKEN = "test-api-token"


class FakeAdapter:
    def __init__(self, capabilities=GITEE_CAPABILITIES, platform=PlatformKind.GITEE):
        self.capabilities = capabilities
        self.repos = [
            Repository(
                "owner", f"repo-{number}", f"owner/repo-{number}", "main",
                f"https://example/{number}", display_name=f"Repo {number}", platform=platform,
            )
            for number in (1, 2)
        ]
        self.file_outcomes = {}
        self.read_file_calls = []
        self.delete_calls = []

    def list_repositories(self):
        return self.repos

    def read_file(self, repository, path):
        self.read_file_calls.append((repository.full_name, path))
        outcome = self.file_outcomes.get(
            repository.full_name, RemoteFile(path, repository.default_branch, f"sha-{repository.name}")
        )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def delete_file(self, repository, snapshot, message, reconcile_before=False):
        self.delete_calls.append((repository.full_name, snapshot, message, reconcile_before))
        return DeleteResult(CommitInfo("commit-1", "https://example/commit/1"), False)


class FakeCredentials:
    def __init__(self):
        self.tokens = {}
        self.last_platform = PlatformKind.GITEE

    def save(self, platform, token):
        self.tokens[platform] = token

    def load(self, platform):
        return self.tokens.get(platform)

    def clear(self, platform):
        self.tokens.pop(platform, None)

    def load_last_platform(self):
        return self.last_platform

    def save_last_platform(self, platform):
        self.last_platform = platform


@pytest.fixture
def services(tmp_path):
    adapters = {
        PlatformKind.GITEE: FakeAdapter(),
        PlatformKind.GITHUB: FakeAdapter(GITHUB_CAPABILITIES, PlatformKind.GITHUB),
        PlatformKind.GITCODE: FakeAdapter(GITCODE_CAPABILITIES, PlatformKind.GITCODE),
    }
    app = create_app(
        client_factories={
            platform: lambda token, adapter=adapter: adapter
            for platform, adapter in adapters.items()
        },
        credential_store=FakeCredentials(),
        report_directory=tmp_path,
    )
    client = TestClient(app, headers={"X-README-Batch-Token": app.state.api_token})
    return adapters, client


def start_session(client, platform=PlatformKind.GITEE):
    response = client.post(
        "/api/session", json={"token": TOKEN, "remember": False, "platform": platform.value}
    )
    assert response.status_code == 204


def wait_for_operation(client, operation_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        payload = client.get(f"/api/operations/{operation_id}").json()
        if payload["state"] == "complete":
            return payload
        time.sleep(0.01)
    raise AssertionError(f"operation did not complete: {payload}")


def wait_for_deletion_preview(client, preview_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        payload = client.get(f"/api/deletion-previews/{preview_id}").json()
        if payload["state"] == "ready":
            return payload
        time.sleep(0.01)
    raise AssertionError(f"deletion preview did not become ready: {payload}")


def test_delete_preview_confirm_and_result_flow(services):
    adapters, client = services
    adapter = adapters[PlatformKind.GITEE]
    adapter.file_outcomes["owner/repo-2"] = PlatformError(ErrorKind.NOT_FOUND, "missing")
    start_session(client)
    client.get("/api/repositories")

    response = client.post(
        "/api/deletion-previews",
        json={
            "selected": ["owner/repo-1", "owner/repo-2"],
            "path": " images/abc.png ",
            "message": " chore: remove image ",
        },
    )

    assert response.status_code == 202
    preview_id = response.json()["id"]
    preview = wait_for_deletion_preview(client, preview_id)
    assert [item["state"] for item in preview["items"]] == ["ready", "missing"]
    assert preview["path"] == "images/abc.png"
    assert "snapshot" not in preview["items"][0]
    assert adapter.read_file_calls == [
        ("owner/repo-1", "images/abc.png"),
        ("owner/repo-2", "images/abc.png"),
    ]
    assert adapter.delete_calls == []

    response = client.post(f"/api/deletion-previews/{preview_id}/delete")
    assert response.status_code == 202
    operation = wait_for_operation(client, response.json()["id"])
    assert operation["kind"] == "delete"
    assert len(operation["items"]) == 1
    assert operation["items"][0]["state"] == "success"
    assert adapter.delete_calls == [
        (
            "owner/repo-1",
            RemoteFile("images/abc.png", "main", "sha-repo-1"),
            "chore: remove image",
            False,
        )
    ]
    assert client.post(f"/api/deletion-previews/{preview_id}/delete").status_code == 409
    assert len(adapter.delete_calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"path": ""},
        {"path": "/root/a.txt"},
        {"path": "C:\\root\\a.txt"},
        {"path": "photo\\a.txt"},
        {"path": "photo//a.txt"},
        {"path": "photo/../a.txt"},
        {"path": "photo/./a.txt"},
        {"path": "photo/a\n.txt"},
        {"message": "  "},
        {"selected": []},
        {"selected": ["owner/unknown"]},
    ],
)
def test_invalid_delete_previews_do_not_read_or_delete(services, payload):
    adapters, client = services
    adapter = adapters[PlatformKind.GITEE]
    start_session(client)
    client.get("/api/repositories")
    request = {
        "selected": ["owner/repo-1"],
        "path": "abc.png",
        "message": "chore: delete",
    }
    request.update(payload)

    response = client.post("/api/deletion-previews", json=request)

    assert response.status_code == 400
    assert adapter.read_file_calls == []
    assert adapter.delete_calls == []


def test_unknown_and_stale_deletion_previews_cannot_delete(services):
    adapters, client = services
    adapter = adapters[PlatformKind.GITEE]
    start_session(client)
    client.get("/api/repositories")
    preview_id = client.post(
        "/api/deletion-previews",
        json={
            "selected": ["owner/repo-1"],
            "path": "abc.png",
            "message": "chore: delete",
        },
    ).json()["id"]
    wait_for_deletion_preview(client, preview_id)

    start_session(client)

    assert client.post("/api/deletion-previews/missing/delete").status_code == 404
    assert client.post(f"/api/deletion-previews/{preview_id}/delete").status_code == 404
    assert adapter.delete_calls == []


def test_deletion_preview_retry_only_repeats_failed_repository(services):
    adapters, client = services
    adapter = adapters[PlatformKind.GITEE]
    adapter.file_outcomes["owner/repo-1"] = PlatformError(ErrorKind.TEMPORARY, "temporary")
    start_session(client)
    client.get("/api/repositories")
    preview_id = client.post(
        "/api/deletion-previews",
        json={
            "selected": ["owner/repo-1"],
            "path": "abc.png",
            "message": "chore: delete",
        },
    ).json()["id"]
    preview = wait_for_deletion_preview(client, preview_id)
    assert preview["items"][0]["retryable"] is True

    response = client.post(
        f"/api/deletion-previews/{preview_id}/retry",
        json={"full_name": "owner/repo-1"},
    )

    assert response.status_code == 202
    wait_for_deletion_preview(client, preview_id)
    assert len(adapter.read_file_calls) == 6
    assert client.post(
        "/api/deletion-previews/missing/retry", json={"full_name": "owner/repo-1"}
    ).status_code == 404


@pytest.mark.parametrize("platform", [PlatformKind.GITHUB, PlatformKind.GITCODE])
def test_unsupported_platform_cannot_preview_or_delete_files(services, platform):
    adapters, client = services
    adapter = adapters[platform]
    start_session(client, platform)
    client.get("/api/repositories")

    response = client.post(
        "/api/deletion-previews",
        json={
            "selected": ["owner/repo-1"],
            "path": "abc.png",
            "message": "chore: delete",
        },
    )

    assert response.status_code == 400
    assert adapter.read_file_calls == []
    assert adapter.delete_calls == []


def test_session_invalidation_during_delete_confirmation_returns_conflict(services, monkeypatch):
    from readme_batch.operations import StaleOperationError

    adapters, client = services
    adapter = adapters[PlatformKind.GITEE]
    start_session(client)
    client.get("/api/repositories")
    preview_id = client.post(
        "/api/deletion-previews",
        json={
            "selected": ["owner/repo-1"],
            "path": "abc.png",
            "message": "chore: delete",
        },
    ).json()["id"]
    wait_for_deletion_preview(client, preview_id)
    operations = client.app.state.editor.operations

    def reject_stale(*args):
        raise StaleOperationError("operation manager is stale")

    monkeypatch.setattr(operations, "start_delete", reject_stale)

    response = client.post(f"/api/deletion-previews/{preview_id}/delete")

    assert response.status_code == 409
    assert adapter.delete_calls == []
