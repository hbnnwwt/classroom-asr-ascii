"""Access decisions use the socket peer and protect every HTTP resource."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def secured(password="", peer="127.0.0.1", base_url="http://127.0.0.1"):
    from asr.web.access import AccessMiddleware
    app = FastAPI()
    app.add_middleware(AccessMiddleware, password=lambda: password)
    @app.api_route("/{path:path}", methods=["GET", "POST"])
    def any_path(path):
        return {"ok": True}
    return TestClient(app, client=(peer, 12345), base_url=base_url)


def test_remote_cannot_spoof_loopback_using_headers():
    with secured(peer="192.168.1.20") as client:
        response = client.get("/", headers={"Host": "127.0.0.1", "X-Forwarded-For": "127.0.0.1"})
        assert response.status_code == 403


@pytest.mark.parametrize("path", ["/", "/static/style.css", "/download/a/txt", "/api/projects"])
def test_basic_auth_protects_all_resources(path):
    with secured("secret", "192.168.1.20") as client:
        denied = client.get(path)
        assert denied.status_code == 401 and denied.headers["www-authenticate"].startswith("Basic")
        assert client.get(path, auth=("classroom", "wrong")).status_code == 401
        assert client.get(path, auth=("classroom", "secret")).status_code == 200


def test_mutations_reject_foreign_origin_but_allow_scripts_and_same_origin():
    with secured() as client:
        assert client.post("/api/projects", headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.post("/api/projects", headers={"Origin": "http://127.0.0.1"}).status_code == 200
        assert client.post("/api/projects").status_code == 200


@pytest.mark.parametrize("origin", ["http://rebind.attacker.example", "http://localhost.attacker.example"])
def test_loopback_without_password_rejects_rebinding_host(origin):
    with secured(base_url=origin) as client:
        response = client.post("/api/llm/test", headers={"Origin": origin})
        assert response.status_code == 403
        assert response.json()["code"] == "local_host_required"


@pytest.mark.parametrize("origin", ["http://localhost:8000", "http://127.0.0.1:8765", "http://[::1]:8000"])
def test_local_hostnames_and_literal_loopback_are_supported(origin):
    from urllib.parse import urlsplit
    # This Starlette TestClient transport cannot parse an IPv6 base_url;
    # Host still exercises the real ASGI middleware's bracket/port parsing.
    with secured() as client:
        assert client.post("/api/projects", headers={"Origin": origin, "Host": urlsplit(origin).netloc}).status_code == 200


def test_password_keeps_authenticated_lan_hostname_support():
    with secured(password="secret", peer="192.168.1.20", base_url="http://classroom.lan:8765") as client:
        assert client.post("/api/projects", headers={"Origin": "http://classroom.lan:8765"},
                           auth=("classroom", "secret")).status_code == 200


def test_runtime_rejects_http_config_paths(tmp_path):
    from asr.web.runtime import WebRuntime
    runtime = WebRuntime(config_path=tmp_path / "fixed.yaml", db_path=tmp_path / "db",
                         upload_dir=tmp_path / "uploads", output_root=tmp_path / "outputs")
    for value in (None, "default", "config.yaml"):
        assert runtime.resolve_config(value) == (tmp_path / "fixed.yaml").resolve()
    with pytest.raises(ValueError, match="config_not_allowed"):
        runtime.resolve_config(str(tmp_path / "other.yaml"))


def test_static_and_registered_download_paths_cannot_escape(tmp_path, monkeypatch):
    import asr.web as web
    static = tmp_path / "static"
    static.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("private", encoding="utf-8")
    monkeypatch.setattr(web, "STATIC_DIR", static)
    with pytest.raises(Exception) as exc:
        web.static_file("../private.txt")
    assert exc.value.status_code == 404


def test_settings_http_config_path_is_refused_and_key_masked(tmp_path, monkeypatch, template_config_path):
    import asr.web as web
    from asr.web.runtime import WebRuntime
    config = tmp_path / "config.yaml"
    config.write_text(template_config_path.read_text(encoding="utf-8") + '\nllm:\n  base_url: "http://fake/v1"\n  model: fake\n  api_key: secret-key\n', encoding="utf-8")
    runtime = WebRuntime(config, tmp_path / "db", tmp_path / "up", tmp_path / "out")
    monkeypatch.setattr(web.app.state, "runtime", runtime, raising=False)
    with TestClient(web.app, client=("127.0.0.1", 12345), base_url="http://127.0.0.1") as client:
        denied = client.get("/api/llm/settings", params={"config": str(config)})
        assert denied.status_code == 400
        assert denied.json()["detail"]["code"] == "config_not_allowed"
        response = client.get("/api/llm/settings")
        assert response.json()["api_key"] is None
        assert response.json()["api_key_set"] is True
        assert "secret-key" not in response.text


def test_settings_key_three_states_and_test_never_persists(web_runtime, monkeypatch):
    import asr.web as web
    from asr.config import load_config
    from asr.web.runtime import WebRuntime
    runtime = WebRuntime(web_runtime.config_path, web_runtime.db_path, web_runtime.upload_dir, web_runtime.output_root)
    monkeypatch.setattr(web.app.state, "runtime", runtime, raising=False)
    body = {"base_url": "http://fake/v1", "model": "fake", "api_key": "first"}
    keys_used = []
    def fake_chat(messages, cfg, **kwargs):
        keys_used.append(cfg.llm.api_key)
        return "ok"
    monkeypatch.setattr(web, "chat", fake_chat)
    with TestClient(web.app, client=("127.0.0.1", 12345), base_url="http://127.0.0.1") as client:
        assert client.post("/api/llm/settings", json=body).status_code == 200
        for key, expected in ((None, "first"), ("replacement", "replacement"), ("", "")):
            before = runtime.config_path.read_bytes()
            assert client.post("/api/llm/test", json={**body, "api_key": key}).json()["ok"]
            assert keys_used[-1] == expected
            assert runtime.config_path.read_bytes() == before
            assert client.post("/api/llm/settings", json={**body, "api_key": key}).status_code == 200
            assert load_config(runtime.config_path, strict_models=False).llm.api_key == expected
        client.post("/api/llm/settings", json=body)
        body.pop("api_key")
        client.post("/api/llm/settings", json=body)
        assert load_config(runtime.config_path, strict_models=False).llm.api_key == "first"
