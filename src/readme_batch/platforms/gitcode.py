import base64
import binascii

import httpx

from readme_batch.models import (
    CommitInfo,
    FileSnapshot,
    PlatformKind,
    Repository,
    UploadFile,
    UploadResult,
)
from readme_batch.platforms.base import (
    GITCODE_CAPABILITIES,
    ErrorKind,
    PlatformError,
)

API_URL = "https://api.gitcode.com/api/v5"


class GitCodeClient:
    capabilities = GITCODE_CAPABILITIES

    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(
            base_url=API_URL,
            transport=transport,
            timeout=30.0,
            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
        )

    def list_repositories(self) -> list[Repository]:
        repositories = []
        page = 1
        while True:
            response = self._request(
                "GET",
                "/user/repos",
                params={
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
                        owner=self._repository_owner(item),
                        name=item["path"],
                        full_name=item["full_name"],
                        default_branch=item["default_branch"],
                        html_url=(
                            item.get("html_url")
                            or item.get("web_url")
                            or f"https://gitcode.com/{item['full_name']}"
                        ),
                        display_name=item["name"],
                        private=bool(item["private"]),
                        platform=PlatformKind.GITCODE,
                    )
                    for item in items
                    if item.get("default_branch")
                )
            except (KeyError, TypeError, ValueError):
                raise PlatformError(ErrorKind.REQUEST, "Invalid GitCode repository response") from None
            if len(items) < 100:
                return repositories
            page += 1

    def update_repository_settings(
        self,
        repository: Repository,
        display_name: str | None,
        private: bool | None,
    ) -> None:
        data = {"name": display_name if display_name is not None else repository.display_name}
        if private is not None:
            data["private"] = private
        self._request("PATCH", f"/repos/{repository.owner}/{repository.name}", json=data)

    def validate_topics(self, labels: tuple[str, ...]) -> None:
        pass

    def add_repository_topics(self, repository: Repository, labels: tuple[str, ...]) -> None:
        base = f"/repos/{repository.owner}/{repository.name}"
        response = self._request("GET", base)
        try:
            payload = response.json()
            existing = payload["project_labels"] if "project_labels" in payload else payload["tags"]
            if not isinstance(existing, list) or not all(
                isinstance(label, str) for label in existing
            ):
                raise TypeError
        except (KeyError, TypeError, ValueError):
            raise PlatformError(ErrorKind.REQUEST, "Invalid GitCode project labels response") from None
        merged = list(dict.fromkeys([*existing, *labels]))
        self._request("PUT", f"{base}/project_labels", json=merged)

    def read_readme(self, repository: Repository) -> FileSnapshot:
        response = self._request(
            "GET",
            self._readme_path(repository),
            params={"ref": repository.default_branch},
        )
        try:
            payload = response.json()
            content = base64.b64decode(
                "".join(payload["content"].split()), validate=True
            ).decode("utf-8")
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
            json={
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
            raise PlatformError(ErrorKind.REQUEST, "Invalid GitCode commit response") from None

    def upload_files(
        self,
        repository: Repository,
        files: tuple[UploadFile, ...],
        overwrite: bool,
        message: str,
        reconcile_before: bool = False,
    ) -> UploadResult:
        self._unsupported()

    @staticmethod
    def _unsupported() -> None:
        raise PlatformError(ErrorKind.UNSUPPORTED, "GitCode operation is not supported")

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
            raise PlatformError(ErrorKind.TEMPORARY, "GitCode network request failed") from None
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
        raise PlatformError(kind, f"GitCode request failed ({response.status_code})", retry_after)

    @staticmethod
    def _repository_owner(item: dict) -> str:
        owner = item.get("owner")
        if isinstance(owner, dict) and isinstance(owner.get("login"), str) and owner["login"]:
            return owner["login"]
        namespace = item.get("namespace")
        if (
            isinstance(namespace, dict)
            and isinstance(namespace.get("path"), str)
            and namespace["path"]
        ):
            return namespace["path"]
        full_name = item["full_name"]
        if not isinstance(full_name, str) or "/" not in full_name:
            raise KeyError("owner")
        return full_name.split("/", 1)[0]

    @staticmethod
    def _readme_path(repository: Repository) -> str:
        return f"/repos/{repository.owner}/{repository.name}/contents/README.md"
