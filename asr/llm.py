"""OpenAI 兼容 LLM 客户端——云 API（GLM/DeepSeek…）与本地 Ollama 一套代码。

不引入 SDK：一个 POST 的事，requests 足够。
"""
from __future__ import annotations

import requests

from .config import AppConfig


class LLMError(Exception):
    """LLM 调用失败（未配置/网络/HTTP/响应结构/空内容）。"""


def llm_enabled(cfg: AppConfig) -> bool:
    """llm 节可用 = base_url 与 model 都非空。"""
    return bool(cfg.llm.base_url.strip() and cfg.llm.model.strip())


def chat(
    messages: list[dict],
    cfg: AppConfig,
    *,
    temperature: float = 0.3,
    max_tokens: int | None = None,
) -> str:
    """调 /chat/completions，返回首条回复文本。任何失败抛 LLMError。"""
    if not llm_enabled(cfg):
        raise LLMError("LLM 未配置（config.yaml 缺 llm 节或 base_url/model 为空）")

    headers = {"Content-Type": "application/json"}
    if cfg.llm.api_key.strip():
        headers["Authorization"] = f"Bearer {cfg.llm.api_key.strip()}"

    body = {
        "model": cfg.llm.model.strip(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens or cfg.llm.max_tokens,
    }
    url = cfg.llm.base_url.strip().rstrip("/") + "/chat/completions"
    try:
        resp = requests.post(url, json=body, headers=headers,
                             timeout=cfg.llm.timeout)
    except requests.RequestException as e:  # noqa: BLE001
        raise LLMError(f"LLM 请求失败: {type(e).__name__}: {e}") from e

    if resp.status_code != 200:
        raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise LLMError(f"LLM 响应结构异常: {e}") from e
    if not content or not content.strip():
        raise LLMError("LLM 返回空内容")
    return content
