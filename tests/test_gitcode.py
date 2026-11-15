import base64
import json

import httpx
import pytest
import respx

from platform_contract import RepositoryPlatformContract
from readme_batch.models import FileSnapshot, PlatformKind, Repository
from readme_batch.platforms.base import ErrorKind, PlatformError
from readme_batch.platforms.gitcode import GitCodeClient

API = "https://api.gitcode.com/api/v5"
TOKEN = "gitcode-test-token"


def repo_payload(number):
    return {
        "id": number,
        "path": f"repo-{number}",
        "name": f"Repo {number}",
        "full_name": f"owner/repo-{number}",
        "owner": {"login": "owner"},
        "default_branch": "main",
        "private": False,
        "html_url": f"https://gitcode.com/owner/repo-{number}",
    }


def repository():
    return Repository(
        "owner",
        "repo-1",
        "owner/repo-1",
        "main",
        "https://gitcode.com/owner/repo-1",
        display_name="Repo One",
        platform=PlatformKind.GITCODE,
    )


class TestGitCodeContract(RepositoryPlatformContract):
    API = API
    TOKEN = TOKEN
    PLATFORM = "gitcode"

    def _client(self):
        return GitCodeClient(self.TOKEN)

    def _repo_payload(self, number):
        return repo_payload(number)

    def _repository(self):
        return repository()


@respx.mock
def test_gitcode_uses_bearer_without_url_token():
    route = respx.get(f"{API}/user/repos").mock(return_value=httpx.Response(200, json=[]))

    GitCodeClient(TOKEN).list_repositories()

    request = route.calls[0].request
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["accept"] == "application/json"
    assert "access_token" not in str(request.url)


def test_gitcode_capabilities_disable_unverified_upload():
    capabilities = GitCodeClient(TOKEN).capabilities

    assert capabilities.display_name is True
    assert capabilities.visibility is True
    assert capabilities.repository_topics is True
    assert capabilities.upload_mode == "unsupported"


@respx.mock
def test_update_readme_uses_json_and_builds_commit_url_when_missing():
    snapshot = FileSnapshot("README.md", "main", "old", "blob-1")
    route = respx.put(f"{API}/repos/owner/repo-1/contents/README.md").mock(
        return_value=httpx.Response(
            200,
            json={"content": {"sha": "blob-2"}, "commit": {"sha": "commit-1"}},
        )
    )

    result = GitCodeClient(TOKEN).update_readme(
        repository(), snapshot, "中文\r\nnew", "docs: update"
    )

    assert result.url == "https://gitcode.com/owner/repo-1/commit/commit-1"
    payload = json.loads(route.calls[0].request.content)
    assert base64.b64decode(payload["content"]).decode("utf-8") == "中文\r\nnew"
    assert payload["sha"] == "blob-1"
    assert payload["branch"] == "main"
    assert payload["message"] == "docs: update"


@respx.mock
def test_visibility_only_update_preserves_display_name_and_path():
    route = respx.patch(f"{API}/repos/owner/repo-1").mock(
        return_value=httpx.Response(200, json={})
    )

    GitCodeClient(TOKEN).update_repository_settings(repository(), None, True)

    payload = json.loads(route.calls[0].request.content)
    assert payload == {"name": "Repo One", "private": True}
    assert "path" not in payload


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
def test_gitcode_errors_are_mapped_without_token_leak(status, kind):
    respx.get(f"{API}/user/repos").mock(
        return_value=httpx.Response(status, json={"message": "denied"})
    )

    with pytest.raises(PlatformError) as caught:
        GitCodeClient(TOKEN).list_repositories()

    assert caught.value.kind is kind
    assert TOKEN not in str(caught.value)
    assert "authorization" not in str(caught.value).lower()


from readme_batch.models import UploadFile


@respx.mock
def test_project_labels_preserve_existing_and_deduplicate():
    respx.get(f"{API}/repos/owner/repo-1").mock(
        return_value=httpx.Response(200, json={"tags": ["existing", "new"]})
    )
    put_route = respx.put(f"{API}/repos/owner/repo-1/project_labels").mock(
        return_value=httpx.Response(200, json=[])
    )

    GitCodeClient(TOKEN).add_repository_topics(repository(), ("new", "other", "new"))

    assert put_route.call_count == 1
    assert json.loads(put_route.calls[0].request.content) == ["existing", "new", "other"]


@pytest.mark.parametrize(
    ("status", "payload", "kind"),
    [
        (404, {}, ErrorKind.NOT_FOUND),
        (403, {}, ErrorKind.PERMISSION),
        (200, {}, ErrorKind.REQUEST),
        (200, {"tags": "not-a-list"}, ErrorKind.REQUEST),
        (200, {"tags": ["valid", 1]}, ErrorKind.REQUEST),
    ],
)
@respx.mock
def test_unreliable_project_labels_never_replace_remote(status, payload, kind):
    respx.get(f"{API}/repos/owner/repo-1").mock(
        return_value=httpx.Response(status, json=payload)
    )
    put_route = respx.put(f"{API}/repos/owner/repo-1/project_labels").mock(
        return_value=httpx.Response(200, json=[])
    )

    with pytest.raises(PlatformError) as caught:
        GitCodeClient(TOKEN).add_repository_topics(repository(), ("new",))

    assert caught.value.kind is kind
    assert not put_route.called


@respx.mock
def test_gitcode_upload_is_unsupported_without_any_remote_call():
    route = respx.route()

    with pytest.raises(PlatformError) as caught:
        GitCodeClient(TOKEN).upload_files(
            repository(), (UploadFile("a.txt", b"a"),), False, "chore: upload"
        )

    assert caught.value.kind is ErrorKind.UNSUPPORTED
    assert not route.called


@respx.mock
def test_list_repositories_uses_namespace_when_owner_is_null():
    payload = repo_payload(1)
    payload["owner"] = None
    payload["namespace"] = {"path": "namespace-owner"}
    respx.get(f"{API}/user/repos").mock(return_value=httpx.Response(200, json=[payload]))

    repositories = GitCodeClient(TOKEN).list_repositories()

    assert repositories[0].owner == "namespace-owner"


@respx.mock
def test_list_repositories_uses_full_name_when_owner_and_namespace_are_unavailable():
    payload = repo_payload(1)
    payload["owner"] = None
    payload["full_name"] = "fallback-owner/repo-1"
    respx.get(f"{API}/user/repos").mock(return_value=httpx.Response(200, json=[payload]))

    repositories = GitCodeClient(TOKEN).list_repositories()

    assert repositories[0].owner == "fallback-owner"


@respx.mock
def test_list_repositories_uses_web_url_when_html_url_is_missing():
    payload = repo_payload(1)
    del payload["html_url"]
    payload["web_url"] = "https://gitcode.com/owner/repo-1"
    respx.get(f"{API}/user/repos").mock(return_value=httpx.Response(200, json=[payload]))

    repositories = GitCodeClient(TOKEN).list_repositories()

    assert repositories[0].html_url == "https://gitcode.com/owner/repo-1"


@respx.mock
def test_list_repositories_constructs_web_url_when_url_fields_are_missing():
    payload = repo_payload(1)
    del payload["html_url"]
    respx.get(f"{API}/user/repos").mock(return_value=httpx.Response(200, json=[payload]))

    repositories = GitCodeClient(TOKEN).list_repositories()

    assert repositories[0].html_url == "https://gitcode.com/owner/repo-1"


@respx.mock
def test_project_labels_field_is_preferred_over_legacy_tags():
    respx.get(f"{API}/repos/owner/repo-1").mock(
        return_value=httpx.Response(
            200,
            json={"project_labels": ["existing"], "tags": ["wrong-source"]},
        )
    )
    put_route = respx.put(f"{API}/repos/owner/repo-1/project_labels").mock(
        return_value=httpx.Response(200, json=[])
    )

    GitCodeClient(TOKEN).add_repository_topics(repository(), ("new", "existing"))

    assert json.loads(put_route.calls[0].request.content) == ["existing", "new"]
