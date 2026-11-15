import base64
import json

import httpx
import pytest
import respx

from readme_batch.models import DeleteResult, FileSnapshot, RemoteFile, Repository
from readme_batch.platforms.base import (
    GITEE_CAPABILITIES,
    GITCODE_CAPABILITIES,
    GITHUB_CAPABILITIES,
    ErrorKind,
    PlatformError,
)
from readme_batch.platforms.gitee import GiteeClient

API = "https://gitee.com/api/v5"
TOKEN = "test-secret-token"


def repo_payload(number=1):
    return {
        "id": number,
        "path": f"repo-{number}",
        "name": f"repo-{number}",
        "full_name": f"owner/repo-{number}",
        "default_branch": "main",
        "private": False,
        "description": f"Repository {number} description",
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


def test_only_gitee_declares_file_deletion_capability():
    assert GITEE_CAPABILITIES.file_deletion is True
    assert GITHUB_CAPABILITIES.file_deletion is False
    assert GITCODE_CAPABILITIES.file_deletion is False


@respx.mock
def test_gitee_repository_list_reads_description_and_normalizes_null():
    described = repo_payload(1)
    described["description"] = "项目简介"
    undescribed = repo_payload(2)
    undescribed["description"] = None
    respx.get(f"{API}/user/repos").mock(
        return_value=httpx.Response(200, json=[described, undescribed])
    )

    repos = GiteeClient(TOKEN).list_repositories()

    assert [repo.description for repo in repos] == ["项目简介", ""]


def repository():
    return Repository("owner", "repo-1", "owner/repo-1", "main", "https://gitee.com/owner/repo-1")


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
def test_read_file_returns_path_branch_and_blob_sha():
    respx.get(f"{API}/repos/owner/repo-1/contents/photo/图片.png").mock(
        return_value=httpx.Response(200, json=content_payload("photo/图片.png", b"image"))
    )

    snapshot = GiteeClient(TOKEN).read_file(repository(), "photo/图片.png")

    assert (snapshot.path, snapshot.branch, snapshot.sha) == (
        "photo/图片.png", "main", "blob-1"
    )


@respx.mock
def test_read_file_treats_empty_response_as_missing():
    respx.get(f"{API}/repos/owner/repo-1/contents/missing.png").mock(
        return_value=httpx.Response(200, json=[])
    )

    with pytest.raises(PlatformError) as caught:
        GiteeClient(TOKEN).read_file(repository(), "missing.png")

    assert caught.value.kind is ErrorKind.NOT_FOUND


@respx.mock
def test_delete_file_uses_preview_sha_branch_and_message():
    route = respx.delete(f"{API}/repos/owner/repo-1/contents/photo/data.bin").mock(
        return_value=httpx.Response(
            200,
            json={
                "content": None,
                "commit": {"sha": "commit-1", "html_url": "https://example/commit/1"},
            },
        )
    )
    snapshot = FileSnapshot("photo/data.bin", "main", "", "blob-1")

    result = GiteeClient(TOKEN).delete_file(repository(), snapshot, "chore: delete")

    assert result.commit_info.sha == "commit-1"
    assert result.commit_info.url == "https://example/commit/1"
    assert result.reconciled is False
    params = route.calls[0].request.url.params
    assert params["sha"] == "blob-1"
    assert params["branch"] == "main"
    assert params["message"] == "chore: delete"


@respx.mock
def test_delete_file_rejects_changed_sha_instead_of_deleting_new_version():
    respx.delete(f"{API}/repos/owner/repo-1/contents/photo/data.bin").mock(
        return_value=httpx.Response(400, json={"message": "invalid sha"})
    )
    changed = content_payload("photo/data.bin", b"changed")
    changed["sha"] = "blob-2"
    respx.get(f"{API}/repos/owner/repo-1/contents/photo/data.bin").mock(
        return_value=httpx.Response(200, json=changed)
    )
    snapshot = FileSnapshot("photo/data.bin", "main", "", "blob-1")

    with pytest.raises(PlatformError) as caught:
        GiteeClient(TOKEN).delete_file(repository(), snapshot, "chore: delete")

    assert caught.value.kind is ErrorKind.CONFLICT
    assert "重新检查" in str(caught.value)


@respx.mock
def test_lost_delete_response_reconciles_missing_file_without_second_delete():
    delete_route = respx.delete(f"{API}/repos/owner/repo-1/contents/photo/data.bin").mock(
        return_value=httpx.Response(500)
    )
    respx.get(f"{API}/repos/owner/repo-1/contents/photo/data.bin").mock(
        return_value=httpx.Response(404)
    )
    snapshot = FileSnapshot("photo/data.bin", "main", "", "blob-1")

    result = GiteeClient(TOKEN).delete_file(repository(), snapshot, "chore: delete")

    assert result.commit_info is None
    assert result.reconciled is True
    assert delete_route.call_count == 1


@respx.mock
def test_delete_reconcile_before_skips_missing_and_rejects_changed_file():
    respx.get(f"{API}/repos/owner/repo-1/contents/photo/data.bin").mock(
        side_effect=[
            httpx.Response(404),
            httpx.Response(200, json=content_payload("photo/data.bin", b"changed")),
        ]
    )
    delete_route = respx.delete(f"{API}/repos/owner/repo-1/contents/photo/data.bin").mock(
        return_value=httpx.Response(200, json={})
    )
    client = GiteeClient(TOKEN)
    snapshot = FileSnapshot("photo/data.bin", "main", "", "different-sha")

    result = client.delete_file(repository(), snapshot, "chore: delete", reconcile_before=True)
    assert result.reconciled is True
    with pytest.raises(PlatformError) as caught:
        client.delete_file(repository(), snapshot, "chore: delete", reconcile_before=True)

    assert caught.value.kind is ErrorKind.CONFLICT
    assert not delete_route.called


@pytest.mark.parametrize(
    ("payload", "expected_detail"),
    [
        ({"message": "仓库名称过长"}, "仓库名称过长"),
        ({"errors": {"name": ["名称无效"]}}, '"名称无效"'),
    ],
)
@respx.mock
def test_update_settings_422_includes_gitee_validation_detail(payload, expected_detail):
    respx.patch(f"{API}/repos/owner/repo-1").mock(
        return_value=httpx.Response(422, json=payload)
    )

    with pytest.raises(PlatformError) as caught:
        GiteeClient(TOKEN).update_repository_settings(repository(), "新名称", None)

    assert caught.value.kind is ErrorKind.REQUEST
    assert "Gitee request failed (422)" in str(caught.value)
    assert expected_detail in str(caught.value)


@respx.mock
def test_error_detail_never_echoes_access_token():
    respx.patch(f"{API}/repos/owner/repo-1").mock(
        return_value=httpx.Response(422, json={"message": f"invalid token {TOKEN}"})
    )

    with pytest.raises(PlatformError) as caught:
        GiteeClient(TOKEN).update_repository_settings(repository(), "新名称", None)

    assert TOKEN not in str(caught.value)
    assert "[已隐藏]" in str(caught.value)


@respx.mock
def test_delete_404_with_missing_repository_is_not_reported_as_success():
    respx.delete(f"{API}/repos/owner/repo-1/contents/photo/data.bin").mock(
        return_value=httpx.Response(404)
    )
    respx.get(f"{API}/repos/owner/repo-1/contents/photo/data.bin").mock(
        return_value=httpx.Response(404)
    )
    snapshot = FileSnapshot("photo/data.bin", "main", "", "blob-1")

    with pytest.raises(PlatformError) as caught:
        GiteeClient(TOKEN).delete_file(repository(), snapshot, "chore: delete")

    assert caught.value.kind is ErrorKind.NOT_FOUND
