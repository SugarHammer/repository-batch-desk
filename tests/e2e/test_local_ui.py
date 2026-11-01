import os
import socket
import threading
import time
import urllib.request

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from readme_batch.app import create_app
from readme_batch.models import CommitInfo, FileSnapshot, Repository
from readme_batch.platforms.base import ErrorKind, PlatformError

TOKEN = "browser-test-token"
XSS_TEXT = "<img src=x onerror=window.__readmeXss=true>"


class BrowserAdapter:
    def __init__(self):
        self.repos = [
            Repository("owner", f"repo-{n}", f"owner/repo-{n}", "main", f"https://example/{n}")
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

    def list_repositories(self):
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


class BrowserCredentials:
    def __init__(self):
        self.saved = TOKEN

    def load(self):
        return self.saved

    def save(self, token):
        self.saved = token

    def clear(self):
        self.saved = None


@pytest.fixture
def browser():
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(
            headless=True, channel=os.environ.get("README_BATCH_BROWSER_CHANNEL") or None
        )
        yield instance
        instance.close()


@pytest.fixture
def site(tmp_path):
    adapter = BrowserAdapter()
    app = create_app(
        client_factory=lambda token: adapter,
        credential_store=BrowserCredentials(),
        report_directory=tmp_path,
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
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
    yield adapter, base_url
    server.should_exit = True
    thread.join(timeout=5)


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

    expect(page.locator(".repository-row")).to_have_count(4)
    expect(page.locator("#connection-status")).to_contain_text("已恢复保存的会话")
