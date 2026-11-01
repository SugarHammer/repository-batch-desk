import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles

from readme_batch.batches import Batch, BatchManager, StalePreviewError
from readme_batch.credentials import CredentialStore
from readme_batch.models import Repository
from readme_batch.platforms.base import ErrorKind, PlatformError, RepositoryPlatform
from readme_batch.platforms.gitee import GiteeClient
from readme_batch.reports import JsonReportStore
from readme_batch.schemas import DraftRequest, RetryRequest, SessionRequest


@dataclass
class AppState:
    adapter: RepositoryPlatform | None = None
    manager: BatchManager | None = None
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

    def activate(token: str, remember: bool = False) -> None:
        adapter = client_factory(token)
        state.adapter = adapter
        state.repositories = {}
        state.manager = BatchManager(adapter, max_workers=3, on_complete=reports.save)
        if remember:
            credentials.save(token)

    saved_token = credentials.load()
    if saved_token:
        activate(saved_token)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/session", status_code=204)
    def create_session(request: SessionRequest) -> Response:
        if not request.token.strip():
            raise HTTPException(422, "token must not be empty")
        if state.manager:
            state.manager.invalidate()
        activate(request.token, request.remember)
        return Response(status_code=204)

    @app.delete("/api/session", status_code=204)
    def delete_session() -> Response:
        if state.manager:
            state.manager.invalidate()
        state.adapter = None
        state.manager = None
        state.repositories = {}
        credentials.clear()
        return Response(status_code=204)

    @app.get("/api/repositories")
    def repositories() -> list[dict[str, str]]:
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


def _repository_dict(repository: Repository) -> dict[str, str]:
    return {
        "owner": repository.owner,
        "name": repository.name,
        "full_name": repository.full_name,
        "default_branch": repository.default_branch,
        "html_url": repository.html_url,
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
