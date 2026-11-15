# 仓库批量管理台

一款在 Windows 本机运行的多平台仓库批量管理工具，支持 **Gitee、GitHub 和 GitCode**。通过浏览器完成仓库选择、预检确认和批量操作，帮助减少在多个仓库之间重复修改、上传和维护文件的工作。

详细操作步骤请参阅 [操作说明.md](操作说明.md)。

## 功能特性

- **多平台连接**：在同一界面中选择 Gitee、GitHub 或 GitCode，每次使用一个平台会话。
- **README 批量替换**：精确匹配旧内容，生成差异预览，确认后逐仓库提交。
- **仓库设置**：批量调整公开/私有状态；Gitee、GitCode 支持修改独立显示名称。
- **仓库标签与主题**：保留已有内容并追加新项；GitHub 使用 Repository Topics。
- **批量上传文件**：支持子目录、二进制文件和覆盖控制，每个仓库只产生一次提交。
- **Gitee 文件删除**：按完整相对路径预检并删除文件，使用 SHA 防止误删新版本。
- **过程可追踪**：逐仓库状态、冲突提示、失败重试、提交链接和 JSON 报告。
- **能力保护**：不支持的功能在界面和后端同时禁用，不伪装成功或静默降级。
- **本机运行**：请求仅在本机与所选平台之间传递，不需要部署服务器。

## 平台能力

| 功能 | Gitee | GitHub | GitCode |
|---|---|---|---|
| README 精确替换 | 支持 | 支持 | 支持 |
| 公开/私有设置 | 支持 | 支持 | 支持 |
| 独立显示名称 | 支持 | 不支持 | 支持 |
| 追加仓库标签或主题 | 项目标签 | Repository Topics | 项目标签 |
| 批量上传文件 | 支持 | 支持 | 当前不支持 |
| 查看仓库简介 | 支持 | 以平台返回信息为准 | 以平台返回信息为准 |
| 按路径批量删除文件 | 支持 | 不支持 | 不支持 |

## 快速开始（Windows 分发版）

1. 获取并解压分发包，确认目录中同时包含：
   - `readme-batch.exe`
   - `_internal/` 文件夹
2. 双击 `readme-batch.exe`。
3. 默认浏览器将打开 `http://127.0.0.1:8765/`。
4. 选择平台，输入对应的私人访问令牌并连接仓库。
5. 使用结束后，点击页面右上角“退出工具”。

> `_internal` 是程序运行所需资源，不能只复制 exe 文件。使用分发版通常不需要安装 Python。

## 从源码运行

环境要求：**Python 3.11 或更高版本**。

在项目根目录执行：

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe -m readme_batch
```

随后访问 `http://127.0.0.1:8765/`。

## 构建 Windows 分发包

```bash
.venv/Scripts/python.exe -m PyInstaller --noconfirm readme-batch.spec
```

构建结果位于：

```text
dist/readme-batch/
```

交付时请保留整个目录，包括 `readme-batch.exe` 和 `_internal/`。

## 运行测试

如果已安装本机 Chrome，可以直接使用：

```bash
README_BATCH_BROWSER_CHANNEL=chrome .venv/Scripts/python.exe -m pytest
```

也可以安装 Playwright 自带的 Chromium：

```bash
.venv/Scripts/python.exe -m playwright install chromium
.venv/Scripts/python.exe -m pytest
```

测试不会要求对真实仓库执行写操作，平台 HTTP 响应使用模拟数据，浏览器流程使用本地测试适配器。

## 基本使用流程

1. 选择平台并输入私人访问令牌。
2. 加载后筛选并勾选仓库。
3. 选择需要的操作页签。
4. 先生成预检或差异，确认实际变更范围。
5. 执行操作并逐仓库检查结果、提交链接和失败重试。

对于公开仓库、覆盖上传和文件删除等高风险操作，建议先在少量测试仓库中验证。

## 项目结构

```text
src/readme_batch/
├── app.py                  # 本地 FastAPI 与会话管理
├── batches.py              # README 预览和提交批次
├── deletions.py            # 文件删除预检管理
├── operations.py           # 通用仓库任务与重试
├── execution.py            # 并发执行和错误处理
├── models.py               # 统一数据模型
├── platforms/
│   ├── base.py             # 平台协议、能力和错误类型
│   ├── gitee.py            # Gitee API
│   ├── github.py           # GitHub API
│   └── gitcode.py          # GitCode API
└── static/                 # 原生 HTML、CSS、JavaScript 页面

tests/                      # 单元、API、平台和浏览器测试
操作说明.md                  # 完整用户操作手册
readme-batch.spec           # PyInstaller 打包配置
pyproject.toml              # 项目依赖与测试配置
```

## 令牌与数据安全

- 仅为令牌开通必要权限，建议为不同平台创建独立专用令牌。
- 勾选“记住令牌”后，令牌按平台保存在 Windows 凭据系统中。
- 不会把令牌、授权头、文件内容或 Base64 文件数据写入 JSON 报告。
- 不要把令牌提交到 Git 仓库，也不要通过截图、文档或聊天工具分享。
- 退出工具或取消“记住令牌”不会吊销平台上已经签发的令牌；怀疑泄露时，应立即到对应平台吊销并更换。

## 注意事项

- 切换平台会清空当前仓库选择、README 草稿和预览，并使旧任务失效。
- 文件删除只支持 Gitee 默认分支中的精确相对路径，不递归删除同名文件。
- 跨仓库操作不是整体原子事务，可能部分成功，应逐仓库检查结果。
- GitCode 多文件上传在能力得到验证前保持禁用。
- GitHub 不提供独立显示名称编辑，因为仓库名称会影响仓库路径。
