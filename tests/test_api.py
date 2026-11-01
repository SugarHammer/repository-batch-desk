import time

import pytest
from fastapi.testclient import TestClient

from readme_batch.app import create_app
from readme_batch.models import CommitInfo, FileSnapshot, Repository
from readme_batch.platforms.base import ErrorKind, PlatformError

TOKEN = "test-api-token"


class FakeAdapter:
    def __init__(self):
        self.repos = [
            Repository(
                "owner", "repo-1", "owner/repo-1", "main", "https://example/1",
                display_name="Repo One", private=True,
            ),
            Repository(
                "owner", "repo-2", "owner/repo-2", "main", "https://example/2",
                display_name="Repo Two", private=False,
            ),
        ]
        self.updates = []
        self.settings_calls = []
        self.label_calls = []
        self.settings_errors = {}
        self.label_errors = {}

    def list_repositories(self):
        return self.repos

    def read_readme(self, repository):
        content = "before old after" if repository.name == "repo-1" else "no match"
        return FileSnapshot("README.md", "main", content, f"sha-{repository.name}")

    def update_readme(self, repository, snapshot, new_content, message):
        self.updates.append((repository.full_name, snapshot.sha, new_content, message))
        return CommitInfo("commit-1", "https://example/commit/1")

    def update_repository_settings(self, repository, display_name, private):
        self.settings_calls.append((repository.full_name, display_name, private))
        errors = self.settings_errors.get(repository.full_name, [])
        if errors:
            raise errors.pop(0)

    def add_project_labels(self, repository, labels):
        self.label_calls.append((repository.full_name, labels))
        errors = self.label_errors.get(repository.full_name, [])
        if errors:
            raise errors.pop(0)


class FakeCredentials:
    def __init__(self):
        self.saved = None
        self.cleared = False

    def save(self, token):
        self.saved = token

    def load(self):
        return self.saved

    def clear(self):
        self.saved = None
        self.cleared = True


@pytest.fixture
def services(tmp_path):
    adapter = FakeAdapter()
    credentials = FakeCredentials()
    app = create_app(
        client_factory=lambda token: adapter,
        credential_store=credentials,
        report_directory=tmp_path,
    )
    client = TestClient(app, headers={"X-README-Batch-Token": app.state.api_token})
    return adapter, credentials, client


def start_session(client, remember=False):
    response = client.post("/api/session", json={"token": TOKEN, "remember": remember})
    assert response.status_code == 204


def put_draft(client, **overrides):
    draft = {
        "selected": ["owner/repo-1"],
        "old": "old",
        "new": "new",
        "commit_message": "docs: update",
    }
    draft.update(overrides)
    response = client.put("/api/draft", json=draft)
    assert response.status_code == 204


def wait_for_state(client, batch_id, expected):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        payload = client.get(f"/api/batches/{batch_id}").json()
        if payload["state"] == expected:
            return payload
        time.sleep(0.01)
    raise AssertionError(f"batch did not reach {expected}: {payload}")


def test_health(services):
    _, _, client = services

    assert client.get("/api/health").json() == {"status": "ok"}


def test_session_remember_list_repositories_and_logout(services):
    adapter, credentials, client = services

    start_session(client, remember=True)
    response = client.get("/api/repositories")

    assert response.status_code == 200
    assert response.json()[0]["full_name"] == "owner/repo-1"
    assert credentials.saved == TOKEN
    assert client.delete("/api/session").status_code == 204
    assert credentials.cleared
    assert client.get("/api/repositories").status_code == 401
    assert adapter.updates == []


def test_preview_and_commit_flow_returns_no_source_snapshot(services, tmp_path):
    adapter, _, client = services
    start_session(client)
    client.get("/api/repositories")
    put_draft(client, selected=["owner/repo-1", "owner/repo-2"])

    preview = client.post("/api/previews")
    assert preview.status_code == 202
    batch_id = preview.json()["id"]
    payload = wait_for_state(client, batch_id, "ready")

    assert payload["items"][0]["preview_state"] == "ready"
    assert "-before old after" in payload["items"][0]["diff"]
    assert "+before new after" in payload["items"][0]["diff"]
    assert payload["items"][1]["preview_state"] == "no_match"
    assert "snapshot" not in payload["items"][0]
    assert "old" not in payload["items"][0]
    assert "new" not in payload["items"][0]
    assert "new_content" not in payload["items"][0]
    assert client.post(f"/api/batches/{batch_id}/commit").status_code == 202
    completed = wait_for_state(client, batch_id, "complete")
    assert completed["items"][0]["commit_info"]["url"] == "https://example/commit/1"
    assert adapter.updates == [("owner/repo-1", "sha-repo-1", "before new after", "docs: update")]
    reports = list(tmp_path.glob("*.json"))
    assert len(reports) == 1
    assert reports[0].stem == batch_id


@pytest.mark.parametrize(
    "draft",
    [
        {"old": ""},
        {"commit_message": "  "},
        {"selected": []},
        {"selected": ["owner/unknown"]},
    ],
)
def test_invalid_preview_requests_are_rejected_without_update(services, draft):
    adapter, _, client = services
    start_session(client)
    client.get("/api/repositories")
    put_draft(client, **draft)

    response = client.post("/api/previews")

    assert 400 <= response.status_code < 500
    assert adapter.updates == []


def test_stale_preview_is_rejected_without_update(services):
    adapter, _, client = services
    start_session(client)
    client.get("/api/repositories")
    put_draft(client)
    batch_id = client.post("/api/previews").json()["id"]
    wait_for_state(client, batch_id, "ready")

    put_draft(client, new="changed")
    response = client.post(f"/api/batches/{batch_id}/commit")

    assert response.status_code == 409
    assert adapter.updates == []


def test_unknown_batch_is_safe_404(services):
    _, _, client = services
    start_session(client)

    assert client.get("/api/batches/missing").status_code == 404
    assert client.post("/api/batches/missing/commit").status_code == 404


def test_rebinding_host_is_rejected(services):
    _, _, client = services

    response = client.get("/api/health", headers={"Host": "attacker.example"})

    assert response.status_code == 403


def test_mutating_api_requires_process_token(services):
    _, _, client = services

    response = client.post(
        "/api/session",
        json={"token": TOKEN, "remember": False},
        headers={"X-README-Batch-Token": ""},
    )

    assert response.status_code == 403


def test_cross_origin_mutation_is_rejected(services):
    adapter, _, client = services
    start_session(client)
    client.get("/api/repositories")

    response = client.put(
        "/api/draft",
        json={"selected": [], "old": "old", "new": "new", "commit_message": "docs"},
        headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == 403
    assert adapter.updates == []


def test_non_remembered_replacement_session_clears_saved_credential(services):
    _, credentials, client = services
    start_session(client, remember=True)
    assert credentials.saved == TOKEN

    start_session(client, remember=False)

    assert credentials.saved is None


def test_saved_session_reports_active(tmp_path):
    adapter = FakeAdapter()
    credentials = FakeCredentials()
    credentials.saved = TOKEN
    app = create_app(
        client_factory=lambda token: adapter,
        credential_store=credentials,
        report_directory=tmp_path,
    )
    client = TestClient(app, headers={"X-README-Batch-Token": app.state.api_token})

    response = client.get("/api/session")

    assert response.status_code == 200
    assert response.json() == {"active": True}


def wait_for_operation(client, operation_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        payload = client.get(f"/api/operations/{operation_id}").json()
        if payload["state"] == "complete":
            return payload
        time.sleep(0.01)
    raise AssertionError(f"operation did not complete: {payload}")


def test_repository_list_includes_display_name_and_private(services):
    _, _, client = services
    start_session(client)

    payload = client.get("/api/repositories").json()

    assert payload[0]["display_name"] == "Repo One"
    assert payload[0]["private"] is True
    assert payload[1]["display_name"] == "Repo Two"
    assert payload[1]["private"] is False


def test_settings_operation_skips_unchanged_items_and_maps_visibility(services):
    adapter, _, client = services
    start_session(client)
    client.get("/api/repositories")

    response = client.post(
        "/api/operations/settings",
        json={
            "selected": ["owner/repo-1", "owner/repo-2"],
            "names": {"owner/repo-1": "  新名称  "},
            "visibility": "public",
        },
    )

    assert response.status_code == 202
    operation = wait_for_operation(client, response.json()["id"])
    assert len(operation["items"]) == 1
    assert operation["items"][0]["repository"]["full_name"] == "owner/repo-1"
    assert adapter.settings_calls == [("owner/repo-1", "新名称", False)]

    response = client.post(
        "/api/operations/settings",
        json={"selected": ["owner/repo-2"], "names": {}, "visibility": "private"},
    )
    wait_for_operation(client, response.json()["id"])
    assert adapter.settings_calls[-1] == ("owner/repo-2", "Repo Two", True)


@pytest.mark.parametrize(
    "payload",
    [
        {"selected": ["owner/repo-1"], "names": {"owner/repo-1": "  "}, "visibility": "keep"},
        {"selected": [], "names": {}, "visibility": "private"},
        {"selected": ["owner/unknown"], "names": {}, "visibility": "private"},
        {"selected": ["owner/repo-1"], "names": {"owner/unknown": "Name"}, "visibility": "keep"},
        {"selected": ["owner/repo-1"], "names": {"owner/repo-1": "Repo One"}, "visibility": "keep"},
    ],
)
def test_invalid_settings_requests_do_not_write(services, payload):
    adapter, _, client = services
    start_session(client)
    client.get("/api/repositories")

    response = client.post("/api/operations/settings", json=payload)

    assert 400 <= response.status_code < 500
    assert adapter.settings_calls == []


def test_labels_operation_trims_and_stably_deduplicates(services):
    adapter, _, client = services
    start_session(client)
    client.get("/api/repositories")

    response = client.post(
        "/api/operations/labels",
        json={
            "selected": ["owner/repo-1", "owner/repo-2"],
            "labels": [" Java ", "Spring Boot", "Java", "", "Spring Boot"],
        },
    )

    assert response.status_code == 202
    operation = wait_for_operation(client, response.json()["id"])
    assert all(item["state"] == "success" for item in operation["items"])
    assert adapter.label_calls == [
        ("owner/repo-1", ("Java", "Spring Boot")),
        ("owner/repo-2", ("Java", "Spring Boot")),
    ]
    assert TOKEN not in str(operation)


@pytest.mark.parametrize("labels", [[], [" ", ""]])
def test_empty_labels_are_rejected(services, labels):
    adapter, _, client = services
    start_session(client)
    client.get("/api/repositories")

    response = client.post(
        "/api/operations/labels", json={"selected": ["owner/repo-1"], "labels": labels}
    )

    assert 400 <= response.status_code < 500
    assert adapter.label_calls == []


def test_unknown_operation_and_invalid_retry_are_safe(services):
    adapter, _, client = services
    start_session(client)
    client.get("/api/repositories")
    adapter.label_errors["owner/repo-1"] = [PlatformError(ErrorKind.PERMISSION, "denied")]
    operation_id = client.post(
        "/api/operations/labels",
        json={"selected": ["owner/repo-1"], "labels": ["Java"]},
    ).json()["id"]
    wait_for_operation(client, operation_id)

    assert client.get("/api/operations/missing").status_code == 404
    response = client.post(
        f"/api/operations/{operation_id}/retry", json={"full_name": "owner/repo-1"}
    )
    assert response.status_code == 409


def test_replacing_and_deleting_session_hides_old_operations(services):
    _, _, client = services
    start_session(client)
    client.get("/api/repositories")
    operation_id = client.post(
        "/api/operations/labels",
        json={"selected": ["owner/repo-1"], "labels": ["Java"]},
    ).json()["id"]
    wait_for_operation(client, operation_id)

    start_session(client)
    assert client.get(f"/api/operations/{operation_id}").status_code == 404
    client.delete("/api/session")
    assert client.get(f"/api/operations/{operation_id}").status_code == 401


def test_operation_mutations_require_process_token(services):
    adapter, _, client = services
    start_session(client)
    client.get("/api/repositories")

    response = client.post(
        "/api/operations/labels",
        json={"selected": ["owner/repo-1"], "labels": ["Java"]},
        headers={"X-README-Batch-Token": ""},
    )

    assert response.status_code == 403
    assert adapter.label_calls == []
