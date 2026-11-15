import base64
import binascii
import os
import secrets
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from readme_batch.batches import Batch, BatchManager, StalePreviewError
from readme_batch.credentials import CredentialStore
from readme_batch.deletions import DeletionManager, DeletionPreview, StaleDeletionError
from readme_batch.models import PlatformKind, Repository, UploadFile
from readme_batch.operations import Operation, OperationManager, StaleOperationError
from readme_batch.platforms import DEFAULT_CLIENT_FACTORIES, ClientFactory
from readme_batch.platforms.base import ErrorKind, PlatformError, RepositoryPlatform
from readme_batch.reports import JsonReportStore
from readme_batch.schemas import (
    DeletionPreviewRequest,
    DraftRequest,
    LabelsOperationRequest,
    RetryRequest,
    SessionRequest,
    SettingsOperationRequest,
    UploadOperationRequest,
)


@dataclass
class AppState:
    adapter: RepositoryPlatform | None = None
    platform: PlatformKind | None = None
    generation: int = 0
    manager: BatchManager | None = None
    operations: OperationManager | None = None
    deletions: DeletionManager | None = None
    repositories: dict[str, Repository] = field(default_factory=dict)
    draft: DraftRequest = field(default_factory=DraftRequest)


def create_app(
    client_factories: Mapping[PlatformKind, ClientFactory] | None = None,
    credential_store: CredentialStore | None = None,
    report_directory: Path | None = None,
) -> FastAPI:
    factories = client_factories or DEFAULT_CLIENT_FACTORIES
    app = FastAPI(title="Repository Batch Desk", docs_url=None, redoc_url=None, openapi_url=None)
    credentials = credential_store or CredentialStore()
    reports = JsonReportStore(report_directory or _default_report_directory())
    state = AppState()
    app.state.editor = state
    app.state.api_token = secrets.token_urlsafe(32)
    allowed_hosts = {"127.0.0.1", "localhost", "::1", "testserver"}

    @app.middleware("http")
    async def protect_local_api(request: Request, call_next):
        if not request.url.path.startswith("/api"):
            return await call_next(request)
        if request.url.hostname not in allowed_hosts:
            return JSONResponse({"detail": "invalid local host"}, status_code=403)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            fetch_site = request.headers.get("sec-fetch-site")
            if fetch_site and fetch_site != "same-origin":
                return JSONResponse({"detail": "cross-site request rejected"}, status_code=403)
            origin = request.headers.get("origin")
            if origin:
                parsed = urlparse(origin)
                if (
                    parsed.scheme != "http"
                    or parsed.hostname != request.url.hostname
                    or parsed.port != request.url.port
                ):
                    return JSONResponse({"detail": "invalid request origin"}, status_code=403)
        public_paths = {"/api/health", "/api/ui-token"}
        supplied_token = request.headers.get("x-readme-batch-token", "")
        if (
            request.url.path not in public_paths
            and not secrets.compare_digest(supplied_token, app.state.api_token)
        ):
            return JSONResponse({"detail": "invalid local API token"}, status_code=403)
        return await call_next(request)

    def activate(platform: PlatformKind, token: str, remember: bool = False) -> None:
        adapter = factories[platform](token)
        state.generation += 1
        state.adapter = adapter
        state.platform = platform
        state.repositories = {}
        state.draft = DraftRequest()
        state.manager = BatchManager(adapter, max_workers=3, on_complete=reports.save)
        state.operations = OperationManager(adapter, max_workers=3, on_complete=reports.save_operation)
        state.deletions = DeletionManager(adapter, max_workers=3)
        if remember:
            credentials.save(platform, token)
        else:
            credentials.clear(platform)
        credentials.save_last_platform(platform)

    last_platform = credentials.load_last_platform() or PlatformKind.GITEE
    saved_token = credentials.load(last_platform)
    if saved_token and last_platform in factories:
        activate(last_platform, saved_token, remember=True)

    @app.get("/api/ui-token")
    def ui_token() -> dict[str, str]:
        return {"token": app.state.api_token}

    @app.get("/api/session")
    def session_status() -> dict[str, object]:
        if state.adapter is None or state.platform is None:
            return {"active": False}
        return {
            "active": True,
            "platform": state.platform.value,
            "capabilities": asdict(state.adapter.capabilities),
        }

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/shutdown", status_code=202)
    def shutdown(request: Request) -> Response:
        server = getattr(request.app.state, "server", None)
        if server is None:
            raise HTTPException(503, "local server control is unavailable")
        server.should_exit = True
        return Response(status_code=202)

    @app.post("/api/session", status_code=204)
    def create_session(request: SessionRequest) -> Response:
        if not request.token.strip():
            raise HTTPException(422, "token must not be empty")
        if request.platform not in factories:
            raise HTTPException(400, "unsupported platform")
        if state.manager:
            state.manager.invalidate()
        if state.operations:
            state.operations.invalidate()
        if state.deletions:
            state.deletions.invalidate()
        activate(request.platform, request.token, request.remember)
        return Response(status_code=204)

    @app.delete("/api/session", status_code=204)
    def delete_session() -> Response:
        if state.manager:
            state.manager.invalidate()
        if state.operations:
            state.operations.invalidate()
        if state.deletions:
            state.deletions.invalidate()
        if state.platform is not None:
            credentials.clear(state.platform)
        state.generation += 1
        state.adapter = None
        state.platform = None
        state.manager = None
        state.operations = None
        state.deletions = None
        state.repositories = {}
        state.draft = DraftRequest()
        return Response(status_code=204)

    @app.get("/api/repositories")
    def repositories() -> list[dict[str, object]]:
        adapter = _require_adapter(state)
        generation = state.generation
        try:
            items = adapter.list_repositories()
        except PlatformError as error:
            raise _platform_http_error(error) from None
        if generation != state.generation or adapter is not state.adapter:
            raise HTTPException(409, "session changed while repositories were loading")
        state.repositories = {repo.full_name: repo for repo in items}
        return [_repository_dict(repo) for repo in items]

    @app.put("/api/draft", status_code=204)
    def update_draft(draft: DraftRequest) -> Response:
        state.draft = draft
        if state.manager:
            state.manager.invalidate()
        return Response(status_code=204)

    @app.post("/api/previews", status_code=202)
    def create_preview() -> dict[str, str]:
        manager = _require_manager(state)
        draft = state.draft
        if not draft.old:
            raise HTTPException(400, "old text must not be empty")
        if not draft.commit_message.strip():
            raise HTTPException(400, "commit message must not be empty")
        if not draft.selected:
            raise HTTPException(400, "at least one repository must be selected")
        try:
            selected = [state.repositories[name] for name in dict.fromkeys(draft.selected)]
        except KeyError:
            raise HTTPException(400, "selected repository is not loaded") from None
        try:
            batch_id = manager.start_preview(
                selected, draft.old, draft.new, draft.commit_message
            )
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        return {"id": batch_id}

    @app.get("/api/batches/{batch_id}")
    def get_batch(batch_id: str) -> dict:
        manager = _require_manager(state)
        try:
            return _batch_dict(manager.get_batch(batch_id))
        except KeyError:
            raise HTTPException(404, "batch not found") from None

    @app.post("/api/batches/{batch_id}/commit", status_code=202)
    def commit_batch(batch_id: str) -> Response:
        manager = _require_manager(state)
        try:
            manager.start_commit(batch_id)
        except KeyError:
            raise HTTPException(404, "batch not found") from None
        except StalePreviewError as error:
            raise HTTPException(409, str(error)) from None
        return Response(status_code=202)

    @app.post("/api/batches/{batch_id}/retry", status_code=202)
    def retry_item(batch_id: str, request: RetryRequest) -> Response:
        manager = _require_manager(state)
        try:
            manager.start_retry(batch_id, request.full_name)
        except KeyError:
            raise HTTPException(404, "batch not found") from None
        except StalePreviewError as error:
            raise HTTPException(409, str(error)) from None
        return Response(status_code=202)

    @app.post("/api/operations/settings", status_code=202)
    def create_settings_operation(request: SettingsOperationRequest) -> dict[str, str]:
        operations = _require_operations(state)
        adapter = _require_adapter(state)
        selected = _selected_repositories(state, request.selected)
        selected_names = {repo.full_name for repo in selected}
        if any(full_name not in selected_names for full_name in request.names):
            raise HTTPException(400, "name change repository is not selected")
        names = {}
        changes = []
        for repo in selected:
            current_name = repo.display_name or repo.name
            requested_name = request.names.get(repo.full_name, current_name).strip()
            if not requested_name:
                raise HTTPException(400, "repository display name must not be empty")
            name_changed = requested_name != current_name
            if name_changed and not adapter.capabilities.display_name:
                raise HTTPException(400, "platform does not support display names")
            private = None if request.visibility == "keep" else request.visibility == "private"
            if not name_changed and (private is None or private == repo.private):
                continue
            names[repo.full_name] = requested_name if name_changed else None
            changes.append(repo)
        if not changes:
            raise HTTPException(400, "no repository settings changed")
        operation_id = operations.start_settings(changes, names, request.visibility)
        return {"id": operation_id}

    @app.post("/api/operations/labels", status_code=202)
    def create_labels_operation(request: LabelsOperationRequest) -> dict[str, str]:
        operations = _require_operations(state)
        adapter = _require_adapter(state)
        selected = _selected_repositories(state, request.selected)
        labels = list(dict.fromkeys(label.strip() for label in request.labels if label.strip()))
        if not labels:
            raise HTTPException(400, "at least one label must be supplied")
        try:
            adapter.validate_topics(tuple(labels))
        except PlatformError as error:
            raise _platform_http_error(error) from None
        operation_id = operations.start_labels(selected, labels)
        return {"id": operation_id}

    @app.post("/api/operations/uploads", status_code=202)
    def create_upload_operation(request: UploadOperationRequest) -> dict[str, str]:
        operations = _require_operations(state)
        adapter = _require_adapter(state)
        if adapter.capabilities.upload_mode != "single_commit":
            raise HTTPException(400, "platform does not support multi-file single commits")
        selected = _selected_repositories(state, request.selected)
        message = request.message.strip()
        if not message:
            raise HTTPException(400, "commit message must not be empty")
        directory = _normalize_upload_directory(request.directory)
        if not request.files:
            raise HTTPException(400, "at least one file must be supplied")
        if len(request.files) > 20:
            raise HTTPException(413, "at most 20 files may be uploaded at once")
        files = []
        names = set()
        total_size = 0
        for item in request.files:
            name = item.name.strip()
            if (
                not name
                or name in {".", ".."}
                or "/" in name
                or "\\" in name
                or any(ord(character) < 32 for character in name)
            ):
                raise HTTPException(400, "invalid upload file name")
            if name in names:
                raise HTTPException(400, "duplicate upload file name")
            names.add(name)
            try:
                content = base64.b64decode(item.content_base64, validate=True)
            except (binascii.Error, ValueError):
                raise HTTPException(400, "invalid Base64 file content") from None
            if len(content) > 10 * 1024 * 1024:
                raise HTTPException(413, "a single file exceeds the 10MiB limit")
            total_size += len(content)
            if total_size > 25 * 1024 * 1024:
                raise HTTPException(413, "total upload size exceeds the 25MiB limit")
            path = f"{directory}/{name}" if directory else name
            files.append(UploadFile(path=path, content=content))
        operation_id = operations.start_upload(selected, tuple(files), request.overwrite, message)
        return {"id": operation_id}

    @app.post("/api/deletion-previews", status_code=202)
    def create_deletion_preview(request: DeletionPreviewRequest) -> dict[str, str]:
        deletions = _require_deletions(state)
        adapter = _require_adapter(state)
        if not adapter.capabilities.file_deletion:
            raise HTTPException(400, "platform does not support file deletion")
        selected = _selected_repositories(state, request.selected)
        path = _normalize_delete_path(request.path)
        message = request.message.strip()
        if not message:
            raise HTTPException(400, "commit message must not be empty")
        preview_id = deletions.start_preview(selected, path, message)
        return {"id": preview_id}

    @app.get("/api/deletion-previews/{preview_id}")
    def get_deletion_preview(preview_id: str) -> dict:
        deletions = _require_deletions(state)
        try:
            return _deletion_dict(deletions.get_preview(preview_id))
        except KeyError:
            raise HTTPException(404, "deletion preview not found") from None

    @app.post("/api/deletion-previews/{preview_id}/delete", status_code=202)
    def delete_previewed_files(preview_id: str) -> dict[str, str]:
        deletions = _require_deletions(state)
        operations = _require_operations(state)
        adapter = _require_adapter(state)
        if not adapter.capabilities.file_deletion:
            raise HTTPException(400, "platform does not support file deletion")
        try:
            selected, snapshots, message = deletions.start_delete(preview_id)
        except KeyError:
            raise HTTPException(404, "deletion preview not found") from None
        except StaleDeletionError as error:
            raise HTTPException(409, str(error)) from None
        try:
            operation_id = operations.start_delete(selected, snapshots, message)
        except StaleOperationError as error:
            raise HTTPException(409, str(error)) from None
        return {"id": operation_id}

    @app.post("/api/deletion-previews/{preview_id}/retry", status_code=202)
    def retry_deletion_preview(preview_id: str, request: RetryRequest) -> Response:
        deletions = _require_deletions(state)
        adapter = _require_adapter(state)
        if not adapter.capabilities.file_deletion:
            raise HTTPException(400, "platform does not support file deletion")
        try:
            deletions.start_retry(preview_id, request.full_name)
        except KeyError:
            raise HTTPException(404, "deletion preview not found") from None
        except StaleDeletionError as error:
            raise HTTPException(409, str(error)) from None
        return Response(status_code=202)

    @app.get("/api/operations/{operation_id}")
    def get_operation(operation_id: str) -> dict:
        operations = _require_operations(state)
        try:
            return _operation_dict(operations.get_operation(operation_id))
        except KeyError:
            raise HTTPException(404, "operation not found") from None

    @app.post("/api/operations/{operation_id}/retry", status_code=202)
    def retry_operation(operation_id: str, request: RetryRequest) -> Response:
        operations = _require_operations(state)
        try:
            operations.start_retry(operation_id, request.full_name)
        except KeyError:
            raise HTTPException(404, "operation not found") from None
        except StaleOperationError as error:
            raise HTTPException(409, str(error)) from None
        return Response(status_code=202)

    static_directory = Path(__file__).resolve().parent / "static"
    app.mount("/", StaticFiles(directory=static_directory, html=True), name="static")
    return app


def _require_adapter(state: AppState) -> RepositoryPlatform:
    if state.adapter is None:
        raise HTTPException(401, "Gitee session required")
    return state.adapter


def _require_manager(state: AppState) -> BatchManager:
    if state.manager is None:
        raise HTTPException(401, "Gitee session required")
    return state.manager


def _require_operations(state: AppState) -> OperationManager:
    if state.operations is None:
        raise HTTPException(401, "Gitee session required")
    return state.operations


def _require_deletions(state: AppState) -> DeletionManager:
    if state.deletions is None:
        raise HTTPException(401, "platform session required")
    return state.deletions


def _selected_repositories(state: AppState, selected: list[str]) -> list[Repository]:
    if not selected:
        raise HTTPException(400, "at least one repository must be selected")
    try:
        repositories = [state.repositories[name] for name in dict.fromkeys(selected)]
    except KeyError:
        raise HTTPException(400, "selected repository is not loaded") from None
    if any(repository.platform is not state.platform for repository in repositories):
        raise HTTPException(409, "selected repository belongs to a different platform session")
    return repositories


def _normalize_delete_path(value: str) -> str:
    path = value.strip()
    if (
        not path
        or "\\" in path
        or path.startswith("/")
        or (len(path) > 1 and path[1] == ":")
        or any(ord(character) < 32 for character in path)
    ):
        raise HTTPException(400, "invalid file path")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise HTTPException(400, "invalid file path")
    return path


def _normalize_upload_directory(value: str) -> str:
    directory = value.strip()
    if not directory:
        return ""
    if (
        "\\" in directory
        or directory.startswith("/")
        or (len(directory) > 1 and directory[1] == ":")
        or any(ord(character) < 32 for character in directory)
    ):
        raise HTTPException(400, "invalid upload directory")
    parts = directory.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(400, "invalid upload directory")
    return "/".join(parts)


def _platform_http_error(error: PlatformError) -> HTTPException:
    status = {
        ErrorKind.AUTH: 401,
        ErrorKind.PERMISSION: 403,
        ErrorKind.NOT_FOUND: 404,
        ErrorKind.CONFLICT: 409,
        ErrorKind.RATE_LIMIT: 429,
        ErrorKind.TEMPORARY: 503,
        ErrorKind.REQUEST: 400,
        ErrorKind.UNSUPPORTED: 400,
    }[error.kind]
    return HTTPException(status, str(error))


def _repository_dict(repository: Repository) -> dict[str, object]:
    return {
        "owner": repository.owner,
        "name": repository.name,
        "full_name": repository.full_name,
        "default_branch": repository.default_branch,
        "html_url": repository.html_url,
        "display_name": repository.display_name,
        "private": repository.private,
        "platform": repository.platform.value,
        "description": repository.description,
    }


def _deletion_dict(preview: DeletionPreview) -> dict:
    return {
        "id": preview.id,
        "state": preview.state.value,
        "path": preview.path,
        "message": preview.message,
        "created_at": preview.created_at.isoformat(),
        "items": [
            {
                "repository": _repository_dict(item.repository),
                "state": item.state.value if item.state else None,
                "error": item.error,
                "retryable": item.retryable,
            }
            for item in preview.items
        ],
    }


def _operation_dict(operation: Operation) -> dict:
    return {
        "id": operation.id,
        "kind": operation.kind.value,
        "state": operation.state.value,
        "created_at": operation.created_at.isoformat(),
        "items": [_operation_item_dict(item) for item in operation.items],
    }


def _operation_item_dict(item) -> dict:
    payload = {
        "repository": _repository_dict(item.repository),
        "state": item.state.value,
        "error": item.error,
        "retryable": item.retryable,
    }
    if item.commit_info:
        payload["commit_info"] = {"sha": item.commit_info.sha, "url": item.commit_info.url}
    if item.reconciled:
        payload["reconciled"] = True
    return payload


def _batch_dict(batch: Batch) -> dict:
    return {
        "id": batch.id,
        "state": batch.state.value,
        "commit_message": batch.commit_message,
        "created_at": batch.created_at.isoformat(),
        "items": [
            {
                "repository": _repository_dict(item.repository),
                "preview_state": item.preview_state.value if item.preview_state else None,
                "match_count": item.match_count,
                "diff": item.diff,
                "error": item.error,
                "reconciled": item.reconciled,
                "commit_state": item.commit_state.value if item.commit_state else None,
                "commit_info": (
                    {"sha": item.commit_info.sha, "url": item.commit_info.url}
                    if item.commit_info
                    else None
                ),
            }
            for item in batch.items
        ],
    }


def _default_report_directory() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "readme-batch" / "reports"
    return Path.home() / ".readme-batch" / "reports"
