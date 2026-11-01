from dataclasses import FrozenInstanceError

import pytest

from readme_batch.models import ReplacementResult, Repository
from readme_batch.replace import replace_literal


def test_replaces_every_literal_match():
    assert replace_literal("a old b old", "old", "new") == ReplacementResult(
        "a new b new", 2
    )


def test_matching_is_case_sensitive():
    assert replace_literal("Old old", "old", "new") == ReplacementResult(
        "Old new", 1
    )


def test_empty_new_text_deletes_match_and_preserves_bom_and_crlf():
    result = replace_literal("\ufeffa\r\nold\r\n", "old", "")

    assert result.content == "\ufeffa\r\n\r\n"
    assert result.match_count == 1


def test_replaces_multiline_chinese_text():
    result = replace_literal("前\n旧链接：示例\n后", "旧链接：示例", "新链接：样例")

    assert result == ReplacementResult("前\n新链接：样例\n后", 1)


def test_no_match_returns_original_content():
    assert replace_literal("unchanged", "missing", "new") == ReplacementResult(
        "unchanged", 0
    )


def test_empty_old_text_is_rejected():
    with pytest.raises(ValueError, match="old text must not be empty"):
        replace_literal("content", "", "new")


def test_same_old_and_new_text_returns_actual_match_count():
    assert replace_literal("same same", "same", "same") == ReplacementResult(
        "same same", 2
    )


def test_repository_is_frozen():
    repository = Repository("owner", "repo", "owner/repo", "main", "https://example")

    with pytest.raises(FrozenInstanceError):
        repository.default_branch = "master"
