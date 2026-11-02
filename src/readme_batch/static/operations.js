const operationIds = { settings: null, labels: null };
const operations = { settings: null, labels: null };
const operationTimers = { settings: null, labels: null };
const operationRefreshed = { settings: false, labels: false };
const operationPollFailures = { settings: 0, labels: 0 };
let settingsChanges = [];

const operationStateLabels = {
  pending: "等待",
  running: "执行中",
  success: "成功",
  failed: "失败",
  conflict: "冲突",
};

function selectOperationView(view) {
  ["readme", "settings", "labels", "upload"].forEach((name) => {
    byId(`view-${name}`).hidden = name !== view;
    byId(`tab-${name}`).setAttribute("aria-selected", String(name === view));
  });
}

function invalidateSettingsPreview() {
  settingsChanges = [];
  byId("settings-change-items").replaceChildren();
  byId("settings-submit").disabled = true;
  byId("settings-summary").textContent = "设置已更改，请重新检查";
}

function renderSettingsRepositories(event) {
  const container = byId("settings-repositories");
  const preserveEdits = event?.type === "repository-selection-changed";
  const previousNames = new Map(
    Array.from(container.querySelectorAll(".setting-name")).map((input) => [
      input.dataset.fullName,
      input.value,
    ])
  );
  container.replaceChildren();
  const chosen = repositories.filter((repository) => selected.has(repository.full_name));
  if (chosen.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "请先在左侧选择仓库。";
    container.append(empty);
  }
  chosen.forEach((repository) => {
    const row = document.createElement("div");
    row.className = "setting-row";
    const path = document.createElement("span");
    path.className = "setting-path";
    path.textContent = repository.full_name;
    const input = document.createElement("input");
    input.type = "text";
    input.className = "setting-name";
    input.dataset.fullName = repository.full_name;
    input.value = preserveEdits && previousNames.has(repository.full_name)
      ? previousNames.get(repository.full_name)
      : repository.display_name || repository.name;
    input.setAttribute("aria-label", `${repository.full_name} 的显示名称`);
    input.addEventListener("input", invalidateSettingsPreview);
    const visibility = document.createElement("span");
    visibility.className = "setting-visibility";
    visibility.textContent = repository.private ? "当前：私有" : "当前：公开";
    row.append(path, input, visibility);
    container.append(row);
  });
  invalidateSettingsPreview();
}

function collectSettingsChanges() {
  const visibility = byId("visibility").value;
  const changes = [];
  let invalid = false;
  repositories.forEach((repository) => {
    if (!selected.has(repository.full_name)) return;
    const input = document.querySelector(`.setting-name[data-full-name="${repository.full_name}"]`);
    if (!input) return;
    const currentName = repository.display_name || repository.name;
    const displayName = input.value.trim();
    if (!displayName) invalid = true;
    const privateValue = visibility === "keep" ? null : visibility === "private";
    if (displayName === currentName && (privateValue === null || privateValue === repository.private)) {
      return;
    }
    changes.push({ repository, displayName, private: privateValue });
  });
  return { changes, invalid };
}

function previewSettings() {
  const result = collectSettingsChanges();
  const container = byId("settings-change-items");
  container.replaceChildren();
  settingsChanges = result.invalid ? [] : result.changes;
  if (result.invalid) {
    byId("settings-summary").textContent = "显示名称不能为空";
  } else if (settingsChanges.length === 0) {
    byId("settings-summary").textContent = "没有需要执行的变更";
  } else {
    byId("settings-summary").textContent = `${settingsChanges.length} 个仓库待修改`;
    settingsChanges.forEach((change) => {
      const item = document.createElement("div");
      item.className = "change-item";
      const currentName = change.repository.display_name || change.repository.name;
      const parts = [];
      if (change.displayName !== currentName) parts.push(`${currentName} → ${change.displayName}`);
      if (change.private !== null) parts.push(change.private ? "设为私有" : "设为公开");
      item.textContent = `${change.repository.full_name}：${parts.join("；")}`;
      container.append(item);
    });
  }
  byId("settings-submit").disabled = settingsChanges.length === 0;
}

async function submitSettings() {
  if (settingsChanges.length === 0) return;
  const publicCount = settingsChanges.filter(
    (change) => change.private === false && change.repository.private
  ).length;
  let message = `将修改 ${settingsChanges.length} 个仓库的设置。确认继续？`;
  if (publicCount > 0) message = `${publicCount} 个仓库将转为公开，任何人都可以访问。\n${message}`;
  if (!window.confirm(message)) return;
  const changes = settingsChanges.slice();
  const names = {};
  changes.forEach((change) => { names[change.repository.full_name] = change.displayName; });
  await startOperation("settings", "/api/operations/settings", {
    selected: changes.map((change) => change.repository.full_name),
    names,
    visibility: byId("visibility").value,
  });
}

function parseLabels() {
  const seen = new Set();
  byId("labels-input").value.split(/[,，]/).forEach((value) => {
    const label = value.trim();
    if (label) seen.add(label);
  });
  return Array.from(seen);
}

async function submitLabels() {
  const labels = parseLabels();
  if (selected.size === 0) {
    byId("labels-summary").textContent = "请先选择仓库";
    return;
  }
  if (labels.length === 0) {
    byId("labels-summary").textContent = "请输入至少一个标签";
    return;
  }
  const message = `将向 ${selected.size} 个仓库追加标签：${labels.join("、")}。确认继续？`;
  if (!window.confirm(message)) return;
  await startOperation("labels", "/api/operations/labels", {
    selected: Array.from(selected),
    labels,
  });
}

function enableOperationSubmit(kind) {
  byId(`${kind}-submit`).disabled = kind === "settings" ? settingsChanges.length === 0 : false;
}

async function startOperation(kind, path, payload) {
  byId(`${kind}-submit`).disabled = true;
  operationRefreshed[kind] = false;
  operationPollFailures[kind] = 0;
  try {
    const result = await api(path, { method: "POST", body: JSON.stringify(payload) });
    operationIds[kind] = result.id;
    await pollOperation(kind);
  } catch (error) {
    byId(`${kind}-summary`).textContent = error.message;
    enableOperationSubmit(kind);
  }
}

async function pollOperation(kind) {
  const id = operationIds[kind];
  if (!id) return;
  clearTimeout(operationTimers[kind]);
  try {
    const current = await api(`/api/operations/${id}`);
    if (operationIds[kind] !== id) return;
    operationPollFailures[kind] = 0;
    operations[kind] = current;
    renderOperation(kind, current);
    if (current.state === "running") {
      operationTimers[kind] = setTimeout(() => pollOperation(kind), 150);
      return;
    }
    if (kind !== "settings") enableOperationSubmit(kind);
    if (kind === "settings" && !operationRefreshed[kind]) {
      operationRefreshed[kind] = true;
      await loadRepositories(true);
    }
  } catch (error) {
    if (operationIds[kind] !== id) return;
    byId(`${kind}-summary`).textContent = error.message;
    if ([401, 404].includes(error.status)) {
      enableOperationSubmit(kind);
      return;
    }
    operationPollFailures[kind] += 1;
    const delay = Math.min(2000, 150 * (2 ** Math.min(operationPollFailures[kind] - 1, 4)));
    byId(`${kind}-summary`).textContent = "状态查询暂时失败，正在重试…";
    operationTimers[kind] = setTimeout(() => pollOperation(kind), delay);
  }
}

function renderOperation(kind, operation) {
  const container = byId(`${kind}-operation-items`);
  container.replaceChildren();
  const succeeded = operation.items.filter((item) => item.state === "success").length;
  byId(`${kind}-summary`).textContent = `${succeeded}/${operation.items.length} 个仓库成功`;
  operation.items.forEach((item) => {
    const article = document.createElement("article");
    article.className = "preview-item operation-item";
    const header = document.createElement("div");
    header.className = "item-header";
    const title = document.createElement("span");
    title.className = "item-title";
    title.textContent = item.repository.full_name;
    const badge = document.createElement("span");
    badge.className = `badge ${item.state}`;
    badge.textContent = operationStateLabels[item.state] || item.state;
    header.append(title, badge);
    article.append(header);
    if (item.error) {
      const error = document.createElement("p");
      error.className = "error";
      error.textContent = item.error;
      article.append(error);
    }
    if (item.commit_info || item.reconciled) {
      const row = document.createElement("div");
      row.className = "result-row";
      if (item.commit_info) {
        const link = document.createElement("a");
        link.className = "commit-link";
        link.href = item.commit_info.url;
        link.target = "_blank";
        link.rel = "noreferrer";
        link.textContent = `查看提交 ${item.commit_info.sha.slice(0, 8)}`;
        row.append(link);
      }
      if (item.reconciled) {
        const reconciled = document.createElement("span");
        reconciled.className = "reconciled";
        reconciled.textContent = "核对成功";
        row.append(reconciled);
      }
      article.append(row);
    }
    if (item.state === "failed" && item.retryable) {
      const row = document.createElement("div");
      row.className = "result-row";
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "retry-button operation-retry";
      retry.textContent = "重试此仓库";
      retry.addEventListener("click", () => retryOperation(kind, item.repository.full_name));
      row.append(retry);
      article.append(row);
    }
    container.append(article);
  });
}

async function retryOperation(kind, fullName) {
  const id = operationIds[kind];
  if (!id) return;
  operationRefreshed[kind] = false;
  try {
    await api(`/api/operations/${id}/retry`, {
      method: "POST",
      body: JSON.stringify({ full_name: fullName }),
    });
    await pollOperation(kind);
  } catch (error) {
    byId(`${kind}-summary`).textContent = error.message;
  }
}

function formatFileSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

function renderUploadFileList() {
  const container = byId("upload-file-list");
  container.replaceChildren();
  Array.from(byId("upload-files").files).forEach((file) => {
    const item = document.createElement("div");
    item.className = "upload-file-item";
    item.textContent = `${file.name} · ${formatFileSize(file.size)}`;
    container.append(item);
  });
}

function validateUploadInput(files, directory, message) {
  if (selected.size === 0) throw new Error("请先选择仓库");
  if (files.length === 0) throw new Error("请选择要上传的文件");
  if (files.length > 20) throw new Error("最多上传 20 个文件");
  if (!message) throw new Error("提交说明不能为空");
  if (
    directory.includes("\\") || directory.startsWith("/") ||
    (directory.length > 1 && directory[1] === ":") || /[\x00-\x1F]/.test(directory) ||
    directory.split("/").some((part) => ["", ".", ".."].includes(part))
  ) {
    if (directory) throw new Error("目标目录必须是有效的仓库相对路径");
  }
  const names = new Set();
  let totalSize = 0;
  files.forEach((file) => {
    const name = file.name.trim();
    if (!name || [".", ".."].includes(name) || /[/\\]/.test(name) || /[\x00-\x1F]/.test(name)) {
      throw new Error("文件名无效");
    }
    if (names.has(name)) throw new Error(`文件名重复：${name}`);
    names.add(name);
    if (file.size > 10 * 1024 * 1024) throw new Error(`${name} 超过单文件 10MiB 限制`);
    totalSize += file.size;
  });
  if (totalSize > 25 * 1024 * 1024) throw new Error("文件总大小超过 25MiB 限制");
  return { names, totalSize };
}

async function fileToBase64(file) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

async function submitUpload() {
  const files = Array.from(byId("upload-files").files);
  const selectedRepositories = Array.from(selected);
  const directory = byId("upload-directory").value.trim();
  const overwrite = byId("upload-overwrite").checked;
  const message = byId("upload-message").value.trim();
  let totalSize = 0;
  try {
    totalSize = validateUploadInput(files, directory, message).totalSize;
  } catch (error) {
    byId("upload-summary").textContent = error.message;
    return;
  }
  let confirmation = `将向 ${selectedRepositories.length} 个仓库上传 ${files.length} 个文件，共 ${formatFileSize(totalSize)}，目标目录：${directory || "仓库根目录"}。确认继续？`;
  if (overwrite) confirmation = `已启用覆盖同名文件。\n${confirmation}`;
  if (!window.confirm(confirmation)) return;

  byId("upload-submit").disabled = true;
  byId("upload-summary").textContent = "正在读取文件…";
  try {
    const payloadFiles = [];
    for (const file of files) {
      payloadFiles.push({ name: file.name.trim(), content_base64: await fileToBase64(file) });
    }
    await startOperation("upload", "/api/operations/uploads", {
      selected: selectedRepositories,
      directory,
      overwrite,
      message,
      files: payloadFiles,
    });
  } catch (error) {
    byId("upload-summary").textContent = error.message;
    byId("upload-submit").disabled = false;
  }
}

["readme", "settings", "labels", "upload"].forEach((view) => {
  byId(`tab-${view}`).addEventListener("click", () => selectOperationView(view));
});
byId("settings-preview").addEventListener("click", previewSettings);
byId("settings-submit").addEventListener("click", submitSettings);
byId("labels-submit").addEventListener("click", submitLabels);
byId("upload-submit").addEventListener("click", submitUpload);
byId("upload-files").addEventListener("change", renderUploadFileList);
byId("visibility").addEventListener("change", invalidateSettingsPreview);
byId("labels-input").addEventListener("input", () => {
  byId("labels-summary").textContent = "标签已编辑，确认后追加";
});
document.addEventListener("repository-selection-changed", renderSettingsRepositories);
document.addEventListener("repositories-loaded", renderSettingsRepositories);
renderSettingsRepositories();
