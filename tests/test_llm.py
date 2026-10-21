"""LLM 客户端测试：mock requests.post，不发真请求。"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests

from asr.config import load_config
from asr.llm import LLMError, chat, llm_enabled

CFG = load_config("config.yaml", strict_models=False)
CFG = CFG.model_copy(update={"llm": SimpleNamespace(
    base_url="http://fake:1/v1", api_key="sk-test", model="fake-model",
    timeout=5, max_tokens=512,
)})
# SimpleNamespace 规避 pydantic 校验；chat 只读属性，够用

MSG = [{"role": "user", "content": "hi"}]


def _resp(status=200, body=None):
    r = SimpleNamespace(status_code=status, text='{"raw": 1}')
    r.json = lambda: body if body is not None else {
        "choices": [{"message": {"content": "你好"}}]
    }
    return r


def test_llm_enabled_requires_base_url_and_model():
    assert llm_enabled(CFG) is True
    off = CFG.model_copy(update={"llm": SimpleNamespace(
        base_url="", api_key="", model="", timeout=5, max_tokens=512)})
    assert llm_enabled(off) is False


def test_chat_ok_and_auth_header():
    with patch("asr.llm.requests.post", return_value=_resp()) as p:
        out = chat(MSG, CFG)
    assert out == "你好"
    url, = p.call_args.args
    assert url == "http://fake:1/v1/chat/completions"
    assert p.call_args.kwargs["headers"]["Authorization"] == "Bearer sk-test"
    assert p.call_args.kwargs["json"]["model"] == "fake-model"
    assert p.call_args.kwargs["timeout"] == 5


def test_chat_local_no_auth_header():
    local = CFG.model_copy(update={"llm": SimpleNamespace(
        base_url="http://localhost:11434/v1", api_key="", model="q",
        timeout=5, max_tokens=512)})
    with patch("asr.llm.requests.post", return_value=_resp()) as p:
        chat(MSG, local)
    assert "Authorization" not in p.call_args.kwargs["headers"]


def test_chat_http_error_raises_llmerror():
    bad = _resp(status=500, body={"error": "boom"})
    with patch("asr.llm.requests.post", return_value=bad):
        with pytest.raises(LLMError, match="500"):
            chat(MSG, CFG)


def test_chat_network_error_wrapped():
    with patch("asr.llm.requests.post",
               side_effect=requests.ConnectionError("refused")):
        with pytest.raises(LLMError, match="ConnectionError"):
            chat(MSG, CFG)


def test_chat_bad_structure_raises():
    for body in ({}, {"choices": []}, {"choices": [{"message": {}}]},
                 {"choices": [{"message": {"content": "  "}}]}):
        with patch("asr.llm.requests.post",
                   return_value=_resp(body=body)):
            with pytest.raises(LLMError):
                chat(MSG, CFG)


def test_chat_disabled_config_raises():
    off = CFG.model_copy(update={"llm": SimpleNamespace(
        base_url="", api_key="", model="", timeout=5, max_tokens=512)})
    with pytest.raises(LLMError, match="未配置"):
        chat(MSG, off)
