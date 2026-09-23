// 管理界面：项目列表 + 新建 + 搜索 + 筛选 + 删除
const $ = (id) => document.getElementById(id);

const STATUS_LABEL = {
  pending: "等待中",
  running: "处理中",
  done: "已完成",
  failed: "失败",
};

let searchTimer = null;
let listPollTimer = null;
const selectedIds = new Set();  // 复选框选中状态

// ---------------------------------------------------------------------------
// 项目列表
// ---------------------------------------------------------------------------
async function loadProjects() {
  const q = $("search-input").value.trim();
  const status = $("status-filter").value;
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (status) params.set("status", status);

  const resp = await fetch(`/api/projects?${params}`);
  if (!resp.ok) {
    showError("加载项目列表失败");
    return;
  }
  const data = await resp.json();
  renderProjects(data.items);
  $("total-count").textContent = `共 ${data.total} 个项目`;
}

function renderProjects(items) {
  const tbody = $("projects-body");
  const empty = $("empty-state");
  tbody.innerHTML = "";

  if (items.length === 0) {
    empty.hidden = false;
    return;
  }
  empty.hidden = true;

  for (const p of items) {
    const tr = document.createElement("tr");

    // 复选框列
    const tdCheck = document.createElement("td");
    tdCheck.className = "col-check";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = selectedIds.has(p.id);
    cb.dataset.id = p.id;
    cb.addEventListener("change", () => toggleSelect(p.id, cb.checked));
    tdCheck.appendChild(cb);

    // 名称（可点击进入详情）
    const tdName = document.createElement("td");
    const aName = document.createElement("a");
    aName.href = `/task/${p.id}`;
    aName.textContent = p.name || "(未命名)";
    aName.className = "project-link";
    tdName.appendChild(aName);

    // 文件名（小字、灰色）
    const tdFile = document.createElement("td");
    tdFile.textContent = p.filename;
    tdFile.className = "filename-cell";

    // 状态
    const tdStatus = document.createElement("td");
    tdStatus.innerHTML = `<span class="status-badge status-${p.status}">${STATUS_LABEL[p.status] || p.status}</span>`;

    // 进度（running/pending 行内迷你进度条；done 全量；failed 无进度）
    const tdProgress = document.createElement("td");
    const pct = Math.round((p.progress || 0) * 100);
    if (p.status === "done") tdProgress.textContent = "100%";
    else if (p.status === "failed") tdProgress.textContent = "—";
    else {
      tdProgress.innerHTML =
        `<div class="mini-progress"><div class="track"><div class="fill" style="width:${pct}%"></div></div><span class="pct">${pct}%</span></div>`;
    }

    // 段数
    const tdSegs = document.createElement("td");
    const segCount = (p.meta && p.meta.segments_count) || (p.status === "done" ? "—" : "");
    tdSegs.textContent = segCount === "" ? "" : String(segCount);

    // 创建时间（只显示日期 + 时分）
    const tdTime = document.createElement("td");
    tdTime.textContent = fmtTime(p.created_at);
    tdTime.className = "time-cell";

    // 操作
    const tdActions = document.createElement("td");
    tdActions.className = "actions-cell";
    if (p.status === "done") {
      const dl = document.createElement("a");
      dl.href = `/download/${p.id}/srt`;
      dl.innerHTML = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/></svg>SRT';
      dl.className = "action-btn";
      dl.title = "下载 SRT 字幕";
      tdActions.appendChild(dl);
    } else if (p.status === "running" || p.status === "pending") {
      const view = document.createElement("a");
      view.href = `/task/${p.id}`;
      view.textContent = "查看进度";
      view.className = "action-btn";
      tdActions.appendChild(view);
    }
    const del = document.createElement("button");
    del.type = "button";
    del.innerHTML = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';
    del.className = "action-btn danger icon-only";
    del.title = "删除项目";
    del.setAttribute("aria-label", `删除项目 ${p.name || p.filename}`);
    del.onclick = () => deleteProject(p);
    tdActions.appendChild(del);

    tr.append(tdCheck, tdName, tdFile, tdStatus, tdProgress, tdSegs, tdTime, tdActions);
    tbody.appendChild(tr);
  }
}

async function deleteProject(p) {
  if (!confirm(`确认删除「${p.name || p.filename}」？\n（输出文件一并删除，不可恢复）`)) return;
  const resp = await fetch(`/api/projects/${p.id}`, { method: "DELETE" });
  if (!resp.ok) {
    alert(`删除失败: ${resp.status}`);
    return;
  }
  await loadProjects();
}

function fmtTime(iso) {
  if (!iso) return "";
  // "2026-09-22T10:30:00" → "09-22 10:30"
  const m = iso.match(/(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  return m ? `${m[1]}-${m[2]} ${m[3]}:${m[4]}` : iso;
}

function showError(msg) {
  // 简单错误提示：塞到 total-count 里
  $("total-count").innerHTML = `<span class="error">${msg}</span>`;
}

// ---------------------------------------------------------------------------
// 新建项目（表单提交）
// ---------------------------------------------------------------------------
function openUploadPanel() {
  $("upload-panel").hidden = false;
  $("project-name-input").focus();
}

$("new-project-btn").addEventListener("click", () => {
  const panel = $("upload-panel");
  if (panel.hidden) openUploadPanel();
  else { panel.hidden = true; $("upload-form").reset(); resetDropzone(); }
});

// 空状态的 CTA 与顶栏按钮同行为
$("empty-new-btn").addEventListener("click", openUploadPanel);

$("cancel-upload-btn").addEventListener("click", () => {
  $("upload-panel").hidden = true;
  $("upload-form").reset();
  resetDropzone();
});

// ---------------------------------------------------------------------------
// 拖放上传：整个 dropzone 区域可拖可点，选中后展示文件名与大小
// ---------------------------------------------------------------------------
const dz = $("dropzone");
const fileInput = $("file-input");

function fmtSize(bytes) {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function showFile(file) {
  $("dz-main").textContent = file ? file.name : "点击选择文件，或拖拽到此处";
  $("dz-sub").textContent = file ? `${fmtSize(file.size)} · 可重新拖拽替换` : "支持 mp4 / mov / mp3 / wav 等音视频";
  dz.classList.toggle("has-file", Boolean(file));
}

function resetDropzone() { showFile(null); }

fileInput.addEventListener("change", () => showFile(fileInput.files[0]));

dz.addEventListener("dragover", (e) => {
  e.preventDefault();
  dz.classList.add("dragover");
});
dz.addEventListener("dragleave", () => dz.classList.remove("dragover"));
dz.addEventListener("drop", (e) => {
  e.preventDefault();
  dz.classList.remove("dragover");
  const file = e.dataTransfer.files && e.dataTransfer.files[0];
  if (!file) return;
  // 拖入的文件写回 input（类型校验走 accept 同款口径）
  if (file.type.startsWith("video/") || file.type.startsWith("audio/") || /\.(mp4|mov|mkv|avi|mp3|wav|m4a|aac|flac)$/i.test(file.name)) {
    fileInput.files = e.dataTransfer.files;
    showFile(file);
  } else {
    alert("不支持的文件类型，请拖入音视频文件");
  }
});

$("upload-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const file = fileInput.files[0];
  if (!file) return;

  const form = new FormData();
  form.append("file", file);
  form.append("name", $("project-name-input").value.trim());
  form.append("quality", $("quality").value);
  form.append("diarize", $("diarize").checked ? "true" : "false");

  $("submit-btn").disabled = true;
  $("submit-btn").innerHTML = '<span class="spinner" aria-hidden="true"></span>上传中…';
  try {
    const resp = await fetch("/api/projects", { method: "POST", body: form });
    if (!resp.ok) throw new Error(`上传失败: ${resp.status}`);
    const data = await resp.json();
    // 收起表单 + 跳到详情
    $("upload-panel").hidden = true;
    $("upload-form").reset();
    resetDropzone();
    window.location.href = `/task/${data.task_id}`;
  } catch (err) {
    alert(err.message);
  } finally {
    $("submit-btn").disabled = false;
    $("submit-btn").textContent = "开始转写";
  }
});

// ---------------------------------------------------------------------------
// 搜索 + 筛选（防抖）
// ---------------------------------------------------------------------------
$("search-input").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadProjects, 200);
});
$("status-filter").addEventListener("change", loadProjects);

// ---------------------------------------------------------------------------
// 轮询：处理中的任务需要刷新进度（每 2 秒一次，避开节流）
// ---------------------------------------------------------------------------
function startListPolling() {
  if (listPollTimer) clearInterval(listPollTimer);
  listPollTimer = setInterval(async () => {
    const hasRunning = document.querySelector(".status-running, .status-pending");
    if (hasRunning) await loadProjects();
  }, 2000);
}

// ---------------------------------------------------------------------------
// 批量操作
// ---------------------------------------------------------------------------
function toggleSelect(id, checked) {
  if (checked) selectedIds.add(id);
  else selectedIds.delete(id);
  updateBatchUI();
}

function updateBatchUI() {
  const n = selectedIds.size;
  $("selected-count").textContent = String(n);
  $("batch-toolbar").hidden = n === 0;

  // 全选 checkbox 状态：部分选 → indeterminate；全选 → checked；都没选 → unchecked
  const all = document.querySelectorAll("#projects-body input[type='checkbox']");
  const allChecked = all.length > 0 && Array.from(all).every((c) => c.checked);
  const sa = $("select-all");
  sa.checked = allChecked;
  sa.indeterminate = n > 0 && !allChecked;
}

$("select-all").addEventListener("change", (e) => {
  const checked = e.target.checked;
  document.querySelectorAll("#projects-body input[type='checkbox']").forEach((cb) => {
    cb.checked = checked;
    const id = cb.dataset.id;
    // 直接同步 Set（dataset.id 在下面 renderProjects 里加上）
  });
  // 重渲染的 checkbox 用 selectedIds 反推；这里直接把当前所有 id 同步到 Set
  // （简化：取 tbody 的 data-attribute；用更稳的方式 —— 遍历当前所有项目）
  syncSelectAll(checked);
  updateBatchUI();
});

function syncSelectAll(checked) {
  // 用每个 checkbox 的 dataset.id 同步 Set
  document.querySelectorAll("#projects-body input[type='checkbox']").forEach((cb) => {
    const id = cb.dataset.id;
    if (!id) return;
    if (checked) selectedIds.add(id);
    else selectedIds.delete(id);
  });
}

$("batch-clear-btn").addEventListener("click", () => {
  selectedIds.clear();
  document.querySelectorAll("#projects-body input[type='checkbox']").forEach((c) => (c.checked = false));
  $("select-all").checked = false;
  $("select-all").indeterminate = false;
  updateBatchUI();
});

$("batch-delete-btn").addEventListener("click", async () => {
  const ids = Array.from(selectedIds);
  if (ids.length === 0) return;
  if (!confirm(`确认删除 ${ids.length} 个项目？\n（输出文件一并删除，不可恢复）`)) return;

  const resp = await fetch("/api/projects/batch-delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids }),
  });
  if (!resp.ok) {
    alert(`删除失败: ${resp.status}`);
    return;
  }
  const data = await resp.json();
  selectedIds.clear();
  await loadProjects();
  const failedMsg = data.failed.length ? `（${data.failed.length} 个失败）` : "";
  alert(`已删除 ${data.deleted.length} 个项目${failedMsg}`);
});

$("batch-export-btn").addEventListener("click", async () => {
  const ids = Array.from(selectedIds);
  if (ids.length === 0) return;
  // 用 fetch 拿 blob，触发浏览器下载
  const resp = await fetch("/api/projects/batch-export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids }),
  });
  if (!resp.ok) {
    alert(`导出失败: ${resp.status}`);
    return;
  }
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `projects_${new Date().toISOString().slice(0, 10)}.zip`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
});

// ---------------------------------------------------------------------------
// 启动
// ---------------------------------------------------------------------------
// 兼容老 URL：/?task=xxx 自动跳到 /task/xxx（详情页）
const oldTaskParam = new URLSearchParams(window.location.search).get("task");
if (oldTaskParam) {
  window.location.replace(`/task/${oldTaskParam}`);
} else {
  loadProjects();
  startListPolling();
}
