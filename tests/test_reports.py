import json
from datetime import datetime, timezone

from readme_batch.batches import Batch, BatchItem, BatchState, CommitState, PreviewState
from readme_batch.models import CommitInfo, FileSnapshot, Repository
from readme_batch.operations import (
    Operation,
    OperationItem,
    OperationItemState,
    OperationKind,
    OperationState,
)
from readme_batch.reports import JsonReportStore


def test_report_contains_results_but_no_sensitive_content(tmp_path):
    repo = Repository("owner", "repo", "owner/repo", "main", "https://gitee.com/owner/repo")
    batch = Batch(
        id="batch-123",
        revision=0,
        state=BatchState.COMPLETE,
        commit_message="docs: update",
        created_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
        items=(
            BatchItem(
                repository=repo,
                snapshot=FileSnapshot(
                    "README.md", "main", "secret-token secret-old secret-new", "sha-1"
                ),
                new_content="secret-new",
                match_count=1,
                diff="-secret-old\n+secret-new",
                preview_state=PreviewState.READY,
                commit_state=CommitState.SUCCESS,
                commit_info=CommitInfo("commit-1", "https://gitee.com/commit/1"),
                reconciled=True,
            ),
            BatchItem(repository=repo, preview_state=PreviewState.NO_MATCH),
            BatchItem(
                repository=repo,
                preview_state=PreviewState.READY,
                commit_state=CommitState.FAILED,
                error="Gitee request failed (500)",
            ),
        ),
    )

    path = JsonReportStore(tmp_path).save(batch)

    assert path.name == "batch-123.json"
    assert list(tmp_path.iterdir()) == [path]
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["id"] == "batch-123"
    assert payload["platform"] == "gitee"
    assert payload["created_at"] == "2026-11-01T00:00:00+00:00"
    assert payload["commit_message"] == "docs: update"
    assert payload["items"][0] == {
        "full_name": "owner/repo",
        "default_branch": "main",
        "preview_state": "ready",
        "commit_state": "success",
        "match_count": 1,
        "commit_sha": "commit-1",
        "commit_url": "https://gitee.com/commit/1",
        "reconciled": True,
        "error": None,
    }
    assert payload["items"][1]["preview_state"] == "no_match"
    assert payload["items"][2]["error"] == "Gitee request failed (500)"
    serialized = path.read_text(encoding="utf-8")
    assert "secret-token" not in serialized
    assert "secret-old" not in serialized
    assert "secret-new" not in serialized
    assert "diff" not in payload["items"][0]


def test_operation_report_contains_public_results_without_parameter_snapshot(tmp_path):
    repo = Repository("owner", "repo", "owner/repo", "main", "https://gitee.com/owner/repo")
    operation = Operation(
        id="operation-123",
        kind=OperationKind.SETTINGS,
        revision=0,
        state=OperationState.COMPLETE,
        created_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
        items=(
            OperationItem(
                repository=repo,
                state=OperationItemState.FAILED,
                error="Gitee request failed (500)",
                retryable=True,
            ),
        ),
    )

    path = JsonReportStore(tmp_path).save_operation(operation)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["id"] == "operation-123"
    assert payload["platform"] == "gitee"
    assert payload["kind"] == "settings"
    assert payload["state"] == "complete"
    assert payload["items"][0] == {
        "full_name": "owner/repo",
        "default_branch": "main",
        "state": "failed",
        "error": "Gitee request failed (500)",
        "retryable": True,
    }
    serialized = path.read_text(encoding="utf-8")
    assert "access_token" not in serialized
    assert "names" not in payload
    assert "labels" not in payload


def test_upload_report_contains_commit_and_reconciliation_without_file_snapshot(tmp_path):
    repo = Repository("owner", "repo", "owner/repo", "main", "https://gitee.com/owner/repo")
    operation = Operation(
        id="upload-123",
        kind=OperationKind.UPLOAD,
        revision=0,
        state=OperationState.COMPLETE,
        created_at=datetime(2026, 11, 2, tzinfo=timezone.utc),
        items=(
            OperationItem(
                repository=repo,
                state=OperationItemState.SUCCESS,
                commit_info=CommitInfo("commit-1", "https://example/commit/1"),
                reconciled=True,
            ),
        ),
    )

    path = JsonReportStore(tmp_path).save_operation(operation)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["kind"] == "upload"
    assert payload["items"][0]["state"] == "success"
    assert payload["items"][0]["commit_sha"] == "commit-1"
    assert payload["items"][0]["commit_url"] == "https://example/commit/1"
    assert payload["items"][0]["reconciled"] is True
    assert "files" not in payload
    assert "content_base64" not in path.read_text(encoding="utf-8")
