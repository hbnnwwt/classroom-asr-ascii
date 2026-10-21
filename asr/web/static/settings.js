// 设置页：LLM 配置回填 / 测试连接 / 保存（写回 config.yaml llm 节）
const $ = (id) => document.getElementById(id);

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

// 回填当前值（未配置 → 空表单）
fetch("/api/llm/settings")
  .then((r) => r.json())
  .then((s) => {
    $("base-url").value = s.base_url || "";
    $("api-key").value = s.api_key || "";
    $("model").value = s.model || "";
    $("timeout").value = s.timeout || 300;
    $("max-tokens").value = s.max_tokens || 16384;
  })
  .catch(() => {});

$("test-btn").addEventListener("click", async () => {
  const box = $("test-result");
  $("test-btn").disabled = true;
  box.hidden = false;
  box.textContent = "测试中…";
  try {
    const { data } = await postJSON("/api/llm/test", collect());
    box.textContent = data.ok
      ? `✓ 连接成功：${data.reply || "ok"}`
      : `✗ 连接失败：${data.error || "未知错误"}`;
  } catch (e) {
    box.textContent = `✗ 请求失败: ${e.message}`;
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
      box.hidden = false;
      box.textContent = `✗ 保存失败：${data.detail || "未知错误"}`;
      return;
    }
    box.hidden = false;
    box.textContent = data.enabled
      ? "✓ 已保存，AI 反馈已启用"
      : "✓ 已保存（base_url/model 为空，AI 反馈保持关闭）";
  } catch (e) {
    box.hidden = false;
    box.textContent = `✗ 请求失败: ${e.message}`;
  } finally {
    $("save-btn").disabled = false;
  }
});
