from readme_batch.models import ReplacementResult


def replace_literal(source: str, old: str, new: str) -> ReplacementResult:
    if not old:
        raise ValueError("old text must not be empty")
    return ReplacementResult(source.replace(old, new), source.count(old))
