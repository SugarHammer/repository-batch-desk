import os
import socket
import threading
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import replace

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from readme_batch.app import create_app
from readme_batch.models import (
    CommitInfo,
    DeleteResult,
    FileSnapshot,
    PlatformKind,
    RemoteFile,
    Repository,
    UploadResult,
)
from readme_batch.platforms.base import (
    GITEE_CAPABILITIES,
    GITCODE_CAPABILITIES,
    GITHUB_CAPABILITIES,
    ErrorKind,
    PlatformError,
)
from readme_batch.platforms.github import GitHubClient

TOKEN = "browser-test-token"
XSS_TEXT = "<img src=x onerror=window.__readmeXss=true>"


class BrowserAdapter:
    def __init__(self, capabilities=GITEE_CAPABILITIES, platform=PlatformKind.GITEE):
        self.capabilities = capabilities
        self.repos = [
            Repository(
                "owner", f"repo-{n}", f"owner/repo-{n}", "main", f"https://example/{n}",
                display_name=f"Repo {n}", private=n % 2 == 1, platform=platform,
                description=(
                    "第一个仓库简介" if n == 1
                    else XSS_TEXT if n == 3
                    else ""
                ),
            )
            for n in range(1, 5)
        ]
        self.contents = {
            "owner/repo-1": "before\nold\nafter",
            "owner/repo-2": "nothing matches",
            "owner/repo-3": f"before {XSS_TEXT} after",
            "owner/repo-4": "old",
        }
        self.updates = []
        self.failures = {
            "owner/repo-4": [PlatformError(ErrorKind.TEMPORARY, "temporary")] * 3
        }
        self.settings_calls = []
        self.label_calls = []
        self.upload_calls = []
        self.settings_errors = {}
        self.label_errors = {}
        self.upload_errors = {}
        self.upload_results = {}
        self.existing_paths = {}
        self.file_outcomes = {}
        self.read_file_calls = []
        self.delete_calls = []
        self.delete_errors = {}
        self.delete_results = {}
        self.list_calls = 0
        self.operation_gate = None

    def list_repositories(self):
        self.list_calls += 1
        return self.repos

    def read_readme(self, repository):
        return FileSnapshot(
            "README.md", "main", self.contents[repository.full_name], f"sha-{repository.name}"
        )

    def update_readme(self, repository, snapshot, new_content, message):
        self.updates.append((repository.full_name, snapshot.sha, new_content, message))
        if self.failures.get(repository.full_name):
            raise self.failures[repository.full_name].pop(0)
        return CommitInfo("commit-1", "https://example/commit/1")

    def update_repository_settings(self, repository, display_name, private):
        self.settings_calls.append((repository.full_name, display_name, private))
        if self.operation_gate:
            self.operation_gate.wait(2)
        errors = self.settings_errors.get(repository.full_name, [])
        if errors:
            raise errors.pop(0)
        self.repos = [
            replace(
                repo,
                display_name=repo.display_name if display_name is None else display_name,
                private=repo.private if private is None else private,
            )
            if repo.full_name == repository.full_name
            else repo
            for repo in self.repos
        ]

    def validate_topics(self, labels):
        if self.capabilities == GITHUB_CAPABILITIES:
            GitHubClient.validate_topics(self, labels)

    def add_repository_topics(self, repository, labels):
        self.label_calls.append((repository.full_name, labels))
        if self.operation_gate:
            self.operation_gate.wait(2)
        errors = self.label_errors.get(repository.full_name, [])
        if errors:
            raise errors.pop(0)

    def upload_files(self, repository, files, overwrite, message, reconcile_before=False):
        self.upload_calls.append((repository.full_name, files, overwrite, message))
        if self.operation_gate:
            self.operation_gate.wait(2)
        errors = self.upload_errors.get(repository.full_name, [])
        if errors:
            raise errors.pop(0)
        existing = self.existing_paths.get(repository.full_name, set())
        if not overwrite and any(file.path in existing for file in files):
            raise PlatformError(ErrorKind.CONFLICT, "同名文件已存在")
        results = self.upload_results.get(repository.full_name, [])
        if results:
            return results.pop(0)
        return UploadResult(CommitInfo("commit-1", "https://example/commit/1"), False)

    def read_file(self, repository, path):
        self.read_file_calls.append((repository.full_name, path))
        outcome = self.file_outcomes.get(
            repository.full_name, RemoteFile(path, repository.default_branch, f"sha-{repository.name}")
        )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def delete_file(self, repository, snapshot, message, reconcile_before=False):
        self.delete_calls.append((repository.full_name, snapshot, message, reconcile_before))
        errors = self.delete_errors.get(repository.full_name, [])
        if errors:
            raise errors.pop(0)
        results = self.delete_results.get(repository.full_name, [])
        if results:
            return results.pop(0)
        return DeleteResult(CommitInfo("commit-1", "https://example/commit/1"), False)


class BrowserCredentials:
    def __init__(self):
        self.saved = TOKEN
        self.tokens = {}
        self.last_platform = PlatformKind.GITEE

    def load(self, platform):
        if platform is PlatformKind.GITEE:
            return self.saved
        return self.tokens.get(platform)

    def save(self, platform, token):
        if platform is PlatformKind.GITEE:
            self.saved = token
        else:
            self.tokens[platform] = token

    def clear(self, platform):
        if platform is PlatformKind.GITEE:
            self.saved = None
        else:
            self.tokens.pop(platform, None)

    def load_last_platform(self):
        return self.last_platform

    def save_last_platform(self, platform):
        self.last_platform = platform


@pytest.fixture
def browser():
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(
            headless=True, channel=os.environ.get("README_BATCH_BROWSER_CHANNEL") or None
        )
        yield instance
        instance.close()


@contextmanager
def serve(app):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    app.state.server = server
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError("test server did not start")
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture
def site(tmp_path):
    adapter = BrowserAdapter()
    app = create_app(
        client_factories={PlatformKind.GITEE: lambda token: adapter},
        credential_store=BrowserCredentials(),
        report_directory=tmp_path,
    )
    with serve(app) as base_url:
        yield adapter, base_url


@pytest.fixture
def platform_site(tmp_path):
    adapters = {
        PlatformKind.GITEE: BrowserAdapter(),
        PlatformKind.GITHUB: BrowserAdapter(GITHUB_CAPABILITIES, PlatformKind.GITHUB),
        PlatformKind.GITCODE: BrowserAdapter(GITCODE_CAPABILITIES, PlatformKind.GITCODE),
    }
    credentials = BrowserCredentials()
    credentials.saved = None
    app = create_app(
        client_factories={platform: lambda token, adapter=adapter: adapter for platform, adapter in adapters.items()},
        credential_store=credentials,
        report_directory=tmp_path,
    )
    with serve(app) as base_url:
        yield adapters, base_url


@pytest.fixture
def page(browser):
    page = browser.new_page()
    yield page
    page.close()


def connect_and_load(page, base_url):
    page.goto(base_url)
    page.locator("#token").fill(TOKEN)
    page.locator("#connect").click()
    expect(page.locator(".repository-row")).to_have_count(4)


def select_repo(page, full_name):
    page.locator(f'.repository-row[data-full-name="{full_name}"] input').check()


def enter_rules(page, old="old", new="new", message="docs: browser test"):
    page.locator("#old-text").fill(old)
    page.locator("#new-text").fill(new)
    page.locator("#commit-message").fill(message)


def preview(page):
    page.locator("#preview").click()
    expect(page.locator("#commit")).to_be_enabled(timeout=7000)


def test_preview_confirmation_commit_and_token_redaction(page, site):
    adapter, base_url = site
    console = []
    page.on("console", lambda message: console.append(message.text))
    connect_and_load(page, base_url)
    select_repo(page, "owner/repo-1")
    enter_rules(page)

    preview(page)

    expect(page.locator(".preview-item")).to_have_count(1)
    expect(page.locator("#diff-0")).to_contain_text("-old")
    assert adapter.updates == []
    dialogs = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
    page.locator("#commit").click()
    expect(page.locator(".commit-link")).to_have_attribute("href", "https://example/commit/1")
    assert adapter.updates == [
        ("owner/repo-1", "sha-repo-1", "before\nnew\nafter", "docs: browser test")
    ]
    assert "1 个仓库" in dialogs[0]
    assert TOKEN not in page.locator("body").inner_text()
    assert TOKEN not in "\n".join(console)


def test_failed_item_can_be_retried(page, site):
    adapter, base_url = site
    connect_and_load(page, base_url)
    select_repo(page, "owner/repo-4")
    enter_rules(page)
    preview(page)
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#commit").click()

    page.locator("#retry-0").click()

    expect(page.locator(".commit-link")).to_have_attribute("href", "https://example/commit/1")
    assert len(adapter.updates) == 4


def test_readme_html_is_rendered_as_text(page, site):
    _, base_url = site
    connect_and_load(page, base_url)
    select_repo(page, "owner/repo-3")
    enter_rules(page, old=XSS_TEXT, new="safe")

    preview(page)

    expect(page.locator("#diff-0")).to_contain_text(XSS_TEXT)
    expect(page.locator("img")).to_have_count(0)
    assert page.evaluate("window.__readmeXss") is None


def test_filter_preserves_selection_and_edits_invalidate_preview(page, site):
    _, base_url = site
    connect_and_load(page, base_url)
    select_repo(page, "owner/repo-1")
    page.locator("#repository-filter").fill("does-not-exist")
    expect(page.locator(".repository-row:visible")).to_have_count(0)
    page.locator("#repository-filter").fill("")
    expect(page.locator('.repository-row[data-full-name="owner/repo-1"] input')).to_be_checked()
    enter_rules(page)
    preview(page)

    page.locator("#old-text").fill("changed")
    expect(page.locator("#commit")).to_be_disabled()
    enter_rules(page)
    preview(page)
    page.locator("#new-text").fill("changed")
    expect(page.locator("#commit")).to_be_disabled()
    enter_rules(page)
    preview(page)
    page.locator("#commit-message").fill("changed")
    expect(page.locator("#commit")).to_be_disabled()
    enter_rules(page)
    preview(page)
    select_repo(page, "owner/repo-2")
    expect(page.locator("#commit")).to_be_disabled()


def test_validation_errors_are_readable_instead_of_object(page, site):
    _, base_url = site
    page.goto(base_url)

    page.locator("#connect").click()

    expect(page.locator("#connection-status")).not_to_have_text("[object Object]")
    expect(page.locator("#connection-status")).not_to_be_empty()


def test_saved_session_loads_repositories_when_page_opens(page, site):
    _, base_url = site

    page.goto(base_url)

    expect(page.locator("#connection-status")).to_contain_text("已恢复保存的会话", timeout=7000)
    expect(page.locator(".repository-row")).to_have_count(4)


def test_settings_preview_commit_and_repository_refresh(page, site):
    adapter, base_url = site
    connect_and_load(page, base_url)
    select_repo(page, "owner/repo-1")
    select_repo(page, "owner/repo-2")
    page.locator("#tab-settings").click()
    page.locator('.setting-name[data-full-name="owner/repo-1"]').fill("新名称一")
    page.locator('.setting-name[data-full-name="owner/repo-2"]').fill("新名称二")
    page.locator("#visibility").select_option("private")
    page.locator("#settings-preview").click()

    expect(page.locator("#settings-change-items .change-item")).to_have_count(2)
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#settings-submit").click()

    expect(page.locator("#settings-operation-items .badge.success")).to_have_count(2)
    assert adapter.settings_calls == [
        ("owner/repo-1", "新名称一", True),
        ("owner/repo-2", "新名称二", True),
    ]
    assert adapter.list_calls >= 2
    expect(page.locator('.repository-row[data-full-name="owner/repo-1"] input')).to_be_checked()
    expect(page.locator('.repository-row[data-full-name="owner/repo-2"] input')).to_be_checked()


def test_public_confirmation_can_be_cancelled_without_writes(page, site):
    adapter, base_url = site
    connect_and_load(page, base_url)
    select_repo(page, "owner/repo-1")
    page.locator("#tab-settings").click()
    page.locator("#visibility").select_option("public")
    page.locator("#settings-preview").click()
    dialogs = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))

    page.locator("#settings-submit").click()

    assert "1 个仓库将转为公开" in dialogs[0]
    assert adapter.settings_calls == []
    expect(page.locator("#settings-operation-items .operation-item")).to_have_count(0)


def test_labels_are_deduplicated_and_tabs_preserve_selection(page, site):
    adapter, base_url = site
    connect_and_load(page, base_url)
    select_repo(page, "owner/repo-1")
    page.locator("#tab-labels").click()
    page.locator("#labels-input").fill("Java， Spring Boot,Java")
    page.locator("#tab-readme").click()
    page.locator("#tab-labels").click()

    expect(page.locator('.repository-row[data-full-name="owner/repo-1"] input')).to_be_checked()
    expect(page.locator("#labels-input")).to_have_value("Java， Spring Boot,Java")
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#labels-submit").click()

    expect(page.locator("#labels-operation-items .badge.success")).to_have_count(1)
    assert adapter.label_calls == [("owner/repo-1", ("Java", "Spring Boot"))]


def test_only_retryable_operation_failure_shows_retry(page, site):
    adapter, base_url = site
    adapter.label_errors = {
        "owner/repo-3": [PlatformError(ErrorKind.PERMISSION, "denied")],
        "owner/repo-4": [PlatformError(ErrorKind.TEMPORARY, "temporary")] * 3,
    }
    connect_and_load(page, base_url)
    select_repo(page, "owner/repo-3")
    select_repo(page, "owner/repo-4")
    page.locator("#tab-labels").click()
    page.locator("#labels-input").fill("Java")
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#labels-submit").click()

    expect(page.locator("#labels-operation-items .badge.failed")).to_have_count(2)
    expect(page.locator("#labels-operation-items .operation-retry")).to_have_count(1)
    page.locator("#labels-operation-items .operation-retry").click()

    expect(page.locator("#labels-operation-items .badge.success")).to_have_count(1)
    assert sum(call[0] == "owner/repo-3" for call in adapter.label_calls) == 1
    assert sum(call[0] == "owner/repo-4" for call in adapter.label_calls) == 4


def test_operation_text_is_not_rendered_as_html(page, site):
    _, base_url = site
    connect_and_load(page, base_url)
    select_repo(page, "owner/repo-1")
    page.locator("#tab-settings").click()
    page.locator('.setting-name[data-full-name="owner/repo-1"]').fill(XSS_TEXT)
    page.locator("#settings-preview").click()

    expect(page.locator("#settings-change-items")).to_contain_text(XSS_TEXT)
    expect(page.locator("img")).to_have_count(0)

    page.locator("#tab-labels").click()
    page.locator("#labels-input").fill(XSS_TEXT)
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#labels-submit").click()

    expect(page.locator("#labels-operation-items .badge.success")).to_have_count(1)
    expect(page.locator("img")).to_have_count(0)
    assert page.evaluate("window.__readmeXss") is None


def test_selection_change_preserves_unsubmitted_name_edits(page, site):
    _, base_url = site
    connect_and_load(page, base_url)
    select_repo(page, "owner/repo-1")
    page.locator("#tab-settings").click()
    page.locator('.setting-name[data-full-name="owner/repo-1"]').fill("尚未提交的新名称")

    select_repo(page, "owner/repo-2")

    expect(page.locator('.setting-name[data-full-name="owner/repo-1"]')).to_have_value(
        "尚未提交的新名称"
    )


def test_labels_polling_recovers_from_transient_failure(page, site):
    adapter, base_url = site
    poll_requests = 0

    def handle_operation(route):
        nonlocal poll_requests
        if route.request.method == "GET":
            poll_requests += 1
            if poll_requests == 1:
                route.fulfill(status=503, json={"detail": "temporary poll failure"})
                return
        route.continue_()

    page.route("**/api/operations/*", handle_operation)
    connect_and_load(page, base_url)
    select_repo(page, "owner/repo-1")
    page.locator("#tab-labels").click()
    page.locator("#labels-input").fill("Java")
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#labels-submit").click()

    expect(page.locator("#labels-operation-items .badge.success")).to_have_count(1, timeout=5000)
    assert poll_requests >= 2
    assert adapter.label_calls == [("owner/repo-1", ("Java",))]


def test_settings_submission_uses_original_snapshot_while_form_changes(page, site):
    adapter, base_url = site
    adapter.operation_gate = threading.Event()
    connect_and_load(page, base_url)
    select_repo(page, "owner/repo-1")
    page.locator("#tab-settings").click()
    page.locator('.setting-name[data-full-name="owner/repo-1"]').fill("原始名称")
    page.locator("#visibility").select_option("private")
    page.locator("#settings-preview").click()
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#settings-submit").click()
    deadline = time.monotonic() + 3
    while not adapter.settings_calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert adapter.settings_calls == [("owner/repo-1", "原始名称", True)]

    page.locator('.setting-name[data-full-name="owner/repo-1"]').fill("后来修改的名称")
    page.locator("#visibility").select_option("public")
    adapter.operation_gate.set()

    expect(page.locator("#settings-operation-items .badge.success")).to_have_count(1)
    assert adapter.settings_calls == [("owner/repo-1", "原始名称", True)]


def test_labels_submission_uses_original_snapshot_while_form_changes(page, site):
    adapter, base_url = site
    adapter.operation_gate = threading.Event()
    connect_and_load(page, base_url)
    select_repo(page, "owner/repo-1")
    page.locator("#tab-labels").click()
    page.locator("#labels-input").fill("Java")
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#labels-submit").click()
    deadline = time.monotonic() + 3
    while not adapter.label_calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert adapter.label_calls == [("owner/repo-1", ("Java",))]

    page.locator("#labels-input").fill("ChangedLater")
    adapter.operation_gate.set()

    expect(page.locator("#labels-operation-items .badge.success")).to_have_count(1)
    assert adapter.label_calls == [("owner/repo-1", ("Java",))]


def memory_file(name, content):
    return {"name": name, "mimeType": "application/octet-stream", "buffer": content}


def test_upload_files_to_subdirectory_and_root_with_commit_links(page, site):
    adapter, base_url = site
    connect_and_load(page, base_url)
    select_repo(page, "owner/repo-1")
    page.locator("#tab-upload").click()
    page.locator("#upload-files").set_input_files([
        memory_file("hello.txt", b"hello"),
        memory_file("binary.bin", b"\x00\xff"),
    ])
    page.locator("#upload-directory").fill("photo/common")
    page.locator("#upload-message").fill("chore: browser upload")
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#upload-submit").click()

    expect(page.locator("#upload-operation-items .commit-link")).to_have_count(1)
    assert len(adapter.upload_calls) == 1
    _, files, overwrite, message = adapter.upload_calls[0]
    assert [(file.path, file.content) for file in files] == [
        ("photo/common/hello.txt", b"hello"),
        ("photo/common/binary.bin", b"\x00\xff"),
    ]
    assert overwrite is False
    assert message == "chore: browser upload"

    page.locator("#upload-files").set_input_files(memory_file("root.txt", b"root"))
    page.locator("#upload-directory").fill("")
    page.locator("#upload-submit").click()
    expect(page.locator("#upload-operation-items .commit-link")).to_have_count(1)
    assert adapter.upload_calls[-1][1][0].path == "root.txt"


def test_upload_conflict_and_overwrite_confirmation(page, site):
    adapter, base_url = site
    adapter.existing_paths["owner/repo-1"] = {"photo/common/a.txt"}
    connect_and_load(page, base_url)
    select_repo(page, "owner/repo-1")
    page.locator("#tab-upload").click()
    page.locator("#upload-files").set_input_files(memory_file("a.txt", b"new"))
    page.locator("#upload-directory").fill("photo/common")
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#upload-submit").click()

    expect(page.locator("#upload-operation-items .badge.conflict")).to_have_count(1)
    expect(page.locator("#upload-operation-items .operation-retry")).to_have_count(0)

    page.locator("#upload-overwrite").check()
    dialogs = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
    page.locator("#upload-submit").click()

    expect(page.locator("#upload-operation-items .badge.success")).to_have_count(1)
    assert any("覆盖同名文件" in message for message in dialogs)
    assert adapter.upload_calls[-1][2] is True


def test_upload_retry_shows_reconciled_success(page, site):
    adapter, base_url = site
    adapter.upload_errors["owner/repo-4"] = [
        PlatformError(ErrorKind.TEMPORARY, "temporary")
    ] * 3
    adapter.upload_results["owner/repo-4"] = [UploadResult(None, reconciled=True)]
    connect_and_load(page, base_url)
    select_repo(page, "owner/repo-4")
    page.locator("#tab-upload").click()
    page.locator("#upload-files").set_input_files(memory_file("a.txt", b"new"))
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#upload-submit").click()

    expect(page.locator("#upload-operation-items .operation-retry")).to_have_count(1)
    page.locator("#upload-operation-items .operation-retry").click()

    expect(page.locator("#upload-operation-items .reconciled")).to_have_text("核对成功")
    assert len(adapter.upload_calls) == 4


def test_upload_submission_uses_original_file_and_form_snapshot(page, site):
    adapter, base_url = site
    adapter.operation_gate = threading.Event()
    connect_and_load(page, base_url)
    select_repo(page, "owner/repo-1")
    page.locator("#tab-upload").click()
    page.locator("#upload-files").set_input_files(memory_file("original.bin", b"\x00\xff"))
    page.locator("#upload-directory").fill("photo/original")
    page.locator("#upload-message").fill("chore: original")
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#upload-submit").click()
    deadline = time.monotonic() + 3
    while not adapter.upload_calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(adapter.upload_calls) == 1

    page.locator("#upload-files").set_input_files(memory_file("changed.txt", b"changed"))
    page.locator("#upload-directory").fill("changed")
    page.locator("#upload-message").fill("chore: changed")
    adapter.operation_gate.set()

    expect(page.locator("#upload-operation-items .badge.success")).to_have_count(1)
    _, files, overwrite, message = adapter.upload_calls[0]
    assert [(file.path, file.content) for file in files] == [
        ("photo/original/original.bin", b"\x00\xff")
    ]
    assert overwrite is False
    assert message == "chore: original"


def test_upload_tab_preserves_files_and_renames_are_text(page, site):
    _, base_url = site
    connect_and_load(page, base_url)
    select_repo(page, "owner/repo-1")
    page.locator("#tab-upload").click()
    page.locator("#upload-files").set_input_files(memory_file(XSS_TEXT, b"safe"))

    expect(page.locator("#upload-file-list")).to_contain_text(XSS_TEXT)
    page.locator("#tab-readme").click()
    page.locator("#tab-upload").click()
    assert page.locator("#upload-files").evaluate("input => input.files.length") == 1
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#upload-submit").click()

    expect(page.locator("#upload-operation-items .badge.success")).to_have_count(1)
    expect(page.locator("img")).to_have_count(0)
    assert page.evaluate("window.__readmeXss") is None


def test_select_all_toggles_between_select_and_clear_current_filter(page, site):
    _, base_url = site
    connect_and_load(page, base_url)
    select_repo(page, "owner/repo-1")
    page.locator("#repository-filter").fill("repo-2")
    expect(page.locator("#select-all-repositories")).to_have_text("全选")

    page.locator("#select-all-repositories").click()

    expect(page.locator('.repository-row[data-full-name="owner/repo-2"] input')).to_be_checked()
    expect(page.locator("#select-all-repositories")).to_have_text("取消全选")
    page.locator("#select-all-repositories").click()
    expect(page.locator('.repository-row[data-full-name="owner/repo-2"] input')).not_to_be_checked()
    expect(page.locator('.repository-row[data-full-name="owner/repo-1"] input')).to_be_checked()
    expect(page.locator("#select-all-repositories")).to_have_text("全选")

    page.locator("#repository-filter").fill("")
    expect(page.locator('.repository-row[data-full-name="owner/repo-3"] input')).not_to_be_checked()
    expect(page.locator('.repository-row[data-full-name="owner/repo-4"] input')).not_to_be_checked()
    page.locator("#select-all-repositories").click()
    expect(page.locator(".repository-row input:checked")).to_have_count(4)
    expect(page.locator("#select-all-repositories")).to_have_text("取消全选")
    page.locator("#tab-settings").click()
    expect(page.locator(".setting-row")).to_have_count(4)

    page.locator("#select-all-repositories").click()
    expect(page.locator(".repository-row input:checked")).to_have_count(0)
    expect(page.locator("#select-all-repositories")).to_have_text("全选")


def test_exit_button_stops_local_server(page, site):
    _, base_url = site
    page.goto(base_url)
    page.on("dialog", lambda dialog: dialog.accept())

    page.locator("#exit-app").click()

    expect(page.locator("#exit-status")).to_have_text("工具已退出，可以关闭此页面")


def connect_platform(page, base_url, platform):
    page.goto(base_url)
    page.locator("#platform").select_option(platform)
    page.locator("#token").fill(TOKEN)
    page.locator("#connect").click()
    expect(page.locator(".repository-row")).to_have_count(4)


def test_github_connection_uses_platform_copy_and_adapter(page, platform_site):
    adapters, base_url = platform_site

    connect_platform(page, base_url, "github")

    expect(page.locator("#connection-title")).to_have_text("连接 GitHub")
    expect(page.locator("#platform")).to_have_value("github")
    expect(page.locator("footer")).to_contain_text("GitHub")
    expect(page.locator("footer")).not_to_contain_text("Gitee")
    assert adapters[PlatformKind.GITHUB].list_calls == 1
    assert adapters[PlatformKind.GITEE].list_calls == 0


def test_github_settings_omit_display_name_and_upload_remains_available(page, platform_site):
    adapters, base_url = platform_site
    adapter = adapters[PlatformKind.GITHUB]
    connect_platform(page, base_url, "github")
    select_repo(page, "owner/repo-2")
    page.locator("#tab-settings").click()

    expect(page.locator(".setting-name")).to_have_count(0)
    page.locator("#visibility").select_option("private")
    page.locator("#settings-preview").click()
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#settings-submit").click()
    expect(page.locator("#settings-operation-items .badge.success")).to_have_count(1)
    assert adapter.settings_calls == [("owner/repo-2", None, True)]

    page.locator("#tab-upload").click()
    expect(page.locator("#upload-submit")).to_be_enabled()
    page.locator("#upload-files").set_input_files(memory_file("hello.txt", b"hello"))
    page.locator("#upload-submit").click()
    expect(page.locator("#upload-operation-items .commit-link")).to_have_count(1)
    assert len(adapter.upload_calls) == 1


def test_github_topics_rules_are_visible_and_enforced(page, platform_site):
    adapters, base_url = platform_site
    adapter = adapters[PlatformKind.GITHUB]
    connect_platform(page, base_url, "github")
    select_repo(page, "owner/repo-1")
    page.locator("#tab-labels").click()

    expect(page.locator("#labels-title")).to_contain_text("Repository Topics")
    expect(page.locator("#labels-hint")).to_contain_text("小写字母、数字和连字符")
    page.locator("#labels-input").fill("Bad Label")
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#labels-submit").click()

    expect(page.locator("#labels-summary")).to_contain_text("Invalid GitHub repository topics")
    assert adapter.label_calls == []


def test_gitcode_upload_is_visibly_disabled(page, platform_site):
    _, base_url = platform_site
    connect_platform(page, base_url, "gitcode")
    select_repo(page, "owner/repo-1")
    page.locator("#tab-upload").click()

    expect(page.locator("#upload-summary")).to_contain_text("当前平台暂不支持多文件单提交")
    expect(page.locator("#upload-submit")).to_be_disabled()


def test_platform_switch_clears_old_repositories_draft_and_preview(page, platform_site):
    _, base_url = platform_site
    connect_platform(page, base_url, "gitee")
    select_repo(page, "owner/repo-1")
    enter_rules(page)
    preview(page)
    page.on("dialog", lambda dialog: dialog.accept())

    page.locator("#platform").select_option("github")

    expect(page.locator(".repository-row")).to_have_count(0)
    expect(page.locator("#old-text")).to_have_value("")
    expect(page.locator("#new-text")).to_have_value("")
    expect(page.locator("#preview-items .preview-item")).to_have_count(0)
    expect(page.locator("#commit")).to_be_disabled()
    expect(page.locator("#connection-title")).to_have_text("连接 GitHub")


def test_saved_github_session_restores_platform_and_capabilities(page, tmp_path):
    adapter = BrowserAdapter(GITHUB_CAPABILITIES, PlatformKind.GITHUB)
    credentials = BrowserCredentials()
    credentials.saved = None
    credentials.tokens[PlatformKind.GITHUB] = TOKEN
    credentials.last_platform = PlatformKind.GITHUB
    app = create_app(
        client_factories={PlatformKind.GITHUB: lambda token: adapter},
        credential_store=credentials,
        report_directory=tmp_path,
    )
    with serve(app) as base_url:
        page.goto(base_url)

        expect(page.locator("#platform")).to_have_value("github")
        expect(page.locator("#connection-title")).to_have_text("连接 GitHub")
        expect(page.locator("#connection-status")).to_contain_text("已恢复保存的会话", timeout=7000)
        expect(page.locator(".repository-row")).to_have_count(4)
        page.locator("#tab-settings").click()
        expect(page.locator(".setting-name")).to_have_count(0)
        assert TOKEN not in page.locator("body").inner_text()


def test_delayed_repository_response_cannot_repopulate_switched_platform(page, platform_site):
    _, base_url = platform_site
    connect_platform(page, base_url, "gitee")
    release = threading.Event()

    def delay_repositories(route):
        release.wait(2)
        route.continue_()

    page.route("**/api/repositories", delay_repositories)
    page.evaluate("void loadRepositories()")
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#platform").select_option("github")
    release.set()

    expect(page.locator("#connection-title")).to_have_text("连接 GitHub")
    expect(page.locator(".repository-row")).to_have_count(0)
    expect(page.locator("#connection-status")).to_contain_text("已切换到 GitHub")


def test_settings_show_repository_descriptions_as_text(page, site):
    _, base_url = site
    connect_and_load(page, base_url)
    select_repo(page, "owner/repo-1")
    select_repo(page, "owner/repo-2")
    select_repo(page, "owner/repo-3")

    page.locator("#tab-settings").click()

    expect(page.locator(".setting-description")).to_have_count(3)
    expect(page.locator(".setting-description").nth(0)).to_have_text("第一个仓库简介")
    expect(page.locator(".setting-description").nth(1)).to_have_text("暂无简介")
    expect(page.locator(".setting-description").nth(2)).to_have_text(XSS_TEXT)
    expect(page.locator("#settings-repositories img")).to_have_count(0)
    assert page.evaluate("window.__readmeXss") is None


def test_delete_file_preview_confirmation_and_commit(page, site):
    adapter, base_url = site
    adapter.file_outcomes["owner/repo-2"] = PlatformError(ErrorKind.NOT_FOUND, "missing")
    connect_and_load(page, base_url)
    select_repo(page, "owner/repo-1")
    select_repo(page, "owner/repo-2")
    page.locator("#tab-delete").click()
    page.locator("#delete-path").fill("images/abc.png")
    page.locator("#delete-message").fill("chore: browser delete")

    page.locator("#delete-preview").click()

    expect(page.locator("#delete-preview-items .deletion-preview-item")).to_have_count(2)
    expect(page.locator("#delete-preview-items .badge.ready")).to_have_count(1)
    expect(page.locator("#delete-preview-items .badge.missing")).to_have_count(1)
    expect(page.locator("#delete-submit")).to_be_enabled()
    assert adapter.delete_calls == []
    dialogs = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
    page.locator("#delete-submit").click()

    expect(page.locator("#delete-operation-items .badge.success")).to_have_count(1)
    expect(page.locator("#delete-operation-items .commit-link")).to_have_count(1)
    expect(page.locator("#delete-preview-items .deletion-preview-item")).to_have_count(0)
    expect(page.locator("#delete-preview-items .badge.ready")).to_have_count(0)
    assert adapter.delete_calls == [
        (
            "owner/repo-1",
            RemoteFile("images/abc.png", "main", "sha-repo-1"),
            "chore: browser delete",
            False,
        )
    ]
    assert "1 个仓库" in dialogs[0]
    assert "images/abc.png" in dialogs[0]


def test_delete_preview_is_invalidated_by_path_and_selection_changes(page, site):
    _, base_url = site
    connect_and_load(page, base_url)
    select_repo(page, "owner/repo-1")
    page.locator("#tab-delete").click()
    page.locator("#delete-path").fill("abc.png")
    page.locator("#delete-preview").click()
    expect(page.locator("#delete-submit")).to_be_enabled()

    page.locator("#delete-path").fill("images/abc.png")
    expect(page.locator("#delete-submit")).to_be_disabled()
    page.locator("#delete-preview").click()
    expect(page.locator("#delete-submit")).to_be_enabled()
    select_repo(page, "owner/repo-2")

    expect(page.locator("#delete-submit")).to_be_disabled()
    expect(page.locator("#delete-summary")).to_contain_text("重新检查")


def test_delete_operation_retry_shows_reconciled_success(page, site):
    adapter, base_url = site
    adapter.delete_errors["owner/repo-4"] = [
        PlatformError(ErrorKind.TEMPORARY, "temporary")
    ] * 3
    adapter.delete_results["owner/repo-4"] = [DeleteResult(None, reconciled=True)]
    connect_and_load(page, base_url)
    select_repo(page, "owner/repo-4")
    page.locator("#tab-delete").click()
    page.locator("#delete-path").fill("abc.png")
    page.locator("#delete-preview").click()
    expect(page.locator("#delete-submit")).to_be_enabled()
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#delete-submit").click()

    expect(page.locator("#delete-operation-items .operation-retry")).to_have_count(1)
    page.locator("#delete-operation-items .operation-retry").click()

    expect(page.locator("#delete-operation-items .reconciled")).to_have_text("核对成功")
    assert len(adapter.delete_calls) == 4


def test_settings_preview_button_does_not_squeeze_hint_into_a_narrow_column(page, site):
    _, base_url = site
    connect_and_load(page, base_url)
    select_repo(page, "owner/repo-1")
    page.locator("#tab-settings").click()

    action_row = page.locator(".action-row").filter(has=page.locator("#settings-preview"))
    row_width = action_row.evaluate("element => element.getBoundingClientRect().width")
    button_width = page.locator("#settings-preview").evaluate(
        "element => element.getBoundingClientRect().width"
    )
    hint_width = action_row.locator(".hint").evaluate(
        "element => element.getBoundingClientRect().width"
    )

    assert button_width < row_width * 0.5
    assert hint_width > 200


@pytest.mark.parametrize("platform", ["github", "gitcode"])
def test_file_delete_tab_is_hidden_for_unsupported_platforms(page, platform_site, platform):
    _, base_url = platform_site

    connect_platform(page, base_url, platform)

    expect(page.locator("#tab-delete")).to_be_hidden()
    assert page.evaluate("document.querySelector('#tab-delete').hidden") is True


def test_product_name_covers_repository_batch_management(page, site):
    _, base_url = site

    page.goto(base_url)

    expect(page).to_have_title("仓库批量管理台")
    expect(page.locator("h1")).to_have_text("仓库批量管理台")
    expect(page.locator("footer")).to_contain_text("REPOSITORY BATCH DESK")
