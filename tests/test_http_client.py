"""Helper HTTP client cho kiểm thử máy chủ Web khách, sử dụng hoàn toàn thư viện chuẩn."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class SimpleHttpResponse:
    def __init__(self, status_code: int, headers: dict[str, str], body: bytes) -> None:
        self.status_code = status_code
        self.headers = headers
        self.content = body
        self.text = body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


class SimpleHttpClient:
    def __init__(self, base_url: str = "", cookies: dict[str, str] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookies = dict(cookies or {})

    async def __aenter__(self) -> SimpleHttpClient:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass

    async def get(
        self,
        url_or_path: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> SimpleHttpResponse:
        return await self._req("GET", url_or_path, params=params, headers=headers)

    async def post(
        self,
        url_or_path: str,
        params: dict[str, str] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> SimpleHttpResponse:
        return await self._req("POST", url_or_path, params=params, content=content, headers=headers)

    async def _req(
        self,
        method: str,
        url_or_path: str,
        params: dict[str, str] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> SimpleHttpResponse:
        if url_or_path.startswith("http://") or url_or_path.startswith("https://"):
            full_url = url_or_path
        else:
            full_url = f"{self.base_url}{url_or_path}"

        if params:
            sep = "&" if "?" in full_url else "?"
            full_url += sep + urllib.parse.urlencode(params)

        def _do() -> SimpleHttpResponse:
            req = urllib.request.Request(full_url, data=content, method=method)
            req_headers = dict(headers or {})
            if self.cookies:
                req_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
            for k, v in req_headers.items():
                req.add_header(k, v)
            try:
                with urllib.request.urlopen(req) as resp:
                    h = {k: v for k, v in resp.headers.items()}
                    return SimpleHttpResponse(resp.status, h, resp.read())
            except urllib.error.HTTPError as err:
                h = {k: v for k, v in err.headers.items()}
                return SimpleHttpResponse(err.code, h, err.read())

        return await asyncio.to_thread(_do)
