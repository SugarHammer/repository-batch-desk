import base64
import binascii
from urllib.parse import quote

import httpx

from readme_batch.models import CommitInfo, FileSnapshot, Repository, UploadFile, UploadResult
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
                        display_name=item["name"],
                        private=item["private"],
                    )
                    for item in items
                )
            except (KeyError, TypeError, ValueError):
                raise PlatformError(ErrorKind.REQUEST, "Invalid Gitee repository response") from None
            if len(items) < 100:
                return repositories
            page += 1

    def update_repository_settings(
        self,
        repository: Repository,
        display_name: str,
        private: bool | None,
    ) -> None:
        data = {"access_token": self._token, "name": display_name}
        if private is not None:
            data["private"] = private
        self._request("PATCH", f"/repos/{repository.owner}/{repository.name}", data=data)

    def add_project_labels(self, repository: Repository, labels: tuple[str, ...]) -> None:
        self._request(
            "POST",
            f"/repos/{repository.owner}/{repository.name}/project_labels",
            params={"access_token": self._token},
            json=list(labels),
        )

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

    def upload_files(
        self,
        repository: Repository,
        files: tuple[UploadFile, ...],
        overwrite: bool,
        message: str,
        reconcile_before: bool = False,
    ) -> UploadResult:
        if reconcile_before and self._remote_files_match(repository, files):
            return UploadResult(commit_info=None, reconciled=True)
        exists = {
            file.path: self._read_file_content(repository, file.path) is not None
            for file in files
        }
        if not overwrite and any(exists.values()):
            raise PlatformError(ErrorKind.CONFLICT, "同名文件已存在")
        actions = [
            {
                "action": "update" if exists[file.path] else "create",
                "path": file.path,
                "content": base64.b64encode(file.content).decode("ascii"),
                "encoding": "base64",
            }
            for file in files
        ]
        try:
            response = self._request(
                "POST",
                f"/repos/{repository.owner}/{repository.name}/commits",
                params={"access_token": self._token},
                json={
                    "branch": repository.default_branch,
                    "message": message,
                    "actions": actions,
                },
            )
        except PlatformError as error:
            uncertain = error.kind in {
                ErrorKind.TEMPORARY,
                ErrorKind.RATE_LIMIT,
                ErrorKind.CONFLICT,
            }
            if uncertain and self._remote_files_match(repository, files):
                return UploadResult(commit_info=None, reconciled=True)
            raise
        try:
            payload = response.json()
            sha = payload["sha"]
            url = payload.get("html_url") or f"{repository.html_url}/commit/{sha}"
            return UploadResult(CommitInfo(sha=sha, url=url), reconciled=False)
        except (KeyError, TypeError, ValueError):
            raise PlatformError(ErrorKind.REQUEST, "Invalid Gitee commit response") from None

    def _read_file_content(self, repository: Repository, path: str) -> bytes | None:
        try:
            response = self._request(
                "GET",
                f"/repos/{repository.owner}/{repository.name}/contents/{quote(path, safe='/')}",
                params={"access_token": self._token, "ref": repository.default_branch},
            )
        except PlatformError as error:
            if error.kind is ErrorKind.NOT_FOUND:
                return None
            raise
        try:
            return base64.b64decode(response.json()["content"])
        except (KeyError, TypeError, ValueError, binascii.Error):
            raise PlatformError(ErrorKind.REQUEST, "File content could not be decoded safely") from None

    def _remote_files_match(self, repository: Repository, files: tuple[UploadFile, ...]) -> bool:
        try:
            return all(
                self._read_file_content(repository, file.path) == file.content
                for file in files
            )
        except PlatformError:
            return False

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
