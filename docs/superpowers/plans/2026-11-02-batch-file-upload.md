# 批量上传文件实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户将最多 20 个图片或小文件一次上传到所有选中 Gitee 仓库，每个仓库只产生一次提交，并安全处理同名冲突和丢失响应。

**Architecture:** 本地 API 将严格校验后的文件转换为不可变二进制快照，扩展 `OperationManager` 产生上传任务。`GiteeClient` 先预检所有目标路径，再用 `/commits` 一次提交全部 actions；不确定结果通过远端二进制内容比对核对。前端新增上传页签并复用通用轮询和结果组件。

**Tech Stack:** Python 3.11、FastAPI、Pydantic、httpx、pytest、respx、原生 JavaScript、Playwright、PyInstaller。

**Spec:** `docs/superpowers/specs/2026-11-02-batch-file-upload-design.md`

## Global Constraints

- 最多 20 个文件，单文件最大 10MiB（10 × 1024 × 1024 字节），单批总大小最大 25MiB（25 × 1024 × 1024 字节）；空文件允许。
- 使用 JSON Base64，不新增 `python-multipart` 或其他第三方依赖。
- 默认不覆盖；任一预检冲突时该仓库不得发送 `/commits`。
- 每仓库默认分支上只发送一次多文件提交，不做文本差异预览。
- 报告和任务响应不得包含文件内容或 Base64；不得对真实 Gitee 仓库执行测试写入。
- 除非用户另行明确要求，不执行 `git commit`、`git push` 或其他 Git 写操作。

## Review Focus

- 二进制字节和中文路径不得在传输中损坏；由 Task 1 的 Base64 与 URL 编码测试覆盖。
- 同名文件或预检错误不得造成部分上传；由 Task 1 的零提交冲突测试覆盖。
- 响应丢失不得产生重复提交；由 Task 1 的内容核对测试覆盖。
- 非法路径、重复名称、超限或非法 Base64 不得创建任务；由 Task 3 的 API 测试覆盖。
- 运行中修改文件/表单不得改变快照，文件名不得注入 HTML；由 Task 4 的浏览器测试覆盖。

---

### Task 1: 上传模型与 Gitee 多文件适配器

**Files:**
- Modify: `src/readme_batch/models.py`
- Modify: `src/readme_batch/platforms/base.py`
- Modify: `src/readme_batch/platforms/gitee.py`
- Test: `tests/test_gitee.py`

**Interfaces:**
- Consumes: 现有 `Repository`、`CommitInfo`、`PlatformError` 和 `GiteeClient._request()`。
- Produces: `UploadFile(path: str, content: bytes)`、`UploadResult(commit_info: CommitInfo | None, reconciled: bool)`；`upload_files(repository, files: tuple[UploadFile, ...], overwrite: bool, message: str) -> UploadResult`。

- [ ] **Step 1: 写多文件、冲突和核对的失败测试**

在 `tests/test_gitee.py` 增加：

- `test_upload_files_creates_one_commit_with_binary_base64_and_encoded_paths`：两个文件含 `0x00/0xff` 和中文路径，预检均 404，断言仅一次 `/commits`，actions 全为 `create`，`encoding=base64`，分支和提交说明正确。
- `test_upload_files_conflict_does_not_commit`：任一目标 GET 返回已存在，默认模式抛出 `CONFLICT`，断言没有 `/commits`。
- `test_upload_files_overwrite_uses_update_and_create`：一个目标存在、一个 404，覆盖模式断言对应 `update/create` 且仍只有一次提交。
- `test_lost_upload_response_reconciles_matching_bytes_without_second_commit`：预检后 POST 500，核对 GET 返回相同二进制内容，断言 `reconciled=True` 且 POST 仅一次。
- `test_lost_upload_response_with_different_content_remains_temporary`：核对内容不同，断言抛出 `TEMPORARY`，不得伪报成功。

提交响应使用 `{"sha":"commit-1","html_url":"https://example/commit/1"}`。内容响应使用真实 Base64 文件结构。

- [ ] **Step 2: 运行新测试并确认失败**

Run: `python -m pytest tests/test_gitee.py -v`
Expected: 新测试因上传模型或方法不存在而失败，既有测试不失效。

- [ ] **Step 3: 实现上传模型和适配器**

在模型和协议中添加上述类型。`GiteeClient` 增加内部读取二进制文件方法，GET 路径用 `quote(path, safe="/")` 编码，404 返回缺失，其他错误保持原分类。

`upload_files()` 先完成所有路径预检，再按覆盖规则构造 actions；原始字节只做一次 Base64 编码。`/commits` 使用查询参数令牌和 JSON 请求体，解析 `sha/html_url`，网页链接缺失时按现有规则回退。临时、限流或冲突错误后重新读取全部目标文件，只有全部字节一致才返回 `UploadResult(None, True)`；核对失败或内容不同则重新抛出原错误。

- [ ] **Step 4: 运行适配器测试**

Run: `python -m pytest tests/test_gitee.py -v`
Expected: 全部通过。

### Task 2: 上传 OperationManager、冲突状态与报告

**Files:**
- Modify: `src/readme_batch/operations.py`
- Modify: `src/readme_batch/reports.py`
- Test: `tests/test_operations.py`
- Test: `tests/test_reports.py`

**Interfaces:**
- Consumes: Task 1 的 `UploadFile`、`UploadResult` 和 `upload_files()`。
- Produces: `OperationKind.UPLOAD`、`OperationItemState.CONFLICT`、`OperationManager.start_upload(repositories, files, overwrite, message) -> str`；操作项 `commit_info/reconciled` 和对应报告字段。

- [ ] **Step 1: 写上传任务与报告的失败测试**

在 `tests/test_operations.py` 使用门控假适配器覆盖：

- 上传任务保存文件、覆盖开关和提交说明快照，运行后修改原输入不改变调用；
- 成功项保存提交链接和 `reconciled`；
- 冲突为终态、不可重试，且其他仓库仍可成功；
- 临时失败手动重试只执行一个仓库，核对成功不会重复提交；
- 现有设置和标签测试保持通过。

在 `tests/test_reports.py` 增加上传报告测试，断言提交 SHA/URL、冲突和核对字段存在，序列化中不包含原始字节、Base64、文件参数快照或令牌。

- [ ] **Step 2: 运行任务与报告测试并确认失败**

Run: `python -m pytest tests/test_operations.py tests/test_reports.py -v`
Expected: 因上传类型、字段或方法不存在而失败。

- [ ] **Step 3: 实现上传任务和结果扩展**

新增 `UPLOAD` 和 `CONFLICT`，将冲突纳入终态且禁止重试。扩展操作项提交信息和核对标记，通用成功回调保存这些值。`start_upload()` 对文件元组及每个字节串做不可变快照，调用适配器的 `upload_files()`；设置和标签结果保持原样。

- [ ] **Step 4: 扩展 JSON 报告**

`save_operation()` 增加可选 `commit_sha/commit_url/reconciled`，状态直接使用 `item.state.value`。不得加入文件、目录、覆盖开关、提交说明或 Base64 参数快照。

- [ ] **Step 5: 运行任务、报告与执行器回归测试**

Run: `python -m pytest tests/test_operations.py tests/test_reports.py tests/test_execution.py tests/test_batches.py -v`
Expected: 全部通过。

### Task 3: 上传本地 API、路径校验和大小限制

**Files:**
- Modify: `src/readme_batch/schemas.py`
- Modify: `src/readme_batch/app.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: Task 2 的 `start_upload()` 和 Task 1 的 `UploadFile`。
- Produces: `UploadFileRequest(name, content_base64)`、`UploadOperationRequest` 和 `POST /api/operations/uploads`。

- [ ] **Step 1: 写 API 失败测试**

扩展 `FakeAdapter.upload_files()` 后覆盖：

- 两个有效文件及一个空文件创建上传任务，根目录和 `photo/common` 路径拼接正确，内容字节一致；
- 非法 Base64、空/重复文件名、路径分隔符、控制字符、`.`/`..`、非法目录、空提交说明、空文件列表和未知仓库均返回 4xx 且零写入；
- 21 个空文件返回 413；10MB+1 字节单文件返回 413；总大小超过 25MB 返回 413；
- 响应不包含 Base64，现有操作端点和安全测试无回归。

- [ ] **Step 2: 运行 API 测试并确认失败**

Run: `python -m pytest tests/test_api.py -v`
Expected: 新端点 404/405 或请求模型不存在。

- [ ] **Step 3: 添加请求模型与规范化函数**

`UploadFileRequest` 包含 `name: str`、`content_base64: str`；`UploadOperationRequest` 包含 `selected`、`directory=""`、`overwrite=False`、`message="chore: 批量上传文件"` 和 `files`。在应用中实现单一规范化入口，依次验证仓库、消息、目录、文件名、Base64 和固定大小限制，全部通过后才创建任务。

- [ ] **Step 4: 实现上传端点**

严格 Base64 解码，按清理后的目录和文件名生成 `UploadFile` 元组，调用 `start_upload()` 并返回 202/任务 ID。输入错误使用 400，超限使用 413；任务查询沿用 Task 2 的提交信息和冲突状态。

- [ ] **Step 5: 运行完整本地 API 测试**

Run: `python -m pytest tests/test_api.py -v`
Expected: 全部通过。

### Task 4: 上传页签、二进制读取与浏览器端到端

**Files:**
- Modify: `src/readme_batch/static/index.html`
- Modify: `src/readme_batch/static/styles.css`
- Modify: `src/readme_batch/static/operations.js`
- Test: `tests/e2e/test_local_ui.py`

**Interfaces:**
- Consumes: Task 3 的上传 API、Task 2 的任务结果、现有 `startOperation/pollOperation/renderOperation`。
- Produces: 文件上传页签、文件检查与 Base64 快照、覆盖确认、提交链接、冲突和核对结果。

- [ ] **Step 1: 写浏览器失败测试**

扩展 `BrowserAdapter.upload_files()` 和门控能力，使用 Playwright 内存文件覆盖：

- 根目录及子目录上传两个文件（含二进制），确认后只有一次仓库调用，结果提交链接可见；
- 默认同名冲突无重试，覆盖模式确认包含警告；
- 临时失败可重试，核对成功标记可见；
- 门控任务运行中更换文件、目录和提交说明，适配器仍收到原始文件和参数；
- 页签切换保留 `input.files`，文件名 XSS 只按文本显示；
- 现有页签和退出测试保持通过。

- [ ] **Step 2: 运行新增浏览器测试并确认失败**

Run: `python -m pytest tests/e2e/test_local_ui.py -v`
Expected: 新测试因上传页签或控件不存在而失败。

- [ ] **Step 3: 实现上传视图和通用结果扩展**

增加文件多选、目录、覆盖开关、提交说明、文件列表和执行按钮。扩展 `selectOperationView`、按钮控制、轮询完成和重试逻辑以支持 `upload`；结果项显示提交链接及 `reconciled`，冲突复用现有徽章样式。

- [ ] **Step 4: 实现文件校验和 Base64 快照**

执行时先检查 20 个、10MB、25MB、重复名称和目录，再固定 `File` 引用及表单值。用 `file.arrayBuffer()` 读取并分块转换为 Base64，读取失败不创建任务。确认包含仓库数、文件数、总大小、目录及覆盖警告。页签和仓库选择变化不清空文件输入。

- [ ] **Step 5: 运行完整浏览器测试**

Run: `python -m pytest tests/e2e/test_local_ui.py -v`
Expected: 全部通过。

### Task 5: 完整验证、构建与独立审查

**Files:**
- Verify: 所有上述源码、测试、设计文档和本计划。
- Modify: 仅修复验证和审查中确认与本轮直接相关的问题。

**Interfaces:**
- Consumes: Tasks 1–4 的完整实现。
- Produces: 可测试、可构建的批量上传桌面工具。

- [ ] **Step 1: 运行完整测试套件**

Run: `python -m pytest -v`
Expected: 全部通过，无跳过的新上传测试。

- [ ] **Step 2: 构建 Windows 可执行程序**

Run: `python -m PyInstaller -y readme-batch.spec`
Expected: 构建成功，`dist/readme-batch/readme-batch.exe` 存在。

- [ ] **Step 3: 验证打包后的本机启动**

启动打包程序，验证健康接口、上传页签、`operations.js` 和原有退出按钮，随后通过 `/api/shutdown` 正常退出；不得连接或写入真实仓库。

- [ ] **Step 4: 独立审查并检查最终工作区**

生成包含所有修改的审查包，由独立 reviewer 检查计划符合性、二进制安全、重复提交、UI 生命周期和测试缺口。Critical/Important 问题用失败测试复现后修复并重跑全套。最后运行 `git status --short`，清理临时文件，按用户选择处理分支交付。
