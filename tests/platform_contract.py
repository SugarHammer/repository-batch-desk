import base64
import json
from urllib.parse import parse_qs

import httpx
import respx


class RepositoryPlatformContract:
    API = None
    TOKEN = None

    def _client(self):
        raise NotImplementedError

    def _repo_payload(self, number):
        raise NotImplementedError

    def _repository(self):
        raise NotImplementedError

    @respx.mock
    def test_contract_list_repositories_reads_all_pages(self):
        route = respx.get(f"{self.API}/user/repos").mock(
            side_effect=[
                httpx.Response(200, json=[self._repo_payload(n) for n in range(1, 101)]),
                httpx.Response(200, json=[self._repo_payload(101)]),
            ]
        )

        repositories = self._client().list_repositories()

        assert len(repositories) == 101
        assert repositories[-1].full_name == "owner/repo-101"
        assert repositories[0].platform.value == self.PLATFORM
        assert [call.request.url.params["page"] for call in route.calls] == ["1", "2"]

    @respx.mock
    def test_contract_read_readme_preserves_bom_crlf_and_chinese(self):
        source = "\ufeff中文\r\nold\r\n".encode("utf-8")
        respx.get(f"{self.API}/repos/owner/repo-1/contents/README.md").mock(
            return_value=httpx.Response(
                200,
                json={
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(source).decode("ascii"),
                    "sha": "blob-1",
                },
            )
        )

        snapshot = self._client().read_readme(self._repository())

        assert snapshot.content == source.decode("utf-8")
        assert snapshot.sha == "blob-1"
        assert snapshot.branch == "main"

    @respx.mock
    def test_contract_update_readme_uses_preview_sha(self):
        snapshot = type(self._repository_snapshot())(
            "README.md", "main", "old", "preview-blob"
        )
        route = respx.put(f"{self.API}/repos/owner/repo-1/contents/README.md").mock(
            return_value=httpx.Response(
                200,
                json={
                    "content": {"sha": "blob-2"},
                    "commit": {"sha": "commit-1", "html_url": "https://example/commit/1"},
                },
            )
        )

        result = self._client().update_readme(
            self._repository(), snapshot, "new", "docs: update"
        )

        assert result.sha == "commit-1"
        assert result.url == "https://example/commit/1"
        request = route.calls[0].request
        if request.headers.get("content-type", "").startswith("application/json"):
            payload = json.loads(request.content)
        else:
            payload = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        assert payload["sha"] == "preview-blob"
        assert payload["branch"] == "main"
        assert payload["message"] == "docs: update"

    def _repository_snapshot(self):
        from readme_batch.models import FileSnapshot

        return FileSnapshot("README.md", "main", "old", "blob-1")

    @respx.mock
    def test_contract_errors_never_expose_token(self):
        respx.get(f"{self.API}/user/repos").mock(
            return_value=httpx.Response(401, json={"message": "bad token"})
        )

        try:
            self._client().list_repositories()
        except Exception as error:
            assert self.TOKEN not in str(error)
            assert "authorization" not in str(error).lower()
        else:
            raise AssertionError("401 response did not fail")
