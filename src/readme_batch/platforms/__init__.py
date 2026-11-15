from collections.abc import Callable

from readme_batch.models import PlatformKind
from readme_batch.platforms.base import RepositoryPlatform
from readme_batch.platforms.gitee import GiteeClient
from readme_batch.platforms.gitcode import GitCodeClient
from readme_batch.platforms.github import GitHubClient

ClientFactory = Callable[[str], RepositoryPlatform]

DEFAULT_CLIENT_FACTORIES: dict[PlatformKind, ClientFactory] = {
    PlatformKind.GITEE: GiteeClient,
    PlatformKind.GITHUB: GitHubClient,
    PlatformKind.GITCODE: GitCodeClient,
}
