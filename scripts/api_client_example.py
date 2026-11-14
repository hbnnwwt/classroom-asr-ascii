# -*- coding: utf-8 -*-
r"""classroom-asr 外部调用客户端，仅使用 Python 标准库。

在服务端终端启动本机服务。
    .\.venv\Scripts\python.exe -m asr.web --port 8765

在客户端终端传入本机文件路径。
    python scripts/api_client_example.py E:\media\lecture.mp4 --local -o lecture.srt

文件只在客户端时使用 HTTP 上传。服务端启用了访问密码时，在客户端终端
设置 ASR_WEB_PASSWORD，或使用 --secret。
    python scripts/api_client_example.py E:\media\lecture.mp4 --base http://10.0.0.9:8765

也可在 Python 进程内调用转写管线。
    from pathlib import Path
    from asr.config import load_config
    from asr.pipeline import run
    result = run(Path(r"E:\media\lecture.mp4"), load_config("config.yaml"),
                 {"quality": "balanced", "formats": ["srt"]})
    print(result.written["srt"])
"""
from __future__ import annotations

import argparse
import base64
import http.client
import ipaddress
import json
import math
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterable
from typing import BinaryIO


_TRANSPORT_ERRORS = (urllib.error.URLError, ConnectionError, TimeoutError, http.client.HTTPException)
_SESSION_ERRORS = (*_TRANSPORT_ERRORS, ValueError)


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AsrClient:
    """classroom-asr 客户端。secret 为空时按服务端"仅本机"模式访问。"""

    def __init__(self, base_url: str = "http://127.0.0.1:8765", secret: str = "",
                 username: str = "classroom", timeout: float = 7200.0):
        self.base = base_url.rstrip("/")
        self.secret = secret
        self.username = username
        self.timeout = timeout
        host = urllib.parse.urlsplit(self.base).hostname or ""
        try:
            address = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            loopback = host.rstrip(".") == "localhost"
        else:
            mapped = getattr(address, "ipv4_mapped", None)
            loopback = address.is_loopback or (mapped is not None and mapped.is_loopback)

        if loopback:
            self._opener = urllib.request.build_opener(_RejectRedirects, urllib.request.ProxyHandler({}))
        else:
            self._opener = urllib.request.build_opener(_RejectRedirects)

    # ------------------------------------------------------------------ HTTP
    def _auth(self) -> dict:
        if not self.secret:
            return {}
        raw = f"{self.username}:{self.secret}".encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}

    def _call(self, method: str, path: str, *, data: bytes | Iterable[bytes] | None = None,
              headers: dict | None = None, timeout: float | None = None) -> tuple[int, bytes]:
        hdr = dict(self._auth())
        if headers:
            hdr.update(headers)
        req = urllib.request.Request(self.base + path, data=data, headers=hdr, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout if timeout is None else timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read()

    def _form(self, fields: dict) -> tuple[bytes, str]:
        return (urllib.parse.urlencode(fields).encode("utf-8"),
                "application/x-www-form-urlencoded")

    def _multipart(self, fields: dict, file_path: str, media: BinaryIO) -> tuple[Iterable[bytes], dict]:
        boundary = "----asr" + uuid.uuid4().hex
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
                 for key, value in fields.items()]
        name = os.path.basename(file_path)
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        name = name.replace("\r", "%0D").replace("\n", "%0A").replace('"', "%22")
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
                     f'filename="{name}"\r\nContent-Type: {ctype}\r\n\r\n')
        prefix = "".join(parts).encode("utf-8")
        suffix = f"\r\n--{boundary}--\r\n".encode()
        size = os.fstat(media.fileno()).st_size

        def chunks():
            yield prefix
            remaining = size
            while remaining:
                chunk = media.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise EOFError("上传期间输入文件缩短")
                yield chunk
                remaining -= len(chunk)
            yield suffix

        return chunks(), {"Content-Type": f"multipart/form-data; boundary={boundary}",
                          "Content-Length": str(len(prefix) + size + len(suffix))}

    # -------------------------------------------------------------- 健康检查
    def health(self) -> dict:
        status, body = self._call("GET", "/health")
        if status in (200, 503):
            try:
                return {**json.loads(body), "http": status}
            except (ValueError, TypeError):
                pass
        return {"http": status, "raw": body[:200]}

    # ---------------------------------------------------- A. 同机：本地路径
    def _request_local(self, file_path: str, *, sync: bool, fields: dict) -> tuple[int, dict]:
        fields = {**fields, "input_path": os.path.abspath(os.path.expanduser(file_path)),
                  "sync": str(sync).lower()}
        body, ctype = self._form(fields)
        status, raw = self._call("POST", "/api/transcribe", data=body,
                                 headers={"Content-Type": ctype})
        if status not in (200, 202):
            raise RuntimeError(f"转写失败 {status}: {raw[:300]}")
        return status, json.loads(raw)

    def transcribe_local(self, file_path: str, *, fmts: str = "srt",
                         wait_timeout: float | None = None, **opts) -> dict:
        """返回字幕文本字典。同步等待超时后继续查询同一任务。"""
        status, result = self._request_local(file_path, sync=True, fields={**opts, "fmts": fmts})
        if status == 200:
            return result

        task_id = result["task_id"]
        info = self.wait(task_id, timeout=wait_timeout, verbose=False)
        meta = info.get("meta") or {}
        available = info.get("available_formats") or []
        formats, errors = {}, {}
        for fmt in dict.fromkeys(token.strip() for token in fmts.split(",") if token.strip()):
            if fmt not in available:
                errors[fmt] = "该格式的产物不存在"
                continue
            encoding = (meta.get("srt_encoding") or "utf-8-sig") if fmt == "srt" else "utf-8-sig"
            try:
                text = self.download(task_id, fmt).decode(encoding)
            except (UnicodeDecodeError, LookupError):
                errors[fmt] = "读取或解码产物失败"
            else:
                formats[fmt] = text.removeprefix("\ufeff")

        return {"task_id": task_id, "status": "done", "name": info.get("name") or "",
                "segments": meta.get("segments_count"), "duration_s": meta.get("duration_s"),
                "formats": formats, "available_formats": available, "format_errors": errors}

    # ------------------------------------------------------- B. 远程：上传流
    def create_upload_session(self) -> str:
        status, body = self._call("POST", "/api/uploads")
        if status not in (200, 201):
            raise RuntimeError(f"建上传会话失败 {status}: {body[:200]}")
        return json.loads(body)["upload_id"]

    def submit(self, file_path: str, *, name: str = "", quality: str = "balanced",
               diarize: bool = False, ai_feedback: bool = False,
               config: str = "config.yaml", use_session: bool = True) -> str:
        fields = {"name": name or os.path.splitext(os.path.basename(file_path))[0],
                  "config": config, "diarize": str(diarize).lower(),
                  "quality": quality, "ai_feedback": str(ai_feedback).lower()}
        with open(file_path, "rb") as media:
            data, headers = self._multipart(fields, file_path, media)
            upload_id = self.create_upload_session() if use_session else None
            if upload_id:
                headers["X-Upload-Id"] = upload_id
            try:
                status, body = self._call("POST", "/api/projects", data=data, headers=headers)
            except _TRANSPORT_ERRORS as exc:
                if not upload_id:
                    raise
                try:
                    status, body = self._call("GET", f"/api/uploads/{upload_id}")
                    session = json.loads(body) if status == 200 else {}
                    if session.get("status") == "accepted" and session.get("task_id"):
                        return session["task_id"]
                except _SESSION_ERRORS:
                    pass
                raise RuntimeError(f"上传结果未确认，上传凭证 {upload_id}") from exc
        if status >= 300:
            raise RuntimeError(f"提交失败 {status}: {body[:300]}")
        return json.loads(body)["task_id"]

    def wait(self, task_id: str, *, poll: float = 2.0, verbose: bool = True,
             timeout: float | None = None) -> dict:
        budget = self.timeout if timeout is None else timeout
        if not math.isfinite(budget) or budget <= 0:
            raise ValueError("轮询时限必须是有限正数")
        if not math.isfinite(poll) or poll < 0:
            raise ValueError("轮询间隔必须是有限非负数")
        t0, last = time.monotonic(), None
        deadline = t0 + budget
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                status, body = self._call("GET", f"/status/{task_id}", timeout=min(self.timeout, remaining))
            except urllib.error.URLError as exc:
                if not isinstance(exc.reason, TimeoutError):
                    raise
                break
            except TimeoutError:
                break
            if time.monotonic() >= deadline:
                break
            if status != 200:
                raise RuntimeError(f"查询状态失败 {status}: {body[:200]}")
            info = json.loads(body)
            st = info.get("transcription_status") or info["status"]
            mark = (st, info.get("stage"), round(float(info.get("progress") or 0) * 100))
            if verbose and mark != last:
                print(f"  [{time.monotonic()-t0:6.1f}s] {st:<8} {str(info.get('stage')):<12} "
                      f"{mark[2]:>3}%  {str(info.get('message') or '')[:50]}", file=sys.stderr)
                last = mark
            if st == "done":
                return info
            if st == "failed":
                raise RuntimeError(f"转写失败: {info.get('error')}")
            time.sleep(max(0, min(poll, deadline - time.monotonic())))
        raise TimeoutError(f"等待转写超时，任务 {task_id}")

    def download(self, task_id: str, fmt: str = "srt") -> bytes:
        status, body = self._call("GET", f"/download/{task_id}/{fmt}")
        if status != 200:
            raise RuntimeError(f"下载 {fmt} 失败 {status}: {body[:200]}")
        return body

    # ------------------------------------------------------------ 统一入口
    def transcribe(self, file_path: str, out: str | None = None, *, fmt: str = "srt",
                   local: bool = False, wait_timeout: float | None = None, **opts) -> bytes:
        """转写并下载原始字幕文件。local=True 时服务端直接读取本机路径。"""
        if local:
            _, accepted = self._request_local(file_path, sync=False, fields=opts)
            task_id = accepted["task_id"]
        else:
            task_id = self.submit(file_path, **opts)
        info = self.wait(task_id, timeout=wait_timeout, verbose=not local)
        if fmt not in info.get("available_formats", ()):
            raise RuntimeError(f"任务 {task_id} 未生成 {fmt} 格式的字幕")
        raw = self.download(task_id, fmt)
        if out:
            with open(out, "wb") as f:
                f.write(raw)
        return raw


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="classroom-asr 外部调用示例")
    ap.add_argument("input", help="视频或音频文件路径")
    ap.add_argument("--out", "-o", default=None, help="输出字幕路径（默认同名）")
    ap.add_argument("--fmt", default="srt", choices=["srt", "vtt", "txt", "md", "json"])
    ap.add_argument("--base", default=os.environ.get("ASR_WEB_URL", "http://127.0.0.1:8765"))
    ap.add_argument("--secret", default=os.environ.get("ASR_WEB_PASSWORD", ""),
                    help="访问密码（HTTP Basic）")
    ap.add_argument("--wait-timeout", type=float, default=None,
                    help="状态轮询总时限（秒，默认 7200）")
    ap.add_argument("--quality", default="balanced", choices=["fast", "balanced", "high"])
    ap.add_argument("--diarize", action="store_true", help="区分说话人（实验性）")
    ap.add_argument("--ai", action="store_true", help="生成 AI 整理稿（需服务端配置 LLM）")
    ap.add_argument("--local", action="store_true",
                    help="服务端与文件同机：直接给路径，不复制文件（推荐）")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.input):
        print(f"文件不存在: {args.input}", file=sys.stderr)
        return 2

    out = args.out or os.path.splitext(args.input)[0] + "." + args.fmt
    c = AsrClient(args.base, args.secret)
    mode = "本地路径" if args.local else "HTTP 上传"
    print(f"提交（{mode}）: {args.input}", file=sys.stderr)
    raw = c.transcribe(args.input, out, fmt=args.fmt, local=args.local,
                       quality=args.quality, diarize=args.diarize,
                       ai_feedback=args.ai, wait_timeout=args.wait_timeout)
    print(f"完成: {out} ({len(raw)} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
