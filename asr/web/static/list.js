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
let llmEnabled = false;         // /api/llm/status 探明后置 true（默认藏好，别给没配置的用户看死按钮）
let visibleProjects = [];
let listRequest = 0;
let uploadSession = null; // 凭证是取消裁决的事实来源；XHR.abort 只停止本端发送。

function errorText(data, fallback) {
  const detail = data && data.detail;
  return typeof detail === "string" ? detail : (detail && detail.message) || fallback;
}
function canDelete(p) {
  return p.can_delete !== undefined ? p.can_delete : !p.execution_active && !["pending", "running"].includes(p.status);
}
function transcriptionDone(p) {
  return p.transcription_status === "done" || p.status === "done" || p.stage === "ai";
}

// ---------------------------------------------------------------------------
// 项目列表
// ---------------------------------------------------------------------------
async function loadProjects() {
  const request = ++listRequest;
  const q = $("search-input").value.trim();
  const status = $("status-filter").value;
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (status) params.set("status", status);

  let resp;
  try { resp = await fetch(`/api/projects?${params}`); }
  catch { if (request === listRequest) showError("暂时无法加载项目，请检查连接后重试"); return; }
  if (request !== listRequest) return;
  if (!resp.ok) {
    showError("加载项目列表失败");
    return;
  }
  const data = await resp.json();
  if (request !== listRequest) return;
  visibleProjects = data.items;
  const visible = new Set(data.items.map((p) => p.id));
  for (const id of selectedIds) if (!visible.has(id)) selectedIds.delete(id);
  renderProjects(data.items);
  updateBatchUI();
  $("total-count").textContent = `共 ${data.total} 个项目`;
}

function renderProjects(items) {
  const tbody = $("projects-body");
  const empty = $("empty-state");
  tbody.innerHTML = "";

  if (items.length === 0) {
    empty.hidden = false;
    const filtered = Boolean($("search-input").value.trim() || $("status-filter").value);
    $("empty-message").textContent = filtered ? "没有符合条件的项目" : "还没有转写项目。上传课堂录音，开始后会逐段显示识别文字。";
    $("empty-new-btn").hidden = filtered;
    $("empty-clear-btn").hidden = !filtered;
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
    cb.setAttribute("aria-label", `选择项目 ${p.name || p.filename}`);
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
    const badge = document.createElement("span");
    badge.className = `status-badge status-${Object.hasOwn(STATUS_LABEL, p.status) ? p.status : "pending"}`;
    badge.textContent = transcriptionDone(p) && ["queued", "running"].includes(p.ai_status) ? "转写完成 · AI 生成中" : STATUS_LABEL[p.status] || p.status;
    tdStatus.appendChild(badge);

    // 进度（running/pending 行内迷你进度条；done 全量；failed 无进度）
    const tdProgress = document.createElement("td");
    const pct = Math.max(0, Math.min(100, Math.round((p.progress || 0) * 100)));
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
    if (transcriptionDone(p)) {
      const dl = document.createElement("a");
      dl.href = `/download/${p.id}/srt`;
      dl.innerHTML = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/></svg>SRT';
      dl.className = "action-btn";
      dl.title = "下载 SRT 字幕";
      if ((p.available_formats || p.meta?.formats || []).includes("srt")) tdActions.appendChild(dl);

      // AI 反馈入口：与详情页同一套显隐规则——没跑过或失败要重试才显示，
      // 已有产物不占 UI（重跑走 API）。未配置 LLM 也显示：点击跳设置页。
      const hasFiles = Boolean(p.meta && p.meta.ai_files);
      const hasError = Boolean(p.meta && p.meta.ai_error);
      if ((!hasFiles || hasError) && !p.execution_active && !["queued", "running"].includes(p.ai_status)) {
        const ai = document.createElement("button");
        ai.type = "button";
        ai.textContent = "AI 反馈";
        ai.className = "action-btn";
        ai.title = hasError ? "上次 AI 反馈失败，点击重试" : "生成 AI 反馈（点评 + 优化稿）";
        ai.onclick = () => runAiFeedback(p);
        tdActions.appendChild(ai);
      }
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
    del.disabled = !canDelete(p);
    if (del.disabled) del.title = "任务仍在排队或处理中，结束后可删除";
    del.setAttribute("aria-label", `删除项目 ${p.name || p.filename}`);
    del.onclick = () => deleteProject(p);
    tdActions.appendChild(del);

    tr.append(tdCheck, tdName, tdFile, tdStatus, tdProgress, tdSegs, tdTime, tdActions);
    tbody.appendChild(tr);
  }
}

async function runAiFeedback(p) {
  // 未配置 → 跳设置页配置（配置好回来再点就是真触发）
  if (!llmEnabled) { window.location.href = `/settings?from_task=${encodeURIComponent(p.id)}`; return; }
  // 详情页按钮的同款出口：202 后行变"处理中"，2 秒轮询自动接管
  const resp = await fetch(`/api/tasks/${p.id}/feedback`, { method: "POST" });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    alert(errorText(err, `请求失败: ${resp.status}`));
    return;
  }
  await loadProjects();
}

async function deleteProject(p) {
  if (!canDelete(p)) return;
  if (!confirm(`确认删除「${p.name || p.filename}」？\n（输出文件一并删除，不可恢复）`)) return;
  const resp = await fetch(`/api/projects/${p.id}`, { method: "DELETE" });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    alert(errorText(err, `删除失败: ${resp.status}`));
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
  $("total-count").textContent = msg;
}

// ---------------------------------------------------------------------------
// 新建项目（表单提交）
// ---------------------------------------------------------------------------
function openUploadPanel() {
  $("upload-panel").hidden = false;
  $("project-name-input").focus();
}

$("new-project-btn").addEventListener("click", () => {
  if (uploadSession) { openUploadPanel(); return; }
  const panel = $("upload-panel");
  if (panel.hidden) openUploadPanel();
  else { panel.hidden = true; $("upload-form").reset(); resetUploadForm(); }
});

// 空状态的 CTA 与顶栏按钮同行为
$("empty-new-btn").addEventListener("click", openUploadPanel);

$("cancel-upload-btn").addEventListener("click", async () => {
  if (uploadSession) { await cancelUpload(); return; }
  $("upload-panel").hidden = true;
  $("upload-form").reset();
  resetUploadForm();
});

// ---------------------------------------------------------------------------
// 拖放上传：整个 dropzone 区域可拖可点。选中 ≥2 个文件时展示段落列表，
// 提交时按列表顺序全部 append 到 "files" 字段——后端逐段抽音轨拼接成
// 一个 WAV 一次转写。前端不探测、不选编码：转写只吃音轨，没有分支。
// ---------------------------------------------------------------------------
const dz = $("dropzone");
const fileInput = $("file-input");

function fmtSize(bytes) {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

let selectedFiles = [];   // 有序：即拼接顺序

const VIDEO_RE = /\.(mp4|mov|mkv|avi|m4v|flv|ts|webm|mpg|mpeg)$/i;
const isMediaFile = (f) => f.type.startsWith("video/") || f.type.startsWith("audio/")
  || VIDEO_RE.test(f.name) || /\.(mp3|wav|m4a|aac|flac)$/i.test(f.name);

function showFile() {
  const n = selectedFiles.length;
  dz.classList.toggle("has-file", n > 0);
  if (n === 0) {
    $("dz-main").textContent = "点击选择文件，或拖拽到此处";
    $("dz-sub").textContent = "支持 mp4 / mov / mp3 / wav 等音视频；多段自动拼接音轨连续转写";
  } else if (n === 1) {
    $("dz-main").textContent = selectedFiles[0].name;
    $("dz-sub").textContent = `${fmtSize(selectedFiles[0].size)} · 重新选择/拖拽可替换`;
  } else {
    $("dz-main").textContent = `已选 ${n} 段`;
    $("dz-sub").textContent = "重新选择/拖拽可整体替换；下方列表可调整拼接顺序";
  }
}

function resetUploadForm() {
  selectedFiles = [];
  $("merge-panel").hidden = true;
  $("upload-feedback").hidden = true;
  showFile();
}

function setFiles(list) {
  if (uploadSession) return;
  selectedFiles = Array.from(list);
  $("merge-panel").hidden = selectedFiles.length < 2;
  renderPartsList();
  showFile();
}

// ---------------------------------------------------------------------------
// 段列表（顺序调整 / 移除）
// ---------------------------------------------------------------------------
function renderPartsList() {
  const ul = $("parts-list");
  ul.innerHTML = "";
  $("parts-count").textContent = String(selectedFiles.length);
  selectedFiles.forEach((f, i) => {
    const li = document.createElement("li");
    li.className = "part-row";

    const idx = document.createElement("span");
    idx.className = "part-idx";
    idx.textContent = String(i + 1);

    const name = document.createElement("span");
    name.className = "part-name";
    name.textContent = f.name;
    name.title = f.name;

    const size = document.createElement("span");
    size.className = "part-size";
    size.textContent = fmtSize(f.size);

    const ops = document.createElement("span");
    ops.className = "part-ops";
    const mkBtn = (label, title, cls, fn) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      b.title = title;
      b.setAttribute("aria-label", `${title}：${f.name}`);
      b.className = cls;
      b.onclick = fn;
      return b;
    };
    ops.append(
      mkBtn("↑", "上移", "", () => movePart(i, -1)),
      mkBtn("↓", "下移", "", () => movePart(i, 1)),
      mkBtn("✕", "移除", "danger", () => removePart(i)),
    );

    li.append(idx, name, size, ops);
    ul.appendChild(li);
  });
}

function movePart(i, delta) {
  const j = i + delta;
  if (j < 0 || j >= selectedFiles.length) return;
  [selectedFiles[i], selectedFiles[j]] = [selectedFiles[j], selectedFiles[i]];
  renderPartsList();
}

function removePart(i) {
  setFiles(selectedFiles.filter((_, k) => k !== i));
}

fileInput.addEventListener("change", () => setFiles(fileInput.files));

dz.addEventListener("dragover", (e) => {
  e.preventDefault();
  dz.classList.add("dragover");
});
dz.addEventListener("dragleave", () => dz.classList.remove("dragover"));
dz.addEventListener("drop", (e) => {
  e.preventDefault();
  dz.classList.remove("dragover");
  const files = e.dataTransfer.files;
  if (uploadSession || !files || !files.length) return;
  // 类型校验走 accept 同款口径（全部文件都要过）
  if (!Array.from(files).every(isMediaFile)) {
    alert("不支持的文件类型，请拖入音视频文件");
    return;
  }
  fileInput.files = files;
  setFiles(files);
});

// ---------------------------------------------------------------------------
// 提交：所有文件按列表顺序 append 到 "files" 字段——单文件、多段同一入口，
// 后端见 1 个走普通转写、见 N 个拼音轨。没有第二个分支。
// ---------------------------------------------------------------------------
$("upload-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (selectedFiles.length === 0 || uploadSession) return;

  const form = new FormData();
  form.append("name", $("project-name-input").value.trim());
  form.append("quality", $("quality").value);
  form.append("diarize", $("diarize").checked ? "true" : "false");
  // 开关藏起来时元素仍在 DOM 里、checked 恒为 false——统一发 "true"/"false"，
  // 不在前端分支，后端拿不到就是 False
  form.append("ai_feedback", $("ai-feedback").checked ? "true" : "false");
  for (const f of selectedFiles) form.append("files", f, f.name);

  const session = { id: null, xhr: null, cancelRequested: false, resolving: false };
  uploadSession = session;
  setUploadLocked(true);
  $("upload-feedback").hidden = false;
  $("upload-task-link").hidden = true;
  $("upload-check-btn").hidden = true;
  $("upload-progress").value = 0;
  uploadMessage("正在准备上传…");
  try {
    session.ticketPromise = fetch("/api/uploads", { method: "POST" }).then(async (r) => {
      const data = await r.json();
      if (!r.ok) throw new Error(errorText(data, "无法创建上传凭证"));
      session.id = data.upload_id;
      return data;
    });
    await session.ticketPromise;
    if (session.cancelRequested) return; // 取消处理器等待凭证后再裁决，永不开始发送。
    const data = await sendUpload(session, form);
    if (!session.cancelRequested) acceptUpload(session, data.task_id, true);
  } catch (err) {
    if (session.cancelRequested) return;
    if (session.id) {
      session.failureMessage = err.message;
      uploadMessage(`${err.message}。正在查询接收结果…`);
      await resolveUpload(session, true);
    } else {
      uploadMessage(`上传未开始：${err.message}。请重试。`);
      finishUpload(session);
    }
  }
});

function uploadMessage(message) { $("upload-message").textContent = message; }
function setUploadLocked(locked) {
  $("upload-form").setAttribute("aria-busy", String(locked));
  for (const id of ["submit-btn", "file-input", "project-name-input", "quality", "diarize", "ai-feedback"]) $(id).disabled = locked;
  document.querySelectorAll("#parts-list button").forEach((b) => { b.disabled = locked; });
  $("submit-btn").textContent = locked ? "正在上传…" : "开始转写";
  $("cancel-upload-btn").textContent = locked ? "取消上传" : "收起";
}
function sendUpload(session, form) {
  return new Promise((resolve, reject) => {
    const xhr = session.xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/projects");
    xhr.setRequestHeader("X-Upload-Id", session.id);
    xhr.upload.onprogress = (e) => {
      if (session.cancelRequested) return;
      if (e.lengthComputable) {
        const pct = Math.min(100, Math.round(e.loaded / e.total * 100));
        $("upload-progress").value = pct;
        uploadMessage(e.loaded >= e.total ? "正在确认接收…" : `正在上传 ${pct}% · ${fmtSize(e.loaded)} / ${fmtSize(e.total)}`);
      } else {
        $("upload-progress").removeAttribute("value");
        uploadMessage(`正在上传 · 已发送 ${fmtSize(e.loaded)}`);
      }
    };
    xhr.upload.onload = () => { if (!session.cancelRequested) uploadMessage("正在确认接收…"); };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch { /* 响应异常由凭证恢复 */ }
      if (xhr.status >= 200 && xhr.status < 300 && data.task_id) resolve(data);
      else reject(new Error(errorText(data, `上传请求失败（${xhr.status}）`)));
    };
    xhr.onerror = () => reject(new Error("网络连接中断"));
    xhr.onabort = () => reject(new Error("已停止发送，等待服务端确认"));
    xhr.send(form);
  });
}
function finishUpload(session) {
  if (uploadSession !== session) return;
  uploadSession = null;
  setUploadLocked(false);
  $("cancel-upload-btn").disabled = false;
  $("upload-check-btn").hidden = true;
}
function acceptUpload(session, taskId, navigate) {
  if (!taskId) return;
  uploadMessage("任务已接收，继续处理。");
  $("upload-progress").value = 100;
  const link = $("upload-task-link");
  link.href = `/task/${encodeURIComponent(taskId)}`;
  link.hidden = false;
  finishUpload(session);
  loadProjects();
  if (navigate && !session.cancelRequested) window.location.href = link.href;
}
async function resolveUpload(session, requestCancel = false) {
  if (!session.id || session.resolving) return;
  session.resolving = true;
  $("upload-check-btn").disabled = true;
  try {
    const resp = await fetch(`/api/uploads/${encodeURIComponent(session.id)}${requestCancel ? "/cancel" : ""}`, requestCancel ? { method: "POST" } : {});
    const data = await resp.json();
    const state = resp.ok ? data : data.detail || {};
    if (state.status === "accepted" || state.code === "upload_already_accepted") {
      acceptUpload(session, state.task_id, false);
    } else if (resp.ok && state.status === "cancelled") {
      uploadMessage(session.cancelRequested ? "已取消上传，未创建转写任务。" : `${session.failureMessage || "上传未完成"}。已确认未创建任务，可以重试。`);
      finishUpload(session);
    } else {
      throw new Error(errorText(data, "服务端尚未确认取消"));
    }
  } catch {
    uploadMessage(`${session.cancelRequested ? "取消" : "接收"}结果暂未确认。请保持此页，恢复连接后查询；任务可能已经接收。`);
    $("upload-check-btn").hidden = false;
    $("cancel-upload-btn").disabled = false;
  } finally {
    session.resolving = false;
    $("upload-check-btn").disabled = false;
  }
}
async function cancelUpload() {
  const session = uploadSession;
  if (!session || session.resolving) return;
  session.cancelRequested = true;
  $("cancel-upload-btn").disabled = true;
  uploadMessage("已停止发送，正在确认取消结果…");
  if (session.xhr) session.xhr.abort();
  try { await session.ticketPromise; }
  catch { uploadMessage("上传未开始，未创建转写任务。"); finishUpload(session); return; }
  await resolveUpload(session, true);
}
$("upload-check-btn").addEventListener("click", () => {
  if (uploadSession) resolveUpload(uploadSession, true);
});

// ---------------------------------------------------------------------------
// 搜索 + 筛选（防抖）
// ---------------------------------------------------------------------------
$("search-input").addEventListener("input", () => {
  clearSelection();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadProjects, 200);
});
$("status-filter").addEventListener("change", () => { clearSelection(); loadProjects(); });
$("empty-clear-btn").addEventListener("click", () => {
  $("search-input").value = "";
  $("status-filter").value = "";
  clearSelection();
  loadProjects();
});

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
  const busy = visibleProjects.filter((p) => selectedIds.has(p.id) && !canDelete(p));
  const noOutput = visibleProjects.filter((p) => selectedIds.has(p.id) && !transcriptionDone(p));
  $("batch-busy-note").hidden = !busy.length;
  $("batch-busy-note").textContent = `${busy.length ? `${busy.length} 个任务仍在处理，删除会跳过。` : ""}${noOutput.length ? `ZIP 将跳过 ${noOutput.length} 个尚无转写结果的项目，并附跳过清单。` : ""}`;
  $("batch-busy-note").hidden = !busy.length && !noOutput.length;
  $("batch-delete-btn").disabled = n > 0 && busy.length === n;

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

function clearSelection() {
  selectedIds.clear();
  document.querySelectorAll("#projects-body input[type='checkbox']").forEach((c) => (c.checked = false));
  $("select-all").checked = false;
  $("select-all").indeterminate = false;
  updateBatchUI();
}
$("batch-clear-btn").addEventListener("click", clearSelection);

$("batch-delete-btn").addEventListener("click", async () => {
  const targets = visibleProjects.filter((p) => selectedIds.has(p.id) && canDelete(p));
  const ids = targets.map((p) => p.id);
  if (ids.length === 0) return;
  const names = targets.map((p) => `• ${p.name || p.filename}`).join("\n");
  const skipped = selectedIds.size - ids.length;
  if (!confirm(`确认删除以下 ${ids.length} 个项目？\n${names}\n\n录音上传副本、转写和 AI 输出文件一并删除，不可恢复。${skipped ? `\n另有 ${skipped} 个处理中任务将跳过。` : ""}`)) return;

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
  const failedMsg = data.failed.length ? `（${data.failed.length} 个未删除，可能仍在处理）` : "";
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

// ---------------------------------------------------------------------------
// LLM 可用性：没配 llm 节就整个开关藏起来，不占位不报错
// ---------------------------------------------------------------------------
fetch("/api/llm/status")
  .then((r) => r.json())
  .then((s) => {
    if (!s.enabled) return;
    llmEnabled = true;
    $("ai-feedback-block").hidden = false;
    $("ai-privacy").hidden = false;
    // 列表可能先于此返回（竞态）：补一次渲染让 AI 按钮出现
    loadProjects();
  })
  .catch(() => {});
