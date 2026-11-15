import base64
import binascii
import re
import time
from collections.abc import Callable
from urllib.parse import quote

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
    GITHUB_CAPABILITIES,
    ErrorKind,
    PlatformError,
)

API_URL = "https://api.github.com"
API_VERSION = "2026-03-10"


class GitHubClient:
    capabilities = GITHUB_CAPABILITIES

    def __init__(
        self,
        token: str,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._clock = clock
        self._client = httpx.Client(
            base_url=API_URL,
            transport=transport,
            timeout=30.0,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": API_VERSION,
            },
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
                    "affiliation": "owner,collaborator,organization_member",
                },
            )
            try:
                items = response.json()
                repositories.extend(
                    Repository(
                        owner=item["owner"]["login"],
                        name=item["name"],
                        full_name=item["full_name"],
                        default_branch=item["default_branch"],
                        html_url=item["html_url"],
                        private=item["private"],
                        platform=PlatformKind.GITHUB,
                    )
                    for item in items
                    if item.get("default_branch")
                )
            except (KeyError, TypeError, ValueError):
                raise PlatformError(ErrorKind.REQUEST, "Invalid GitHub repository response") from None
            if len(items) < 100:
                return repositories
            page += 1

    def read_readme(self, repository: Repository) -> FileSnapshot:
        response = self._request(
            "GET",
            self._readme_path(repository),
            params={"ref": repository.default_branch},
        )
        try:
            payload = response.json()
            if payload.get("encoding") == "none":
                raw = self._request(
                    "GET",
                    self._readme_path(repository),
                    params={"ref": repository.default_branch},
                    headers={"Accept": "application/vnd.github.raw+json"},
                )
                content = raw.content.decode("utf-8")
            else:
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
        )
        try:
            commit = response.json()["commit"]
            url = commit.get("html_url") or f"{repository.html_url}/commit/{commit['sha']}"
            return CommitInfo(sha=commit["sha"], url=url)
        except (KeyError, TypeError, ValueError):
            raise PlatformError(ErrorKind.REQUEST, "Invalid GitHub commit response") from None

    def update_repository_settings(
        self,
        repository: Repository,
        display_name: str | None,
        private: bool | None,
    ) -> None:
        if display_name is not None:
            self._unsupported()
        if private is not None:
            self._request(
                "PATCH",
                f"/repos/{repository.owner}/{repository.name}",
                json={"private": private},
            )

    def validate_topics(self, labels: tuple[str, ...]) -> None:
        if len(labels) > 20 or any(
            re.fullmatch(r"[a-z0-9][a-z0-9-]{0,49}", label) is None for label in labels
        ):
            raise PlatformError(ErrorKind.REQUEST, "Invalid GitHub repository topics")

    def add_repository_topics(self, repository: Repository, labels: tuple[str, ...]) -> None:
        self.validate_topics(labels)
        path = f"/repos/{repository.owner}/{repository.name}/topics"
        response = self._request("GET", path)
        try:
            existing = response.json()["names"]
            if not isinstance(existing, list) or not all(
                isinstance(topic, str) for topic in existing
            ):
                raise TypeError
        except (KeyError, TypeError, ValueError):
            raise PlatformError(ErrorKind.REQUEST, "Invalid GitHub topics response") from None
        merged = list(dict.fromkeys([*existing, *labels]))
        if len(merged) > 20:
            raise PlatformError(ErrorKind.REQUEST, "GitHub repositories allow at most 20 topics")
        self._request("PUT", path, json={"names": merged})

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
        try:
            return self._upload_files_once(repository, files, overwrite, message)
        except PlatformError as error:
            uncertain = error.kind in {
                ErrorKind.TEMPORARY,
                ErrorKind.RATE_LIMIT,
                ErrorKind.CONFLICT,
            }
            if uncertain and self._remote_files_match(repository, files):
                return UploadResult(commit_info=None, reconciled=True)
            raise

    def _upload_files_once(
        self,
        repository: Repository,
        files: tuple[UploadFile, ...],
        overwrite: bool,
        message: str,
    ) -> UploadResult:
        base = f"/repos/{repository.owner}/{repository.name}"
        branch = quote(repository.default_branch, safe="/")
        try:
            head_response = self._request("GET", f"{base}/git/ref/heads/{branch}")
            head_sha = head_response.json()["object"]["sha"]
            exists = {
                file.path: self._file_exists(repository, file.path, ref=head_sha) for file in files
            }
            if not overwrite and any(exists.values()):
                raise PlatformError(ErrorKind.CONFLICT, "同名文件已存在")
            head_commit = self._request("GET", f"{base}/git/commits/{head_sha}")
            base_tree = head_commit.json()["tree"]["sha"]
            entries = []
            for file in files:
                blob_response = self._request(
                    "POST",
                    f"{base}/git/blobs",
                    json={
                        "content": base64.b64encode(file.content).decode("ascii"),
                        "encoding": "base64",
                    },
                )
                entries.append(
                    {
                        "path": file.path,
                        "mode": "100644",
                        "type": "blob",
                        "sha": blob_response.json()["sha"],
                    }
                )
            tree_response = self._request(
                "POST",
                f"{base}/git/trees",
                json={"base_tree": base_tree, "tree": entries},
            )
            tree_sha = tree_response.json()["sha"]
            commit_response = self._request(
                "POST",
                f"{base}/git/commits",
                json={"message": message, "tree": tree_sha, "parents": [head_sha]},
            )
            commit = commit_response.json()
            commit_sha = commit["sha"]
            self._request(
                "PATCH",
                f"{base}/git/refs/heads/{branch}",
                json={"sha": commit_sha, "force": False},
                unprocessable_kind=ErrorKind.CONFLICT,
            )
            url = commit.get("html_url") or f"{repository.html_url}/commit/{commit_sha}"
            return UploadResult(CommitInfo(commit_sha, url), reconciled=False)
        except (KeyError, TypeError, ValueError):
            raise PlatformError(ErrorKind.REQUEST, "Invalid GitHub git data response") from None

    def _file_exists(self, repository: Repository, path: str, ref: str | None = None) -> bool:
        try:
            response = self._request(
                "GET",
                f"/repos/{repository.owner}/{repository.name}/contents/{quote(path, safe='/')}",
                params={"ref": ref or repository.default_branch},
            )
        except PlatformError as error:
            if error.kind is ErrorKind.NOT_FOUND:
                return False
            raise
        try:
            payload = response.json()
        except ValueError:
            raise PlatformError(ErrorKind.REQUEST, "Invalid GitHub file response") from None
        if isinstance(payload, dict) and payload.get("type") == "file":
            return True
        if isinstance(payload, (dict, list)):
            raise PlatformError(ErrorKind.CONFLICT, "目标路径不是普通文件")
        raise PlatformError(ErrorKind.REQUEST, "Invalid GitHub file response")

    def _read_file_content(self, repository: Repository, path: str) -> bytes | None:
        try:
            response = self._request(
                "GET",
                f"/repos/{repository.owner}/{repository.name}/contents/{quote(path, safe='/')}",
                params={"ref": repository.default_branch},
            )
        except PlatformError as error:
            if error.kind is ErrorKind.NOT_FOUND:
                return None
            raise
        try:
            payload = response.json()
            if payload.get("encoding") == "none":
                raw = self._request(
                    "GET",
                    f"/repos/{repository.owner}/{repository.name}/contents/{quote(path, safe='/')}",
                    params={"ref": repository.default_branch},
                    headers={"Accept": "application/vnd.github.raw+json"},
                )
                return raw.content
            content = payload["content"]
            if not isinstance(content, str):
                raise TypeError
            return base64.b64decode("".join(content.split()), validate=True)
        except (KeyError, TypeError, ValueError, binascii.Error):
            raise PlatformError(ErrorKind.REQUEST, "File content could not be decoded safely") from None

    def _remote_files_match(self, repository: Repository, files: tuple[UploadFile, ...]) -> bool:
        try:
            return all(
                self._read_file_content(repository, file.path) == file.content for file in files
            )
        except PlatformError:
            return False

    @staticmethod
    def _unsupported() -> None:
        raise PlatformError(ErrorKind.UNSUPPORTED, "GitHub operation is not supported yet")

    def _request(
        self,
        method: str,
        path: str,
        unprocessable_kind: ErrorKind = ErrorKind.REQUEST,
        **kwargs,
    ) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise PlatformError(ErrorKind.TEMPORARY, "GitHub network request failed") from None
        if response.is_success:
            return response
        kind = {
            400: ErrorKind.REQUEST,
            401: ErrorKind.AUTH,
            403: ErrorKind.PERMISSION,
            404: ErrorKind.NOT_FOUND,
            409: ErrorKind.CONFLICT,
            422: unprocessable_kind,
            429: ErrorKind.RATE_LIMIT,
        }.get(response.status_code)
        if response.status_code == 403:
            if (
                response.headers.get("x-ratelimit-remaining") == "0"
                or "retry-after" in response.headers
                or self._is_secondary_rate_limit(response)
            ):
                kind = ErrorKind.RATE_LIMIT
        if kind is None:
            kind = ErrorKind.TEMPORARY if response.status_code >= 500 else ErrorKind.REQUEST
        retry_after = self._retry_after(response) if kind is ErrorKind.RATE_LIMIT else None
        if kind is ErrorKind.RATE_LIMIT and retry_after is None:
            retry_after = 60.0
        if kind is ErrorKind.NOT_FOUND:
            message = "GitHub resource was not found or the current token cannot access it"
        else:
            message = f"GitHub request failed ({response.status_code})"
        raise PlatformError(kind, message, retry_after)

    @staticmethod
    def _is_secondary_rate_limit(response: httpx.Response) -> bool:
        try:
            message = response.json().get("message", "")
        except ValueError:
            return False
        return isinstance(message, str) and "secondary rate limit" in message.lower()

    def _retry_after(self, response: httpx.Response) -> float | None:
        try:
            return float(response.headers["Retry-After"])
        except (KeyError, TypeError, ValueError):
            pass
        try:
            return max(0.0, float(response.headers["X-RateLimit-Reset"]) - self._clock())
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _readme_path(repository: Repository) -> str:
        return f"/repos/{repository.owner}/{repository.name}/contents/README.md"
