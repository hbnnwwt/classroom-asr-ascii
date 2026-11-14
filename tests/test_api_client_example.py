"""Exercise the example client's HTTP contract over a loopback connection."""
import builtins
import json
import socket
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from scripts import api_client_example as example


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    routes, requests = {}, []

    class Handler(BaseHTTPRequestHandler):
        def dispatch(self):
            request = (self.command, self.path)
            length = int(self.headers.get("Content-Length", "0"))
            requests.append((request, dict(self.headers.items()), self.rfile.read(length)))
            response = routes.get(request, (404, {"detail": "unexpected request"}))
            if response is None:
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            code, body, *extra_headers = response
            payload = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            for name, value in (extra_headers[0] if extra_headers else {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_POST = do_GET = dispatch

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield example.AsrClient(f"http://127.0.0.1:{server.server_port}", timeout=3), routes, requests
        finally:
            server.shutdown()
            thread.join()


def test_sync_timeout_continues_same_task_and_keeps_original_srt_bytes(api, tmp_path):
    client, routes, requests = api
    text = "1\r\n00:00:00,000 --> 00:00:01,000\r\n课堂\r\n"
    subtitle = text.encode("utf-8-sig")
    routes["POST", "/api/transcribe"] = (202, {"status": "accepted", "task_id": "a1"})
    routes["GET", "/status/a1"] = (200, {"status": "running", "stage": "ai", "progress": 0.5,
                                          "transcription_status": "done", "available_formats": ["srt"],
                                          "name": "课程", "meta": {"segments_count": 1, "duration_s": 1}})
    routes["GET", "/download/a1/srt"] = (200, subtitle)
    out = tmp_path / "lesson.srt"

    assert client.transcribe("lesson.wav", out, local=True, wait_timeout=1) == subtitle
    assert out.read_bytes() == subtitle
    assert [request for request, _, _ in requests] == [
        ("POST", "/api/transcribe"), ("GET", "/status/a1"), ("GET", "/download/a1/srt")
    ]
    assert parse_qs(requests[0][2].decode())["sync"] == ["false"]

    result = client.transcribe_local("lesson.wav", wait_timeout=1)
    assert result["task_id"] == "a1"
    assert result["segments"] == 1
    assert result["formats"] == {"srt": text}
    assert parse_qs(requests[3][2].decode())["sync"] == ["true"]


def test_text_fallback_uses_reported_srt_encoding(api):
    client, routes, _ = api
    routes["POST", "/api/transcribe"] = (202, {"task_id": "a1", "status": "accepted"})
    routes["GET", "/status/a1"] = (200, {"status": "done", "available_formats": ["srt"],
                                          "meta": {"srt_encoding": "gb18030"}})
    routes["GET", "/download/a1/srt"] = (200, "课堂\r\n".encode("gb18030"))

    result = client.transcribe_local("lesson.wav", fmts="srt,vtt")

    assert result["formats"] == {"srt": "课堂\r\n"}
    assert set(result["format_errors"]) == {"vtt"}


def test_missing_local_format_preserves_existing_output(api, tmp_path):
    client, routes, _ = api
    routes["POST", "/api/transcribe"] = (202, {"task_id": "a1", "status": "accepted"})
    routes["GET", "/status/a1"] = (200, {"status": "done", "available_formats": ["txt"]})
    out = tmp_path / "lesson.srt"
    out.write_bytes(b"existing content")

    with pytest.raises(RuntimeError):
        client.transcribe("lesson.wav", out, local=True)

    assert out.read_bytes() == b"existing content"


def test_wait_has_a_monotonic_total_budget(api, monkeypatch):
    client, routes, requests = api
    routes["GET", "/status/a1"] = (200, {"status": "pending", "transcription_status": "pending"})
    current = [100.0]

    def sleep(seconds):
        current[0] += seconds

    monkeypatch.setattr(example, "time", SimpleNamespace(monotonic=lambda: current[0], sleep=sleep))
    with pytest.raises(TimeoutError, match="a1"):
        client.wait("a1", timeout=2.5, poll=2, verbose=False)

    assert current[0] == 102.5
    assert len(requests) == 2


def test_health_decodes_unavailable_json(api):
    client, routes, _ = api
    routes["GET", "/health"] = (503, {"status": "degraded", "checks": {"db": False}})

    assert client.health() == {"http": 503, "status": "degraded", "checks": {"db": False}}


def test_api_redirect_is_returned_to_the_caller_without_following(api):
    client, routes, requests = api
    client.secret = "test-credential"
    routes["GET", "/health"] = (302, b"", {"Location": "/other"})
    routes["GET", "/other"] = (200, {"status": "ok"})

    assert client.health()["http"] == 302
    assert [request for request, _, _ in requests] == [("GET", "/health")]


def test_loopback_api_bypasses_environment_proxy(api, monkeypatch):
    client, routes, requests = api
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("http_proxy", client.base)
    routes["GET", "/health"] = (200, {"status": "ok"})

    direct = example.AsrClient(client.base, timeout=3)

    assert direct.health()["http"] == 200
    assert [request for request, _, _ in requests] == [("GET", "/health")]


def test_submit_streams_a_valid_multipart_body(api, monkeypatch, tmp_path):
    client, routes, requests = api
    routes["POST", "/api/uploads"] = (201, {"upload_id": "u1", "status": "open", "task_id": None})
    routes["POST", "/api/projects"] = (200, {"task_id": "a1", "filename": "lecture.mp4", "name": "课程"})
    media = tmp_path / "lecture.mp4"
    content = bytes(range(256)) * 8192
    media.write_bytes(content)
    original_open = builtins.open
    reads = []

    def open_media(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        if path != str(media) or mode != "rb":
            return stream

        class CheckedStream:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                stream.close()

            def fileno(self):
                return stream.fileno()

            def read(self, size=-1):
                assert 0 < size < len(content)
                reads.append(size)
                return stream.read(size)

        return CheckedStream()

    monkeypatch.setattr(builtins, "open", open_media)

    assert client.submit(str(media), name="课程") == "a1"
    assert len(reads) > 1
    request, headers, body = requests[-1]
    assert request == ("POST", "/api/projects")
    headers = {k.lower(): v for k, v in headers.items()}
    assert headers["x-upload-id"] == "u1"
    assert int(headers["content-length"]) == len(body)
    assert "transfer-encoding" not in headers
    content_type = headers["content-type"].encode()
    multipart = BytesParser(policy=policy.default).parsebytes(b"Content-Type: " + content_type + b"\r\n\r\n" + body)
    fields = {part.get_param("name", header="content-disposition"): part for part in multipart.iter_parts()}
    assert fields["name"].get_payload(decode=True).decode() == "课程"
    assert fields["files"].get_filename() == "lecture.mp4"
    assert fields["files"].get_payload(decode=True) == content


def test_disconnected_upload_looks_up_the_original_session(api, tmp_path):
    client, routes, requests = api
    routes["POST", "/api/uploads"] = (201, {"upload_id": "u1", "status": "open", "task_id": None})
    routes["POST", "/api/projects"] = None
    routes["GET", "/api/uploads/u1"] = (200, {"upload_id": "u1", "status": "accepted", "task_id": "a1"})
    media = tmp_path / "lecture.mp4"
    media.write_bytes(b"media")

    assert client.submit(str(media)) == "a1"
    assert [request for request, _, _ in requests] == [
        ("POST", "/api/uploads"), ("POST", "/api/projects"), ("GET", "/api/uploads/u1")
    ]
