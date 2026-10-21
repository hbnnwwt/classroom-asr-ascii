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
let streamRendered = 0;       // 已渲染到 DOM 的分段数
let collapsedShown = 0;       // DOM 因 MAX_VISIBLE 被折叠掉的段数
let liveMode = true;          // true=auto-scroll 到底；false=用户自由滚动
let elapsedTimer = null;      // 阶段级每秒 ticker（区别于任务级 taskElapsedTimer）
let taskStartTimeMs = 0;     // 任务 started_at 毫秒
let taskEndTimeMs = 0;       // 任务 ended_at 毫秒（0 表示还在跑）
let taskElapsedTimer = null; // 每秒刷整体已用时间
let currentStage = null;      // 当前阶段名（用于 elapsed 显示）
let stageStartTime = 0;       // 当前阶段开始时间（来自后端 stage_started_at，刷新不重置）
let taskStatus = null;        // 当前任务状态（done 时一次性渲染 transcript）
let llmAvailable = false;     // LLM 是否配置（未配置时点"生成 AI 反馈"跳设置页）

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
  // 同步 URL：旧版 ?task= 进来时，转成标准 /task/{id}
  if (!window.location.pathname.startsWith("/task/")) setTaskIdInUrl(taskId);
  // 清掉旧 localStorage（迁移完成后不再用）
  localStorage.removeItem("lastTaskId");

  $("task-id").textContent = taskId;
  $("status-panel").hidden = false;
  $("transcript-panel").hidden = false;
  resetStages();
  streamSegments = [];
  streamRendered = 0;
  collapsedShown = 0;
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
  if (!currentTaskId) return;
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
    const aiPending = t.status === "done" && t.meta
      && t.meta.ai_feedback && !t.meta.ai_files && !t.meta.ai_error;
    if ((t.status === "done" && !aiPending) || t.status === "failed") {
      stopPolling();
    }
  } catch (err) {
    showError(err.message);
  }
}

function updateUI(t) {
  taskStatus = t.status;
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
    t.meta && (t.meta.ai_feedback || t.meta.ai_error || t.meta.ai_files));
  const aiLi = document.querySelector('#stages li[data-stage="ai"]');
  if (aiLi) aiLi.style.display = hasAi ? "" : "none";
  const mp4Btn = document.querySelector('#downloads .btn[data-fmt="mp4"]');
  if (mp4Btn) mp4Btn.hidden = !(t.meta && t.meta.merge_mode);
  $("status").textContent = statusLabel(t.status);
  // 项目名（页面顶部）
  if (t.name && $("project-name").textContent !== t.name) {
    $("project-name").textContent = t.name;
    document.title = `${t.name} · Classroom ASR`;
  }
  // 任务整体已用时间（started_at → now；未启动显示 -；已完成用 ended_at）
  updateTaskElapsed(t);
  if (t.stage) renderStages(t.stage, t.message, t.stage_started_at, t.started_at);

  // 整体进度条
  const pct = Math.round((t.progress || 0) * 100);
  $("progress-fill").style.width = pct + "%";
  $("progress-fill").textContent = pct + "%";

  // 伪实时转写：transcript 是 JSON 编码的 segments 数组
  if (t.transcript) handleTranscript(t.transcript);

  if (t.status === "failed") showError(t.error || "失败");
  // AI stage（running+stage=ai）期间转写产物已最终落盘，后端已放行下载：
  // 下载区照常显示，别让用户白等一次 LLM。
  if (t.status === "done" || (t.status === "running" && t.stage === "ai")) {
    $("downloads").hidden = false;
    document.querySelectorAll("#downloads a.btn").forEach((a) => {
      a.href = `/download/${currentTaskId}/${a.dataset.fmt}`;
      a.download = "";
    });
    renderPartDownloads(t.meta);
    renderAiUi(t);
  }
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
 * AI 反馈按钮区（仅 done 分支调用）：查看按钮显隐 + 生成/重新生成。
 * 有产物 → 查看按钮亮，动作按钮变"重新生成 AI 反馈"（confirm 防误触烧 token）；
 * 无产物或有错误 → 按原有逻辑显示错误条 + 生成/重试。
 */
let aiHasFiles = false;   // 点击时判定 confirm 用（产物可能被上一轮 renderAiUi 刷新）

function renderAiUi(t) {
  const meta = t.meta || {};
  const fbView = document.querySelector('#downloads .btn-view[data-view="feedback"]');
  const poView = document.querySelector('#downloads .btn-view[data-view="polished"]');
  // ai_error 判定前移到 hasFiles 短路之前：已有产物的任务 API 补跑再失败时
  // meta 两键并存（旧 ai_files + 新 ai_error），必须显示错误条+重试，
  // 不得拿陈旧下载掩住错误。
  const hasError = Boolean(meta.ai_error);
  const hasFiles = Boolean(meta.ai_files) && !hasError;
  aiHasFiles = Boolean(meta.ai_files);
  // 查看按钮与产物同条件显隐（有产物才可看）
  if (fbView) fbView.hidden = !hasFiles;
  if (poView) poView.hidden = !hasFiles;

  const box = $("ai-actions");
  if (t.status !== "done") { box.hidden = true; return; }
  box.hidden = false;
  const errBox = $("ai-error");
  if (hasError) {
    errBox.hidden = false;
    errBox.textContent = `AI 反馈失败：${meta.ai_error}`;
    $("ai-retry").textContent = "重试 AI 反馈";
  } else {
    errBox.hidden = true;
    $("ai-retry").textContent = hasFiles ? "重新生成 AI 反馈" : "生成 AI 反馈";
  }
}

// 手动生成/重新生成：POST 触发后台 AI stage，恢复轮询看进度（失败只 alert，不打断页面）
$("ai-retry").addEventListener("click", async () => {
  // 未配置 LLM：不 POST（必吃 409），直接送去设置页
  if (!llmAvailable) { window.location.href = "/settings"; return; }
  // 已有产物：覆盖旧文件 + 再烧一次 token，确认了才动
  if (aiHasFiles && !confirm("重新生成会覆盖现有 AI 点评和优化稿，并再次调用 LLM。继续？")) return;
  $("ai-retry").disabled = true;
  try {
    const resp = await fetch(`/api/tasks/${currentTaskId}/feedback`, { method: "POST" });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `触发失败: ${resp.status}`);
    }
    $("ai-actions").hidden = true;   // 重跑期间收起按钮，防二次点击吃 409
    startPolling();   // 恢复轮询，看 AI stage 进度
  } catch (err) {
    alert(err.message);
  } finally {
    $("ai-retry").disabled = false;
  }
});

/**
 * 收到新的 transcript JSON：解析成 segments，把未渲染的段全部补上。
 *
 * 识别进行中后端逐组增量写 transcript（见 asr/web/__init__.py _on_segs），
 * 轮询到的数组 append-only 增长；任务 done 时改用最终 .json 整体覆盖
 * （后处理 ITN/术语替换可能改写已渲染的文本，甚至丢段）——检测到前缀
 * 变化就整体重置重绘，否则只补尾部。
 */
function handleTranscript(transcriptJson) {
  let segs;
  try { segs = JSON.parse(transcriptJson); } catch { return; }
  if (!Array.isArray(segs) || segs.length === 0) return;

  // 前缀变化检测：新数组比已渲染的短，或已渲染位置文本不一致 → done 覆盖
  let prefixIntact = segs.length >= streamRendered;
  for (let i = 0; prefixIntact && i < streamRendered; i++) {
    if (!segs[i] || segs[i].text !== streamSegments[i].text) prefixIntact = false;
  }
  if (!prefixIntact) resetTranscriptDom();

  streamSegments = segs;
  $("transcript-panel").hidden = false;

  while (streamRendered < streamSegments.length) {
    renderNextSegment();
  }
  // 渲染完毕：去掉末段的 current 高亮（旧流式路径由停表时清理）
  const cur = document.querySelector(".transcript .line.current");
  if (cur) cur.classList.remove("current");
}

/** 清空 transcript DOM 与渲染计数（done 覆盖改写前缀时整体重绘用）。 */
function resetTranscriptDom() {
  $("transcript").innerHTML = "";
  streamRendered = 0;
  collapsedShown = 0;
}

function renderNextSegment() {
  const seg = streamSegments[streamRendered];
  const div = document.createElement("div");
  div.className = "line";
  div.style.animationDelay = "0s";
  div.innerHTML =
    `<span class="ts">${fmtTs(seg.start)}</span>` +
    `<span class="txt"></span>`;
  div.querySelector(".txt").textContent = seg.text || "";

  // 去掉上一段的 current 高亮
  const prev = document.querySelector(".transcript .line.current");
  if (prev) prev.classList.remove("current");
  div.classList.add("current");

  const container = $("transcript");

  // 折叠提示：删掉最早的"折叠 X 段"占位（保留最新一个）
  const oldCollapsed = container.querySelector(".collapsed-placeholder");
  if (oldCollapsed) oldCollapsed.remove();

  container.appendChild(div);

  // DOM 截断：超过 MAX_VISIBLE_LINES 就删最老的 .line，加折叠提示
  const lines = container.querySelectorAll(".line");
  if (lines.length > MAX_VISIBLE_LINES) {
    const toRemove = lines.length - MAX_VISIBLE_LINES;
    for (let i = 0; i < toRemove; i++) {
      const first = container.querySelector(".line");
      if (first) {
        container.removeChild(first);
        collapsedShown++;
      }
    }
    // 在最前面加折叠提示（独立 class，不参与 .line 计数）
    const placeholder = document.createElement("div");
    placeholder.className = "collapsed-placeholder";
    placeholder.textContent = `··· 已折叠 ${collapsedShown} 段（查看完整请下载 JSON）···`;
    container.insertBefore(placeholder, container.firstChild);
  }

  // live 模式下自动滚到底；paused 时不打扰用户
  if (liveMode) {
    container.scrollTop = container.scrollHeight;
  }

  // 顶部计数器：已显示 / 总段数（先自增再显示，终态 (N/N) 而非差一的 (N-1/N)）
  const total = streamSegments.length;
  streamRendered++;
  $("transcript-count").textContent = `(${streamRendered}/${total})`;
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
 * "现在"的有效值：任务终态（done/failed）后时间冻结在 ended_at，
 * 所有计时（任务级 + 阶段级）随之停走，不再随真实时间增长。
 */
function effectiveNowMs() {
  return taskEndTimeMs || Date.now();
}

/**
 * 当前阶段消息区显示：原始消息 + 已用秒数。
 * 例如："提取音轨 · 已用 5s"
 * 阶段切换时由 renderStages 重置计时。
 */
function updateStageMsg(li, baseMsg) {
  const secs = Math.max(0, Math.floor((effectiveNowMs() - stageStartTime) / 1000));
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
      const secs = Math.max(0, Math.floor((effectiveNowMs() - taskStartTimeMs) / 1000));
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
  $("error").textContent = msg;
  $("error").hidden = false;
}
// ---------------------------------------------------------------------------
// AI 产物阅读层：服务端把 md 渲染成 HTML（先转义后渲染，XSS 无门），
// 这里只负责取回来填进弹层；下载与否看完再决定，链接挂现成 /download。
// 优化稿有两个页签：正文 / 修改痕迹（红字 diff，懒加载 + 本次打开缓存）。
// ---------------------------------------------------------------------------
let aiCache = {};   // {clean: {name,html,diff}, diff: {...}}，每次打开重置

function loadAiTab(fmt, tab) {
  const body = $("ai-modal-body");
  const fill = (d) => {
    if (d.name) $("ai-modal-title").textContent = d.name;
    // diff 页签但后端给不了（原逐字稿缺失）→ 老实说，别渲染成正文
    body.innerHTML = (tab === "diff" && !d.diff)
      ? "<p>原逐字稿缺失，无法比对修改痕迹。</p>"
      : d.html;   // 服务端 render_markdown / render_diff 产物（全文转义后渲染）
  };
  if (aiCache[tab]) { fill(aiCache[tab]); return; }
  body.textContent = "加载中…";
  fetch(`/api/tasks/${currentTaskId}/ai/${fmt}${tab === "diff" ? "?diff=1" : ""}`)
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
    .then((d) => { aiCache[tab] = d; fill(d); })
    .catch((e) => { body.textContent = `✗ 加载失败：${e.message}`; });
}

function setAiTab(tab) {
  document.querySelectorAll("#ai-modal-tabs .tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab));
  loadAiTab("polished", tab);
}

function openAiViewer(fmt) {
  aiCache = {};
  $("ai-modal-tabs").hidden = fmt !== "polished";
  $("ai-modal-title").textContent = fmt === "feedback" ? "AI 点评报告" : "优化逐字稿";
  $("ai-modal-dl").href = `/download/${currentTaskId}/${fmt}`;
  $("ai-modal").hidden = false;
  loadAiTab(fmt, "clean");
}

function closeAiViewer() { $("ai-modal").hidden = true; }

document.querySelectorAll("#downloads .btn-view").forEach((b) =>
  b.addEventListener("click", () => openAiViewer(b.dataset.view)));
document.querySelectorAll("#ai-modal-tabs .tab").forEach((b) =>
  b.addEventListener("click", () => setAiTab(b.dataset.tab)));
$("ai-modal-x").addEventListener("click", closeAiViewer);
$("ai-modal-close").addEventListener("click", closeAiViewer);
$("ai-modal").addEventListener("click", (e) => {
  if (e.target === $("ai-modal")) closeAiViewer();   // 点遮罩关闭
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("ai-modal").hidden) closeAiViewer();
});
