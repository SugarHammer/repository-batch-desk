# 多平台支持实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 让现有本机工具以单平台会话支持 Gitee、GitHub 和 GitCode，复用 README 批处理、设置、主题、上传、重试和报告，同时明确限制平台不具备或未验证的能力。

**Architecture:** 通过平台枚举、能力声明和客户端工厂注册表接入三个 REST 适配器，共享现有批次与任务核心。GitHub 多文件上传使用 Blob、Tree、Commit 和非强推 Ref 更新保持每仓库一次提交；GitCode 多文件上传在验证前由界面和后端共同禁用。

**Tech Stack:** Python 3.11、FastAPI、Pydantic、httpx、keyring、pytest、respx、原生 JavaScript、Playwright、PyInstaller。

**Spec:** `docs/superpowers/specs/2026-11-03-multi-platform-support-design.md`

## Global Constraints

- 每次只激活一个平台；切换平台清空仓库选择、README 草稿和预览，并使旧任务失效。
- 不实现跨平台混合批次、GitHub 仓库改名、Issue/PR 标签或本地 clone/push。
- GitHub 的仓库标签只使用 Repository Topics；保留已有 Topics，不自动翻译或改写输入。
- GitHub 多文件上传必须保持每仓库一次提交，分支引用只以 `force=false` 更新。
- GitCode 多文件单提交未验证前，`upload_mode="unsupported"`；不得逐文件提交冒充单提交。
- 沿用最多 20 个文件、单文件 10MiB、总计 25MiB、默认不覆盖及不可变文件快照。
- 自动化测试不得对真实仓库执行写入；GitCode 实机验证不属于本计划的自动测试。
- 不新增第三方依赖；令牌、授权头、文件内容和 Base64 不得进入错误或报告。
- 除非用户另行明确要求，不执行 `git commit`、`git push` 或其他 Git 写操作。

## File Structure

- `src/readme_batch/models.py`：统一仓库模型及平台标识。
- `src/readme_batch/platforms/base.py`：平台枚举、能力模型、适配器协议和统一错误。
- `src/readme_batch/platforms/gitee.py`：现有 Gitee 行为与新增协议兼容。
- `src/readme_batch/platforms/github.py`：GitHub 认证、仓库、README、设置、Topics、限流和 Git 数据上传。
- `src/readme_batch/platforms/gitcode.py`：GitCode 独立字段解析、仓库、README、设置、项目标签和上传禁用。
- `src/readme_batch/platforms/__init__.py`：默认客户端工厂注册表。
- `src/readme_batch/credentials.py`：平台隔离凭据和最近平台偏好。
- `src/readme_batch/app.py`、`schemas.py`：单平台会话、能力响应及写操作能力校验。
- `src/readme_batch/operations.py`、`execution.py`：通用设置/主题调用与长限流处理。
- `src/readme_batch/reports.py`：报告平台标识。
- `src/readme_batch/static/`：平台选择、能力驱动控件和平台化文案。
- `tests/platform_contract.py`：不被 pytest 直接收集的共享适配器契约测试基类。
- `tests/test_github.py`、`tests/test_gitcode.py`：平台 HTTP 契约与特有行为。

## Review Focus

- 平台切换时旧请求可能已到达远端；新会话不得读取或改写旧任务结果，由 Task 2 和 Task 8 的切换测试覆盖。
- 多平台保存令牌后不得自动连接错误平台或跨平台清除令牌，由 Task 2 的凭据测试覆盖。
- GitHub 403 既可能是权限也可能是限流，长时间限流不得占住 worker，由 Task 3 的错误与执行器测试覆盖。
- GitHub 引用更新后响应丢失或 HEAD 并发移动不得造成重复提交/强推，由 Task 5 的核对和冲突测试覆盖。
- GitCode 缺少可靠现有标签或未验证上传能力时必须零写入，由 Task 7 的失败测试覆盖。

---

### Task 1: 平台模型、能力声明与 Gitee 兼容

**Files:**
- Modify: `src/readme_batch/models.py`
- Modify: `src/readme_batch/platforms/base.py`
- Modify: `src/readme_batch/platforms/gitee.py`
- Modify: `src/readme_batch/operations.py`
- Modify: `src/readme_batch/app.py`
- Modify: `src/readme_batch/reports.py`
- Test: `tests/test_gitee.py`
- Test: `tests/test_operations.py`
- Test: `tests/test_reports.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: 现有 `RepositoryPlatform`、`GiteeClient`、`OperationManager` 和报告模型。
- Produces: `PlatformKind(str, Enum)`、`UploadMode = Literal["single_commit", "unsupported"]`、`PlatformCapabilities(display_name: bool, visibility: bool, repository_topics: bool, upload_mode: UploadMode)`；`Repository.platform: PlatformKind = PlatformKind.GITEE`；`RepositoryPlatform.capabilities: PlatformCapabilities`、`validate_topics(labels: tuple[str, ...]) -> None`、`add_repository_topics(repository, labels) -> None`；设置方法的 `display_name: str | None`。

- [x] **Step 1: 写平台模型和 Gitee 兼容的失败测试**

增加以下断言：默认构造的 `Repository` 属于 Gitee；Gitee 能力为显示名称/可见性/主题均支持且 `upload_mode="single_commit"`；`add_repository_topics()` 仍发送原追加请求；仅修改可见性时 Gitee 仍发送当前显示名称；通用任务调用新主题方法；仓库 API 与新旧报告包含 `platform="gitee"`，序列化不包含能力对象或敏感信息。

- [x] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/test_gitee.py tests/test_operations.py tests/test_reports.py tests/test_api.py -v`
Expected: 因平台类型、能力或新方法不存在而失败，既有 Gitee HTTP 断言不变。

- [x] **Step 3: 实现统一类型并迁移 Gitee**

在 `base.py` 定义上述类型、三个平台能力常量和 `ErrorKind.UNSUPPORTED`。`Repository.platform` 使用默认值保持现有位置参数构造兼容；Gitee `validate_topics()` 不做额外限制。将任务管理器迁移到 `add_repository_topics()`，设置快照允许 `None`，Gitee 仅在适配器内部补齐平台要求的当前名称。仓库响应和两种 JSON 报告增加平台值，`UNSUPPORTED` 映射为本地 400。

- [x] **Step 4: 运行基础回归**

Run: `python -m pytest tests/test_gitee.py tests/test_operations.py tests/test_reports.py tests/test_api.py tests/test_batches.py -v`
Expected: 全部通过，Gitee 远端请求与既有行为无回归。

### Task 2: 单平台会话、客户端注册与凭据隔离

**Files:**
- Modify: `src/readme_batch/platforms/__init__.py`
- Modify: `src/readme_batch/credentials.py`
- Modify: `src/readme_batch/schemas.py`
- Modify: `src/readme_batch/app.py`
- Test: `tests/test_credentials.py`
- Test: `tests/test_api.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: Task 1 的 `PlatformKind`、`PlatformCapabilities`、`RepositoryPlatform` 和 `UNSUPPORTED`。
- Produces: `ClientFactory = Callable[[str], RepositoryPlatform]`；`create_app(client_factories: Mapping[PlatformKind, ClientFactory] | None = None, credential_store=None, report_directory=None)`；`CredentialStore.load(platform)`、`save(platform, token)`、`clear(platform)`、`load_last_platform() -> PlatformKind | None`、`save_last_platform(platform) -> None`；会话请求的 `platform: PlatformKind = GITEE` 和会话响应的 `platform/capabilities`。

- [x] **Step 1: 写会话与凭据失败测试**

覆盖：三个平台分别保存/读取/清除互不影响；最近平台只恢复对应令牌；缺省会话平台仍为 Gitee；GitHub 会话返回正确工厂和能力；切换平台清空仓库及 README 草稿，旧批次/任务提交或重试返回 409/失效结果；注销只清当前平台；GitHub 实际显示名称修改和 `upload_mode="unsupported"` 的上传均返回 400 且适配器零写入。

- [x] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/test_credentials.py tests/test_api.py tests/test_main.py -v`
Expected: 因平台参数、注册表或凭据偏好不存在而失败。

- [x] **Step 3: 实现平台化工厂与会话生命周期**

默认注册表延迟到本任务先接入现有 Gitee，后续任务加入 GitHub/GitCode；测试 fixture 改为显式工厂映射。`AppState` 记录当前平台，激活前使旧管理器失效并重置 `DraftRequest`。启动时读取最近平台及其令牌，无对应凭据则不激活。会话状态只在活动时返回平台与能力字典，错误文案改为通用“平台会话 required”。

- [x] **Step 4: 实现凭据隔离与能力守卫**

Keyring 服务使用 `readme-batch/{platform.value}`，用户名使用平台值；最近平台使用独立非敏感偏好键。`remember=False` 和注销只清当前平台。设置端点在存在实际名称变更时检查 `display_name`，标签端点先调用 `validate_topics()`，上传端点要求 `single_commit`，所有检查在创建任务前完成。

- [x] **Step 5: 运行会话、安全与任务回归**

Run: `python -m pytest tests/test_credentials.py tests/test_api.py tests/test_main.py tests/test_operations.py tests/test_batches.py -v`
Expected: 全部通过，本机 Host/Origin/进程令牌防护不变。

### Task 3: GitHub 仓库、README 与限流适配器

**Files:**
- Create: `src/readme_batch/platforms/github.py`
- Create: `tests/platform_contract.py`
- Create: `tests/test_github.py`
- Modify: `src/readme_batch/platforms/__init__.py`
- Modify: `src/readme_batch/execution.py`
- Test: `tests/test_execution.py`
- Test: `tests/test_gitee.py`

**Interfaces:**
- Consumes: Task 1 的协议和 Task 2 的注册表。
- Produces: `GitHubClient(token: str, transport: httpx.BaseTransport | None = None, clock: Callable[[], float] = time.time)`；`list_repositories()`、`read_readme()`、`update_readme()`；GitHub `capabilities`；其他暂未实现操作统一抛 `UNSUPPORTED`。

- [x] **Step 1: 写共享契约与 GitHub 核心失败测试**

`tests/platform_contract.py` 定义无 `Test` 前缀的契约基类，要求仓库分页/字段、BOM+CRLF 读取、预览 SHA 更新、提交链接和令牌脱敏；Gitee 与 GitHub 测试类分别提供 HTTP fixture 并继承它。

GitHub 特有断言：`GET /user/repos` 使用 Bearer、`application/vnd.github+json` 和 `X-GitHub-Api-Version: 2026-03-10`；README 路径/ref/Base64 正确；409 更新为冲突；401 为认证；403 权限不盲目重试；`x-ratelimit-remaining: 0` 的 403 和 429 均为限流；`Retry-After` 与 `x-ratelimit-reset` 转换正确；404 文案不暴露令牌也不武断断言仓库存在。

为执行器增加 `test_long_rate_limit_does_not_sleep_worker`：`retry_after=61` 时不调用 sleeper，项目以可手动重试失败结束；60 秒及以下沿用短退避。

- [x] **Step 2: 运行契约与执行器测试并确认失败**

Run: `python -m pytest tests/test_github.py tests/test_gitee.py tests/test_execution.py -v`
Expected: GitHub 客户端不存在且长等待行为不符合断言。

- [x] **Step 3: 实现 GitHub 核心客户端**

使用独立 httpx Client 和授权头，分页每页 100，跳过无默认分支仓库。严格解析统一仓库字段和 README Base64；更新发送 `message/content/sha/branch`，解析 commit SHA/URL。设置初始 `GITHUB_CAPABILITIES` 的上传为 `unsupported`，后续任务单独开启。未实现的方法明确抛不支持，不返回假成功。将 GitHub 工厂加入默认注册表。

- [x] **Step 4: 实现平台错误与长限流处理**

GitHub `_request()` 根据响应头、状态及具体写操作映射错误；从 reset epoch 用注入时钟计算非负秒数。`RepositoryExecutor` 对大于 60 秒的 `retry_after` 不睡眠，直接以 `retryable=True` 失败回调；短等待保持最多三次和指数退避。

- [x] **Step 5: 运行 GitHub 与共享核心回归**

Run: `python -m pytest tests/test_github.py tests/test_gitee.py tests/test_execution.py tests/test_batches.py -v`
Expected: 全部通过。

### Task 4: GitHub 可见性与 Repository Topics

**Files:**
- Modify: `src/readme_batch/platforms/github.py`
- Modify: `src/readme_batch/app.py`
- Test: `tests/test_github.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: Task 3 的 `GitHubClient._request()` 和 Task 2 的设置/主题守卫。
- Produces: GitHub `update_repository_settings()`、`add_repository_topics()` 和 `validate_topics()`；`display_name=False` 的完整设置行为。

- [x] **Step 1: 写设置和 Topics 失败测试**

覆盖：GitHub 可见性 PATCH 只发送 `private` 或等价可见性字段，不发送 `name/path`；传入实际显示名称变更抛不支持；Topics 先 GET `{"names":["existing"]}`，再 PUT `{"names":["existing","new-topic"]}`；重复输入不重复；GET 失败时零 PUT；不得调用 `/labels`。

Topics 输入只接受 `^[a-z0-9][a-z0-9-]{0,49}$`，大写、空格、中文、前导连字符和超过 50 字符均在 API 创建任务前返回 400 且零远端请求；合并后每仓库最多 20 个，超出时该仓库零 PUT。

- [x] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/test_github.py tests/test_api.py -v`
Expected: 新设置/Topics 行为尚未实现。

- [x] **Step 3: 实现 GitHub 设置与 Topics**

可见性只修改仓库私有状态。Topics 使用读取—合并—整体替换，保持已有顺序并追加新项；每个仓库独立处理合并后 20 个上限。`validate_topics()` 只验证本次输入格式和数量，API 在创建任务前调用，远端合并错误不产生该仓库写请求。

- [x] **Step 4: 运行平台与任务回归**

Run: `python -m pytest tests/test_github.py tests/test_api.py tests/test_operations.py -v`
Expected: 全部通过。

### Task 5: GitHub 多文件单提交与结果核对

**Files:**
- Modify: `src/readme_batch/platforms/github.py`
- Test: `tests/test_github.py`
- Test: `tests/test_operations.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: 现有 `UploadFile`、`UploadResult`、`reconcile_before` 和 Task 3 的 GitHub 请求方法。
- Produces: GitHub `upload_files(repository, files, overwrite, message, reconcile_before=False) -> UploadResult`；`GITHUB_CAPABILITIES.upload_mode="single_commit"`。

- [x] **Step 1: 写完整 Git 数据上传失败测试**

使用两个文件（含 `0x00/0xff` 和中文路径）覆盖以下场景：

- 预检全部 404 后，依次读取 HEAD/基础 Tree、创建 Base64 Blobs、创建带 `base_tree` 的 Tree、以旧 HEAD 为 parent 创建 Commit、以 `force=false` PATCH Ref；只有一次 Ref 更新，返回提交链接。
- 混合已存在/新文件在覆盖模式使用相同 Tree 替换语义；默认模式任一已存在则冲突且零 Blob、Tree、Commit、Ref 写入。
- HEAD 在提交期间移动导致 Ref 409，远端内容不同则保持冲突，绝不强推。
- Ref 更新后响应丢失，重新读取全部文件一致时返回 `UploadResult(None, True)`，不再次创建提交。
- 自动/手动重试未核对成功时重新读取 HEAD，不复用第一次 parent。
- 能力开启后本地上传 API 可创建任务，既有数量、大小和路径限制不变。

- [x] **Step 2: 运行上传测试并确认失败**

Run: `python -m pytest tests/test_github.py::test_upload_files_creates_one_commit_with_git_data tests/test_api.py -v`
Expected: GitHub 上传仍为不支持或调用序列不符合断言。

- [x] **Step 3: 实现预检、二进制读取与 Git 数据提交流程**

复用统一文件快照，先完成全部 Contents 预检；获取 `git/ref/heads/{default_branch}` 和 HEAD Commit 的 Tree。每个原始字节串以 Base64 创建 Blob，Tree entries 使用 `path/mode=100644/type=blob/sha`，创建 Commit 后仅非强推更新分支。解析提交 URL，缺失时使用仓库网页地址回退。

- [x] **Step 4: 实现不确定结果核对与重试**

`reconcile_before=True` 或临时/限流/冲突错误后比较所有目标文件字节；全部一致才返回核对成功。否则重新抛出原错误，下一次尝试从最新 HEAD 和预检开始。权限、参数和分支规则拒绝不得变成可重试成功。

- [x] **Step 5: 运行 GitHub 全部与上传任务回归**

Run: `python -m pytest tests/test_github.py tests/test_operations.py tests/test_api.py tests/test_gitee.py -v`
Expected: 全部通过，Gitee 上传调用不变。

### Task 6: GitCode 仓库、README 与设置适配器

**Files:**
- Create: `src/readme_batch/platforms/gitcode.py`
- Create: `tests/test_gitcode.py`
- Modify: `src/readme_batch/platforms/__init__.py`
- Test: `tests/test_gitee.py`

**Interfaces:**
- Consumes: Task 1 协议、Task 2 注册表和 Task 3 共享契约基类。
- Produces: `GitCodeClient(token: str, transport: httpx.BaseTransport | None = None)`；仓库列表、README 读写、显示名称/可见性和 `validate_topics()`；初始 `upload_mode="unsupported"`。

- [x] **Step 1: 写 GitCode 契约与特有失败测试**

继承共享契约并提供 GitCode 响应 fixture。额外断言：Base URL 为 `https://api.gitcode.com/api/v5`，所有请求使用 Bearer 而非 URL 令牌；仓库 `path/name/full_name/default_branch/private` 映射正确；README 更新使用 JSON Base64、SHA、分支和提交说明；提交 URL 缺失时安全回退；设置 PATCH 不发送 `path`，仅可见性时保留当前显示名称；429/5xx/401/403/404/409 映射及脱敏正确。

- [x] **Step 2: 运行 GitCode 测试并确认失败**

Run: `python -m pytest tests/test_gitcode.py -v`
Expected: 客户端不存在。

- [x] **Step 3: 实现独立 GitCode 适配器**

按协议实现分页、仓库、README 和设置，不继承 GiteeClient。严格校验响应结构，不能假设所有 Gitee 响应字段完全一致。`validate_topics()` 保留 GitCode 输入语义，上传和主题方法在下一任务前明确抛不支持。

- [x] **Step 4: 注册并运行三平台核心测试**

将 GitCode 工厂加入默认注册表。

Run: `python -m pytest tests/test_gitcode.py tests/test_github.py tests/test_gitee.py tests/test_api.py -v`
Expected: 全部通过。

### Task 7: GitCode 项目标签与上传安全禁用

**Files:**
- Modify: `src/readme_batch/platforms/gitcode.py`
- Test: `tests/test_gitcode.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: Task 6 的 `GitCodeClient._request()` 和 Task 2 的能力守卫。
- Produces: GitCode `add_repository_topics()`；始终拒绝的 `upload_files()` 和稳定的 `upload_mode="unsupported"`。

- [x] **Step 1: 写标签保留和零写入失败测试**

标签流程约定为 `GET /repos/{owner}/{repo}` 读取字符串数组 `tags`，合并去重后 `PUT /project_labels` 发送完整数组。覆盖：已有标签保留；重复项不重复；GET 404/403、`tags` 缺失、非列表或含非字符串时抛请求/权限错误且零 PUT；PUT 只发生一次。

上传测试断言能力与直接调用均为 `UNSUPPORTED`，本地 API 返回 400，且没有任何 Contents、Commits、Blobs 或其他远端写请求；不得出现逐文件循环。

- [x] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/test_gitcode.py tests/test_api.py -v`
Expected: 项目标签尚未实现，上传禁用缺少直接适配器断言。

- [x] **Step 3: 实现安全标签合并和上传禁用**

读取仓库元数据并严格验证 `tags`，不可靠时在任何写入前失败；合并后调用文档化项目标签整体替换接口。上传方法只抛统一不支持错误，能力不在本计划中开启；未来启用必须另附单提交证明与契约测试。

- [x] **Step 4: 运行 GitCode 与能力守卫回归**

Run: `python -m pytest tests/test_gitcode.py tests/test_api.py tests/test_operations.py -v`
Expected: 全部通过。

### Task 8: 平台选择、能力驱动界面与浏览器端到端

**Files:**
- Modify: `src/readme_batch/static/index.html`
- Modify: `src/readme_batch/static/styles.css`
- Modify: `src/readme_batch/static/app.js`
- Modify: `src/readme_batch/static/operations.js`
- Modify: `pyproject.toml`
- Test: `tests/e2e/test_local_ui.py`

**Interfaces:**
- Consumes: Task 2 的会话响应和三个平台完整能力。
- Produces: 平台选择器、`currentPlatform/capabilities`、平台切换重置、GitHub 仅可见性设置、Topics 文案和 GitCode 上传禁用。

- [x] **Step 1: 写平台浏览器失败测试**

测试 fixture 改为三平台工厂与平台化假凭据，使用 Playwright 覆盖：

- 默认 Gitee 原流程无回归；选择 GitHub 后连接请求带 `platform="github"`，标题/提示/页脚不显示 Gitee。
- GitHub 设置行没有显示名称输入，只提交可见性；主题页显示 Repository Topics，非法输入展示后端错误且零调用。
- GitHub 上传按钮可用并走通确认、提交链接和核对结果。
- GitCode 上传页显示“当前平台暂不支持多文件单提交”，按钮禁用且无法创建任务。
- 有活动时切换平台需要确认；确认后仓库、选择、README 草稿、预览和旧轮询被清理，旧任务结果不出现在新平台。
- 保存会话恢复时平台选择和能力正确，令牌不出现在 DOM 或控制台。

- [x] **Step 2: 运行新增浏览器测试并确认失败**

Run: `python -m pytest tests/e2e/test_local_ui.py -v`
Expected: 平台选择器及能力驱动行为不存在。

- [x] **Step 3: 实现平台会话界面**

增加三平台选择控件，连接请求显式发送平台。集中实现 `applySession(session)` 和 `resetForPlatformSwitch()`，保存当前能力、清理选择/草稿/预览/操作轮询并调用失效逻辑。切换确认后注销旧会话；恢复会话时应用同一能力入口，避免初始化与切换行为分叉。

- [x] **Step 4: 实现能力驱动设置、主题和上传**

GitHub 设置不渲染名称输入，变更快照只包含可见性；Gitee/GitCode 保留名称编辑。主题标签和确认文案按平台切换，上传根据 `upload_mode` 禁用并解释原因。所有动态仓库、标签和错误仍使用 `textContent`，不引入 HTML 注入。

- [x] **Step 5: 更新项目描述并运行完整浏览器测试**

将 `pyproject.toml` 描述改为多平台批量仓库工具，不调整依赖版本。

Run: `python -m pytest tests/e2e/test_local_ui.py -v`
Expected: 全部通过。

### Task 9: 完整验证、构建与独立审查

**Files:**
- Verify: 所有上述源码、测试、设计文档和本计划。
- Modify: 仅修复验证和审查中确认与本轮直接相关的问题。

**Interfaces:**
- Consumes: Tasks 1–8 的完整实现。
- Produces: 可测试、可构建、三平台行为明确的 Windows 本机工具。

- [x] **Step 1: 运行完整测试套件**

Run: `python -m pytest -v`
Expected: 全部通过，无跳过的 GitHub/GitCode 新测试，无真实远端写入。

- [x] **Step 2: 构建 Windows 可执行程序**

Run: `python -m PyInstaller -y readme-batch.spec`
Expected: 构建成功，`dist/readme-batch/readme-batch.exe` 存在，新增平台模块和静态资源均已打包。

- [x] **Step 3: 验证打包后的本机启动**

启动打包程序，验证健康接口、平台选择器、三个静态脚本和退出按钮；使用假会话/无远端写入方式检查能力文案，随后通过 `/api/shutdown` 正常退出。若打包程序无法注入假适配器，只验证本机启动和静态能力，真实平台流程以 pytest/Playwright 结果为证据，不连接真实仓库。

- [x] **Step 4: 独立审查并检查最终工作区**

由独立 reviewer 检查计划符合性、凭据隔离、会话生命周期、GitHub 提交原子性、Topics 保留、GitCode 零写入降级和测试缺口。Critical/Important 问题先用失败测试复现再修复，重跑全套。最后运行 `git status --short`，清理测试/构建产生的非交付临时文件，按用户选择处理分支交付；不得自行提交或推送。
