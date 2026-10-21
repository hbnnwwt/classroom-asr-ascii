// 设置页：LLM 配置回填 / 测试连接 / 保存（写回 config.yaml llm 节）
const $ = (id) => document.getElementById(id);

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
    api_key: $("api-key").value.trim(),
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
    $("api-key").value = s.api_key || "";
    $("model").value = s.model || "";
    $("timeout").value = s.timeout || 300;
    $("max-tokens").value = s.max_tokens || 16384;
    setLlmState(Boolean(s.base_url && s.model));
  })
  .catch(() => {});

$("test-btn").addEventListener("click", async () => {
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
  const box = $("save-result");
  $("save-btn").disabled = true;
  try {
    const { ok, data } = await postJSON("/api/llm/settings", collect());
    if (!ok) {
      showMsg(box, false, `✗ 保存失败：${data.detail || "未知错误"}`);
      return;
    }
    setLlmState(Boolean(data.enabled));
    showMsg(box, true, data.enabled
      ? "✓ 已保存，AI 反馈已启用"
      : "✓ 已保存（base_url/model 为空，AI 反馈保持关闭）");
  } catch (e) {
    showMsg(box, false, `✗ 请求失败: ${e.message}`);
  } finally {
    $("save-btn").disabled = false;
  }
});
