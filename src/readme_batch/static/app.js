const byId = (id) => document.getElementById(id);
const selected = new Set();
const PLATFORM_NAMES = { gitee: "Gitee", github: "GitHub", gitcode: "GitCode" };
const DEFAULT_CAPABILITIES = {
  gitee: { display_name: true, visibility: true, repository_topics: true, upload_mode: "single_commit", file_deletion: true },
  github: { display_name: false, visibility: true, repository_topics: true, upload_mode: "single_commit", file_deletion: false },
  gitcode: { display_name: true, visibility: true, repository_topics: true, upload_mode: "unsupported", file_deletion: false },
};
let repositories = [];
let batchId = null;
let batch = null;
let draftTimer = null;
let pollTimer = null;
let apiToken = "";
let currentPlatform = "gitee";
let capabilities = DEFAULT_CAPABILITIES.gitee;
let sessionActive = false;
let sessionGeneration = 0;
const tokenReady = fetch("/api/ui-token")
  .then((response) => response.json())
  .then((payload) => { apiToken = payload.token; });

async function api(path, options = {}) {
  await tokenReady;
  const response = await fetch(path, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      "X-README-Batch-Token": apiToken,
    },
  });
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try {
      const payload = await response.json();
      if (typeof payload.detail === "string") message = payload.detail;
      else if (Array.isArray(payload.detail)) {
        message = payload.detail.map((issue) => issue.msg || "请求参数无效").join("；");
      } else if (payload.detail) message = "请求参数无效";
    } catch (_) {}
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  if (response.status === 204 || response.headers.get("content-length") === "0") return null;
  return response.json();
}

function setConnectionStatus(message) {
  byId("connection-status").textContent = message;
}

function renderRepositories() {
  const list = byId("repository-list");
  list.replaceChildren();
  repositories.forEach((repository) => {
    const row = document.createElement("label");
    row.className = "repository-row";
    row.dataset.fullName = repository.full_name;
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = selected.has(repository.full_name);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) selected.add(repository.full_name);
      else selected.delete(repository.full_name);
      draftChanged();
      document.dispatchEvent(new Event("repository-selection-changed"));
      updateSelectAllButton();
    });
    const text = document.createElement("span");
    const name = document.createElement("span");
    name.className = "repository-name";
    name.textContent = repository.full_name;
    const branch = document.createElement("span");
    branch.className = "repository-branch";
    branch.textContent = `默认分支 · ${repository.default_branch}`;
    text.append(name, branch);
    row.append(checkbox, text);
    list.append(row);
  });
  filterRepositories();
}

function visibleRepositoryRows() {
  return Array.from(document.querySelectorAll(".repository-row")).filter((row) => !row.hidden);
}

function allFilteredRepositoriesSelected() {
  const rows = visibleRepositoryRows();
  return rows.length > 0 && rows.every((row) => selected.has(row.dataset.fullName));
}

function updateSelectAllButton() {
  byId("select-all-repositories").textContent = allFilteredRepositoriesSelected() ? "取消全选" : "全选";
}

function filterRepositories() {
  const query = byId("repository-filter").value.trim().toLowerCase();
  document.querySelectorAll(".repository-row").forEach((row) => {
    row.hidden = !row.dataset.fullName.toLowerCase().includes(query);
  });
  updateSelectAllButton();
}

function selectAllFilteredRepositories() {
  const shouldSelect = !allFilteredRepositoriesSelected();
  visibleRepositoryRows().forEach((row) => {
    if (shouldSelect) selected.add(row.dataset.fullName);
    else selected.delete(row.dataset.fullName);
    row.querySelector("input").checked = shouldSelect;
  });
  draftChanged();
  document.dispatchEvent(new Event("repository-selection-changed"));
  updateSelectAllButton();
}

function currentDraft() {
  return {
    selected: Array.from(selected),
    old: byId("old-text").value,
    new: byId("new-text").value,
    commit_message: byId("commit-message").value,
  };
}

function invalidatePreview() {
  batchId = null;
  batch = null;
  clearTimeout(pollTimer);
  byId("commit").disabled = true;
  byId("preview-items").replaceChildren();
  byId("preview-summary").textContent = "规则已更改，请重新预览";
}

async function flushDraft() {
  clearTimeout(draftTimer);
  await api("/api/draft", { method: "PUT", body: JSON.stringify(currentDraft()) });
}

function draftChanged() {
  invalidatePreview();
  clearTimeout(draftTimer);
  draftTimer = setTimeout(() => {
    flushDraft().catch((error) => { byId("preview-summary").textContent = error.message; });
  }, 150);
}

async function loadRepositories(preserveSelection = false, generation = sessionGeneration) {
  const previousSelection = new Set(selected);
  let items;
  try {
    items = await api("/api/repositories");
  } catch (error) {
    if (generation !== sessionGeneration) return false;
    throw error;
  }
  if (generation !== sessionGeneration) return false;
  repositories = items;
  selected.clear();
  if (preserveSelection) {
    repositories.forEach((repository) => {
      if (previousSelection.has(repository.full_name)) selected.add(repository.full_name);
    });
  }
  invalidatePreview();
  renderRepositories();
  document.dispatchEvent(new Event("repositories-loaded"));
  return true;
}

async function connect() {
  const generation = ++sessionGeneration;
  byId("connect").disabled = true;
  setConnectionStatus("正在连接并读取仓库…");
  try {
    await api("/api/session", {
      method: "POST",
      body: JSON.stringify({
        token: byId("token").value,
        remember: byId("remember").checked,
        platform: currentPlatform,
      }),
    });
    if (generation !== sessionGeneration) return;
    byId("token").value = "";
    const session = await api("/api/session");
    if (generation !== sessionGeneration) return;
    applySession(session);
    if (!await loadRepositories(false, generation)) return;
    setConnectionStatus(`已加载 ${repositories.length} 个仓库`);
  } catch (error) {
    setConnectionStatus(error.message);
  } finally {
    byId("connect").disabled = false;
  }
}

async function createPreview() {
  const generation = sessionGeneration;
  byId("preview").disabled = true;
  byId("commit").disabled = true;
  byId("preview-summary").textContent = "正在读取 README，尚未进行任何提交…";
  try {
    await flushDraft();
    const result = await api("/api/previews", { method: "POST" });
    if (generation !== sessionGeneration) return;
    batchId = result.id;
    await pollBatch();
  } catch (error) {
    if (generation !== sessionGeneration) return;
    byId("preview-summary").textContent = error.message;
  } finally {
    byId("preview").disabled = false;
  }
}

async function pollBatch() {
  if (!batchId) return;
  const id = batchId;
  try {
    const current = await api(`/api/batches/${id}`);
    if (batchId !== id) return;
    batch = current;
    renderBatch();
    if (["previewing", "committing"].includes(current.state)) {
      pollTimer = setTimeout(pollBatch, 150);
    }
  } catch (error) {
    byId("preview-summary").textContent = error.message;
  }
}

const stateLabels = {
  ready: "待提交", no_match: "无匹配", unchanged: "无变化", read_failed: "读取失败",
  success: "成功", failed: "失败", conflict: "冲突",
};

function renderBatch() {
  const items = byId("preview-items");
  items.replaceChildren();
  const ready = batch.items.filter((item) => item.preview_state === "ready");
  const replacements = ready.reduce((total, item) => total + item.match_count, 0);
  byId("preview-summary").textContent = `${ready.length} 个仓库待提交 · ${replacements} 处替换`;
  byId("commit").disabled = batch.state !== "ready" || ready.length === 0;

  batch.items.forEach((item, index) => {
    const article = document.createElement("article");
    article.className = "preview-item";
    const header = document.createElement("div");
    header.className = "item-header";
    const title = document.createElement("span");
    title.className = "item-title";
    title.textContent = item.repository.full_name;
    const state = item.commit_state || item.preview_state || "reading";
    const badge = document.createElement("span");
    badge.className = `badge ${state}`;
    badge.textContent = stateLabels[state] || "读取中";
    header.append(title, badge);
    article.append(header);
    if (item.diff) {
      const diff = document.createElement("pre");
      diff.id = `diff-${index}`;
      diff.textContent = item.diff;
      article.append(diff);
    }
    if (item.error) {
      const error = document.createElement("p");
      error.className = "error";
      error.textContent = item.error;
      article.append(error);
    }
    if (item.commit_info || item.commit_state === "failed" || item.preview_state === "read_failed") {
      const result = document.createElement("div");
      result.className = "result-row";
      if (item.commit_info) {
        const link = document.createElement("a");
        link.className = "commit-link";
        link.href = item.commit_info.url;
        link.target = "_blank";
        link.rel = "noreferrer";
        link.textContent = `查看提交 ${item.commit_info.sha.slice(0, 8)}`;
        result.append(link);
      } else {
        const retry = document.createElement("button");
        retry.id = `retry-${index}`;
        retry.className = "retry-button";
        retry.type = "button";
        retry.textContent = "重试此仓库";
        retry.addEventListener("click", () => retryItem(item.repository.full_name));
        result.append(retry);
      }
      article.append(result);
    }
    items.append(article);
  });
}

async function commitBatch() {
  if (!batch) return;
  const ready = batch.items.filter((item) => item.preview_state === "ready");
  const replacements = ready.reduce((total, item) => total + item.match_count, 0);
  if (!window.confirm(`将直接提交 ${ready.length} 个仓库，共替换 ${replacements} 处。确认继续？`)) return;
  byId("commit").disabled = true;
  try {
    await api(`/api/batches/${batchId}/commit`, { method: "POST" });
    await pollBatch();
  } catch (error) {
    byId("preview-summary").textContent = error.message;
  }
}

async function retryItem(fullName) {
  byId("commit").disabled = true;
  try {
    await api(`/api/batches/${batchId}/retry`, {
      method: "POST",
      body: JSON.stringify({ full_name: fullName }),
    });
    await pollBatch();
  } catch (error) {
    byId("preview-summary").textContent = error.message;
  }
}

function applyPlatformPresentation() {
  const name = PLATFORM_NAMES[currentPlatform];
  byId("platform").value = currentPlatform;
  byId("connection-title").textContent = `连接 ${name}`;
  byId("token").placeholder = `仅发送至 ${name} 官方接口`;
  byId("platform-footer").textContent = `所有请求仅在本机与 ${name} 之间传递`;
  const github = currentPlatform === "github";
  byId("labels-title").textContent = github ? "追加仓库主题（Repository Topics）" : "追加仓库标签";
  byId("labels-hint").textContent = github
    ? "仅支持小写字母、数字和连字符。只追加，不清除已有 Topics。"
    : "使用中英文逗号分隔，自动去空白和去重。只追加，不清除已有标签。";
  const unsupported = capabilities.upload_mode !== "single_commit";
  byId("tab-delete").hidden = !capabilities.file_deletion;
  if (!capabilities.file_deletion && !byId("view-delete").hidden) selectOperationView("readme");
  byId("upload-submit").disabled = unsupported;
  byId("upload-summary").textContent = unsupported
    ? "当前平台暂不支持多文件单提交"
    : "每个仓库只产生一次提交";
  if (typeof renderSettingsRepositories === "function") renderSettingsRepositories();
}

function applySession(session) {
  sessionActive = session.active;
  if (session.active) {
    currentPlatform = session.platform;
    capabilities = session.capabilities;
  } else {
    capabilities = DEFAULT_CAPABILITIES[currentPlatform];
  }
  applyPlatformPresentation();
}

function resetForPlatformSwitch() {
  clearTimeout(draftTimer);
  selected.clear();
  repositories = [];
  invalidatePreview();
  renderRepositories();
  byId("old-text").value = "";
  byId("new-text").value = "";
  byId("commit-message").value = "docs: 批量更新 README";
  ["settings", "labels", "upload", "delete"].forEach((kind) => {
    clearTimeout(operationTimers[kind]);
    operationIds[kind] = null;
    operations[kind] = null;
    operationRefreshed[kind] = false;
    operationPollFailures[kind] = 0;
    byId(`${kind}-operation-items`).replaceChildren();
  });
  settingsChanges = [];
  byId("settings-change-items").replaceChildren();
  byId("settings-submit").disabled = true;
  byId("labels-input").value = "";
  byId("labels-summary").textContent = "等待输入标签";
  byId("upload-files").value = "";
  byId("upload-file-list").replaceChildren();
  invalidateDeletionPreview();
}

async function changePlatform() {
  const next = byId("platform").value;
  if (next === currentPlatform) return;
  if (sessionActive && !window.confirm("切换平台将清空当前仓库选择、草稿和预览。确认继续？")) {
    byId("platform").value = currentPlatform;
    return;
  }
  const generation = ++sessionGeneration;
  try {
    if (sessionActive) await api("/api/session", { method: "DELETE" });
    if (generation !== sessionGeneration) return;
    currentPlatform = next;
    sessionActive = false;
    capabilities = DEFAULT_CAPABILITIES[currentPlatform];
    resetForPlatformSwitch();
    applyPlatformPresentation();
    setConnectionStatus(`已切换到 ${PLATFORM_NAMES[currentPlatform]}，请输入令牌连接`);
  } catch (error) {
    if (generation !== sessionGeneration) return;
    byId("platform").value = currentPlatform;
    setConnectionStatus(error.message);
  }
}

byId("platform").addEventListener("change", changePlatform);
byId("connect").addEventListener("click", connect);
byId("preview").addEventListener("click", createPreview);
byId("commit").addEventListener("click", commitBatch);
byId("repository-filter").addEventListener("input", filterRepositories);
byId("select-all-repositories").addEventListener("click", selectAllFilteredRepositories);
["old-text", "new-text", "commit-message"].forEach((id) => {
  byId(id).addEventListener("input", draftChanged);
});

async function exitApp() {
  if (!window.confirm("确定退出工具？本地服务将停止，不会影响远端仓库。")) return;
  try {
    await api("/api/shutdown", { method: "POST" });
    document.querySelectorAll("button, input, textarea").forEach((control) => {
      control.disabled = true;
    });
    byId("exit-status").textContent = "工具已退出，可以关闭此页面";
  } catch (error) {
    byId("exit-status").textContent = error.message;
  }
}

byId("exit-app").addEventListener("click", exitApp);

async function initializeSession() {
  const generation = sessionGeneration;
  const session = await api("/api/session");
  if (generation !== sessionGeneration) return;
  applySession(session);
  if (session.active) {
    if (!await loadRepositories(false, generation)) return;
    setConnectionStatus(`已恢复保存的会话，共 ${repositories.length} 个仓库`);
  }
}

initializeSession().catch((error) => setConnectionStatus(error.message));
