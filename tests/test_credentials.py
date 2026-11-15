import keyring

from readme_batch.credentials import CredentialStore
from readme_batch.models import PlatformKind


class FakeKeyring:
    def __init__(self):
        self.values = {}
        self.calls = []

    def get_password(self, service, username):
        self.calls.append(("load", service, username))
        return self.values.get((service, username))

    def set_password(self, service, username, token):
        self.calls.append(("save", service, username))
        self.values[(service, username)] = token

    def delete_password(self, service, username):
        self.calls.append(("clear", service, username))
        if (service, username) not in self.values:
            raise keyring.errors.PasswordDeleteError("missing")
        del self.values[(service, username)]


def test_credentials_are_isolated_by_platform(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(keyring, "get_password", fake.get_password)
    monkeypatch.setattr(keyring, "set_password", fake.set_password)
    monkeypatch.setattr(keyring, "delete_password", fake.delete_password)
    store = CredentialStore()

    store.save(PlatformKind.GITEE, "gitee-token")
    store.save(PlatformKind.GITHUB, "github-token")
    store.save(PlatformKind.GITCODE, "gitcode-token")

    assert store.load(PlatformKind.GITEE) == "gitee-token"
    assert store.load(PlatformKind.GITHUB) == "github-token"
    assert store.load(PlatformKind.GITCODE) == "gitcode-token"
    store.clear(PlatformKind.GITHUB)
    assert store.load(PlatformKind.GITHUB) is None
    assert store.load(PlatformKind.GITEE) == "gitee-token"
    assert store.load(PlatformKind.GITCODE) == "gitcode-token"
    assert ("save", "readme-batch/github", "github") in fake.calls


def test_last_platform_is_restored_and_invalid_preference_is_ignored(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(keyring, "get_password", fake.get_password)
    monkeypatch.setattr(keyring, "set_password", fake.set_password)
    monkeypatch.setattr(keyring, "delete_password", fake.delete_password)
    store = CredentialStore()

    assert store.load_last_platform() is None
    store.save_last_platform(PlatformKind.GITCODE)
    assert store.load_last_platform() is PlatformKind.GITCODE
    fake.values[("readme-batch/preferences", "last-platform")] = "unknown"
    assert store.load_last_platform() is None


def test_clear_tolerates_missing_credential(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(keyring, "delete_password", fake.delete_password)
    CredentialStore().clear(PlatformKind.GITEE)
    assert fake.calls == [("clear", "readme-batch/gitee", "gitee")]


def test_credentials_never_write_project_files(monkeypatch, tmp_path):
    fake = FakeKeyring()
    monkeypatch.setattr(keyring, "get_password", fake.get_password)
    monkeypatch.setattr(keyring, "set_password", fake.set_password)
    monkeypatch.setattr(keyring, "delete_password", fake.delete_password)
    monkeypatch.chdir(tmp_path)

    CredentialStore().save(PlatformKind.GITHUB, "secret-token")

    assert list(tmp_path.iterdir()) == []
