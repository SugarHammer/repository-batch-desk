import base64
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from readme_batch.app import create_app
from readme_batch.models import CommitInfo, FileSnapshot, PlatformKind, Repository, UploadResult
from readme_batch.platforms.base import (
    GITEE_CAPABILITIES,
    GITHUB_CAPABILITIES,
    ErrorKind,
    PlatformError,
)

TOKEN = "test-api-token"


class FakeAdapter:
    def __init__(self, capabilities=GITEE_CAPABILITIES):
        self.capabilities = capabilities
        platform = PlatformKind.GITEE
        if not capabilities.display_name:
            platform = PlatformKind.GITHUB
        elif capabilities.upload_mode == "unsupported":
            platform = PlatformKind.GITCODE
        self.repos = [
            Repository(
                "owner", "repo-1", "owner/repo-1", "main", "https://example/1",
                display_name="Repo One", private=True, platform=platform,
                description="第一个仓库简介",
            ),
            Repository(
                "owner", "repo-2", "owner/repo-2", "main", "https://example/2",
                display_name="Repo Two", private=False, platform=platform,
            ),
        ]
        self.updates = []
        self.settings_calls = []
        self.label_calls = []
        self.validated_topics = []
        self.upload_calls = []
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

    def validate_topics(self, labels):
        self.validated_topics.append(labels)

    def add_repository_topics(self, repository, labels):
        self.label_calls.append((repository.full_name, labels))
        errors = self.label_errors.get(repository.full_name, [])
        if errors:
            raise errors.pop(0)

    def upload_files(self, repository, files, overwrite, message, reconcile_before=False):
        self.upload_calls.append((repository.full_name, files, overwrite, message))
        return UploadResult(CommitInfo("commit-1", "https://example/commit/1"), False)


class FakeCredentials:
    def __init__(self):
        self.saved = None
        self.tokens = {}
        self.last_platform = PlatformKind.GITEE
        self.cleared = False

    def save(self, platform, token):
        if platform is PlatformKind.GITEE:
            self.saved = token
        else:
            self.tokens[platform] = token

    def load(self, platform):
        if platform is PlatformKind.GITEE:
            return self.saved
        return self.tokens.get(platform)

    def clear(self, platform):
        if platform is PlatformKind.GITEE:
            self.saved = None
        else:
            self.tokens.pop(platform, None)
        self.cleared = True

    def load_last_platform(self):
        return self.last_platform

    def save_last_platform(self, platform):
        self.last_platform = platform


@pytest.fixture
def services(tmp_path):
    adapter = FakeAdapter()
    credentials = FakeCredentials()
    app = create_app(
        client_factories={PlatformKind.GITEE: lambda token: adapter},
        credential_store=credentials,
        report_directory=tmp_path,
    )
    client = TestClient(app, headers={"X-README-Batch-Token": app.state.api_token})
    return adapter, credentials, client


def start_session(client, remember=False, platform=None):
    payload = {"token": TOKEN, "remember": remember}
    if platform is not None:
        payload["platform"] = platform
    response = client.post("/api/session", json=payload)
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
        client_factories={PlatformKind.GITEE: lambda token: adapter},
        credential_store=credentials,
        report_directory=tmp_path,
    )
    client = TestClient(app, headers={"X-README-Batch-Token": app.state.api_token})

    response = client.get("/api/session")

    assert response.status_code == 200
    assert response.json() == {
        "active": True,
        "platform": "gitee",
        "capabilities": {
            "display_name": True,
            "visibility": True,
            "repository_topics": True,
            "upload_mode": "single_commit",
            "file_deletion": True,
        },
    }


def test_last_platform_restores_only_its_own_saved_token(tmp_path):
    adapter = FakeAdapter(capabilities=GITHUB_CAPABILITIES)
    credentials = FakeCredentials()
    credentials.tokens[PlatformKind.GITHUB] = TOKEN
    credentials.last_platform = PlatformKind.GITHUB
    app = create_app(
        client_factories={PlatformKind.GITHUB: lambda token: adapter},
        credential_store=credentials,
        report_directory=tmp_path,
    )
    client = TestClient(app, headers={"X-README-Batch-Token": app.state.api_token})

    session = client.get("/api/session").json()

    assert session["active"] is True
    assert session["platform"] == "github"


def test_platform_switch_invalidates_old_work_and_enforces_capabilities(tmp_path):
    gitee = FakeAdapter()
    github = FakeAdapter(
        capabilities=replace(GITHUB_CAPABILITIES, upload_mode="unsupported")
    )
    app = create_app(
        client_factories={
            PlatformKind.GITEE: lambda token: gitee,
            PlatformKind.GITHUB: lambda token: github,
        },
        credential_store=FakeCredentials(),
        report_directory=tmp_path,
    )
    client = TestClient(app, headers={"X-README-Batch-Token": app.state.api_token})
    start_session(client)
    client.get("/api/repositories")
    put_draft(client)
    batch_id = client.post("/api/previews").json()["id"]
    wait_for_state(client, batch_id, "ready")

    start_session(client, platform="github")

    assert client.post(f"/api/batches/{batch_id}/commit").status_code == 404
    assert client.post("/api/previews").status_code == 400
    assert gitee.updates == []
    client.get("/api/repositories")
    settings = client.post(
        "/api/operations/settings",
        json={
            "selected": ["owner/repo-1"],
            "names": {"owner/repo-1": " renamed "},
            "visibility": "keep",
        },
    )
    upload = client.post(
        "/api/operations/uploads",
        json={
            "selected": ["owner/repo-1"],
            "files": [{"name": "a.txt", "content_base64": "eA=="}],
            "message": "chore: upload",
        },
    )
    assert settings.status_code == 400
    assert upload.status_code == 400
    assert github.settings_calls == []
    assert github.upload_calls == []


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
    assert payload[0]["platform"] == "gitee"
    assert payload[0]["private"] is True
    assert payload[0]["description"] == "第一个仓库简介"
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
    assert adapter.settings_calls[-1] == ("owner/repo-2", None, True)


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
    assert adapter.validated_topics == [("Java", "Spring Boot")]
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


def upload_file_payload(name, content=b""):
    return {"name": name, "content_base64": base64.b64encode(content).decode("ascii")}


def test_upload_operation_accepts_binary_and_empty_files(services):
    adapter, _, client = services
    start_session(client)
    client.get("/api/repositories")

    response = client.post(
        "/api/operations/uploads",
        json={
            "selected": ["owner/repo-1"],
            "directory": "photo/common",
            "overwrite": True,
            "message": "chore: upload",
            "files": [
                upload_file_payload("hello.txt", b"hello"),
                upload_file_payload("empty.bin", b""),
                upload_file_payload("图片.png", b"\x00\xff"),
            ],
        },
    )

    assert response.status_code == 202
    operation = wait_for_operation(client, response.json()["id"])
    _, files, overwrite, message = adapter.upload_calls[0]
    assert [(file.path, file.content) for file in files] == [
        ("photo/common/hello.txt", b"hello"),
        ("photo/common/empty.bin", b""),
        ("photo/common/图片.png", b"\x00\xff"),
    ]
    assert overwrite is True
    assert message == "chore: upload"
    assert operation["items"][0]["commit_info"]["url"] == "https://example/commit/1"
    assert "aGVsbG8=" not in str(operation)

    response = client.post(
        "/api/operations/uploads",
        json={
            "selected": ["owner/repo-1"],
            "files": [upload_file_payload("root.txt", b"root")],
            "message": "chore: root",
        },
    )
    wait_for_operation(client, response.json()["id"])
    assert adapter.upload_calls[-1][1][0].path == "root.txt"


@pytest.mark.parametrize(
    "payload",
    [
        {"files": [{"name": "a.txt", "content_base64": "not-base64"}]},
        {"files": [upload_file_payload(" ")]},
        {"files": [upload_file_payload("a.txt"), upload_file_payload(" a.txt ")]},
        {"files": [upload_file_payload("a/b.txt")]},
        {"files": [upload_file_payload("a\\b.txt")]},
        {"files": [upload_file_payload("a\n.txt")]},
        {"files": [upload_file_payload(".")]},
        {"files": [upload_file_payload("..")]},
        {"directory": "/root", "files": [upload_file_payload("a.txt")]},
        {"directory": "C:\\root", "files": [upload_file_payload("a.txt")]},
        {"directory": "photo//common", "files": [upload_file_payload("a.txt")]},
        {"directory": "photo/../common", "files": [upload_file_payload("a.txt")]},
        {"directory": "photo\\common", "files": [upload_file_payload("a.txt")]},
        {"message": "  ", "files": [upload_file_payload("a.txt")]},
        {"files": []},
        {"selected": ["owner/unknown"], "files": [upload_file_payload("a.txt")]},
    ],
)
def test_invalid_upload_requests_do_not_create_tasks(services, payload):
    adapter, _, client = services
    start_session(client)
    client.get("/api/repositories")
    request = {
        "selected": ["owner/repo-1"],
        "directory": "",
        "overwrite": False,
        "message": "chore: upload",
        "files": [upload_file_payload("a.txt")],
    }
    request.update(payload)

    response = client.post("/api/operations/uploads", json=request)

    assert 400 <= response.status_code < 500
    assert adapter.upload_calls == []


def test_upload_rejects_more_than_twenty_files(services):
    adapter, _, client = services
    start_session(client)
    client.get("/api/repositories")

    response = client.post(
        "/api/operations/uploads",
        json={
            "selected": ["owner/repo-1"],
            "message": "chore: upload",
            "files": [upload_file_payload(f"{number}.txt") for number in range(21)],
        },
    )

    assert response.status_code == 413
    assert adapter.upload_calls == []


def test_upload_rejects_single_file_over_ten_mib(services):
    adapter, _, client = services
    start_session(client)
    client.get("/api/repositories")

    response = client.post(
        "/api/operations/uploads",
        json={
            "selected": ["owner/repo-1"],
            "message": "chore: upload",
            "files": [upload_file_payload("large.bin", b"\0" * (10 * 1024 * 1024 + 1))],
        },
    )

    assert response.status_code == 413
    assert adapter.upload_calls == []


def test_upload_rejects_total_size_over_twenty_five_mib(services):
    adapter, _, client = services
    start_session(client)
    client.get("/api/repositories")

    response = client.post(
        "/api/operations/uploads",
        json={
            "selected": ["owner/repo-1"],
            "message": "chore: upload",
            "files": [
                upload_file_payload(f"large-{number}.bin", b"\0" * (9 * 1024 * 1024))
                for number in range(3)
            ],
        },
    )

    assert response.status_code == 413
    assert adapter.upload_calls == []


def test_shutdown_marks_local_server_for_exit(services):
    _, _, client = services
    client.app.state.server = type("FakeServer", (), {"should_exit": False})()

    response = client.post("/api/shutdown")

    assert response.status_code == 202
    assert client.app.state.server.should_exit is True


@pytest.mark.parametrize("label", ["Uppercase", "has space", "中文", "-leading", "a" * 51])
def test_github_invalid_topics_are_rejected_before_remote_work(tmp_path, label):
    from readme_batch.platforms.github import GitHubClient

    adapter = FakeAdapter(capabilities=GITHUB_CAPABILITIES)
    adapter.validate_topics = lambda labels: GitHubClient.validate_topics(adapter, labels)
    app = create_app(
        client_factories={PlatformKind.GITHUB: lambda token: adapter},
        credential_store=FakeCredentials(),
        report_directory=tmp_path,
    )
    client = TestClient(app, headers={"X-README-Batch-Token": app.state.api_token})
    start_session(client, platform="github")
    client.get("/api/repositories")

    response = client.post(
        "/api/operations/labels",
        json={"selected": ["owner/repo-1"], "labels": [label]},
    )

    assert response.status_code == 400
    assert adapter.label_calls == []


def test_github_upload_capability_allows_existing_upload_api(tmp_path):
    adapter = FakeAdapter(capabilities=GITHUB_CAPABILITIES)
    app = create_app(
        client_factories={PlatformKind.GITHUB: lambda token: adapter},
        credential_store=FakeCredentials(),
        report_directory=tmp_path,
    )
    client = TestClient(app, headers={"X-README-Batch-Token": app.state.api_token})
    start_session(client, platform="github")
    client.get("/api/repositories")

    response = client.post(
        "/api/operations/uploads",
        json={
            "selected": ["owner/repo-1"],
            "files": [{"name": "a.txt", "content_base64": "eA=="}],
            "message": "chore: upload",
        },
    )

    assert response.status_code == 202
    wait_for_operation(client, response.json()["id"])
    assert len(adapter.upload_calls) == 1


def test_gitcode_upload_is_rejected_before_remote_work(tmp_path):
    from readme_batch.platforms.base import GITCODE_CAPABILITIES

    adapter = FakeAdapter(capabilities=GITCODE_CAPABILITIES)
    app = create_app(
        client_factories={PlatformKind.GITCODE: lambda token: adapter},
        credential_store=FakeCredentials(),
        report_directory=tmp_path,
    )
    client = TestClient(app, headers={"X-README-Batch-Token": app.state.api_token})
    start_session(client, platform="gitcode")
    client.get("/api/repositories")

    response = client.post(
        "/api/operations/uploads",
        json={
            "selected": ["owner/repo-1"],
            "files": [{"name": "a.txt", "content_base64": "eA=="}],
            "message": "chore: upload",
        },
    )

    assert response.status_code == 400
    assert adapter.upload_calls == []


def test_legacy_gitee_token_restores_without_last_platform_preference(tmp_path):
    adapter = FakeAdapter()
    credentials = FakeCredentials()
    credentials.saved = TOKEN
    credentials.last_platform = None
    app = create_app(
        client_factories={PlatformKind.GITEE: lambda token: adapter},
        credential_store=credentials,
        report_directory=tmp_path,
    )
    client = TestClient(app, headers={"X-README-Batch-Token": app.state.api_token})

    session = client.get("/api/session").json()

    assert session["active"] is True
    assert session["platform"] == "gitee"
    assert credentials.last_platform is PlatformKind.GITEE


def test_in_flight_repository_list_cannot_publish_into_replacement_session(tmp_path):
    import threading

    class BlockingAdapter(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def list_repositories(self):
            self.entered.set()
            self.release.wait(2)
            return self.repos

    old_adapter = BlockingAdapter()
    new_adapter = FakeAdapter(capabilities=GITHUB_CAPABILITIES)
    app = create_app(
        client_factories={
            PlatformKind.GITEE: lambda token: old_adapter,
            PlatformKind.GITHUB: lambda token: new_adapter,
        },
        credential_store=FakeCredentials(),
        report_directory=tmp_path,
    )
    old_client = TestClient(app, headers={"X-README-Batch-Token": app.state.api_token})
    new_client = TestClient(app, headers={"X-README-Batch-Token": app.state.api_token})
    start_session(old_client)
    responses = []
    thread = threading.Thread(target=lambda: responses.append(old_client.get("/api/repositories")))
    thread.start()
    assert old_adapter.entered.wait(2)

    start_session(new_client, platform="github")
    old_adapter.release.set()
    thread.join(2)

    assert responses[0].status_code == 409
    assert app.state.editor.repositories == {}
    assert app.state.editor.platform is PlatformKind.GITHUB
