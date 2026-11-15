# Gitee、GitHub 与 GitCode 多平台支持设计

## 背景与目标

现有工具支持对 Gitee 仓库进行 README 精确替换、显示名称与可见性设置、追加仓库标签和多文件上传。此次扩展让用户在同一套本机界面中选择 Gitee、GitHub 或 GitCode，并沿用仓库选择、预览确认、逐仓库进度、失败重试和 JSON 报告。

采用单平台会话：每次只连接一个平台，切换平台时清空仓库选择与草稿，并使旧预览和旧任务失效。不在同一批次中混合不同平台的仓库，也不同时执行多个平台的后台任务。

核心目标是复用现有批处理语义，同时明确呈现平台差异，不把不支持的功能静默替换成其他行为。

## 范围与限制

本轮包含：

- Gitee、GitHub、GitCode 的平台选择、独立令牌存储和单平台会话；
- 三个平台的仓库列表、README 读取、替换预览、SHA 更新、冲突与结果核对；
- 三个平台的可见性设置；
- Gitee、GitCode 的独立显示名称设置；
- 三个平台的仓库主题/标签追加，其中 GitHub 明确使用 Repository Topics；
- Gitee 现有多文件单提交和 GitHub 基于 Git 数据 API 的多文件单提交；
- 平台能力声明、界面禁用与后端强制校验；
- 平台化错误映射、限流处理、报告和契约测试。

本轮不包含：

- 跨平台混合批次或多个同时活动的会话；
- GitHub 仓库改名，因为改名会改变仓库路径，不等同于独立显示名称；
- Issue/PR 标签、Git 版本标签、分支选择、Pull Request 或分支保护绕过；
- GitCode 多文件上传的静默逐文件提交降级；
- 本地 clone/push 执行模型、Git LFS、大文件分片或断点续传；
- 自动化测试对真实仓库执行写入。

GitCode 多文件上传只有在确认能够保持“每仓库一次提交”后才启用。未完成验证前，其上传能力明确为 `unsupported`，界面禁用且后端拒绝请求；不得循环调用单文件接口冒充原有语义。

## 方案概述

采用“能力声明 + 平台适配器 + 共享批处理核心”。

- `GiteeClient`、`GitHubClient`、`GitCodeClient` 分别负责认证、分页、字段映射、平台端点和 HTTP 错误解释。
- `BatchManager`、`OperationManager`、`RepositoryExecutor`、替换与差异逻辑、结果核对和报告继续处理平台无关流程。
- 能力声明同时驱动界面与本地 API 校验，避免平台判断散落在 JavaScript 和任务管理器中。
- 不为统一实现而引入本地 Git 工作区，也不复制三套界面或三套任务系统。

## 平台与能力模型

新增 `PlatformKind`：`gitee`、`github`、`gitcode`。

新增不可变 `PlatformCapabilities`，至少包含：

- `display_name: bool`：是否支持不改变路径的独立显示名称；
- `visibility: bool`：是否支持公开/私有设置；
- `repository_topics: bool`：是否支持追加仓库主题/标签；
- `upload_mode: "single_commit" | "unsupported"`：文件上传语义。

初始能力矩阵：

| 能力 | Gitee | GitHub | GitCode |
|---|---|---|---|
| README 全流程 | 支持 | 支持 | 支持 |
| 独立显示名称 | 支持 | 不支持 | 支持 |
| 可见性 | 支持 | 支持 | 支持 |
| 仓库主题/标签 | 支持 | Repository Topics | 项目标签 |
| 多文件一次提交 | 支持 | 支持，Git 数据 API | 验证前禁用 |

`Repository` 增加 `platform: PlatformKind`。现有 `name` 继续表示 URL 路径，`display_name` 只表示平台真正提供的独立显示名称；GitHub 不将 `description` 或仓库 `name` 伪装成 `display_name`。

新增 `UnsupportedCapabilityError` 或等价错误类型。界面禁用不是安全边界，本地 API 在创建任务前必须再次检查能力，不支持的操作不得产生远端写入。

## 会话、客户端注册与凭据

`create_app` 接收按 `PlatformKind` 索引的客户端工厂注册表，保留测试注入假适配器的能力。`activate(platform, token, remember)` 创建当前平台适配器以及新的批次和任务管理器。

`POST /api/session` 请求增加 `platform`。为兼容现有调用，缺省值可以为 `gitee`，新界面始终显式发送平台。创建新会话前使旧管理器失效，并清空仓库、选择和相关草稿。

`GET /api/session` 返回：

- `active`；
- 活动时的 `platform`；
- 活动时的 `capabilities`。

不返回访问令牌。`DELETE /api/session` 只注销当前平台，不影响其他平台已保存的凭据。

Keyring 凭据按平台隔离：

- `readme-batch/gitee`；
- `readme-batch/github`；
- `readme-batch/gitcode`。

另保存非敏感的 `last-platform` 偏好。启动时只自动激活该平台对应的已保存令牌；没有对应令牌则保持未连接。未选择“记住令牌”或退出当前平台时，只清除当前平台凭据，不清除其他平台凭据。

GitHub 和 GitCode 使用 `Authorization: Bearer`，GitHub 同时集中设置 Accept 和 API 版本头。不得为了复用代码而把令牌统一放到 URL 查询参数中。

## 共享适配器接口与设置语义

保留 `RepositoryPlatform` 作为 coarse-grained 接口，包含能力查询、仓库列表、README 读写、设置、追加主题和上传。平台不支持的方法抛出明确的不支持错误，任务层不伪造成功。

`update_repository_settings` 的显示名称参数改为可选：只有实际修改显示名称时才传值。Gitee/GitCode 适配器在仅修改可见性但平台要求 `name` 时，自行发送当前显示名称；GitHub 仅发送可见性字段。

现有 `/api/operations/settings` 请求结构可以继续使用。GitHub 界面不提供显示名称编辑，后端也拒绝 GitHub 的实际名称变更请求；可见性变更继续排除无变化项，并在私有转公开时显示确认。

内部 `add_project_labels` 语义改为 `add_repository_topics`。现有本地 `/api/operations/labels` 和报告类型可以保留，以避免无意义的 API 迁移，但界面应根据平台显示“仓库标签”或“仓库主题（Topics）”。

## 平台适配器

### Gitee

现有行为保持不变：

- 分页读取授权用户仓库，跳过没有默认分支的空仓库；
- Contents API 读取和更新根目录 `README.md`；
- 更新已有 README 时发送预览 blob SHA；
- `PATCH /repos/{owner}/{repo}` 修改显示名称和可见性，不发送 `path`；
- `POST /project_labels` 追加项目标签；
- `POST /commits` 的 actions 保持每仓库一次多文件提交。

### GitHub

认证使用 Bearer 令牌。列表使用 `GET /user/repos` 和分页，字段映射到统一 `Repository`。fine-grained PAT 只返回其可访问的仓库，工具不承诺绕过令牌资源范围。

README 使用 Contents API：读取默认分支根目录 `README.md`，严格 Base64 解码，更新时发送预览 SHA、默认分支和提交说明。现有差异、冲突和丢失响应核对逻辑不变。

可见性使用 `PATCH /repos/{owner}/{repo}`。组织策略、SAML SSO、分支保护和仓库 rulesets 产生的拒绝按权限或请求错误展示，不重试绕过。

仓库标签使用 Repository Topics，而不是 Issue Labels：

1. `GET /repos/{owner}/{repo}/topics` 读取现有 Topics；
2. 与本次输入合并并按首次出现顺序去重；
3. `PUT /repos/{owner}/{repo}/topics` 整体替换为合并结果。

输入在创建任务前按 GitHub Topics 规则验证，不自动翻译、音译或静默改写无效标签。整体替换请求必须包含读取到的已有 Topics，不能只发送新增项。

### GitCode

使用 `https://api.gitcode.com/api/v5` 和 Bearer 认证，分别实现字段与响应解析，不通过继承 GiteeClient 默认所有行为。

仓库列表、README 和设置使用已记录的 API v5 端点。显示名称与路径保持现有语义，设置请求不发送 `path`。

项目标签采用读取后合并再整体替换的方式，使用项目标签或仓库 `tags` 的官方等价能力。若响应无法提供可靠的现有标签，适配器必须在写入前失败，不得直接覆盖可能存在的标签。

GitCode 多文件提交先进行独立验证：只有官方接口或实机测试证明能够原子产生一个分支提交时，才声明 `single_commit`。验证前上传方法始终抛出能力不支持错误。任何后续验证都只能使用专门测试仓库并获得用户明确授权，不纳入自动测试套件。

## GitHub 多文件单提交

沿用现有应用层限制、目标路径校验、默认不覆盖、不可变文件快照和上传后二进制核对。GitHub 不循环调用 Contents API，而使用 Git 数据 API：

1. 预检所有目标路径，任一非覆盖文件已存在时，在创建任何 Blob 前返回冲突；
2. 获取默认分支最新 HEAD 及其基础 Tree；
3. 为每个文件创建 `encoding=base64` 的 Blob，保留原始二进制字节；
4. 使用 `base_tree` 和所有新 Blob 创建新 Tree；
5. 以读取到的 HEAD 为唯一 parent 创建 Commit；
6. 使用 `force=false` 更新默认分支引用；
7. 返回提交 SHA 和网页链接。

只有最后一步改变分支可见状态。Blob、Tree 或 Commit 创建失败时可以安全重试；重试前仍先核对远端全部目标文件，未核对成功则重新读取 HEAD 并执行完整流程，不能复用旧 parent 或强推。

更新引用冲突时，如果远端文件与任务快照一致则返回 `reconciled=true`；否则保留冲突，不把其他并发提交覆盖掉。分支保护或 rulesets 的拒绝原样映射为失败。

## 错误、限流与重试

保留 `ErrorKind` 和统一 `PlatformError`，平台适配器负责转换为认证、权限、不存在、冲突、限流、临时或请求错误。认证失败后继续沿用停止排队任务的行为。

GitHub 特殊处理：

- 401 映射认证失败；
- 403 需要区分权限/SSO 拒绝与 primary/secondary rate limit；
- 429 映射限流；
- 同时解析 `Retry-After` 和 `X-RateLimit-Reset`；
- 404 的文案同时允许“资源不存在或当前令牌不可见”，不断言仓库一定不存在；
- 409 映射冲突，422 按具体操作映射参数或状态冲突，不全局套用 Gitee 的 400 规则。

短等待限流沿用指数退避和最多三次尝试。等待超过 60 秒时，不让 worker 睡眠占用执行器，而是结束当前执行并标记为可稍后手动重试。所有错误和报告继续脱敏，不包含令牌、授权头或带凭据 URL。

## 本地 API 与报告

现有 README、设置、标签、上传、查询和重试端点保持同一交互模型。新增或调整只限于：

- 会话的平台字段和能力响应；
- 仓库及报告中的 `platform`；
- 不支持能力的任务在创建前返回 400，且零远端写入；
- GitHub Topics 输入在任务创建前验证；
- GitCode 上传在能力未启用时稳定返回不支持，而非部分执行。

报告继续记录任务、仓库、状态、提交和错误，不记录令牌、文件内容或 Base64。历史报告缺少 `platform` 时按旧版 Gitee 报告理解；新报告显式写入平台。

## 界面

连接区增加 Gitee、GitHub、GitCode 选择，标题、令牌提示和页脚只显示当前平台。平台切换在用户确认后注销当前会话并清空仓库选择、预览和草稿，避免把旧平台操作提交到新平台。

能力驱动行为：

- GitHub 设置页隐藏显示名称列，只提供可见性；
- GitHub 标签页说明使用 Repository Topics，并按其规则提示输入；
- GitCode 上传未验证前显示“当前平台暂不支持多文件单提交”，按钮禁用；
- 所有平台继续显示逐仓库状态、冲突、重试、提交链接和核对成功标记；
- 私有转公开确认、上传覆盖确认和 XSS 防护保持不变。

## 测试与验收

### 共享契约测试

抽取适用于三个适配器的契约测试，分别提供平台响应 fixture 和请求断言，覆盖：

- 分页、默认分支、仓库字段与平台标识；
- UTF-8 BOM、CRLF、中文 README 的精确保留；
- 预览 SHA 更新、冲突、错误映射和令牌脱敏；
- 可见性、主题追加和保留已有主题；
- 不支持能力零写入；
- 通用批次、任务、重试和报告逻辑不因平台分支重复实现。

### GitHub 特有测试

使用 `respx` 验证 Bearer 与版本头、Topics 读取合并、403/429 限流、长等待不阻塞 worker，以及多文件提交的完整调用顺序。

多文件上传必须覆盖：二进制 Blob、混合新建/覆盖、预检冲突零 Blob、正确 `base_tree` 和 parent、`force=false`、HEAD 移动冲突、引用更新后丢失响应的核对，以及重试重新读取 HEAD。

### GitCode 特有测试

验证独立字段解析、Bearer 认证、显示名称不修改路径、标签读取失败时零写入，以及未验证上传能力时本地 API 和适配器均拒绝且零写入。任何未来的上传启用都必须先增加证明单提交语义的契约测试。

### 本地 API 与浏览器端到端

覆盖三个平台的连接、凭据隔离、平台切换使旧任务失效、能力响应、越权名称修改、无效 Topics 和不支持上传。使用假适配器与 Playwright 走通 README、设置、主题、GitHub 上传及 GitCode 上传禁用流程，并验证旧 Gitee 功能无回归。

最终运行完整 `pytest`、`pyinstaller readme-batch.spec` 和打包程序本机启动检查。不得对真实仓库执行自动化写入；交付前清理测试和构建产生的非交付临时文件。

## 已确认决策

- 采用单平台会话，不做跨平台混合批次；
- 采用 REST 适配器和能力声明，不引入本地 clone/push；
- GitHub 第一版不提供显示名称或仓库改名；
- 仓库标签统一按仓库主题语义处理，GitHub 不使用 Issue Labels；
- GitHub 多文件上传通过 Git 数据 API 保持每仓库一次提交；
- GitCode 多文件单提交未验证前禁用，不静默降级为每文件一次提交；
- 平台不支持的能力由界面和后端共同限制。

## 官方接口依据

- GitHub Contents API：https://docs.github.com/en/rest/repos/contents
- GitHub Repositories API：https://docs.github.com/en/rest/repos/repos
- GitHub Git Blobs、Trees、Commits、Refs：https://docs.github.com/en/rest/git
- GitHub REST 限流：https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
- GitCode OpenAPI 入门：https://docs.gitcode.com/docs/apis/
- GitCode 仓库接口：https://docs.gitcode.com/v1-docs/docs/openapi/repos/
- GitCode 项目标签：https://docs.gitcode.com/en/docs/apis/put-api-v-5-repos-owner-repo-project-labels/
