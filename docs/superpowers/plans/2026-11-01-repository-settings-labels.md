# 仓库设置与标签批量操作实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 Gitee 批量工具中加入逐仓库改名、批量公开/私有设置和批量追加仓库标签，同时保持 README 替换流程兼容。

**Architecture:** 扩展仓库元数据和 Gitee 适配器，抽出负责并发、重试和认证停止的 `RepositoryExecutor`。README 的 `BatchManager` 保留预览语义，新 `OperationManager` 用同一执行器实现设置和标签任务；本地 API 与页签界面分别暴露两类操作。

**Tech Stack:** Python 3.11、FastAPI、Pydantic、httpx、pytest、respx、原生 JavaScript、Playwright、PyInstaller。

**Spec:** `docs/superpowers/specs/2026-11-01-repository-settings-labels-design.md`

## Global Constraints

- `Repository.name` 始终表示 URL 路径；任何设置请求都不得发送 Gitee `path` 参数。
- 本轮不实现文件上传、标签替换/删除、Issue 标签或 Git 版本标签。
- 标签只追加，名称逐仓库编辑，可见性取值为 `keep/public/private`。
- 所有远端写入测试使用假适配器或 `respx`，不得写入真实 Gitee 仓库。
- 不新增第三方依赖，不改变现有本机 API 安全中间件和 README 公共接口。
- 除非用户另行明确要求，不执行 `git commit`、`git push` 或其他 Git 写操作。

## Review Focus

- 空白名称、未变化名称和相同可见性不得产生远端请求；由 Task 4 的 API 校验测试覆盖。
- 表单布尔值和名称必须正确序列化且绝不包含 `path`；由 Task 1 的适配器测试覆盖。
- 认证失败后排队仓库不得继续请求；由 Task 2 和 Task 3 的执行器/任务测试覆盖。
- 手动重试不得重复成功项，权限错误不得显示为可重试；由 Task 3 和 Task 4 覆盖。
- 页签切换、表单编辑和脚本样标签不得改变任务快照或注入 HTML；由 Task 5 的浏览器测试覆盖。

---

### Task 1: 仓库元数据与 Gitee 写接口

**Files:**
- Modify: `src/readme_batch/models.py`
- Modify: `src/readme_batch/platforms/base.py`
- Modify: `src/readme_batch/platforms/gitee.py`
- Test: `tests/test_gitee.py`

**Interfaces:**
- Consumes: 现有 `Repository`、`RepositoryPlatform` 和 `GiteeClient._request()`。
- Produces: `Repository(display_name: str = "", private: bool = False)` 新字段；`update_repository_settings(repository: Repository, display_name: str, private: bool | None) -> None`；`add_project_labels(repository: Repository, labels: tuple[str, ...]) -> None`。

- [ ] **Step 1: 写仓库列表与新接口的失败测试**

在 `tests/test_gitee.py` 增加：

- `test_list_repositories_reads_display_name_and_private()`：响应含 `name="显示名称"`、`path="repo-1"`、`private=true`，断言三个字段含义不混淆。
- `test_update_repository_settings_sends_name_and_private_without_path()`：拦截 `PATCH /repos/owner/repo-1`，断言表单 `name="新名称"`、`private="false"`，且没有 `path`。
- `test_visibility_only_update_sends_current_display_name()`：调用时传当前名称和 `True`，断言名称为当前显示名称、`private="true"`。
- `test_add_project_labels_posts_json_array()`：拦截 `POST /repos/owner/repo-1/project_labels`，断言 JSON 为 `["Java", "Spring Boot"]`。

为现有 `repo_payload()` 增加 `private` 测试数据。

- [ ] **Step 2: 运行新测试并确认失败**

Run: `python -m pytest tests/test_gitee.py -v`
Expected: 新测试因模型字段或适配器方法不存在而失败，既有测试不失效。

- [ ] **Step 3: 实现模型和适配器方法**

在 `Repository` 末尾添加带默认值的 `display_name` 和 `private`，保持现有五参数构造兼容。列表解析使用 `item["name"]` 和 `item["private"]`。

在协议和 `GiteeClient` 中实现上述两个方法。设置请求使用 `PATCH` 和表单数据，始终包含 `access_token`、`name`，仅在 `private is not None` 时包含布尔 `private`，不包含 `path`；标签请求使用 `params` 传令牌、`json=list(labels)` 传数组。成功响应无需解析。

- [ ] **Step 4: 运行适配器测试**

Run: `python -m pytest tests/test_gitee.py -v`
Expected: 全部通过。

### Task 2: 通用 RepositoryExecutor 与 README 接入

**Files:**
- Create: `src/readme_batch/execution.py`
- Modify: `src/readme_batch/batches.py`
- Test: `tests/test_execution.py`
- Test: `tests/test_batches.py`

**Interfaces:**
- Consumes: `Repository`、`PlatformError`、`ErrorKind`。
- Produces: `RepositoryExecutor(max_workers: int = 3, max_attempts: int = 3, sleeper: Callable[[float], None] = time.sleep)` 和 `submit(repositories, operation, on_success, on_failure, on_start=None) -> None`；操作签名为 `(Repository) -> T`，成功回调为 `(Repository, T) -> None`，失败回调为 `(Repository, PlatformError, bool) -> None`，开始回调为 `(Repository) -> None`。

- [ ] **Step 1: 写通用执行器失败测试**

在 `tests/test_execution.py` 覆盖：

- 三个并发上限及每个成功结果只回调一次；
- 临时错误按 `0.25、0.5` 退避并最终以 `retryable=True` 失败；
- `Retry-After` 优先于默认退避；
- 认证错误后，单 worker 队列中的后续仓库不调用操作，统一返回认证失败；
- 非 `PlatformError` 异常转化为安全的 `REQUEST` 错误，不泄露异常内容。

可使用 `threading.Event` 和线程安全计数器，等待任务完成的超时时间为 3 秒。

- [ ] **Step 2: 运行执行器测试并确认失败**

Run: `python -m pytest tests/test_execution.py -v`
Expected: 因 `readme_batch.execution` 不存在而失败。

- [ ] **Step 3: 实现 RepositoryExecutor**

使用 `ThreadPoolExecutor`、锁和认证失败标志。每个仓库依次尝试操作，临时/限流错误按现有策略重试；开始回调只在线程实际取得任务且未发生全局认证失败时调用。最终失败回调的布尔值表示是否可手动重试。认证失败停止尚未开始的远端操作。

- [ ] **Step 4: 让 BatchManager 复用执行器**

将 `BatchManager` 的线程池、认证标志和 `_read_with_retries()` 调度替换为 `RepositoryExecutor`，保留其批次锁、预览差异、状态、失效和报告逻辑。把 README 更新中的丢失响应核对包装在单次操作内：若远端已匹配则返回核对成功，否则重新抛出原错误，由执行器决定是否重试。保持所有现有公共方法和状态值不变。

- [ ] **Step 5: 运行执行器和 README 批次测试**

Run: `python -m pytest tests/test_execution.py tests/test_batches.py -v`
Expected: 全部通过，尤其 SHA 冲突、丢失响应核对和认证停止测试。

### Task 3: 设置/标签 OperationManager 与报告

**Files:**
- Create: `src/readme_batch/operations.py`
- Modify: `src/readme_batch/reports.py`
- Test: `tests/test_operations.py`
- Test: `tests/test_reports.py`

**Interfaces:**
- Consumes: Task 2 的 `RepositoryExecutor` 和 Task 1 的适配器方法。
- Produces: `OperationManager.start_settings(repositories, names, visibility) -> str`、`start_labels(repositories, labels) -> str`、`get_operation(operation_id)`、`start_retry(operation_id, full_name)`、`invalidate()`；`JsonReportStore.save_operation(operation) -> Path`。

- [ ] **Step 1: 写操作任务的失败测试**

在 `tests/test_operations.py` 使用假适配器覆盖：

- 设置任务以提交时名称和可见性快照调用适配器，后续修改原输入字典不影响任务；
- 标签任务按原顺序发送去重后的元组；
- 部分仓库失败不影响其他仓库成功；
- 临时错误最终 `failed/retryable=true`，权限错误 `failed/retryable=false`；
- 手动重试只执行一个失败项，成功项不重复；
- 运行中重试、未知任务和不可重试项抛出 `StaleOperationError` 或 `KeyError`；
- 认证失败后排队项不再调用适配器。

在 `tests/test_reports.py` 增加设置任务 JSON 报告测试，断言包含 `kind`、仓库、状态和错误，不包含令牌或参数快照。

- [ ] **Step 2: 运行操作与报告测试并确认失败**

Run: `python -m pytest tests/test_operations.py tests/test_reports.py -v`
Expected: 因操作模型、管理器或报告方法不存在而失败。

- [ ] **Step 3: 实现操作模型和 OperationManager**

定义 `OperationKind(settings/labels)`、`OperationState(running/complete)`、`OperationItemState(pending/running/success/failed)`、不可变操作和项目数据类。每个任务保存独立参数快照；通过 Task 2 执行器更新运行状态，所有项目终态后触发一次完成回调。仅允许对已完成任务中的可重试失败项手动重试。`invalidate()` 增加修订号，使旧任务结果不能作为当前任务继续操作。

设置操作调用 `update_repository_settings()`；`visibility=keep` 对应 `None`，否则映射布尔值。标签操作调用 `add_project_labels()`。

- [ ] **Step 4: 实现 JSON 操作报告**

保留现有 `JsonReportStore.save()` 签名和 README 格式，新增 `save_operation()`，沿用唯一临时文件和原子替换。报告只保存公开状态字段及必要错误，不保存访问令牌或完整参数快照。

- [ ] **Step 5: 运行操作、报告和回归测试**

Run: `python -m pytest tests/test_operations.py tests/test_reports.py tests/test_execution.py tests/test_batches.py -v`
Expected: 全部通过。

### Task 4: 本地 API、校验与会话生命周期

**Files:**
- Modify: `src/readme_batch/schemas.py`
- Modify: `src/readme_batch/app.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: Task 3 的 `OperationManager`、Task 1 的仓库字段。
- Produces: `SettingsOperationRequest`、`LabelsOperationRequest`；`POST /api/operations/settings`、`POST /api/operations/labels`、`GET /api/operations/{id}`、`POST /api/operations/{id}/retry`。

- [ ] **Step 1: 写 API 失败测试**

扩展 `FakeAdapter` 和仓库夹具后覆盖：

- 仓库列表返回 `display_name/private`；
- 设置请求创建任务并只执行实际变化项，公开/私有映射正确；
- 空白名称、空选择、未知仓库、越界 `names` 键、无实际变化返回 4xx 且零远端写入；
- 标签请求去空白、忽略空项、稳定去重，空标签返回 4xx；
- 查询未知任务为 404，无效重试为 409；
- 新会话和退出登录使旧操作失效；
- 新端点仍要求本机进程令牌，响应不包含会话令牌。

- [ ] **Step 2: 运行 API 测试并确认失败**

Run: `python -m pytest tests/test_api.py -v`
Expected: 新端点 404 或模型字段断言失败。

- [ ] **Step 3: 添加请求模型与应用状态**

`SettingsOperationRequest` 包含 `selected: list[str]`、`names: dict[str, str] = {}`、`visibility: Literal["keep", "public", "private"] = "keep"`；`LabelsOperationRequest` 包含 `selected` 和 `labels: list[str]`。在 `AppState` 增加 `operations`，激活会话时创建 `OperationManager`，替换/删除会话时使其失效。

- [ ] **Step 4: 实现端点和序列化**

解析选中仓库时去重并验证已加载。设置名称去除首尾空白，验证映射键属于选择集；用 `repo.display_name or repo.name` 作为当前名称，同时排除无变化项。标签按规格清理并稳定去重。返回 202 和 `{"id": ...}`；查询响应包含任务类型、状态、创建时间和逐项结果。重试复用 `RetryRequest`。

- [ ] **Step 5: 运行完整本地 API 测试**

Run: `python -m pytest tests/test_api.py -v`
Expected: 全部通过，现有 README 和安全测试无回归。

### Task 5: 页签、设置/标签界面与浏览器流程

**Files:**
- Modify: `src/readme_batch/static/index.html`
- Modify: `src/readme_batch/static/styles.css`
- Modify: `src/readme_batch/static/app.js`
- Create: `src/readme_batch/static/operations.js`
- Test: `tests/e2e/test_local_ui.py`

**Interfaces:**
- Consumes: Task 4 的 API、现有 `api()`、`selected`、`repositories` 和仓库刷新流程。
- Produces: 三个页签、设置变更预览与确认、标签追加确认、通用操作结果和单仓库重试。

- [ ] **Step 1: 写浏览器失败测试**

扩展 `BrowserAdapter` 的设置/标签假实现，增加：

- 修改两个仓库名称并批量私有，确认后断言适配器参数，成功结果可见且仓库资料刷新；
- 私有转公开的确认文字包含正确数量，取消时零写入；
- 输入 `Java， Spring Boot,Java` 后只追加两个标签，切换页签保留仓库选择；
- 临时失败项出现重试按钮，权限失败项没有重试按钮；
- 名称或标签含 `<img src=x onerror=...>` 时仅按文本显示，不创建图片或执行脚本；
- 现有 README 测试继续通过。

- [ ] **Step 2: 运行新增浏览器测试并确认失败**

Run: `python -m pytest tests/e2e/test_local_ui.py -v`
Expected: 新测试因页签或控件不存在而失败。

- [ ] **Step 3: 实现页签与共享选择事件**

保留现有 README DOM 和控件 ID，将其包入 README 页视图；新增设置和标签视图及按钮。页签切换只改变可见视图，不清空选择。仓库选择或列表刷新时分发自定义事件，供操作界面重绘；所有远端文本继续使用 `textContent`。

- [ ] **Step 4: 实现设置和标签操作**

在 `operations.js` 中实现：

- 设置行显示只读路径、名称输入和当前可见性；编辑或选择变化使旧预览失效；
- “检查变更”生成实际变更清单，无变化时禁用执行；执行确认包含公开数量；
- 标签按中英文逗号解析、去空白和稳定去重，确认文字显示标签与仓库数；
- 统一轮询操作状态并渲染等待/执行中/成功/失败；仅 `retryable=true` 显示重试；
- 设置任务完成后刷新仓库资料并保留有效选择；任务参数在创建请求时固定。

样式沿用现有面板、按钮、徽章和响应式布局，不引入框架或外部资源。

- [ ] **Step 5: 运行浏览器和现有 UI 回归测试**

Run: `python -m pytest tests/e2e/test_local_ui.py -v`
Expected: 全部通过。

### Task 6: 完整验证与交付检查

**Files:**
- Verify: 所有上述源码、测试、设计文档和本计划。
- Modify: 仅修复验证中确认与本轮改动直接相关的问题。

**Interfaces:**
- Consumes: Tasks 1–5 的完整实现。
- Produces: 可测试、可构建的桌面工具交付物。

- [ ] **Step 1: 运行完整测试套件**

Run: `python -m pytest -v`
Expected: 全部通过，无跳过的新功能测试。

- [ ] **Step 2: 构建 Windows 可执行程序**

Run: `python -m PyInstaller readme-batch.spec`
Expected: 构建成功，`dist/readme-batch/readme-batch.exe` 存在。

- [ ] **Step 3: 验证打包后的本机启动**

使用测试会话或健康检查启动打包程序，确认本机页面和静态资源可访问，随后正常退出；不得连接或写入真实仓库。

- [ ] **Step 4: 检查最终工作区**

Run: `git status --short`
Expected: 仅保留源码、测试、文档及用户需要的构建产物变化；删除本轮产生的缓存、日志或临时文件，不删除原有文件。最终说明测试证据、改动范围和未实施的文件上传功能。
