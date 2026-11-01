import json
import uuid
from pathlib import Path

from readme_batch.batches import Batch
from readme_batch.operations import Operation


class JsonReportStore:
    def __init__(self, directory: Path):
        self._directory = directory

    def save(self, batch: Batch) -> Path:
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._directory / f"{batch.id}.json"
        temporary = self._directory / f".{batch.id}.{uuid.uuid4().hex}.tmp"
        payload = {
            "id": batch.id,
            "created_at": batch.created_at.isoformat(),
            "state": batch.state.value,
            "commit_message": batch.commit_message,
            "items": [
                {
                    "full_name": item.repository.full_name,
                    "default_branch": item.repository.default_branch,
                    "preview_state": item.preview_state.value if item.preview_state else None,
                    "commit_state": item.commit_state.value if item.commit_state else None,
                    "match_count": item.match_count,
                    "commit_sha": item.commit_info.sha if item.commit_info else None,
                    "commit_url": item.commit_info.url if item.commit_info else None,
                    "reconciled": item.reconciled,
                    "error": item.error,
                }
                for item in batch.items
            ],
        }
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def save_operation(self, operation: Operation) -> Path:
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._directory / f"{operation.id}.json"
        temporary = self._directory / f".{operation.id}.{uuid.uuid4().hex}.tmp"
        payload = {
            "id": operation.id,
            "kind": operation.kind.value,
            "created_at": operation.created_at.isoformat(),
            "state": operation.state.value,
            "items": [
                {
                    "full_name": item.repository.full_name,
                    "default_branch": item.repository.default_branch,
                    "state": item.state.value,
                    "error": item.error,
                    "retryable": item.retryable,
                }
                for item in operation.items
            ],
        }
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return path
