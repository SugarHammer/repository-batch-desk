import base64
import json

import httpx
import pytest
import respx

from platform_contract import RepositoryPlatformContract
from readme_batch.models import FileSnapshot, PlatformKind, Repository
from readme_batch.platforms.base import ErrorKind, PlatformError
from readme_batch.platforms.github import GitHubClient

API = "https://api.github.com"
TOKEN = "github-test-token"


def repo_payload(number):
    return {
        "id": number,
        "name": f"repo-{number}",
        "full_name": f"owner/repo-{number}",
        "owner": {"login": "owner"},
        "default_branch": "main",
        "private": False,
        "html_url": f"https://github.com/owner/repo-{number}",
    }


def repository():
    return Repository(
        "owner",
        "repo-1",
        "owner/repo-1",
        "main",
        "https://github.com/owner/repo-1",
        platform=PlatformKind.GITHUB,
    )


class TestGitHubContract(RepositoryPlatformContract):
    API = API
    TOKEN = TOKEN
    PLATFORM = "github"

    def _client(self):
        return GitHubClient(self.TOKEN)

    def _repo_payload(self, number):
        return repo_payload(number)

    def _repository(self):
        return repository()


@respx.mock
def test_github_authentication_accept_and_version_headers():
    route = respx.get(f"{API}/user/repos").mock(return_value=httpx.Response(200, json=[]))

    GitHubClient(TOKEN).list_repositories()

    headers = route.calls[0].request.headers
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["accept"] == "application/vnd.github+json"
    assert headers["x-github-api-version"] == "2026-03-10"
    assert "access_token" not in str(route.calls[0].request.url)


def test_github_capabilities_enable_git_data_upload():
    capabilities = GitHubClient(TOKEN).capabilities

    assert capabilities.display_name is False
    assert capabilities.visibility is True
    assert capabilities.repository_topics is True
    assert capabilities.upload_mode == "single_commit"


@respx.mock
def test_update_readme_sends_json_content_branch_and_message():
    snapshot = FileSnapshot("README.md", "main", "old", "blob-1")
    route = respx.put(f"{API}/repos/owner/repo-1/contents/README.md").mock(
        return_value=httpx.Response(
            200,
            json={"content": {}, "commit": {"sha": "commit-1", "html_url": "https://example/1"}},
        )
    )

    GitHubClient(TOKEN).update_readme(repository(), snapshot, "中文\r\nnew", "docs: update")

    payload = json.loads(route.calls[0].request.content)
    assert base64.b64decode(payload["content"]).decode("utf-8") == "中文\r\nnew"
    assert payload["sha"] == "blob-1"
    assert payload["branch"] == "main"
    assert payload["message"] == "docs: update"


@pytest.mark.parametrize(
    ("status", "headers", "kind"),
    [
        (401, {}, ErrorKind.AUTH),
        (403, {}, ErrorKind.PERMISSION),
        (404, {}, ErrorKind.NOT_FOUND),
        (409, {}, ErrorKind.CONFLICT),
        (429, {}, ErrorKind.RATE_LIMIT),
        (500, {}, ErrorKind.TEMPORARY),
        (422, {}, ErrorKind.REQUEST),
        (403, {"x-ratelimit-remaining": "0"}, ErrorKind.RATE_LIMIT),
    ],
)
@respx.mock
def test_github_errors_are_mapped(status, headers, kind):
    respx.get(f"{API}/user/repos").mock(
        return_value=httpx.Response(status, headers=headers, json={"message": "denied"})
    )

    with pytest.raises(PlatformError) as caught:
        GitHubClient(TOKEN).list_repositories()

    assert caught.value.kind is kind


@respx.mock
def test_github_rate_limit_uses_retry_after_and_reset_clock():
    respx.get(f"{API}/user/repos").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "12"}),
            httpx.Response(429, headers={"x-ratelimit-reset": "1000"}),
        ]
    )
    client = GitHubClient(TOKEN, clock=lambda: 900.0)

    with pytest.raises(PlatformError) as first:
        client.list_repositories()
    with pytest.raises(PlatformError) as second:
        client.list_repositories()

    assert first.value.retry_after == 12
    assert second.value.retry_after == 100


@respx.mock
def test_github_not_found_mentions_hidden_resources_without_leaking_token():
    respx.get(f"{API}/user/repos").mock(return_value=httpx.Response(404))

    with pytest.raises(PlatformError) as caught:
        GitHubClient(TOKEN).list_repositories()

    assert "or the current token cannot access it" in str(caught.value)
    assert TOKEN not in str(caught.value)


@respx.mock
def test_visibility_update_sends_only_private_field():
    route = respx.patch(f"{API}/repos/owner/repo-1").mock(
        return_value=httpx.Response(200, json={})
    )

    GitHubClient(TOKEN).update_repository_settings(repository(), None, True)

    assert json.loads(route.calls[0].request.content) == {"private": True}


def test_display_name_update_is_rejected_without_request():
    with pytest.raises(PlatformError) as caught:
        GitHubClient(TOKEN).update_repository_settings(repository(), "renamed", None)

    assert caught.value.kind is ErrorKind.UNSUPPORTED


@respx.mock
def test_topics_are_merged_without_duplicates_or_issue_label_calls():
    get_route = respx.get(f"{API}/repos/owner/repo-1/topics").mock(
        return_value=httpx.Response(200, json={"names": ["existing", "new-topic"]})
    )
    put_route = respx.put(f"{API}/repos/owner/repo-1/topics").mock(
        return_value=httpx.Response(200, json={"names": ["existing", "new-topic", "other"]})
    )
    labels_route = respx.post(f"{API}/repos/owner/repo-1/labels").mock(
        return_value=httpx.Response(200, json={})
    )

    GitHubClient(TOKEN).add_repository_topics(
        repository(), ("new-topic", "other", "new-topic")
    )

    assert get_route.called
    assert json.loads(put_route.calls[0].request.content) == {
        "names": ["existing", "new-topic", "other"]
    }
    assert not labels_route.called


@respx.mock
def test_topics_read_failure_does_not_replace_topics():
    respx.get(f"{API}/repos/owner/repo-1/topics").mock(
        return_value=httpx.Response(403, json={"message": "denied"})
    )
    put_route = respx.put(f"{API}/repos/owner/repo-1/topics").mock(
        return_value=httpx.Response(200, json={})
    )

    with pytest.raises(PlatformError):
        GitHubClient(TOKEN).add_repository_topics(repository(), ("new-topic",))

    assert not put_route.called


@respx.mock
def test_topics_merged_limit_rejects_before_put():
    respx.get(f"{API}/repos/owner/repo-1/topics").mock(
        return_value=httpx.Response(200, json={"names": [f"topic-{n}" for n in range(20)]})
    )
    put_route = respx.put(f"{API}/repos/owner/repo-1/topics").mock(
        return_value=httpx.Response(200, json={})
    )

    with pytest.raises(PlatformError) as caught:
        GitHubClient(TOKEN).add_repository_topics(repository(), ("new-topic",))

    assert caught.value.kind is ErrorKind.REQUEST
    assert not put_route.called


@pytest.mark.parametrize(
    "labels",
    [
        ("Uppercase",),
        ("has space",),
        ("中文",),
        ("-leading",),
        ("a" * 51,),
        tuple(f"topic-{n}" for n in range(21)),
    ],
)
def test_invalid_topics_are_rejected(labels):
    with pytest.raises(PlatformError) as caught:
        GitHubClient(TOKEN).validate_topics(labels)

    assert caught.value.kind is ErrorKind.REQUEST


import threading

from readme_batch.execution import RepositoryExecutor
from readme_batch.models import UploadFile


def content_payload(path, content):
    return {
        "type": "file",
        "encoding": "base64",
        "path": path,
        "content": base64.b64encode(content).decode("ascii"),
        "sha": "blob-existing",
    }


def mock_git_data(head="head-1", ref_response=None):
    routes = {
        "head": respx.get(f"{API}/repos/owner/repo-1/git/ref/heads/main").mock(
            return_value=httpx.Response(200, json={"object": {"sha": head}})
        ),
        "head_commit": respx.get(f"{API}/repos/owner/repo-1/git/commits/{head}").mock(
            return_value=httpx.Response(200, json={"tree": {"sha": "base-tree"}})
        ),
        "blobs": respx.post(f"{API}/repos/owner/repo-1/git/blobs").mock(
            side_effect=[
                httpx.Response(201, json={"sha": "blob-1"}),
                httpx.Response(201, json={"sha": "blob-2"}),
            ]
        ),
        "trees": respx.post(f"{API}/repos/owner/repo-1/git/trees").mock(
            return_value=httpx.Response(201, json={"sha": "new-tree"})
        ),
        "commits": respx.post(f"{API}/repos/owner/repo-1/git/commits").mock(
            return_value=httpx.Response(
                201,
                json={"sha": "commit-1", "html_url": "https://example/commit/1"},
            )
        ),
        "ref": respx.patch(f"{API}/repos/owner/repo-1/git/refs/heads/main").mock(
            return_value=ref_response or httpx.Response(200, json={"object": {"sha": "commit-1"}})
        ),
    }
    return routes


@respx.mock
def test_upload_files_creates_one_commit_with_git_data():
    files = (UploadFile("photo/图片.png", b"\x00\xff"), UploadFile("photo/data.bin", b"\x01\x00"))
    respx.get(f"{API}/repos/owner/repo-1/contents/photo/图片.png").mock(
        return_value=httpx.Response(404)
    )
    respx.get(f"{API}/repos/owner/repo-1/contents/photo/data.bin").mock(
        return_value=httpx.Response(404)
    )
    routes = mock_git_data()

    result = GitHubClient(TOKEN).upload_files(repository(), files, False, "chore: upload")

    assert result.commit_info.sha == "commit-1"
    assert result.commit_info.url == "https://example/commit/1"
    blob_payloads = [json.loads(call.request.content) for call in routes["blobs"].calls]
    assert [base64.b64decode(payload["content"]) for payload in blob_payloads] == [
        b"\x00\xff",
        b"\x01\x00",
    ]
    assert all(payload["encoding"] == "base64" for payload in blob_payloads)
    tree = json.loads(routes["trees"].calls[0].request.content)
    assert tree["base_tree"] == "base-tree"
    assert [entry["path"] for entry in tree["tree"]] == [file.path for file in files]
    commit = json.loads(routes["commits"].calls[0].request.content)
    assert commit["parents"] == ["head-1"]
    assert commit["message"] == "chore: upload"
    ref = json.loads(routes["ref"].calls[0].request.content)
    assert ref == {"sha": "commit-1", "force": False}
    assert routes["ref"].call_count == 1


@respx.mock
def test_upload_conflict_creates_no_git_objects_or_ref_update():
    respx.get(f"{API}/repos/owner/repo-1/contents/photo/图片.png").mock(
        return_value=httpx.Response(200, json=content_payload("photo/图片.png", b"old"))
    )
    respx.get(f"{API}/repos/owner/repo-1/contents/photo/data.bin").mock(
        return_value=httpx.Response(404)
    )
    routes = mock_git_data()

    with pytest.raises(PlatformError) as caught:
        GitHubClient(TOKEN).upload_files(
            repository(),
            (UploadFile("photo/图片.png", b"new"), UploadFile("photo/data.bin", b"new")),
            False,
            "chore: upload",
        )

    assert caught.value.kind is ErrorKind.CONFLICT
    assert not routes["blobs"].called
    assert not routes["trees"].called
    assert not routes["commits"].called
    assert not routes["ref"].called


@respx.mock
def test_upload_overwrite_replaces_existing_and_new_paths_in_one_tree():
    files = (UploadFile("photo/existing.bin", b"new"), UploadFile("photo/new.bin", b"\x00\xff"))
    respx.get(f"{API}/repos/owner/repo-1/contents/photo/existing.bin").mock(
        return_value=httpx.Response(200, json=content_payload("photo/existing.bin", b"old"))
    )
    respx.get(f"{API}/repos/owner/repo-1/contents/photo/new.bin").mock(
        return_value=httpx.Response(404)
    )
    routes = mock_git_data()

    result = GitHubClient(TOKEN).upload_files(repository(), files, True, "chore: upload")

    assert result.commit_info.sha == "commit-1"
    tree = json.loads(routes["trees"].calls[0].request.content)
    assert [entry["path"] for entry in tree["tree"]] == [file.path for file in files]
    assert routes["ref"].call_count == 1


@respx.mock
def test_upload_head_conflict_is_not_forced_or_reconciled_with_different_content():
    path = "photo/data.bin"
    respx.get(f"{API}/repos/owner/repo-1/contents/{path}").mock(
        side_effect=[
            httpx.Response(404),
            httpx.Response(200, json=content_payload(path, b"different")),
        ]
    )
    routes = mock_git_data(ref_response=httpx.Response(409))

    with pytest.raises(PlatformError) as caught:
        GitHubClient(TOKEN).upload_files(
            repository(), (UploadFile(path, b"new"),), False, "chore: upload"
        )

    assert caught.value.kind is ErrorKind.CONFLICT
    assert json.loads(routes["ref"].calls[0].request.content)["force"] is False
    assert routes["ref"].call_count == 1


@respx.mock
def test_lost_ref_response_reconciles_without_second_commit():
    path = "photo/data.bin"
    content = b"\x00\xff"
    respx.get(f"{API}/repos/owner/repo-1/contents/{path}").mock(
        side_effect=[
            httpx.Response(404),
            httpx.Response(200, json=content_payload(path, content)),
        ]
    )
    routes = mock_git_data(ref_response=httpx.Response(500))

    result = GitHubClient(TOKEN).upload_files(
        repository(), (UploadFile(path, content),), False, "chore: upload"
    )

    assert result.commit_info is None
    assert result.reconciled is True
    assert routes["commits"].call_count == 1
    assert routes["ref"].call_count == 1


@respx.mock
def test_retry_rereads_head_after_uncertain_ref_update():
    path = "photo/data.bin"
    content = b"new"
    respx.get(f"{API}/repos/owner/repo-1/contents/{path}").mock(
        side_effect=[
            httpx.Response(404),
            httpx.Response(200, json=content_payload(path, b"different")),
            httpx.Response(200, json=content_payload(path, b"different")),
            httpx.Response(404),
        ]
    )
    respx.get(f"{API}/repos/owner/repo-1/git/ref/heads/main").mock(
        side_effect=[
            httpx.Response(200, json={"object": {"sha": "head-1"}}),
            httpx.Response(200, json={"object": {"sha": "head-2"}}),
        ]
    )
    respx.get(f"{API}/repos/owner/repo-1/git/commits/head-1").mock(
        return_value=httpx.Response(200, json={"tree": {"sha": "tree-1"}})
    )
    respx.get(f"{API}/repos/owner/repo-1/git/commits/head-2").mock(
        return_value=httpx.Response(200, json={"tree": {"sha": "tree-2"}})
    )
    respx.post(f"{API}/repos/owner/repo-1/git/blobs").mock(
        side_effect=[
            httpx.Response(201, json={"sha": "blob-1"}),
            httpx.Response(201, json={"sha": "blob-2"}),
        ]
    )
    respx.post(f"{API}/repos/owner/repo-1/git/trees").mock(
        side_effect=[
            httpx.Response(201, json={"sha": "new-tree-1"}),
            httpx.Response(201, json={"sha": "new-tree-2"}),
        ]
    )
    commit_route = respx.post(f"{API}/repos/owner/repo-1/git/commits").mock(
        side_effect=[
            httpx.Response(201, json={"sha": "commit-1", "html_url": "https://example/1"}),
            httpx.Response(201, json={"sha": "commit-2", "html_url": "https://example/2"}),
        ]
    )
    ref_route = respx.patch(f"{API}/repos/owner/repo-1/git/refs/heads/main").mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json={"object": {"sha": "commit-2"}})]
    )
    client = GitHubClient(TOKEN)
    executor = RepositoryExecutor(max_workers=1, max_attempts=2, sleeper=lambda _: None)
    succeeded = []
    failed = []
    done = threading.Event()

    executor.submit(
        [repository()],
        lambda repo, attempt: client.upload_files(
            repo,
            (UploadFile(path, content),),
            False,
            "chore: upload",
            reconcile_before=attempt > 0,
        ),
        lambda repo, result: (succeeded.append(result), done.set()),
        lambda repo, error, retryable: (failed.append(error), done.set()),
    )

    assert done.wait(3)
    assert failed == []
    assert succeeded[0].commit_info.sha == "commit-2"
    parents = [json.loads(call.request.content)["parents"] for call in commit_route.calls]
    assert parents == [["head-1"], ["head-2"]]
    assert ref_route.call_count == 2


@respx.mock
def test_no_overwrite_preflight_uses_same_head_as_commit_parent():
    path = "photo/data.bin"
    head_route = respx.get(f"{API}/repos/owner/repo-1/git/ref/heads/main").mock(
        return_value=httpx.Response(200, json={"object": {"sha": "head-1"}})
    )
    content_route = respx.get(f"{API}/repos/owner/repo-1/contents/{path}").mock(
        return_value=httpx.Response(200, json=content_payload(path, b"concurrent"))
    )
    routes = mock_git_data()

    with pytest.raises(PlatformError) as caught:
        GitHubClient(TOKEN).upload_files(
            repository(), (UploadFile(path, b"new"),), False, "chore: upload"
        )

    assert caught.value.kind is ErrorKind.CONFLICT
    assert head_route.called
    assert content_route.calls[0].request.url.params["ref"] == "head-1"
    assert not routes["blobs"].called
    assert not routes["commits"].called
    assert not routes["ref"].called


@respx.mock
def test_large_readme_falls_back_to_raw_content_and_preserves_sha():
    source = ("中文\r\n" * 400_000).encode("utf-8")
    route = respx.get(f"{API}/repos/owner/repo-1/contents/README.md").mock(
        side_effect=[
            httpx.Response(
                200,
                json={"type": "file", "encoding": "none", "content": "", "sha": "blob-large"},
            ),
            httpx.Response(200, content=source),
        ]
    )

    snapshot = GitHubClient(TOKEN).read_readme(repository())

    assert snapshot.content == source.decode("utf-8")
    assert snapshot.sha == "blob-large"
    assert route.call_count == 2
    assert route.calls[1].request.headers["accept"] == "application/vnd.github.raw+json"


@respx.mock
def test_large_upload_lost_ref_response_reconciles_raw_bytes_without_second_commit():
    path = "photo/large.bin"
    content = b"\x00\xff" * (600_000)
    respx.get(f"{API}/repos/owner/repo-1/contents/{path}").mock(
        side_effect=[
            httpx.Response(404),
            httpx.Response(200, json={"type": "file", "encoding": "none", "content": "", "sha": "blob-large"}),
            httpx.Response(200, content=content),
        ]
    )
    routes = mock_git_data(ref_response=httpx.Response(500))

    result = GitHubClient(TOKEN).upload_files(
        repository(), (UploadFile(path, content),), False, "chore: upload"
    )

    assert result.reconciled is True
    assert result.commit_info is None
    assert routes["commits"].call_count == 1
    assert routes["ref"].call_count == 1


@respx.mock
def test_secondary_rate_limit_message_without_headers_is_retryable():
    respx.get(f"{API}/user/repos").mock(
        return_value=httpx.Response(
            403,
            json={"message": "You have exceeded a secondary rate limit. Please wait a few minutes."},
        )
    )

    with pytest.raises(PlatformError) as caught:
        GitHubClient(TOKEN).list_repositories()

    assert caught.value.kind is ErrorKind.RATE_LIMIT
    assert caught.value.retry_after == 60
    assert "secondary rate limit" not in str(caught.value).lower()


@respx.mock
def test_ref_unprocessable_entity_is_a_conflict_and_checks_remote():
    path = "photo/data.bin"
    respx.get(f"{API}/repos/owner/repo-1/contents/{path}").mock(
        side_effect=[
            httpx.Response(404),
            httpx.Response(200, json=content_payload(path, b"different")),
        ]
    )
    routes = mock_git_data(ref_response=httpx.Response(422))

    with pytest.raises(PlatformError) as caught:
        GitHubClient(TOKEN).upload_files(
            repository(), (UploadFile(path, b"new"),), False, "chore: upload"
        )

    assert caught.value.kind is ErrorKind.CONFLICT
    assert json.loads(routes["ref"].calls[0].request.content)["force"] is False
