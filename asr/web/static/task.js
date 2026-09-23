// Vanilla JS：上传 → 轮询 → 阶段可视化 → 转写展示 → 下载
const $ = (id) => document.getElementById(id);

const STAGE_ORDER = ["probe", "extract", "denoise", "asr", "postprocess", "write", "done"];
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
  $("live-toggle").textContent = "⏸ 暂停滚动";
  startPolling();
}

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
    if (t.status === "done" || t.status === "failed") {
      stopPolling();
    }
  } catch (err) {
    showError(err.message);
  }
}

function updateUI(t) {
  taskStatus = t.status;
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
  if (t.status === "done") {
    $("downloads").hidden = false;
    document.querySelectorAll("#downloads .btn").forEach((a) => {
      a.href = `/download/${currentTaskId}/${a.dataset.fmt}`;
      a.download = "";
    });
  }
}

/**
 * 收到新的 transcript JSON：解析成 segments，把未渲染的段全部补上。
 *
 * 后端在任务 done 的同一笔 UPDATE 里写入全量 transcript（见
 * asr/web/__init__.py _run_pipeline），done 即停轮询也不会漏段——
 * 旧版"逐段 fade-in 伪直播"后端线程与前端 done 即停轮询构成竞态，
 * 页面会冻在第一段，已删除。
 */
function handleTranscript(transcriptJson) {
  let segs;
  try { segs = JSON.parse(transcriptJson); } catch { return; }
  if (!Array.isArray(segs) || segs.length === 0) return;

  streamSegments = segs;
  $("transcript-panel").hidden = false;

  while (streamRendered < streamSegments.length) {
    renderNextSegment();
  }
  // 渲染完毕：去掉末段的 current 高亮（旧流式路径由停表时清理）
  const cur = document.querySelector(".transcript .line.current");
  if (cur) cur.classList.remove("current");
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
  $("live-toggle").textContent = liveMode ? "⏸ 暂停滚动" : "▶ 恢复滚动";
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
  const s = Math.floor(sec % 60);
  return h > 0
    ? `${pad(h)}:${pad(m)}:${pad(s)}`
    : `${pad(m)}:${pad(s)}`;
}
const pad = (n) => String(n).padStart(2, "0");

function statusLabel(s) {
  return ({ pending: "等待中", running: "处理中", done: "完成", failed: "失败" })[s] || s;
}
function showError(msg) {
  $("error").textContent = msg;
  $("error").hidden = false;
}