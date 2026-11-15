import base64
import json
import threading
from dataclasses import replace
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from readme_batch.execution import RepositoryExecutor
from readme_batch.models import FileSnapshot, Repository, UploadFile
from readme_batch.platforms.base import ErrorKind, PlatformError
from readme_batch.platforms.gitee import GiteeClient

API = "https://gitee.com/api/v5"
TOKEN = "test-secret-token"


def repo_payload(number: int) -> dict:
    return {
        "id": number,
        "path": f"repo-{number}",
        "name": f"repo-{number}",
        "full_name": f"owner/repo-{number}",
        "default_branch": "main",
        "private": False,
        "html_url": f"https://gitee.com/owner/repo-{number}",
        "owner": {
            "id": 1,
            "login": "owner",
            "name": "Owner",
            "avatar_url": "https://example/avatar",
            "url": "https://gitee.com/api/v5/users/owner",
            "html_url": "https://gitee.com/owner",
        },
    }


def repository() -> Repository:
    return Repository("owner", "repo-1", "owner/repo-1", "main", "https://gitee.com/owner/repo-1")


@respx.mock
def test_list_repositories_reads_all_pages():
    route = respx.get(f"{API}/user/repos").mock(
        side_effect=[
            httpx.Response(200, json=[repo_payload(n) for n in range(1, 101)]),
            httpx.Response(200, json=[repo_payload(101)]),
        ]
    )

    repos = GiteeClient(TOKEN).list_repositories()

    assert len(repos) == 101
    assert repos[100].full_name == "owner/repo-101"
    assert repos[0].default_branch == "main"
    assert [call.request.url.params["page"] for call in route.calls] == ["1", "2"]
    assert all(call.request.url.params["per_page"] == "100" for call in route.calls)


@respx.mock
def test_list_repositories_skips_empty_repositories_without_default_branch():
    empty = repo_payload(1)
    empty["default_branch"] = None
    respx.get(f"{API}/user/repos").mock(
        return_value=httpx.Response(200, json=[empty, repo_payload(2)])
    )

    repos = GiteeClient(TOKEN).list_repositories()

    assert [repo.full_name for repo in repos] == ["owner/repo-2"]


@respx.mock
def test_read_readme_decodes_base64_without_normalizing_content():
    source = "\ufeff中文\r\nold\r\n".encode("utf-8")
    route = respx.get(f"{API}/repos/owner/repo-1/contents/README.md").mock(
        return_value=httpx.Response(
            200,
            json={
                "type": "file",
                "encoding": "base64",
                "size": len(source),
                "name": "README.md",
                "path": "README.md",
                "content": base64.b64encode(source).decode("ascii"),
                "sha": "blob-1",
                "url": f"{API}/repos/owner/repo-1/contents/README.md",
                "html_url": "https://gitee.com/owner/repo-1/blob/main/README.md",
            },
        )
    )

    snapshot = GiteeClient(TOKEN).read_readme(repository())

    assert snapshot == FileSnapshot("README.md", "main", source.decode("utf-8"), "blob-1")
    assert route.calls[0].request.url.params["ref"] == "main"


@respx.mock
def test_update_readme_sends_preview_sha_and_encoded_content():
    snapshot = FileSnapshot("README.md", "main", "old\n", "blob-1")
    route = respx.put(f"{API}/repos/owner/repo-1/contents/README.md").mock(
        return_value=httpx.Response(
            200,
            json={
                "content": {"path": "README.md", "sha": "blob-2"},
                "commit": {
                    "sha": "commit-1",
                    "html_url": "https://gitee.com/owner/repo-1/commit/commit-1",
                },
            },
        )
    )

    result = GiteeClient(TOKEN).update_readme(repository(), snapshot, "中文\r\nnew", "docs: update")

    assert result.sha == "commit-1"
    assert result.url == "https://gitee.com/owner/repo-1/commit/commit-1"
    form = parse_qs(route.calls[0].request.content.decode("utf-8"))
    assert form["sha"] == ["blob-1"]
    assert form["message"] == ["docs: update"]
    assert form["branch"] == ["main"]
    assert base64.b64decode(form["content"][0]).decode("utf-8") == "中文\r\nnew"


@respx.mock
def test_update_accepts_api_commit_url_and_builds_web_link():
    snapshot = FileSnapshot("README.md", "main", "old\n", "blob-1")
    respx.put(f"{API}/repos/owner/repo-1/contents/README.md").mock(
        return_value=httpx.Response(
            200,
            json={
                "content": {"path": "README.md", "sha": "blob-2"},
                "commit": {
                    "sha": "commit-1",
                    "url": f"{API}/repos/owner/repo-1/commits/commit-1",
                },
            },
        )
    )

    result = GiteeClient(TOKEN).update_readme(repository(), snapshot, "new", "docs: update")

    assert result.sha == "commit-1"
    assert result.url == "https://gitee.com/owner/repo-1/commit/commit-1"


@respx.mock
def test_update_http_400_is_a_sha_conflict():
    snapshot = FileSnapshot("README.md", "main", "old", "stale-sha")
    respx.put(f"{API}/repos/owner/repo-1/contents/README.md").mock(
        return_value=httpx.Response(400, json={"message": "invalid sha"})
    )

    with pytest.raises(PlatformError) as caught:
        GiteeClient(TOKEN).update_readme(repository(), snapshot, "new", "docs: update")

    assert caught.value.kind is ErrorKind.CONFLICT


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, ErrorKind.AUTH),
        (403, ErrorKind.PERMISSION),
        (404, ErrorKind.NOT_FOUND),
        (409, ErrorKind.CONFLICT),
        (429, ErrorKind.RATE_LIMIT),
        (500, ErrorKind.TEMPORARY),
        (422, ErrorKind.REQUEST),
    ],
)
@respx.mock
def test_http_errors_are_mapped(status, kind):
    respx.get(f"{API}/repos/owner/repo-1/contents/README.md").mock(
        return_value=httpx.Response(status, json={"message": "request failed"})
    )

    with pytest.raises(PlatformError) as caught:
        GiteeClient(TOKEN).read_readme(repository())

    assert caught.value.kind is kind


@respx.mock
def test_errors_never_expose_token():
    respx.get(f"{API}/user/repos").mock(
        return_value=httpx.Response(401, json={"message": "bad token"})
    )

    with pytest.raises(PlatformError) as caught:
        GiteeClient(TOKEN).list_repositories()

    message = str(caught.value)
    assert TOKEN not in message
    assert "access_token" not in message
    assert "https://" not in message


@respx.mock
def test_update_errors_never_expose_form_token():
    snapshot = FileSnapshot("README.md", "main", "old", "blob-1")
    respx.put(f"{API}/repos/owner/repo-1/contents/README.md").mock(
        return_value=httpx.Response(403, json={"message": "denied"})
    )

    with pytest.raises(PlatformError) as caught:
        GiteeClient(TOKEN).update_readme(repository(), snapshot, "new", "docs: update")

    assert TOKEN not in str(caught.value)
    assert "access_token" not in str(caught.value)


@respx.mock
def test_network_errors_are_temporary_and_redacted():
    respx.get(f"{API}/user/repos").mock(
        side_effect=httpx.ConnectError("connection failed")
    )

    with pytest.raises(PlatformError) as caught:
        GiteeClient(TOKEN).list_repositories()

    assert caught.value.kind is ErrorKind.TEMPORARY
    assert TOKEN not in str(caught.value)


@respx.mock
def test_list_repositories_reads_display_name_and_private():
    payload = repo_payload(1)
    payload["name"] = "显示名称"
    payload["private"] = True
    respx.get(f"{API}/user/repos").mock(return_value=httpx.Response(200, json=[payload]))

    repos = GiteeClient(TOKEN).list_repositories()

    assert repos[0].name == "repo-1"
    assert repos[0].display_name == "显示名称"
    assert repos[0].private is True


def test_repository_defaults_to_gitee_and_client_declares_capabilities():
    capabilities = GiteeClient(TOKEN).capabilities

    assert repository().platform.value == "gitee"
    assert capabilities.display_name is True
    assert capabilities.visibility is True
    assert capabilities.repository_topics is True
    assert capabilities.upload_mode == "single_commit"


@respx.mock
def test_update_repository_settings_sends_name_and_private_without_path():
    route = respx.patch(f"{API}/repos/owner/repo-1").mock(
        return_value=httpx.Response(200, json={})
    )

    GiteeClient(TOKEN).update_repository_settings(repository(), "新名称", False)

    form = parse_qs(route.calls[0].request.content.decode("utf-8"))
    assert form["name"] == ["新名称"]
    assert form["private"] == ["false"]
    assert "path" not in form


@respx.mock
def test_visibility_only_update_sends_current_display_name():
    route = respx.patch(f"{API}/repos/owner/repo-1").mock(
        return_value=httpx.Response(200, json={})
    )
    repo = replace(repository(), display_name="显示名称")

    GiteeClient(TOKEN).update_repository_settings(repo, None, True)

    form = parse_qs(route.calls[0].request.content.decode("utf-8"))
    assert form["name"] == ["显示名称"]
    assert form["private"] == ["true"]


@respx.mock
def test_add_repository_topics_posts_json_array():
    route = respx.post(f"{API}/repos/owner/repo-1/project_labels").mock(
        return_value=httpx.Response(200, json=[])
    )

    GiteeClient(TOKEN).add_repository_topics(repository(), ("Java", "Spring Boot"))

    assert json.loads(route.calls[0].request.content) == ["Java", "Spring Boot"]


def content_payload(path, content):
    return {
        "type": "file",
        "encoding": "base64",
        "size": len(content),
        "name": path.rsplit("/", 1)[-1],
        "path": path,
        "content": base64.b64encode(content).decode("ascii"),
        "sha": "blob-1",
        "url": f"{API}/repos/owner/repo-1/contents/{path}",
        "html_url": f"https://gitee.com/owner/repo-1/blob/main/{path}",
    }


@respx.mock
def test_upload_files_creates_one_commit_with_binary_base64_and_encoded_paths():
    files = (
        UploadFile("photo/图片.png", b"\x00\xff"),
        UploadFile("photo/data.bin", b"\x01\x00"),
    )
    encoded_route = respx.get(f"{API}/repos/owner/repo-1/contents/photo/图片.png").mock(
        return_value=httpx.Response(404)
    )
    respx.get(f"{API}/repos/owner/repo-1/contents/photo/data.bin").mock(
        return_value=httpx.Response(404)
    )
    commit_route = respx.post(f"{API}/repos/owner/repo-1/commits").mock(
        return_value=httpx.Response(
            200, json={"sha": "commit-1", "html_url": "https://example/commit/1"}
        )
    )

    result = GiteeClient(TOKEN).upload_files(repository(), files, False, "chore: upload")

    assert result.commit_info.sha == "commit-1"
    assert result.commit_info.url == "https://example/commit/1"
    assert result.reconciled is False
    assert len(commit_route.calls) == 1
    payload = json.loads(commit_route.calls[0].request.content)
    assert payload["branch"] == "main"
    assert payload["message"] == "chore: upload"
    assert payload["actions"] == [
        {
            "action": "create",
            "path": "photo/图片.png",
            "content": base64.b64encode(b"\x00\xff").decode("ascii"),
            "encoding": "base64",
        },
        {
            "action": "create",
            "path": "photo/data.bin",
            "content": base64.b64encode(b"\x01\x00").decode("ascii"),
            "encoding": "base64",
        },
    ]
    assert "%E5%9B%BE%E7%89%87.png" in str(encoded_route.calls[0].request.url)


@respx.mock
def test_upload_files_conflict_does_not_commit():
    respx.get(f"{API}/repos/owner/repo-1/contents/photo/data.bin").mock(
        return_value=httpx.Response(200, json=content_payload("photo/data.bin", b"old"))
    )
    commit_route = respx.post(f"{API}/repos/owner/repo-1/commits").mock(
        return_value=httpx.Response(200, json={"sha": "commit-1"})
    )

    with pytest.raises(PlatformError) as caught:
        GiteeClient(TOKEN).upload_files(
            repository(), (UploadFile("photo/data.bin", b"new"),), False, "chore: upload"
        )

    assert caught.value.kind is ErrorKind.CONFLICT
    assert not commit_route.called


@respx.mock
def test_upload_files_overwrite_uses_update_and_create():
    files = (
        UploadFile("photo/existing.bin", b"new"),
        UploadFile("photo/new.bin", b"\x00\xff"),
    )
    respx.get(f"{API}/repos/owner/repo-1/contents/photo/existing.bin").mock(
        return_value=httpx.Response(200, json=content_payload("photo/existing.bin", b"old"))
    )
    respx.get(f"{API}/repos/owner/repo-1/contents/photo/new.bin").mock(
        return_value=httpx.Response(404)
    )
    commit_route = respx.post(f"{API}/repos/owner/repo-1/commits").mock(
        return_value=httpx.Response(200, json={"sha": "commit-1"})
    )

    GiteeClient(TOKEN).upload_files(repository(), files, True, "chore: upload")

    actions = json.loads(commit_route.calls[0].request.content)["actions"]
    assert [action["action"] for action in actions] == ["update", "create"]
    assert len(commit_route.calls) == 1


@respx.mock
def test_lost_upload_response_reconciles_matching_bytes_without_second_commit():
    content = b"\x00\xff"
    respx.get(f"{API}/repos/owner/repo-1/contents/photo/data.bin").mock(
        side_effect=[
            httpx.Response(404),
            httpx.Response(200, json=content_payload("photo/data.bin", content)),
        ]
    )
    commit_route = respx.post(f"{API}/repos/owner/repo-1/commits").mock(
        return_value=httpx.Response(500)
    )

    result = GiteeClient(TOKEN).upload_files(
        repository(), (UploadFile("photo/data.bin", content),), False, "chore: upload"
    )

    assert result.commit_info is None
    assert result.reconciled is True
    assert len(commit_route.calls) == 1


@respx.mock
def test_lost_upload_response_with_different_content_remains_temporary():
    respx.get(f"{API}/repos/owner/repo-1/contents/photo/data.bin").mock(
        side_effect=[
            httpx.Response(404),
            httpx.Response(200, json=content_payload("photo/data.bin", b"different")),
        ]
    )
    commit_route = respx.post(f"{API}/repos/owner/repo-1/commits").mock(
        return_value=httpx.Response(500)
    )

    with pytest.raises(PlatformError) as caught:
        GiteeClient(TOKEN).upload_files(
            repository(), (UploadFile("photo/data.bin", b"new"),), False, "chore: upload"
        )

    assert caught.value.kind is ErrorKind.TEMPORARY
    assert len(commit_route.calls) == 1


@pytest.mark.parametrize("overwrite", [False, True])
@respx.mock
def test_second_attempt_reconciles_before_post_or_conflict(overwrite):
    content = b"\x00\xff"
    respx.get(f"{API}/repos/owner/repo-1/contents/photo/data.bin").mock(
        side_effect=[
            httpx.Response(404),
            httpx.Response(500),
            httpx.Response(200, json=content_payload("photo/data.bin", content)),
        ]
    )
    commit_route = respx.post(f"{API}/repos/owner/repo-1/commits").mock(
        return_value=httpx.Response(500)
    )
    client = GiteeClient(TOKEN)
    executor = RepositoryExecutor(max_workers=1, max_attempts=2, sleeper=lambda _: None)
    succeeded = []
    failed = []
    done = threading.Event()

    executor.submit(
        [repository()],
        lambda repo, attempt: client.upload_files(
            repo,
            (UploadFile("photo/data.bin", content),),
            overwrite,
            "chore: upload",
            reconcile_before=attempt > 0,
        ),
        lambda repo, result: (succeeded.append(result), done.set()),
        lambda repo, error, retryable: (failed.append(error), done.set()),
    )

    assert done.wait(3)
    assert failed == []
    assert succeeded[0].reconciled is True
    assert len(commit_route.calls) == 1


@respx.mock
def test_upload_preflight_conflict_does_not_decode_remote_content():
    respx.get(f"{API}/repos/owner/repo-1/contents/photo/data.bin").mock(
        return_value=httpx.Response(200, json={"type": "file", "content": None})
    )
    commit_route = respx.post(f"{API}/repos/owner/repo-1/commits").mock(
        return_value=httpx.Response(200, json={"sha": "commit-1"})
    )

    with pytest.raises(PlatformError) as caught:
        GiteeClient(TOKEN).upload_files(
            repository(), (UploadFile("photo/data.bin", b"new"),), False, "chore: upload"
        )

    assert caught.value.kind is ErrorKind.CONFLICT
    assert not commit_route.called


@respx.mock
def test_upload_overwrite_preflight_does_not_require_content_field():
    respx.get(f"{API}/repos/owner/repo-1/contents/photo/data.bin").mock(
        return_value=httpx.Response(200, json={"type": "file"})
    )
    commit_route = respx.post(f"{API}/repos/owner/repo-1/commits").mock(
        return_value=httpx.Response(200, json={"sha": "commit-1"})
    )

    result = GiteeClient(TOKEN).upload_files(
        repository(), (UploadFile("photo/data.bin", b"new"),), True, "chore: upload"
    )

    assert result.commit_info.sha == "commit-1"
    assert commit_route.call_count == 1


@respx.mock
def test_malformed_remote_base64_does_not_reconcile_empty_upload():
    malformed = content_payload("photo/data.bin", b"")
    malformed["content"] = "!!!"
    respx.get(f"{API}/repos/owner/repo-1/contents/photo/data.bin").mock(
        side_effect=[httpx.Response(404), httpx.Response(200, json=malformed)]
    )
    commit_route = respx.post(f"{API}/repos/owner/repo-1/commits").mock(
        return_value=httpx.Response(500)
    )

    with pytest.raises(PlatformError) as caught:
        GiteeClient(TOKEN).upload_files(
            repository(), (UploadFile("photo/data.bin", b""),), False, "chore: upload"
        )

    assert caught.value.kind is ErrorKind.TEMPORARY
    assert commit_route.call_count == 1


@respx.mock
def test_upload_empty_list_means_file_does_not_exist():
    respx.get(f"{API}/repos/owner/repo-1/contents/photo/new.png").mock(
        return_value=httpx.Response(200, json=[])
    )
    commit_route = respx.post(f"{API}/repos/owner/repo-1/commits").mock(
        return_value=httpx.Response(200, json={"sha": "commit-1"})
    )

    result = GiteeClient(TOKEN).upload_files(
        repository(), (UploadFile("photo/new.png", b"image"),), False, "chore: upload"
    )

    assert result.commit_info.sha == "commit-1"
    actions = json.loads(commit_route.calls[0].request.content)["actions"]
    assert actions[0]["action"] == "create"


@respx.mock
def test_upload_directory_path_is_rejected_as_not_a_file():
    respx.get(f"{API}/repos/owner/repo-1/contents/photo").mock(
        return_value=httpx.Response(200, json=[{"type": "file", "name": "child.png"}])
    )
    commit_route = respx.post(f"{API}/repos/owner/repo-1/commits").mock(
        return_value=httpx.Response(200, json={"sha": "commit-1"})
    )

    with pytest.raises(PlatformError) as caught:
        GiteeClient(TOKEN).upload_files(
            repository(), (UploadFile("photo", b"image"),), False, "chore: upload"
        )

    assert caught.value.kind is ErrorKind.CONFLICT
    assert str(caught.value) == "目标路径不是普通文件"
    assert not commit_route.called


from platform_contract import RepositoryPlatformContract


class TestGiteeContract(RepositoryPlatformContract):
    API = API
    TOKEN = TOKEN
    PLATFORM = "gitee"

    def _client(self):
        return GiteeClient(self.TOKEN)

    def _repo_payload(self, number):
        return repo_payload(number)

    def _repository(self):
        return repository()
