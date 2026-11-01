import json
import uuid
from pathlib import Path

from readme_batch.batches import Batch


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
