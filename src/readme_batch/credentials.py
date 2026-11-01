import keyring

SERVICE_NAME = "readme-batch/gitee"
USERNAME = "gitee"


class CredentialStore:
    def load(self) -> str | None:
        return keyring.get_password(SERVICE_NAME, USERNAME)

    def save(self, token: str) -> None:
        keyring.set_password(SERVICE_NAME, USERNAME, token)

    def clear(self) -> None:
        try:
            keyring.delete_password(SERVICE_NAME, USERNAME)
        except keyring.errors.PasswordDeleteError:
            pass
