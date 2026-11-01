import base64
import binascii

import httpx

from readme_batch.models import CommitInfo, FileSnapshot, Repository
from readme_batch.platforms.base import ErrorKind, PlatformError

API_URL = "https://gitee.com/api/v5"


class GiteeClient:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self._token = token
        self._client = httpx.Client(
            base_url=API_URL,
            transport=transport,
            timeout=30.0,
            headers={"Accept": "application/json"},
        )

    def list_repositories(self) -> list[Repository]:
        repositories = []
        page = 1
        while True:
            response = self._request(
                "GET",
                "/user/repos",
                params={
                    "access_token": self._token,
                    "page": page,
                    "per_page": 100,
                    "sort": "full_name",
                    "direction": "asc",
                },
            )
            try:
                items = response.json()
                repositories.extend(
                    Repository(
                        owner=item["owner"]["login"],
                        name=item["path"],
                        full_name=item["full_name"],
                        default_branch=item["default_branch"],
                        html_url=item["html_url"],
                    )
                    for item in items
                )
            except (KeyError, TypeError, ValueError):
                raise PlatformError(ErrorKind.REQUEST, "Invalid Gitee repository response") from None
            if len(items) < 100:
                return repositories
            page += 1

    def read_readme(self, repository: Repository) -> FileSnapshot:
        response = self._request(
            "GET",
            self._readme_path(repository),
            params={"access_token": self._token, "ref": repository.default_branch},
        )
        try:
            payload = response.json()
            content = base64.b64decode(payload["content"]).decode("utf-8")
            return FileSnapshot(
                path="README.md",
                branch=repository.default_branch,
                content=content,
                sha=payload["sha"],
            )
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, binascii.Error):
            raise PlatformError(ErrorKind.REQUEST, "README content could not be decoded safely") from None

    def update_readme(
        self,
        repository: Repository,
        snapshot: FileSnapshot,
        new_content: str,
        message: str,
    ) -> CommitInfo:
        response = self._request(
            "PUT",
            self._readme_path(repository),
            data={
                "access_token": self._token,
                "content": base64.b64encode(new_content.encode("utf-8")).decode("ascii"),
                "sha": snapshot.sha,
                "message": message,
                "branch": snapshot.branch,
            },
            bad_request_kind=ErrorKind.CONFLICT,
        )
        try:
            commit = response.json()["commit"]
            url = commit.get("html_url") or f"{repository.html_url}/commit/{commit['sha']}"
            return CommitInfo(sha=commit["sha"], url=url)
        except (KeyError, TypeError, ValueError):
            raise PlatformError(ErrorKind.REQUEST, "Invalid Gitee commit response") from None

    def _request(
        self,
        method: str,
        path: str,
        bad_request_kind: ErrorKind = ErrorKind.REQUEST,
        **kwargs,
    ) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise PlatformError(ErrorKind.TEMPORARY, "Gitee network request failed") from None
        if response.is_success:
            return response
        kind = {
            400: bad_request_kind,
            401: ErrorKind.AUTH,
            403: ErrorKind.PERMISSION,
            404: ErrorKind.NOT_FOUND,
            409: ErrorKind.CONFLICT,
            429: ErrorKind.RATE_LIMIT,
        }.get(response.status_code)
        if kind is None:
            kind = ErrorKind.TEMPORARY if response.status_code >= 500 else ErrorKind.REQUEST
        retry_after = None
        if kind is ErrorKind.RATE_LIMIT:
            try:
                retry_after = float(response.headers["Retry-After"])
            except (KeyError, TypeError, ValueError):
                pass
        raise PlatformError(kind, f"Gitee request failed ({response.status_code})", retry_after)

    @staticmethod
    def _readme_path(repository: Repository) -> str:
        return f"/repos/{repository.owner}/{repository.name}/contents/README.md"
