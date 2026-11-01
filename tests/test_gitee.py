import base64
import json
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from readme_batch.models import FileSnapshot, Repository
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

    GiteeClient(TOKEN).update_repository_settings(repository(), "显示名称", True)

    form = parse_qs(route.calls[0].request.content.decode("utf-8"))
    assert form["name"] == ["显示名称"]
    assert form["private"] == ["true"]


@respx.mock
def test_add_project_labels_posts_json_array():
    route = respx.post(f"{API}/repos/owner/repo-1/project_labels").mock(
        return_value=httpx.Response(200, json=[])
    )

    GiteeClient(TOKEN).add_project_labels(repository(), ("Java", "Spring Boot"))

    assert json.loads(route.calls[0].request.content) == ["Java", "Spring Boot"]
