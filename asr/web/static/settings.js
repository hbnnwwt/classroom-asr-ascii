// 设置页：LLM 配置回填 / 测试连接 / 保存（写回 config.yaml llm 节）
const $ = (id) => document.getElementById(id);
let savedKeySet = false;
let returnTask = null;
let settingsSaved = false;
function showReturnTask() {
  if (!settingsSaved || !returnTask) return;
  $("return-task").href = `/task/${encodeURIComponent(returnTask.id)}`;
  $("return-task").textContent = `返回《${returnTask.name}》`;
  $("return-task").hidden = false;
}
const fromTask = new URLSearchParams(window.location.search).get("from_task");
if (fromTask && /^[a-zA-Z0-9]+$/.test(fromTask)) {
  fetch(`/api/projects/${encodeURIComponent(fromTask)}`)
    .then((r) => r.ok ? r.json() : Promise.reject(new Error("项目不存在")))
    .then((p) => { returnTask = { id: fromTask, name: p.name || p.filename || "原项目" }; showReturnTask(); })
    .catch(() => {});
}

function showKeyState() {
  $("key-state").textContent = savedKeySet ? "已保存访问密钥，出于安全原因不回显。留空将保留原密钥。" : "尚未保存访问密钥。本地服务通常不需要密钥。";
}
$("clear-api-key").addEventListener("change", () => {
  $("api-key").disabled = $("clear-api-key").checked;
});

// 常用服务商预设：选中只填 base_url 与 model，api_key 是账号秘密，预设一律不碰。
// 默认模型对齐 Exam_v5.0 参考项目（generate.py 硬编码值）：MiniMax-M2.7、
// Qwen/Qwen3.5-397B-A17B、gpt-4o；参考项目没给的（Ollama/DeepSeek/硅基/智谱）自选。
const PRESETS = {
  ollama: { base_url: "http://localhost:11434/v1", model: "qwen2.5:7b" },
  deepseek: { base_url: "https://api.deepseek.com/v1", model: "deepseek-chat" },
  siliconflow: { base_url: "https://api.siliconflow.cn/v1", model: "deepseek-ai/DeepSeek-V3" },
  modelscope: { base_url: "https://api-inference.modelscope.cn/v1", model: "Qwen/Qwen3.5-397B-A17B" },
  minimax: { base_url: "https://api.minimaxi.com/v1", model: "MiniMax-M2.7" },
  zhipu: { base_url: "https://open.bigmodel.cn/api/paas/v4", model: "glm-4-flash" },
  openai: { base_url: "https://api.openai.com/v1", model: "gpt-4o" },
};

$("preset").addEventListener("change", (e) => {
  const p = PRESETS[e.target.value];
  if (!p) return;
  $("base-url").value = p.base_url;
  $("model").value = p.model;
});

function collect() {
  return {
    base_url: $("base-url").value.trim(),
    api_key: $("clear-api-key").checked ? "" : ($("api-key").value.trim() || null),
    model: $("model").value.trim(),
    timeout: Number($("timeout").value) || 300,
    max_tokens: Number($("max-tokens").value) || 16384,
  };
}

async function postJSON(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  return { ok: resp.ok, status: resp.status, data };
}

// 状态徽章：与首页 status-badge 同一套（绿=已启用 / 灰=未配置）
function setLlmState(enabled) {
  const b = $("llm-state");
  b.hidden = false;
  b.textContent = enabled ? "已启用" : "未配置";
  b.className = "status-badge " + (enabled ? "status-done" : "status-pending");
}

// 结果消息：ok/err 色对，✓/✗ 前缀保色盲可辨
function showMsg(el, ok, text) {
  el.hidden = false;
  el.textContent = text;
  el.className = ok ? "msg-ok" : "msg-err";
}

// 回填当前值（未配置 → 空表单）
fetch("/api/llm/settings")
  .then((r) => r.json())
  .then((s) => {
    $("base-url").value = s.base_url || "";
    $("api-key").value = "";
    savedKeySet = Boolean(s.api_key_set);
    showKeyState();
    $("model").value = s.model || "";
    $("timeout").value = s.timeout || 300;
    $("max-tokens").value = s.max_tokens || 16384;
    setLlmState(Boolean(s.base_url && s.model));
  })
  .catch(() => showMsg($("save-result"), false, "加载设置失败，请刷新后重试。"));

$("test-btn").addEventListener("click", async () => {
  if (!$("llm-form").reportValidity()) return;
  const box = $("test-result");
  $("test-btn").disabled = true;
  box.hidden = false;
  box.textContent = "测试中…";
  box.className = "";
  try {
    const { data } = await postJSON("/api/llm/test", collect());
    showMsg(box, data.ok,
      data.ok
        ? `✓ 连接成功：${data.reply || "ok"}`
        : `✗ 连接失败：${data.error || "未知错误"}`);
  } catch (e) {
    showMsg(box, false, `✗ 请求失败: ${e.message}`);
  } finally {
    $("test-btn").disabled = false;
  }
});

$("save-btn").addEventListener("click", async () => {
  if (!$("llm-form").reportValidity()) return;
  const box = $("save-result");
  $("save-btn").disabled = true;
  try {
    const values = collect();
    const { ok, data } = await postJSON("/api/llm/settings", values);
    if (!ok) {
      showMsg(box, false, `✗ 保存失败：${typeof data.detail === "string" ? data.detail : data.detail?.message || "请检查输入"}`);
      return;
    }
    setLlmState(Boolean(data.enabled));
    if (values.api_key !== null) savedKeySet = Boolean(values.api_key);
    $("api-key").value = "";
    $("clear-api-key").checked = false;
    $("api-key").disabled = false;
    showKeyState();
    showMsg(box, true, data.enabled
      ? "✓ 已保存，AI 反馈已启用"
      : "✓ 已保存，服务地址或模型名称为空，AI 保持关闭");
    settingsSaved = true;
    showReturnTask();
  } catch (e) {
    showMsg(box, false, `✗ 请求失败: ${e.message}`);
  } finally {
    $("save-btn").disabled = false;
  }
});
