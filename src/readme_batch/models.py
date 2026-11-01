from dataclasses import dataclass


@dataclass(frozen=True)
class Repository:
    owner: str
    name: str
    full_name: str
    default_branch: str
    html_url: str
    display_name: str = ""
    private: bool = False


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    branch: str
    content: str
    sha: str


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    url: str


@dataclass(frozen=True)
class ReplacementResult:
    content: str
    match_count: int
