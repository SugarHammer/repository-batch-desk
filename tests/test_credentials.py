import keyring
import pytest

from readme_batch.credentials import CredentialStore


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


@pytest.fixture
def fake_keyring(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(keyring, "get_password", fake.get_password)
    monkeypatch.setattr(keyring, "set_password", fake.set_password)
    monkeypatch.setattr(keyring, "delete_password", fake.delete_password)
    return fake


def test_save_load_and_clear_use_one_fixed_service(fake_keyring):
    store = CredentialStore()

    store.save("secret-token")

    assert store.load() == "secret-token"
    store.clear()
    assert store.load() is None
    assert {call[1:] for call in fake_keyring.calls} == {("readme-batch/gitee", "gitee")}


def test_clear_tolerates_missing_credential(fake_keyring):
    CredentialStore().clear()

    assert fake_keyring.calls == [("clear", "readme-batch/gitee", "gitee")]


def test_credentials_never_write_project_files(fake_keyring, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    CredentialStore().save("secret-token")

    assert list(tmp_path.iterdir()) == []
