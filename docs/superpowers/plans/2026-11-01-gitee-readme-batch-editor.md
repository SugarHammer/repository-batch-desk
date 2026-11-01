# Gitee README Batch Editor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Windows local graphical tool that previews and commits fixed-text replacements across selected Gitee repositories without cloning them.

**Architecture:** A FastAPI application bound to `127.0.0.1` serves a vanilla HTML/CSS/JavaScript interface. A pure replacement core, a Gitee API adapter, and a thread-safe batch coordinator keep content transformation, network access, and commit safety separate; Windows Credential Manager and local JSON reports handle optional token persistence and audit results.

**Tech Stack:** Python 3.12 (minimum 3.11), FastAPI, Uvicorn, HTTPX, Keyring, vanilla JavaScript, Pytest, RESPX, Playwright, PyInstaller.

**Spec:** `docs/superpowers/specs/2026-11-01-gitee-readme-batch-editor-design.md`

## Global Constraints

- Bind only to `127.0.0.1`; do not deploy or expose the application on a public interface.
- Modify only selected repositories' default-branch root `README.md`; never create a missing README or touch another file.
- Use literal, case-sensitive replacement of every match; empty old text is invalid, empty new text is allowed.
- Require a valid per-repository diff preview and second confirmation before any update call.
- Invalidate previews when repository selection, old text, new text, or commit message changes.
- Update with the exact blob SHA captured during preview; never resolve a conflict by silently reusing a newer SHA.
- Limit repository work to three concurrent workers; one repository's failure must not stop the others.
- Keep tokens out of source, configuration, reports, logs, exception messages, and test fixtures.
- Default to in-memory token storage; use Windows Credential Manager only after explicit opt-in.
- Do not implement GitHub, GitCode, regular expressions, section replacement, Pull Requests, or automatic rollback in this version.
- Run all tests on Windows before real Gitee verification; use only a dedicated disposable Gitee repository for remote tests.

## Review Focus

- **Exactly full repository pages:** a 100-item page must not be mistaken for the final page; Task 2 adds `test_list_repositories_reads_all_pages`.
- **Lost response after a remote commit:** retrying must not create a duplicate commit or bypass SHA protection; Task 3 adds `test_conflict_requires_new_preview_instead_of_automatic_retry`.
- **Credential-bearing request URLs:** HTTP errors must not expose Gitee's `access_token` query parameter; Task 2 adds `test_errors_never_expose_token`.
- **HTML inside README content:** preview must render it as text rather than execute markup; Task 6 adds `test_readme_html_is_rendered_as_text`.
- **Filtered lists and edited rules:** hiding a selected row must not deselect it, and any rule edit must disable stale commits; Task 6 adds `test_filter_preserves_selection_and_edits_invalidate_preview`.

---

### Task 1: Project Skeleton, Domain Models, and Literal Replacement

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `src/readme_batch/__init__.py`
- Create: `src/readme_batch/models.py`
- Create: `src/readme_batch/replace.py`
- Test: `tests/test_replace.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Repository`, `FileSnapshot`, `CommitInfo`, `ReplacementResult`, and `replace_literal(source: str, old: str, new: str) -> ReplacementResult` for all later tasks.

- [ ] **Step 1: Initialize the project and test configuration**

Initialize Git, configure a `src` layout, and declare Python `>=3.11`. Put runtime dependencies `fastapi`, `uvicorn`, `httpx`, and `keyring` in the project dependencies; put `pytest`, `respx`, `playwright`, and `pyinstaller` in a `dev` optional dependency group. Ignore virtual environments, Python caches, test caches, Playwright output, build directories, and packaged binaries.

- [ ] **Step 2: Write failing replacement tests**

Define frozen dataclasses `Repository(owner, name, full_name, default_branch, html_url)`, `FileSnapshot(path, branch, content, sha)`, `CommitInfo(sha, url)`, and `ReplacementResult(content, match_count)`. Add tests proving:

```python
assert replace_literal("a old b old", "old", "new") == ReplacementResult("a new b new", 2)
assert replace_literal("Old old", "old", "new") == ReplacementResult("Old new", 1)
assert replace_literal("\ufeffa\r\nold\r\n", "old", "") .content == "\ufeffa\r\n\r\n"
```

Also test multi-line Chinese text, no match, empty old text raising `ValueError`, and old equal to new returning the original content with its actual match count.

- [ ] **Step 3: Run the tests and verify the initial failure**

Run: `python -m pytest tests/test_replace.py -v`
Expected: collection or test failure because the models and replacement function do not exist.

- [ ] **Step 4: Implement the models and pure replacement function**

Implement `replace_literal` with literal string operations only. Do not normalize newlines, strip a BOM, interpret regular expressions, or special-case Markdown.

- [ ] **Step 5: Run the replacement tests and full suite**

Run: `python -m pytest -v`
Expected: all replacement tests PASS.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml .gitignore src tests
git commit -m "feat: add literal README replacement core"
```

### Task 2: Gitee Repository Adapter

**Files:**
- Create: `src/readme_batch/platforms/__init__.py`
- Create: `src/readme_batch/platforms/base.py`
- Create: `src/readme_batch/platforms/gitee.py`
- Test: `tests/test_gitee.py`

**Interfaces:**
- Consumes: Task 1 `Repository`, `FileSnapshot`, and `CommitInfo`.
- Produces: `RepositoryPlatform`, `ErrorKind`, `PlatformError`, and `GiteeClient(token: str, transport: httpx.BaseTransport | None = None)` with `list_repositories()`, `read_readme(repository)`, and `update_readme(repository, snapshot, new_content, message)`.

- [ ] **Step 1: Write failing adapter contract tests**

Use RESPX to test the exact Gitee behavior:

```python
repos = client.list_repositories()
assert len(repos) == 101
assert repos[100].full_name == "owner/repo-101"
assert client.read_readme(repos[0]).path == "README.md"
```

Cover two pages with `per_page=100`, default branches, Base64 content containing Chinese/CRLF/BOM, the `ref` query parameter, and update form fields `content`, `sha`, `message`, and `branch`. Assert update returns the response commit SHA and URL.

- [ ] **Step 2: Add failing error-mapping and redaction tests**

Map 401 to `AUTH`, 403 to `PERMISSION`, 404 to `NOT_FOUND`, 409 to `CONFLICT`, 429 to `RATE_LIMIT`, 5xx to `TEMPORARY`, and other failures to `REQUEST`. Include `test_errors_never_expose_token`: for query and form failures, `str(error)` and its safe message must contain neither the token nor a credential-bearing URL.

- [ ] **Step 3: Run adapter tests and verify failure**

Run: `python -m pytest tests/test_gitee.py -v`
Expected: FAIL because the platform protocol and Gitee client are absent.

- [ ] **Step 4: Implement the platform protocol and Gitee client**

Define the three-method `RepositoryPlatform` protocol in `base.py`. In `gitee.py`, call `/api/v5/user/repos`, `/api/v5/repos/{owner}/{repo}/contents/README.md`, and the corresponding PUT endpoint. Use HTTPX parameters/data for escaping, paginate until a short page, decode Base64 as UTF-8 without newline normalization, and preserve the preview SHA for updates. Raise only sanitized `PlatformError` instances across the adapter boundary.

- [ ] **Step 5: Run adapter and replacement tests**

Run: `python -m pytest tests/test_gitee.py tests/test_replace.py -v`
Expected: PASS, including pagination, encoding, SHA propagation, error mapping, and token redaction.

- [ ] **Step 6: Commit**

```bash
git add src/readme_batch/platforms tests/test_gitee.py
git commit -m "feat: add Gitee README API adapter"
```

### Task 3: Preview, Commit, Invalidation, and Retry Coordinator

**Files:**
- Create: `src/readme_batch/batches.py`
- Test: `tests/test_batches.py`

**Interfaces:**
- Consumes: Task 1 models and replacement function; Task 2 `RepositoryPlatform`, `PlatformError`, and `ErrorKind`.
- Produces: `Batch`, `BatchItem`, `PreviewState`, `CommitState`, and `BatchManager` with `start_preview`, `start_commit`, `start_retry`, `get_batch`, and `invalidate`; Task 4 reports and Task 5 API routes consume these types.

- [ ] **Step 1: Define batch states and write failing preview tests**

`BatchManager(adapter, max_workers=3, max_attempts=3, sleeper=time.sleep, on_complete=None)` creates a batch from `start_preview(repositories, old, new, commit_message) -> str`. `get_batch(batch_id) -> Batch` returns a snapshot. Each item has preview state `READY`, `NO_MATCH`, `UNCHANGED`, or `READ_FAILED`, plus repository, snapshot, new content, match count, unified diff, safe error, commit state, and commit info.

Test that preview performs no updates, generates a unified diff, skips no-match/unchanged/missing files, isolates read failures, and calls `on_complete` only after all items finish.

- [ ] **Step 2: Write failing commit-safety tests**

Cover these exact behaviors:

- `start_commit(batch_id)` submits only `READY` items using the stored commit message.
- `invalidate()` makes every uncommitted batch stale; `start_commit` then raises `StalePreviewError` without updating any repository.
- A conflict sets `CONFLICT`; `test_conflict_requires_new_preview_instead_of_automatic_retry` proves no second update and no SHA refresh occurs.
- Successful items cannot be committed again; retry affects one failed item only.
- Temporary errors stop after `max_attempts`; injected sleep records backoff without real waiting.

- [ ] **Step 3: Write a failing concurrency test**

Use a fake thread-safe adapter that records active calls. Start work for seven repositories and assert the observed maximum is at most three, all seven reach a terminal state, and one raised exception does not cancel the others.

- [ ] **Step 4: Run coordinator tests and verify failure**

Run: `python -m pytest tests/test_batches.py -v`
Expected: FAIL because batch types and coordinator are absent.

- [ ] **Step 5: Implement the thread-safe coordinator**

Use a `ThreadPoolExecutor`, per-manager lock, immutable returned snapshots, and a monotonically increasing revision. Store old/new text and commit message only in memory. Retry only `TEMPORARY` and `RATE_LIMIT`; never retry `CONFLICT`, auth, permission, missing-file, or content errors. `start_retry(batch_id, full_name)` re-reads only read failures and retries only the existing snapshot for temporary update failures.

- [ ] **Step 6: Run coordinator and earlier tests**

Run: `python -m pytest -v`
Expected: all tests PASS with no real network calls.

- [ ] **Step 7: Commit**

```bash
git add src/readme_batch/batches.py tests/test_batches.py
git commit -m "feat: coordinate safe README batch updates"
```

### Task 4: Credential Persistence and JSON Reports

**Files:**
- Create: `src/readme_batch/credentials.py`
- Create: `src/readme_batch/reports.py`
- Test: `tests/test_credentials.py`
- Test: `tests/test_reports.py`

**Interfaces:**
- Consumes: Task 3 `Batch` snapshots.
- Produces: `CredentialStore` with `load`, `save`, and `clear`; `JsonReportStore(directory: Path).save(batch: Batch) -> Path` for application wiring.

- [ ] **Step 1: Write failing credential-store tests**

Using a fake Keyring backend, prove `load() -> str | None`, `save(token) -> None`, and `clear() -> None` use one fixed service name, never write a project file, and tolerate a missing saved credential during clear.

- [ ] **Step 2: Write failing report tests**

Save a batch containing successful, skipped, and failed items to a temporary directory. Assert the JSON includes batch time, commit message, repository full name, branch, status, match count, commit ID/URL, and safe error. Assert serialized content excludes token, complete README text, old text, and new text.

- [ ] **Step 3: Run tests and verify failure**

Run: `python -m pytest tests/test_credentials.py tests/test_reports.py -v`
Expected: FAIL because persistence modules are absent.

- [ ] **Step 4: Implement credential and report stores**

Use Keyring's Windows backend through the Keyring API. Write reports atomically to an application-data directory supplied by the caller; serialize only non-sensitive batch result fields and use a unique batch ID for the filename.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/readme_batch/credentials.py src/readme_batch/reports.py tests/test_credentials.py tests/test_reports.py
git commit -m "feat: persist credentials safely and export task reports"
```

### Task 5: Local HTTP API and Application Wiring

**Files:**
- Create: `src/readme_batch/schemas.py`
- Create: `src/readme_batch/app.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: Tasks 1–4 models, manager, adapter, credential store, and report store.
- Produces: `create_app() -> FastAPI` and the local API consumed by the browser interface.

- [ ] **Step 1: Write failing API tests with fakes**

Use FastAPI `TestClient` and fake services; never use a real token. Specify these routes:

- `POST /api/session` with `{token, remember}` creates the in-memory Gitee client and optionally saves the credential.
- `DELETE /api/session` clears the in-memory client and saved credential.
- `GET /api/repositories` returns the adapter's repositories.
- `PUT /api/draft` accepts `{selected, old, new, commit_message}` and calls `invalidate()`.
- `POST /api/previews` starts a preview from the current draft and returns a batch ID.
- `GET /api/batches/{batch_id}` returns status and items without old/new text or snapshots' original content.
- `POST /api/batches/{batch_id}/commit` starts the stored, still-valid preview.
- `POST /api/batches/{batch_id}/retry` with `{full_name}` retries one item.
- `GET /api/health` returns `{"status": "ok"}`.

Assert empty old text, empty commit message, unknown repositories, stale previews, and unknown batch IDs produce safe 4xx responses without calling update.

- [ ] **Step 2: Run API tests and verify failure**

Run: `python -m pytest tests/test_api.py -v`
Expected: FAIL because schemas and application routes do not exist.

- [ ] **Step 3: Implement schemas, application state, and routes**

Create Pydantic request schemas and explicit response serialization. Keep the active token/client, loaded repositories, current draft, and one manager in application state. Construct `BatchManager` with three workers and `JsonReportStore.save` as `on_complete`. Return only diff and non-sensitive result metadata to the browser.

- [ ] **Step 4: Run API and full tests**

Run: `python -m pytest -v`
Expected: PASS; no test performs real HTTP outside the local test client.

- [ ] **Step 5: Commit**

```bash
git add src/readme_batch/schemas.py src/readme_batch/app.py tests/test_api.py
git commit -m "feat: expose local batch editor API"
```

### Task 6: Browser Interface and Interaction Tests

**Files:**
- Create: `src/readme_batch/static/index.html`
- Create: `src/readme_batch/static/styles.css`
- Create: `src/readme_batch/static/app.js`
- Modify: `src/readme_batch/app.py`
- Test: `tests/e2e/test_local_ui.py`

**Interfaces:**
- Consumes: every Task 5 API route.
- Produces: the complete local graphical workflow at `http://127.0.0.1:8765/`.

- [ ] **Step 1: Write failing browser tests**

Start the app on an ephemeral loopback port with fake platform data and Playwright Chromium. Test loading/checking repositories, entering multiline replacement text, generating a diff, requiring confirmation before commit, displaying commit links, and retrying a failed item. Add these exact tests:

- `test_readme_html_is_rendered_as_text`: preview a README containing `<img src=x onerror=window.__readmeXss=true>`; assert the diff text contains that literal string, the page has no `img` element, and `window.__readmeXss` is undefined.
- `test_filter_preserves_selection_and_edits_invalidate_preview`: check a repository, hide it with the filter, clear the filter, and assert it is still checked; after a valid preview, change old text, new text, commit message, and selection one at a time, asserting each change disables commit until another preview.

Also assert no update request occurs during preview and no credential appears in page text or browser console errors.

- [ ] **Step 2: Run browser tests and verify failure**

Run: `python -m playwright install chromium`
Run: `python -m pytest tests/e2e/test_local_ui.py -v`
Expected: FAIL because the interface is not implemented.

- [ ] **Step 3: Implement the four interface regions**

Build connection/repository selection, replacement rules, per-repository previews, and execution results. Use text nodes/`textContent` for README and error content, a `<pre>` for diffs, disabled controls during inconsistent states, and a second confirmation showing repository and replacement counts. Filtering must be display-only. Every draft-changing control calls `PUT /api/draft` and invalidates the current preview.

- [ ] **Step 4: Serve the static interface**

Mount the packaged static assets at `/` in `app.py` while preserving `/api` routes. Do not add a frontend build system or external CDN dependency.

- [ ] **Step 5: Run browser and full tests**

Run: `python -m pytest -v`
Expected: all unit, API, and browser tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/readme_batch/static src/readme_batch/app.py tests/e2e
git commit -m "feat: add local graphical batch editor"
```

### Task 7: Windows Startup and Executable Packaging

**Files:**
- Create: `src/readme_batch/__main__.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: Task 5 `create_app` and Task 6 static assets.
- Produces: `python -m readme_batch` and a double-clickable Windows executable.

- [ ] **Step 1: Write failing startup tests**

Mock Uvicorn and `webbrowser.open`; assert `main()` uses host `127.0.0.1`, port `8765`, opens the matching local URL, and does not enable debug/reload or listen on `0.0.0.0`.

- [ ] **Step 2: Run startup tests and verify failure**

Run: `python -m pytest tests/test_main.py -v`
Expected: FAIL because the entry point is absent.

- [ ] **Step 3: Implement startup and verify the source application**

Implement `main() -> None`, then run `python -m readme_batch` in the background. Open `http://127.0.0.1:8765/api/health` and the UI, verify both, and stop only the process started by this task.

- [ ] **Step 4: Build and smoke-test the Windows executable**

Run PyInstaller with the package static files included and the default `dist` output layout. Launch the generated `.exe`, repeat the health/UI smoke check, close it, and verify no token or report is written into the source directory.

- [ ] **Step 5: Run the full suite and commit**

Run: `python -m pytest -v`
Expected: PASS.

```bash
git add src/readme_batch/__main__.py tests/test_main.py
git commit -m "feat: add Windows application entry point"
```

### Task 8: Full Verification and Disposable Gitee Repository Test

**Files:**
- Modify only if a verified defect requires it; add or update the owning test first.

**Interfaces:**
- Consumes: the complete application from Tasks 1–7.

- [ ] **Step 1: Run all automated checks on the deliverable**

Run: `python -m pytest -v`
Expected: no failures or skipped critical browser tests. Confirm generated caches/build outputs are ignored and no test artifact contains a token.

- [ ] **Step 2: Exercise the mocked complete user scenario in the browser**

Load repositories, select several, replace multiline text, inspect differing per-repository results, confirm once at the preview action and again at commit, inspect commit links, and retry a synthetic failure. Verify no-match and missing-README repositories never issue updates.

- [ ] **Step 3: Prepare safe real-Gitee verification**

Ask the user to create or identify a dedicated disposable Gitee repository containing a UTF-8 `README.md`. The user enters a minimally scoped token directly into the running local UI; never request the token in chat or write it into a file. Do not run this step against formal projects.

- [ ] **Step 4: Run the real happy path and no-match path**

Select only the disposable repository, replace known text, verify the preview, submit, and open the actual Gitee commit link to compare content. Run a second preview with absent text and verify no commit is created.

- [ ] **Step 5: Run the real conflict check**

Generate a new preview, edit the same README through the Gitee web UI, then attempt the tool commit. Expected: the item is marked as a conflict, the web edit remains intact, and no automatic retry or forced overwrite occurs. If Gitee's actual conflict response differs from the adapter mapping, first add the observed sanitized response to `tests/test_gitee.py`, then fix the mapping and rerun the suite.

- [ ] **Step 6: Verify local persistence boundaries**

Confirm a non-remembered token disappears after restart, an explicitly remembered token is stored only in Windows Credential Manager, and the JSON report contains results but no token, full README, old text, or new text.

- [ ] **Step 7: Commit any verified fixes and report results**

```bash
git add <only-files-with-regression-tests-and-verified-fixes>
git commit -m "fix: handle verified Gitee integration behavior"
```

Report automated test results, packaged executable path, real commit URL, conflict-check result, and any verification the user still needs to perform.
