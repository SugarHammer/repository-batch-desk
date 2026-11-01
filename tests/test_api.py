import time

import pytest
from fastapi.testclient import TestClient

from readme_batch.app import create_app
from readme_batch.models import CommitInfo, FileSnapshot, Repository

TOKEN = "test-api-token"


class FakeAdapter:
    def __init__(self):
        self.repos = [
            Repository("owner", "repo-1", "owner/repo-1", "main", "https://example/1"),
            Repository("owner", "repo-2", "owner/repo-2", "main", "https://example/2"),
        ]
        self.updates = []

    def list_repositories(self):
        return self.repos

    def read_readme(self, repository):
        content = "before old after" if repository.name == "repo-1" else "no match"
        return FileSnapshot("README.md", "main", content, f"sha-{repository.name}")

    def update_readme(self, repository, snapshot, new_content, message):
        self.updates.append((repository.full_name, snapshot.sha, new_content, message))
        return CommitInfo("commit-1", "https://example/commit/1")


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
