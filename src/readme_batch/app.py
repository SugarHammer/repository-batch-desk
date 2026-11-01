import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from readme_batch.batches import Batch, BatchManager, StalePreviewError
from readme_batch.credentials import CredentialStore
from readme_batch.models import Repository
from readme_batch.operations import Operation, OperationManager, StaleOperationError
from readme_batch.platforms.base import ErrorKind, PlatformError, RepositoryPlatform
from readme_batch.platforms.gitee import GiteeClient
from readme_batch.reports import JsonReportStore
from readme_batch.schemas import (
    DraftRequest,
    LabelsOperationRequest,
    RetryRequest,
    SessionRequest,
    SettingsOperationRequest,
)


@dataclass
class AppState:
    adapter: RepositoryPlatform | None = None
    manager: BatchManager | None = None
    operations: OperationManager | None = None
    repositories: dict[str, Repository] = field(default_factory=dict)
    draft: DraftRequest = field(default_factory=DraftRequest)


def create_app(
    client_factory: Callable[[str], RepositoryPlatform] = GiteeClient,
    credential_store: CredentialStore | None = None,
    report_directory: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="README Batch Editor", docs_url=None, redoc_url=None, openapi_url=None)
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

    def activate(token: str, remember: bool = False) -> None:
        adapter = client_factory(token)
        state.adapter = adapter
        state.repositories = {}
        state.manager = BatchManager(adapter, max_workers=3, on_complete=reports.save)
        state.operations = OperationManager(adapter, max_workers=3, on_complete=reports.save_operation)
        if remember:
            credentials.save(token)
        else:
            credentials.clear()

    saved_token = credentials.load()
    if saved_token:
        activate(saved_token, remember=True)

    @app.get("/api/ui-token")
    def ui_token() -> dict[str, str]:
        return {"token": app.state.api_token}

    @app.get("/api/session")
    def session_status() -> dict[str, bool]:
        return {"active": state.adapter is not None}

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/session", status_code=204)
    def create_session(request: SessionRequest) -> Response:
        if not request.token.strip():
            raise HTTPException(422, "token must not be empty")
        if state.manager:
            state.manager.invalidate()
        if state.operations:
            state.operations.invalidate()
        activate(request.token, request.remember)
        return Response(status_code=204)

    @app.delete("/api/session", status_code=204)
    def delete_session() -> Response:
        if state.manager:
            state.manager.invalidate()
        if state.operations:
            state.operations.invalidate()
        state.adapter = None
        state.manager = None
        state.operations = None
        state.repositories = {}
        credentials.clear()
        return Response(status_code=204)

    @app.get("/api/repositories")
    def repositories() -> list[dict[str, object]]:
        adapter = _require_adapter(state)
        try:
            items = adapter.list_repositories()
        except PlatformError as error:
            raise _platform_http_error(error) from None
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
        selected = _selected_repositories(state, request.selected)
        selected_names = {repo.full_name for repo in selected}
        if any(full_name not in selected_names for full_name in request.names):
            raise HTTPException(400, "name change repository is not selected")
        names = {}
        changes = []
        for repo in selected:
            current_name = repo.display_name or repo.name
            display_name = request.names.get(repo.full_name, current_name).strip()
            if not display_name:
                raise HTTPException(400, "repository display name must not be empty")
            private = None if request.visibility == "keep" else request.visibility == "private"
            if display_name == current_name and (private is None or private == repo.private):
                continue
            names[repo.full_name] = display_name
            changes.append(repo)
        if not changes:
            raise HTTPException(400, "no repository settings changed")
        operation_id = operations.start_settings(changes, names, request.visibility)
        return {"id": operation_id}

    @app.post("/api/operations/labels", status_code=202)
    def create_labels_operation(request: LabelsOperationRequest) -> dict[str, str]:
        operations = _require_operations(state)
        selected = _selected_repositories(state, request.selected)
        labels = list(dict.fromkeys(label.strip() for label in request.labels if label.strip()))
        if not labels:
            raise HTTPException(400, "at least one label must be supplied")
        operation_id = operations.start_labels(selected, labels)
        return {"id": operation_id}

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


def _selected_repositories(state: AppState, selected: list[str]) -> list[Repository]:
    if not selected:
        raise HTTPException(400, "at least one repository must be selected")
    try:
        return [state.repositories[name] for name in dict.fromkeys(selected)]
    except KeyError:
        raise HTTPException(400, "selected repository is not loaded") from None


def _platform_http_error(error: PlatformError) -> HTTPException:
    status = {
        ErrorKind.AUTH: 401,
        ErrorKind.PERMISSION: 403,
        ErrorKind.NOT_FOUND: 404,
        ErrorKind.CONFLICT: 409,
        ErrorKind.RATE_LIMIT: 429,
        ErrorKind.TEMPORARY: 503,
        ErrorKind.REQUEST: 400,
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
    }


def _operation_dict(operation: Operation) -> dict:
    return {
        "id": operation.id,
        "kind": operation.kind.value,
        "state": operation.state.value,
        "created_at": operation.created_at.isoformat(),
        "items": [
            {
                "repository": _repository_dict(item.repository),
                "state": item.state.value,
                "error": item.error,
                "retryable": item.retryable,
            }
            for item in operation.items
        ],
    }


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
