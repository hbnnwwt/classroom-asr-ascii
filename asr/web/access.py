"""Socket-peer access control for pages, APIs, static assets and downloads."""
from __future__ import annotations

import base64
import binascii
import ipaddress
import secrets
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse


def loopback(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
        return address.is_loopback or bool(getattr(address, "ipv4_mapped", None) and address.ipv4_mapped.is_loopback)
    except ValueError:
        return False


def _origin_tuple(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


class AccessMiddleware:
    def __init__(self, app, password):
        self.app, self.password = app, password

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        password = self.password()
        response = None
        if password:
            valid = False
            try:
                scheme, token = headers.get("authorization", "").split(" ", 1)
                decoded = base64.b64decode(token, validate=True).decode("utf-8")
                username, supplied = decoded.split(":", 1)
                valid = (scheme.lower() == "basic" and
                         secrets.compare_digest(username.encode(), b"classroom") and
                         secrets.compare_digest(supplied.encode(), password.encode()))
            except (ValueError, UnicodeError, binascii.Error):
                pass
            if not valid:
                response = JSONResponse({"detail": "需要访问密码", "code": "authentication_required"},
                                        status_code=401, headers={"WWW-Authenticate": 'Basic realm="Classroom ASR"'})
        elif not scope.get("client") or not loopback(scope["client"][0]):
            response = JSONResponse({"detail": "仅允许本机访问", "code": "loopback_required"}, status_code=403)
        elif not password:
            # Peer identifies the connection; Host restricts the browser origin.
            # Checking both prevents a public hostname from rebinding to loopback.
            target = _origin_tuple(f"{scope.get('scheme', 'http')}://{headers.get('host', '')}")
            if target is None or not (target[1].rstrip(".") == "localhost" or loopback(target[1])):
                response = JSONResponse({"detail": "请使用 localhost 或本机回环地址访问",
                                         "code": "local_host_required"}, status_code=403)
        if response is None and scope["method"] not in ("GET", "HEAD", "OPTIONS"):
            origin = headers.get("origin")
            if origin:
                expected = f"{scope.get('scheme', 'http')}://{headers.get('host', '')}"
                if _origin_tuple(origin) is None or _origin_tuple(origin) != _origin_tuple(expected):
                    response = JSONResponse({"detail": "请求来源不匹配", "code": "origin_not_allowed"}, status_code=403)
        if response is not None:
            return await response(scope, receive, send)
        await self.app(scope, receive, send)
