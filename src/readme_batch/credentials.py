import keyring

from readme_batch.models import PlatformKind

PREFERENCES_SERVICE = "readme-batch/preferences"
LAST_PLATFORM_USERNAME = "last-platform"


class CredentialStore:
    def load(self, platform: PlatformKind) -> str | None:
        return keyring.get_password(f"readme-batch/{platform.value}", platform.value)

    def save(self, platform: PlatformKind, token: str) -> None:
        keyring.set_password(f"readme-batch/{platform.value}", platform.value, token)

    def clear(self, platform: PlatformKind) -> None:
        try:
            keyring.delete_password(f"readme-batch/{platform.value}", platform.value)
        except keyring.errors.PasswordDeleteError:
            pass

    def load_last_platform(self) -> PlatformKind | None:
        value = keyring.get_password(PREFERENCES_SERVICE, LAST_PLATFORM_USERNAME)
        try:
            return PlatformKind(value) if value else None
        except ValueError:
            return None

    def save_last_platform(self, platform: PlatformKind) -> None:
        keyring.set_password(PREFERENCES_SERVICE, LAST_PLATFORM_USERNAME, platform.value)
