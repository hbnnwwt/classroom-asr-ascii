// Vanilla JS：上传 → 轮询 → 阶段可视化 → 转写展示 → 下载
const $ = (id) => document.getElementById(id);

// 滚动开关按钮的两态内容（内联 SVG 图标 + 文字）
const LIVE_ON = '<svg class="icon" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M6 4h4v16H6zM14 4h4v16h-4z"/></svg>暂停滚动';
const LIVE_OFF = '<svg class="icon" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M6 4l14 8-14 8z"/></svg>恢复滚动';

// ai 是 done 之后追加的 stage：开了 AI 反馈的任务转写完成后还会走它
const STAGE_ORDER = ["merge", "probe", "extract", "denoise", "asr", "postprocess", "write", "ai", "done"];
const MAX_VISIBLE_LINES = 80;    // DOM 中最多保留的段数（防长 transcript 把页面撑爆）

let currentTaskId = null;
let pollTimer = null;
let streamSegments = [];      // 当前任务的全部分段
let liveMode = true;          // true=auto-scroll 到底；false=用户自由滚动
let elapsedTimer = null;      // 阶段级每秒 ticker（区别于任务级 taskElapsedTimer）
let taskStartTimeMs = 0;     // 任务 started_at 毫秒
let taskEndTimeMs = 0;       // 任务 ended_at 毫秒（0 表示还在跑）
let taskElapsedTimer = null; // 每秒刷整体已用时间
let currentStage = null;      // 当前阶段名（用于 elapsed 显示）
let stageStartTime = 0;       // 当前阶段开始时间（来自后端 stage_started_at，刷新不重置）
let taskStatus = null;        // 当前任务状态（done 时一次性渲染 transcript）
let llmAvailable = false;     // LLM 是否配置（未配置时点"生成 AI 反馈"跳设置页）
let transcriptionFinished = false;
let transcriptPage = 0;
let transcriptQuery = "";
let lastTranscript = null;
let lastTranscriptFinal = false;
let polling = false;

/**
 * 页面加载时恢复任务：URL 路径 /task/{id} 优先（管理界面跳转过来），
 * ?task=xxx 兼容旧版（用户从老链接进），localStorage 兜底。
 * 拉到 /status 自动恢复 stages + transcript + downloads。
 */
function getTaskIdFromUrl() {
  // 1) 路径 /task/{id}
  const m = window.location.pathname.match(/^\/task\/([a-zA-Z0-9]+)\/?$/);
  if (m) return m[1];
  // 2) 兼容 ?task=xxx
  const params = new URLSearchParams(window.location.search);
  return params.get("task");
}

function setTaskIdInUrl(taskId) {
  // 切到标准路径 /task/{id}
  if (taskId) {
    window.history.replaceState({}, "", `/task/${taskId}`);
  } else {
    window.history.replaceState({}, "", "/");
  }
}

async function restoreLastTask() {
  // 优先级：URL > localStorage（旧版兼容）
  const taskId = getTaskIdFromUrl() || localStorage.getItem("lastTaskId");
  if (!taskId) {
    // 详情页必须有 task_id，否则回主页
    window.location.href = "/";
    return;
  }

  currentTaskId = taskId;
  $("task-settings-link").href = `/settings?from_task=${encodeURIComponent(taskId)}`;
  // ID 已收进完成态摘要小字行（updateSummary），不再单独设
  // 同步 URL：旧版 ?task= 进来时，转成标准 /task/{id}
  if (!window.location.pathname.startsWith("/task/")) setTaskIdInUrl(taskId);
  // 清掉旧 localStorage（迁移完成后不再用）
  localStorage.removeItem("lastTaskId");

  $("status-panel").hidden = false;
  $("transcript-panel").hidden = false;
  resetStages();
  streamSegments = [];
  liveMode = true;
  $("live-toggle").innerHTML = LIVE_ON;
  startPolling();
}

// LLM 可用性：只影响"生成 AI 反馈"点击行为（未配置跳设置页），不再门控按钮渲染。
fetch("/api/llm/status")
  .then((r) => r.json())
  .then((s) => { llmAvailable = Boolean(s.enabled); })
  .catch(() => {});

restoreLastTask();

function resetStages() {
  document.querySelectorAll("#stages li").forEach((li) => {
    li.classList.remove("done", "active", "pending");
    li.classList.add("pending");
    li.querySelector(".mark").textContent = "·";
    li.querySelector(".msg").textContent = "";
  });
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  poll();
  pollTimer = setInterval(poll, 1000);
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

async function poll() {
  if (!currentTaskId || polling) return;
  polling = true;
  try {
    const resp = await fetch(`/status/${currentTaskId}`);
    if (resp.status === 404) {
      // 任务不存在（服务重启 + 数据库重置等）：回到主页
      localStorage.removeItem("lastTaskId");
      stopPolling();
      window.location.href = "/";
      return;
    }
    if (!resp.ok) { showError(`查询失败: ${resp.status}`); stopPolling(); return; }
    const t = await resp.json();
    updateUI(t);
    // AI 反馈可能还在 done 之后跑：开关开了且没有产物/错误 → 继续轮询
    const aiPending = ["queued", "running"].includes(t.ai_status) || t.execution_active || (t.status === "done" && t.meta
      && t.meta.ai_feedback && !t.meta.ai_files && !t.meta.ai_error
      && !t.meta.ai_errors);
    if ((t.status === "done" || t.status === "failed") && !aiPending) {
      stopPolling();
    }
  } catch (err) {
    showError(err.message);
  } finally {
    polling = false;
  }
}

function updateUI(t) {
  taskStatus = t.status;
  transcriptionFinished = t.transcription_status === "done" || t.status === "done" || t.stage === "ai";
  // 完成态折叠：阶段列表/进度条收起（CSS body.is-final）。折叠跟 status 走、
  // 不跟历史走——AI 重跑时 status 回 running，class 自动摘除、阶段列表自动回归
  document.body.classList.toggle("is-final", transcriptionFinished);
  // 多段任务专属 UI：拼接阶段行（新任务 meta.parts，旧任务 meta.merge_mode）。
  // MP4 下载按钮只属于旧版"真合并"任务——新方案不产视频，只产字幕。
  const isMulti = Boolean(t.meta && (t.meta.parts || t.meta.merge_mode));
  const mergeLi = document.querySelector('#stages li[data-stage="merge"]');
  if (mergeLi) mergeLi.style.display = isMulti ? "" : "none";
  // AI stage 行显隐不放 done 分支：AI 进行中任务仍是 running，行得跟着每轮轮询走。
  // 三个信号任一命中就显示——开过开关 / 失败过 / 出过产物。
  // 转写失败（status=failed）不留 AI 行：meta 里的 ai_feedback 开关只代表
  // "开过"，任务失败时该行会永远 pending，误导用户在等一个不会来的结果。
  const hasAi = t.status !== "failed" && Boolean(
    t.meta && (t.meta.ai_feedback || t.meta.ai_error
               || t.meta.ai_errors || t.meta.ai_files));
  const aiLi = document.querySelector('#stages li[data-stage="ai"]');
  if (aiLi) aiLi.style.display = hasAi ? "" : "none";
  const mp4Btn = document.querySelector('#downloads .btn[data-fmt="mp4"]');
  if (mp4Btn) mp4Btn.hidden = !(t.meta && t.meta.merge_mode);
  // 术语日志跟管线事实走：写过 replace.log（meta.formats 有登记）才亮按钮，
  // 没命中术语的任务不再点出 404
  const rlBtn = document.querySelector('#downloads .btn[data-fmt="replace.log"]');
  if (rlBtn) rlBtn.hidden = !(t.meta && (t.meta.formats || []).includes("replace_log"));
  $("status").textContent = transcriptionFinished ? "转写已完成" : statusLabel(t.status);
  $("error").hidden = true;
  // 项目名（页面顶部）
  if (t.name && $("project-name").textContent !== t.name) {
    $("project-name").textContent = t.name;
    document.title = `${t.name} · Classroom ASR`;
  }
  // 任务整体已用时间（started_at → now；未启动显示 -；已完成用 ended_at）
  updateTaskElapsed(t);
  updateSummary(t);
  if (t.stage) renderStages(t.stage, t.message, t.stage_started_at, t.started_at);
  renderMainPhases(t);

  // 整体进度条
  const pct = transcriptionFinished ? 100 : Math.max(0, Math.min(100, Math.round((t.progress || 0) * 100)));
  $("progress-fill").style.width = pct + "%";
  $("progress-fill").textContent = pct + "%";
  document.querySelector(".progress-bar").setAttribute("aria-valuenow", String(pct));

  // 伪实时转写：transcript 是 JSON 编码的 segments 数组
  if (t.transcript || transcriptionFinished) handleTranscript(t.transcript || "[]");

  if (t.status === "failed" && !transcriptionFinished) showError(t.error || "转写失败，请检查录音和设置后重试。");
  // AI stage（running+stage=ai）期间转写产物已最终落盘，后端已放行下载：
  // 下载区照常显示，别让用户白等一次 LLM。
  if (transcriptionFinished) {
    $("downloads").hidden = false;
    $("ai-section").hidden = false;   // AI 产物区与下载区同条件显隐
    document.querySelectorAll("#downloads a.btn").forEach((a) => {
      const formats = t.available_formats || t.meta?.formats || [];
      const format = a.dataset.fmt;
      a.hidden = !formats.includes(format) && !(format === "replace.log" && formats.includes("replace_log")) && !(format === "mp4" && t.meta?.merge_mode);
      a.href = `/download/${currentTaskId}/${a.dataset.fmt}`;
      a.download = "";
    });
    $("more-formats").hidden = !Array.from(document.querySelectorAll("#more-formats a")).some((a) => !a.hidden);
    const fast = t.meta?.quality === "fast";
    $("quality-note").hidden = !fast;
    $("quality-note").textContent = "本任务使用快速文字模式，字幕时间信息较粗；制作精细字幕建议选择字幕转写模式。";
    renderPartDownloads(t.meta);
    renderAiUi(t);
  }
}

function renderMainPhases(t) {
  const phase = ["asr"].includes(t.stage) ? 1 : ["postprocess", "write", "done", "ai"].includes(t.stage) ? 2 : 0;
  document.querySelectorAll("#main-phases li").forEach((li, i) => {
    li.classList.toggle("done", transcriptionFinished || i < phase);
    li.classList.toggle("active", !transcriptionFinished && t.status === "running" && i === phase);
    if (!transcriptionFinished && i === phase) li.setAttribute("aria-current", "step");
    else li.removeAttribute("aria-current");
  });
  const aiActive = ["queued", "running"].includes(t.ai_status) || (t.stage === "ai" && ["pending", "running"].includes(t.status));
  $("phase-message").textContent = transcriptionFinished
    ? `转写已完成，可以阅读和下载。${aiActive ? "AI 内容正在生成，可稍后查看。" : ""}`
    : t.status === "failed" ? "转写未完成，请查看下方原因。"
    : t.status === "pending" ? "任务已接收，正在排队。开始后将逐段显示文字。"
    : ["正在准备录音，第一批文字出现前可能需要等待。", "正在生成文字，可在右侧查看已识别内容。", "文字已生成，正在整理时间轴和下载文件。"][phase];
}

/**
 * 多段任务 done 后：渲染每段独立 SRT 下载链接（/download/{id}/part_srt/{i}）。
 * part_files 由后端完成后写进 meta；每次整体重建，天然幂等。
 */
function renderPartDownloads(meta) {
  const box = $("part-downloads");
  const ul = $("part-downloads-list");
  const files = (meta && meta.part_files) || [];
  const parts = (meta && meta.parts) || [];
  if (!files.length) { box.hidden = true; return; }

  box.hidden = false;
  ul.innerHTML = "";
  files.forEach((fname, i) => {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = `/download/${currentTaskId}/part_srt/${i}`;
    const num = document.createElement("span");
    num.className = "part-num";
    num.textContent = String(i + 1);
    const label = document.createElement("span");
    label.textContent = parts[i] || fname;   // 段原名（与上传文件一致）
    const size = document.createElement("span");
    size.className = "part-fname";
    size.textContent = fname;
    a.append(num, label, size);
    li.appendChild(a);
    ul.appendChild(li);
  });
}

/**
 * AI 反馈按钮区（仅 done 分支调用）：三件产物各自独立显隐。
 * 查看按钮：该件有登记才亮；错误条：全局（前置失败）+ 按件（单件失败
 * 不连坐）谁失败报谁；动作按钮：点评+优化稿整组、深度优化单独，各配
 * 生成/重新生成/重试三态（confirm 防误触烧 token）。
 */
const AI_NAMES = { feedback: "教学点评", polished: "整理后的逐字稿", deep: "课堂讲义" };
const AI_TITLES = AI_NAMES;
let aiPairFiles = false;   // 点击时判定 confirm 用（产物可能被上一轮 renderAiUi 刷新）
let aiDeepFiles = false;

function renderAiUi(t) {
  const meta = t.meta || {};
  const files = meta.ai_files || {};
  const errs = meta.ai_errors || {};
  document.querySelectorAll("#ai-section .btn-view").forEach((b) => {
    b.hidden = !files[b.dataset.view];
  });
  aiPairFiles = Boolean(files.feedback || files.polished);
  aiDeepFiles = Boolean(files.deep);

  const active = ["queued", "running"].includes(t.ai_status) || (t.stage === "ai" && ["pending", "running"].includes(t.status));
  $("ai-state-message").textContent = active ? (t.ai_status === "queued" ? "AI 内容正在排队，转写结果已经可以下载。" : "AI 内容生成中，转写结果已经可以下载。") : "教学点评帮助回顾课堂；整理后的逐字稿便于阅读；课堂讲义用于课后复习。";

  const box = $("ai-actions");
  box.hidden = active || t.execution_active;
  $("ai-modal-regen").disabled = active || Boolean(t.execution_active);

  // 错误条：全局 ai_error（无转写/LLM 未配置等前置失败）+ 按件 ai_errors，
  // 一条一行；已有产物 + 补跑再失败时两键并存，不得拿陈旧产物掩住错误
  const lines = [];
  if (meta.ai_error) lines.push(`AI 反馈失败：${meta.ai_error}`);
  Object.entries(errs).forEach(([fmt, msg]) =>
    lines.push(`${AI_NAMES[fmt] || fmt}本次生成失败：${msg}${files[fmt] ? "。仍可查看上次版本。" : ""}`));
  Object.entries(meta.ai_attempts || {}).forEach(([fmt, attempt]) => {
    if (attempt.status === "failed" && !errs[fmt]) lines.push(`${AI_NAMES[fmt] || fmt}本次生成失败。${files[fmt] ? "仍可查看上次版本。" : "请重试。"}`);
  });
  const errBox = $("ai-error");
  errBox.hidden = !lines.length;
  const errorText = lines.join("\n");
  if (errBox.textContent !== errorText) errBox.textContent = errorText;

  const pairErr = errs.feedback || errs.polished || meta.ai_error;
  $("ai-retry").textContent = pairErr ? "重试 AI 反馈"
    : (aiPairFiles ? "重新生成 AI 反馈" : "生成 AI 反馈");
  $("ai-deep").textContent = errs.deep ? "重试课堂讲义"
    : (aiDeepFiles ? "重新生成课堂讲义" : "生成课堂讲义");
}

// 手动生成/重新生成（任务页两键 + 阅读层单件键共用）：POST 触发后台
// AI stage，收起动作区防二次点击吃 409，恢复轮询看进度（失败只 alert）
async function triggerAiRun(path, btn, confirmMsg) {
  // 未配置 LLM：不 POST（必吃 409），直接送去设置页
  if (!llmAvailable) { window.location.href = `/settings?from_task=${encodeURIComponent(currentTaskId)}`; return; }
  if (confirmMsg && !confirm(confirmMsg)) return;
  btn.disabled = true;
  try {
    const resp = await fetch(`/api/tasks/${currentTaskId}${path}`, { method: "POST" });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(typeof err.detail === "string" ? err.detail : err.detail?.message || `触发失败: ${resp.status}`);
    }
    closeAiViewer();
    $("ai-actions").hidden = true;   // 重跑期间收起按钮，防二次点击吃 409
    startPolling();   // 恢复轮询，看 AI stage 进度
  } catch (err) {
    alert(err.message);
  } finally {
    btn.disabled = false;
  }
}

$("ai-retry").addEventListener("click", () =>
  triggerAiRun("/feedback", $("ai-retry"),
    aiPairFiles ? "重新生成会覆盖现有 AI 点评和优化稿，并再次调用 LLM。继续？" : null));
$("ai-deep").addEventListener("click", () =>
  triggerAiRun("/ai/deep", $("ai-deep"),
    aiDeepFiles ? "重新生成会覆盖现有深度优化讲义，并再次调用 LLM。继续？" : null));
$("ai-modal-regen").addEventListener("click", () => {
  const fmt = currentModalFmt;
  if (!fmt) return;
  triggerAiRun(`/ai/${fmt}`, $("ai-modal-regen"),
    `重新生成会覆盖现有${AI_TITLES[fmt]}，并再次调用 LLM。继续？`);
});

/**
 * 识别中展示最近 80 段；完成后按最终数组分页，搜索覆盖全部段落。
 * 未变化的轮询不重绘，保留用户当前页与阅读位置。
 */
function handleTranscript(transcriptJson) {
  let segs;
  try { segs = JSON.parse(transcriptJson); } catch { return; }
  if (!Array.isArray(segs)) return;
  if (lastTranscript === transcriptJson && lastTranscriptFinal === transcriptionFinished) return;
  if (lastTranscriptFinal !== transcriptionFinished) transcriptPage = 0;
  lastTranscript = transcriptJson;
  lastTranscriptFinal = transcriptionFinished;
  streamSegments = segs;
  $("transcript-panel").hidden = false;
  renderTranscript();
}

/** 全量数组始终保留；仅当前页或最近 80 段进入 DOM。 */
function renderTranscript() {
  const container = $("transcript");
  const previousScroll = container.scrollTop;
  const query = transcriptQuery.toLocaleLowerCase();
  const matching = streamSegments.map((segment, index) => ({ segment, index }))
    .filter(({ segment }) => !transcriptionFinished || !query || `${segment.spk || ""} ${segment.text || ""}`.toLocaleLowerCase().includes(query));
  const pages = Math.max(1, Math.ceil(matching.length / MAX_VISIBLE_LINES));
  transcriptPage = Math.max(0, Math.min(transcriptPage, pages - 1));
  const start = transcriptionFinished ? transcriptPage * MAX_VISIBLE_LINES : Math.max(0, matching.length - MAX_VISIBLE_LINES);
  const rows = matching.slice(start, start + MAX_VISIBLE_LINES);
  const fragment = document.createDocumentFragment();
  for (const { segment: seg, index } of rows) {
    const line = document.createElement("div");
    line.className = "line";
    line.dataset.segment = String(index + 1);
    const ts = document.createElement("span");
    ts.className = "ts";
    ts.textContent = fmtTs(seg.start);
    const text = document.createElement("span");
    text.className = "txt";
    text.textContent = `${seg.spk ? `${seg.spk}：` : ""}${seg.text || ""}`;
    line.append(ts, text);
    fragment.appendChild(line);
  }
  container.replaceChildren(fragment);
  $("transcript-heading").textContent = transcriptionFinished ? "完整逐字稿" : "实时转写";
  $("transcript-reading").hidden = !transcriptionFinished;
  $("live-toggle").hidden = transcriptionFinished;
  $("copy-transcript").disabled = !streamSegments.length;
  $("transcript-count").textContent = `（共 ${streamSegments.length} 段）`;
  $("transcript-page").textContent = `${transcriptPage + 1} / ${pages} 页${query ? ` · ${matching.length} 个匹配段落` : ""}`;
  $("transcript-prev").disabled = transcriptPage === 0;
  $("transcript-next").disabled = transcriptPage + 1 >= pages;
  $("transcript-note").textContent = transcriptionFinished
    ? (!matching.length ? "没有匹配的段落，请修改搜索内容。" : `显示第 ${start + 1}–${start + rows.length} 个${query ? "匹配" : ""}段落。搜索与复制覆盖完整逐字稿。`)
    : streamSegments.length ? `已识别 ${streamSegments.length} 段，显示最近 ${rows.length} 段。完成后可分页阅读与搜索全文。`
    : "正在准备录音。第一批文字出现前可能需要等待，请查看处理阶段。";
  container.scrollTop = transcriptionFinished ? previousScroll : liveMode ? container.scrollHeight : previousScroll;
}

$("transcript-search").addEventListener("input", (e) => {
  transcriptQuery = e.target.value.trim();
  transcriptPage = 0;
  renderTranscript();
  $("transcript").scrollTop = 0;
});
for (const [id, delta] of [["transcript-prev", -1], ["transcript-next", 1]]) {
  $(id).addEventListener("click", () => { transcriptPage += delta; renderTranscript(); $("transcript").scrollTop = 0; });
}

/** 暂停/恢复自动滚动 */
$("live-toggle").addEventListener("click", () => {
  liveMode = !liveMode;
  $("live-toggle").innerHTML = liveMode ? LIVE_ON : LIVE_OFF;
  if (liveMode) {
    // 恢复时立即滚到底
    $("transcript").scrollTop = $("transcript").scrollHeight;
  }
});

/** 复制全文：说话人前缀 + 正文，逐段换行（无 spk 就纯文本）。
 * 用 streamSegments 全量数组而非 DOM——DOM 只留最近 80 段，复制不能跟着截断。
 * 要时间戳版去下载 TXT，一个出口一种格式。 */
$("copy-transcript").addEventListener("click", async () => {
  if (!streamSegments.length) return;
  const text = streamSegments
    .map((s) => (s.spk ? `${s.spk}: ${s.text || ""}` : (s.text || "")))
    .join("\n");
  const btn = $("copy-transcript");
  try {
    await navigator.clipboard.writeText(text);
    btn.textContent = "已复制";
    setTimeout(() => { btn.textContent = "复制全文"; }, 1500);
  } catch (err) {
    alert(`复制失败：${err.message}`);
  }
});

function renderStages(stage, msg, stageStartedAt, taskStartedAt) {
  const idx = STAGE_ORDER.indexOf(stage);
  if (idx < 0) return;

  // 阶段切换 / 首次渲染：把 stageStartTime 对齐到后端的 stage_started_at。
  // 老任务（迁移前已创建的）stage_started_at 为空，回退到 taskStartedAt（仍是真实时间，不是 Date.now()）。
  // 都没有才 fallback 到本地时间（极老数据，几乎不会出现）。
  let startTimeMs;
  if (stageStartedAt) {
    startTimeMs = new Date(stageStartedAt).getTime();
  } else if (taskStartedAt) {
    startTimeMs = new Date(taskStartedAt).getTime();
  } else {
    startTimeMs = Date.now();
  }

  if (stage !== currentStage) {
    currentStage = stage;
    stageStartTime = startTimeMs;
    baseMsgFor(stage, msg);
    startElapsedTicker();
  } else if (stageStartedAt) {
    // 同阶段但后端时间戳更新了（重新同步一次，避免漂移）
    stageStartTime = startTimeMs;
  }
  // 同阶段内 msg 可能更新（如 "识别中：25s"），同步缓存
  baseMsgFor(stage, msg);

  document.querySelectorAll("#stages li").forEach((li) => {
    const s = li.dataset.stage;
    const i = STAGE_ORDER.indexOf(s);
    li.classList.remove("done", "active", "pending");
    const mark = li.querySelector(".mark");
    if (i < idx) { li.classList.add("done"); mark.textContent = "✓"; }
    else if (i === idx) {
      li.classList.add("active"); mark.textContent = "●";
      updateStageMsg(li, msg);
    } else { li.classList.add("pending"); mark.textContent = "·"; }
  });
}

/**
 * 任务级"现在"：ended_at 在即冻结——已用 = started_at→ended_at（转写管线的
 * 一生）。AI 重生成不清 ended_at，重载页面计时也不从 started_at 起跳。
 */
function taskNowMs() {
  return taskEndTimeMs || Date.now();
}

/**
 * 阶段级"现在"：任务终态才冻结在 ended_at；运行中（含 AI 重生成，此时
 * ended_at 还是管线旧值）必须走真实时间，否则阶段计时被旧终点冻死。
 */
function stageNowMs() {
  return (taskStatus === "done" || taskStatus === "failed")
    ? taskEndTimeMs || Date.now()
    : Date.now();
}

/**
 * 当前阶段消息区显示：原始消息 + 已用秒数。
 * 例如："提取音轨 · 已用 5s"
 * 阶段切换时由 renderStages 重置计时。
 */
function updateStageMsg(li, baseMsg) {
  const secs = Math.max(0, Math.floor((stageNowMs() - stageStartTime) / 1000));
  if (secs <= 0) {
    // 没有可展示的已用时间（阶段刚开始，或终态后 0 时长的 done 阶段）：只显示消息
    li.querySelector(".msg").textContent = baseMsg || "";
    return;
  }
  const elapsed = secs < 60 ? `${secs}s` : `${Math.floor(secs / 60)}m${secs % 60}s`;
  li.querySelector(".msg").textContent = baseMsg ? `${baseMsg} · ${elapsed}` : elapsed;
}

function updateTaskElapsed(t) {
  // 用 started_at/ended_at 算"任务整体已用时间"。
  // started_at 由后端在 _run_pipeline 起跑时写入，永不被前端重置。
  if (t.started_at) taskStartTimeMs = new Date(t.started_at).getTime();
  if (t.ended_at) taskEndTimeMs = new Date(t.ended_at).getTime();
  if (taskElapsedTimer) clearInterval(taskElapsedTimer);
  if (taskStartTimeMs > 0) {
    const render = () => {
      const secs = Math.max(0, Math.floor((taskNowMs() - taskStartTimeMs) / 1000));
      $("task-elapsed").textContent = formatDuration(secs);
    };
    render();
    // 进行中才启动 ticker；done/failed 一次渲染就够
    if (!taskEndTimeMs) {
      taskElapsedTimer = setInterval(render, 1000);
    }
  }
}

function formatDuration(secs) {
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m${secs % 60}s`;
  return `${Math.floor(secs / 3600)}h${Math.floor((secs % 3600) / 60)}m`;
}

/**
 * 摘要使用录音真实时长 meta.duration_s；段数与说话人数来自完整逐字稿。
 */
function updateSummary(t) {
  const final = transcriptionFinished;
  let segs = [];
  if (t.transcript) {
    try { segs = JSON.parse(t.transcript) || []; } catch { /* 脏数据按无转写处理 */ }
  }
  if (final && segs.length) {
    const dur = Number(t.meta?.duration_s);
    $("sum-duration").textContent = `· 录音时长 ${formatDuration(Math.round(dur))}`;
    $("sum-duration").hidden = !Number.isFinite(dur) || dur <= 0;
    $("sum-segs").textContent = `· ${segs.length} 段`;
    $("sum-segs").hidden = false;
    const speakers = new Set(segs.map((s) => s.spk).filter(Boolean));
    $("sum-speakers").hidden = !speakers.size;
    if (speakers.size) {
      $("sum-speakers").textContent = `· ${speakers.size} 位说话人`;
      $("sum-speakers").hidden = false;
    }
  } else {
    $("sum-duration").hidden = true;
    $("sum-segs").hidden = true;
    $("sum-speakers").hidden = true;
  }
  // 小字行：文件名 · 创建时间 · ID（终态专属；failed 排障看 #error，不掺和）
  const sub = $("summary-sub");
  if (final) {
    const parts = [];
    if (t.filename) parts.push(t.filename);
    if (t.created_at) {
      parts.push(`创建于 ${String(t.created_at).replace("T", " ").slice(0, 16)}`);
    }
    parts.push(`ID: ${currentTaskId}`);
    sub.textContent = parts.join(" · ");
    sub.hidden = false;
  } else {
    sub.hidden = true;
  }
}

function startElapsedTicker() {
  if (elapsedTimer) clearInterval(elapsedTimer);
  elapsedTimer = setInterval(() => {
    if (!currentStage) return;
    // 终态：阶段计时已冻结，ticker 自停（否则 done 阶段的 li 永远 active，永不退出）
    if (taskStatus === "done" || taskStatus === "failed") {
      clearInterval(elapsedTimer);
      elapsedTimer = null;
      return;
    }
    const li = document.querySelector(`#stages li[data-stage="${currentStage}"]`);
    if (li && li.classList.contains("active")) {
      // 保留 baseMsg：通过 dataset 缓存
      updateStageMsg(li, li.dataset.baseMsg || "");
    } else {
      // 阶段已切走，停止
      clearInterval(elapsedTimer);
      elapsedTimer = null;
    }
  }, 1000);
}

/** renderStages 在切换到新阶段时记录 baseMsg，便于 ticker 拼装。*/
function baseMsgFor(stage, msg) {
  const li = document.querySelector(`#stages li[data-stage="${stage}"]`);
  if (li) li.dataset.baseMsg = msg || "";
}

function fmtTs(sec) {
  if (sec == null || isNaN(sec)) return "--:--";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  // 十分位精度：相邻段起点常在同 1 秒内（如 60.51/60.81），秒级分不开
  const s = (sec % 60).toFixed(1).padStart(4, "0");   // "05.3"
  return h > 0 ? `${pad(h)}:${pad(m)}:${s}` : `${pad(m)}:${s}`;
}
const pad = (n) => String(n).padStart(2, "0");

function statusLabel(s) {
  return ({ pending: "等待中", running: "处理中", done: "完成", failed: "失败" })[s] || s;
}
function showError(msg) {
  if ($("error").textContent !== msg) $("error").textContent = msg;
  $("error").hidden = false;
}
// ---------------------------------------------------------------------------
// AI 产物阅读层：服务端把 md 渲染成 HTML（先转义后渲染，XSS 无门），
// 这里只负责取回来填进弹层；下载与否看完再决定，链接挂现成 /download。
// 优化稿/深度优化有两个页签：正文 / 修改痕迹（红字 diff，懒加载 + 本次打开
// 缓存）；阅读层内可单独重新生成本件（POST /ai/{fmt}）。
// ---------------------------------------------------------------------------
let aiCache = {};           // {clean: {name,html,diff}, diff: {...}}，每次打开重置
let currentModalFmt = null; // 当前阅读的产物件（重新生成按钮的 POST 目标）
let modalOpener = null;
let modalRequest = 0;
let modalTab = "clean";
let previousBodyOverflow = "";

function loadAiTab(fmt, tab) {
  const request = ++modalRequest;
  const body = $("ai-modal-body");
  const fill = (d) => {
    $("ai-modal-title").textContent = AI_TITLES[fmt];
    // diff 页签但后端给不了（原逐字稿缺失）→ 老实说，别渲染成正文
    body.innerHTML = (tab === "diff" && !d.diff)
      ? "<p>原逐字稿缺失，无法比对修改痕迹。</p>"
      : d.html;   // 服务端 render_markdown / render_diff 产物（全文转义后渲染）
  };
  if (aiCache[tab]) { fill(aiCache[tab]); return; }
  body.textContent = "加载中…";
  fetch(`/api/tasks/${currentTaskId}/ai/${fmt}${tab === "diff" ? "?diff=1" : ""}`)
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
    .then((d) => { if (request !== modalRequest || currentModalFmt !== fmt || modalTab !== tab) return; aiCache[tab] = d; fill(d); })
    .catch((e) => { if (request === modalRequest && currentModalFmt === fmt) body.textContent = `加载失败：${e.message}。请关闭后重试。`; });
}

function setAiTab(tab) {
  modalTab = tab;
  document.querySelectorAll("#ai-modal-tabs .tab").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === tab);
    b.setAttribute("aria-pressed", String(b.dataset.tab === tab));
  });
  loadAiTab(currentModalFmt, tab);
}

function openAiViewer(fmt) {
  modalOpener = document.activeElement;
  currentModalFmt = fmt;
  aiCache = {};
  // 正文页签文案随件走：深度优化是讲义成稿，不叫"优化稿"
  // （双向设置：先看 deep 再看 polished 时不得残留"讲义"）
  document.querySelector('#ai-modal-tabs .tab[data-tab="clean"]').textContent =
    fmt === "deep" ? "讲义正文" : "逐字稿正文";
  $("ai-modal-tabs").hidden = fmt === "feedback";
  $("ai-modal-title").textContent = AI_TITLES[fmt];
  $("ai-modal-dl").href = `/download/${currentTaskId}/${fmt}`;
  $("ai-modal").hidden = false;
  document.querySelector("main").inert = true;
  previousBodyOverflow = document.body.style.overflow;
  document.body.style.overflow = "hidden";
  setAiTab("clean");
  $("ai-modal-title").focus();
}

function closeAiViewer() {
  if ($("ai-modal").hidden) return;
  $("ai-modal").hidden = true;
  $("ai-modal").classList.remove("is-expanded");
  $("ai-modal-expand").setAttribute("aria-pressed", "false");
  $("ai-modal-expand").textContent = "全屏阅读";
  document.querySelector("main").inert = false;
  document.body.style.overflow = previousBodyOverflow;
  modalRequest++;
  currentModalFmt = null;
  if (modalOpener && modalOpener.isConnected) modalOpener.focus();
}

$("ai-modal-expand").addEventListener("click", () => {
  const expanded = $("ai-modal").classList.toggle("is-expanded");
  $("ai-modal-expand").setAttribute("aria-pressed", String(expanded));
  $("ai-modal-expand").textContent = expanded ? "退出全屏阅读" : "全屏阅读";
});

document.querySelectorAll("#ai-section .btn-view").forEach((b) =>
  b.addEventListener("click", () => openAiViewer(b.dataset.view)));
document.querySelectorAll("#ai-modal-tabs .tab").forEach((b) =>
  b.addEventListener("click", () => setAiTab(b.dataset.tab)));
$("ai-modal-x").addEventListener("click", closeAiViewer);
$("ai-modal-close").addEventListener("click", closeAiViewer);
$("ai-modal").addEventListener("click", (e) => {
  if (e.target === $("ai-modal")) closeAiViewer();   // 点遮罩关闭
});
document.addEventListener("keydown", (e) => {
  if ($("ai-modal").hidden) return;
  if (e.key === "Escape") { e.preventDefault(); closeAiViewer(); return; }
  if (e.key !== "Tab") return;
  const focusable = Array.from($("ai-modal").querySelectorAll('a[href], button:not([disabled]), [tabindex="0"]')).filter((el) => el.getClientRects().length);
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (e.shiftKey && (document.activeElement === first || document.activeElement === $("ai-modal-title"))) { e.preventDefault(); last?.focus(); }
  else if (!e.shiftKey && (document.activeElement === last || document.activeElement === $("ai-modal-title"))) { e.preventDefault(); first?.focus(); }
});
document.addEventListener("focusin", (e) => {
  if (!$("ai-modal").hidden && !$("ai-modal").contains(e.target)) $("ai-modal-title").focus();
});
