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
  else { panel.hidden = true; $("upload-form").reset(); resetUploadForm(); }
});

// 空状态的 CTA 与顶栏按钮同行为
$("empty-new-btn").addEventListener("click", openUploadPanel);

$("cancel-upload-btn").addEventListener("click", () => {
  $("upload-panel").hidden = true;
  $("upload-form").reset();
  resetUploadForm();
});

// ---------------------------------------------------------------------------
// 拖放上传：整个 dropzone 区域可拖可点。多选/拖入 ≥2 段视频时走
// "暂存探测（/api/uploads）→ 选转码类型 → 创建合并项目"流程。
// ---------------------------------------------------------------------------
const dz = $("dropzone");
const fileInput = $("file-input");

function fmtSize(bytes) {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

let selectedFiles = [];   // 有序：即拼接顺序
let probeResult = null;   // /api/uploads 响应 {upload_id, compatible, parts}
let stagingBusy = false;  // 暂存上传 + 探测进行中

const VIDEO_RE = /\.(mp4|mov|mkv|avi|m4v|flv|ts|webm|mpg|mpeg)$/i;
const isVideoFile = (f) => f.type.startsWith("video/") || VIDEO_RE.test(f.name);
const isMediaFile = (f) => f.type.startsWith("video/") || f.type.startsWith("audio/")
  || VIDEO_RE.test(f.name) || /\.(mp3|wav|m4a|aac|flac)$/i.test(f.name);

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function showFile() {
  const n = selectedFiles.length;
  dz.classList.toggle("has-file", n > 0);
  if (n === 0) {
    $("dz-main").textContent = "点击选择文件，或拖拽到此处";
    $("dz-sub").textContent = "支持 mp4 / mov / mp3 / wav 等音视频；多段视频可拼接合并";
  } else if (n === 1) {
    $("dz-main").textContent = selectedFiles[0].name;
    $("dz-sub").textContent = `${fmtSize(selectedFiles[0].size)} · 重新选择/拖拽可替换`;
  } else {
    $("dz-main").textContent = `已选 ${n} 段视频`;
    $("dz-sub").textContent = "重新选择/拖拽可整体替换；下方列表可调整拼接顺序";
  }
}

function resetUploadForm() {
  selectedFiles = [];
  probeResult = null;
  stagingBusy = false;
  $("merge-panel").hidden = true;
  $("probe-status").hidden = true;
  $("probe-status").innerHTML = "";
  showFile();
  updateSubmitLabel();
}

function setFiles(list) {
  const files = Array.from(list);
  if (files.length >= 2) {
    const bad = files.filter((f) => !isVideoFile(f));
    if (bad.length) {
      alert(`拼接合并要求全部为视频文件，以下不是：\n${bad.map((f) => f.name).join("\n")}`);
      return;
    }
  }
  selectedFiles = files;
  probeResult = null;
  $("merge-panel").hidden = files.length < 2;
  $("probe-status").hidden = true;
  $("probe-status").innerHTML = "";
  renderPartsList();
  showFile();
  updateSubmitLabel();
  if (files.length >= 2) uploadStaging();
}

// ---------------------------------------------------------------------------
// 段列表（顺序调整 / 移除 / 编码徽标）
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

    const codec = document.createElement("span");
    codec.className = "part-codec";
    const p = probeResult && probeResult.parts.find((x) => x.name === f.name);
    if (p) codec.textContent = `${p.video.codec} ${p.video.width}×${p.video.height} · ${p.audio.codec}`;

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

    li.append(idx, name, size, codec, ops);
    ul.appendChild(li);
  });
}

function movePart(i, delta) {
  const j = i + delta;
  if (j < 0 || j >= selectedFiles.length) return;
  [selectedFiles[i], selectedFiles[j]] = [selectedFiles[j], selectedFiles[i]];
  // 集合没变，探测结论仍有效（compatible 与顺序无关）；
  // 只需重渲染 + 刷新转码选项（"以第一段为基准"跟随新的第一段）
  renderPartsList();
  renderProbeStatus();
}

function removePart(i) {
  setFiles(selectedFiles.filter((_, k) => k !== i));  // 集合变了 → 重新暂存探测
}

// ---------------------------------------------------------------------------
// 暂存上传 + 编码探测
// ---------------------------------------------------------------------------
async function uploadStaging() {
  stagingBusy = true;
  const status = $("probe-status");
  status.hidden = false;
  status.innerHTML = '<span class="probe-wait"><span class="spinner" aria-hidden="true"></span>上传并探测各段编码…</span>';
  try {
    const form = new FormData();
    for (const f of selectedFiles) form.append("files", f, f.name);
    const resp = await fetch("/api/uploads", { method: "POST", body: form });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `上传失败: ${resp.status}`);
    }
    probeResult = await resp.json();
    renderPartsList();      // 补上编码徽标
    renderProbeStatus();
  } catch (err) {
    status.innerHTML = `<span class="probe-err">${escapeHtml(err.message)}</span>`;
  } finally {
    stagingBusy = false;
  }
}

/** "以第一段为基准"是否可用：当前列表第一段的编码在 h264/hevc 家族内。 */
function anchorSupportedNow() {
  if (!probeResult || !selectedFiles.length) return false;
  const first = probeResult.parts.find((p) => p.name === selectedFiles[0].name);
  return Boolean(first && ["h264", "hevc"].includes(first.video.codec));
}

function renderProbeStatus() {
  const status = $("probe-status");
  if (!probeResult) return;
  status.hidden = false;

  if (probeResult.compatible) {
    status.innerHTML =
      '<span class="status-badge status-done">编码一致</span>' +
      '<span class="probe-note">无损流拷贝拼接，秒级完成</span>';
    return;
  }

  // 不一致：编码对比 + 转码类型选择
  const rows = probeResult.parts.map((p) =>
    `<tr><td>${escapeHtml(p.name)}</td>` +
    `<td>${escapeHtml(p.video.codec)}${p.video.profile ? " · " + escapeHtml(p.video.profile) : ""}</td>` +
    `<td>${p.video.width}×${p.video.height} @ ${p.video.fps}fps</td>` +
    `<td>${escapeHtml(p.audio.codec)} ${(p.audio.sample_rate / 1000).toFixed(1)}kHz·${p.audio.channels}ch</td></tr>`,
  ).join("");
  status.innerHTML =
    '<span class="status-badge status-failed">编码不一致</span>' +
    '<span class="probe-note">需统一转码后拼接（耗时与视频总时长相关）</span>' +
    `<table class="codec-table"><thead><tr><th>文件</th><th>视频编码</th><th>分辨率@帧率</th><th>音频</th></tr></thead><tbody>${rows}</tbody></table>`;

  const pick = document.createElement("label");
  pick.className = "transcode-pick";
  const caption = document.createElement("span");
  caption.textContent = "转码类型：";
  const sel = document.createElement("select");
  sel.id = "transcode-select";
  const opts = [
    ["", "请选择…"],
    ["h264", "H.264（兼容性最好，推荐）"],
    ["h265", "H.265（体积更小，速度较慢）"],
  ];
  if (anchorSupportedNow()) opts.push(["first", "以第一段编码为基准"]);
  for (const [v, label] of opts) {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = label;
    sel.appendChild(o);
  }
  pick.append(caption, sel);
  status.appendChild(pick);
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
  if (!files || !files.length) return;
  // 类型校验走 accept 同款口径（全部文件都要过）
  if (!Array.from(files).every(isMediaFile)) {
    alert("不支持的文件类型，请拖入音视频文件");
    return;
  }
  fileInput.files = files;
  setFiles(files);
});

// ---------------------------------------------------------------------------
// 提交：单文件走老路径（直传转写）；多段走合并（upload_id + 顺序 + 转码）
// ---------------------------------------------------------------------------
function currentMergeMode() {
  const r = document.querySelector('input[name="merge-mode"]:checked');
  return r ? r.value : "transcribe";
}

function updateSubmitLabel() {
  const multi = selectedFiles.length >= 2;
  $("submit-btn").textContent = multi && currentMergeMode() === "merge_only" ? "开始合并" : "开始转写";
}
document.querySelectorAll('input[name="merge-mode"]').forEach((r) =>
  r.addEventListener("change", updateSubmitLabel));

$("upload-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (selectedFiles.length === 0) return;

  const form = new FormData();
  form.append("name", $("project-name-input").value.trim());
  form.append("quality", $("quality").value);
  form.append("diarize", $("diarize").checked ? "true" : "false");

  if (selectedFiles.length >= 2) {
    if (stagingBusy) { alert("还在上传/探测编码，请稍候"); return; }
    if (!probeResult) { alert("编码探测未完成或失败，请重新选择文件"); return; }
    const sel = document.getElementById("transcode-select");
    const transcode = sel ? sel.value : "";
    if (!probeResult.compatible && !transcode) {
      alert("各段编码不一致，请先选择转码类型");
      return;
    }
    form.append("upload_id", probeResult.upload_id);
    form.append("merge_mode", currentMergeMode());
    form.append("transcode", transcode);
    form.append("order", JSON.stringify(selectedFiles.map((f) => f.name)));
  } else {
    form.append("file", selectedFiles[0], selectedFiles[0].name);
  }

  $("submit-btn").disabled = true;
  $("submit-btn").innerHTML = '<span class="spinner" aria-hidden="true"></span>提交中…';
  try {
    const resp = await fetch("/api/projects", { method: "POST", body: form });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `提交失败: ${resp.status}`);
    }
    const data = await resp.json();
    // 收起表单 + 跳到详情
    $("upload-panel").hidden = true;
    $("upload-form").reset();
    resetUploadForm();
    window.location.href = `/task/${data.task_id}`;
  } catch (err) {
    alert(err.message);
  } finally {
    $("submit-btn").disabled = false;
    updateSubmitLabel();
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
