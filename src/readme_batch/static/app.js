const byId = (id) => document.getElementById(id);
const selected = new Set();
let repositories = [];
let batchId = null;
let batch = null;
let draftTimer = null;
let pollTimer = null;

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
  });
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try {
      const payload = await response.json();
      if (payload.detail) message = payload.detail;
    } catch (_) {}
    throw new Error(message);
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

function filterRepositories() {
  const query = byId("repository-filter").value.trim().toLowerCase();
  document.querySelectorAll(".repository-row").forEach((row) => {
    row.hidden = !row.dataset.fullName.toLowerCase().includes(query);
  });
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

async function connect() {
  byId("connect").disabled = true;
  setConnectionStatus("正在连接并读取仓库…");
  try {
    await api("/api/session", {
      method: "POST",
      body: JSON.stringify({ token: byId("token").value, remember: byId("remember").checked }),
    });
    byId("token").value = "";
    repositories = await api("/api/repositories");
    selected.clear();
    invalidatePreview();
    renderRepositories();
    setConnectionStatus(`已加载 ${repositories.length} 个仓库`);
  } catch (error) {
    setConnectionStatus(error.message);
  } finally {
    byId("connect").disabled = false;
  }
}

async function createPreview() {
  byId("preview").disabled = true;
  byId("commit").disabled = true;
  byId("preview-summary").textContent = "正在读取 README，尚未进行任何提交…";
  try {
    await flushDraft();
    const result = await api("/api/previews", { method: "POST" });
    batchId = result.id;
    await pollBatch();
  } catch (error) {
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

byId("connect").addEventListener("click", connect);
byId("preview").addEventListener("click", createPreview);
byId("commit").addEventListener("click", commitBatch);
byId("repository-filter").addEventListener("input", filterRepositories);
["old-text", "new-text", "commit-message"].forEach((id) => {
  byId(id).addEventListener("input", draftChanged);
});
