"""A small client for the Neermattam gateway's public and developer APIs.

Every JSON error the gateway sends is {"error": "...", "code": "..."}
(docs/CONTRACTS.md section 3). This module turns those into GatewayError so
the tools can explain them in plain words.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_URL = "https://applemacbook6sep2004--neermattam-web-web-serve.us-east.modal.run"

# The wording the gateway puts on every report answer (report.Advisory in
# gateway/internal/report/pipeline.go). Every tool answer carries it.
ADVISORY = "Advisory only. Check local news and official alerts before you travel."


class GatewayError(Exception):
    """An answer from the gateway that is not a success."""

    def __init__(self, status: int, code: str, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after

    def __str__(self) -> str:
        text = f"{self.message} (HTTP {self.status}, code {self.code})"
        if self.retry_after:
            text += f" Retry after {self.retry_after} s."
        return text


@dataclass
class Answer:
    status: int
    body: Any
    headers: httpx.Headers


class Gateway:
    """Calls one Neermattam gateway. The API key, when set, is sent only to
    the /api/v1 endpoints."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
    ):
        self.base_url = (base_url or DEFAULT_URL).rstrip("/")
        self.api_key = api_key or None
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            transport=transport,
            timeout=timeout,
            headers={"User-Agent": "neermattam-mcp/0.1", "Accept": "application/json"},
            follow_redirects=False,
        )

    @classmethod
    def from_env(cls) -> "Gateway":
        return cls(
            base_url=os.environ.get("NEERMATTAM_URL") or DEFAULT_URL,
            api_key=os.environ.get("NEERMATTAM_API_KEY") or None,
        )

    @property
    def keyed(self) -> bool:
        return self.api_key is not None

    def url(self, path: str) -> str:
        return self.base_url + path

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, path: str, params: dict[str, Any] | None = None, keyed: bool = False, timeout: float | None = None) -> Answer:
        headers = {}
        if keyed:
            if not self.api_key:
                raise GatewayError(401, "missing_key", "This endpoint needs a developer API key. Set NEERMATTAM_API_KEY.")
            headers["Authorization"] = f"Bearer {self.api_key}"
        kwargs: dict[str, Any] = {"params": params, "headers": headers}
        if timeout is not None:
            kwargs["timeout"] = timeout
        return await self._send("GET", path, **kwargs)

    async def post_report(self, files: dict[str, tuple[str, bytes, str]], data: dict[str, str], timeout: float = 270.0) -> Answer:
        return await self._send("POST", "/api/report", files=files, data=data, timeout=timeout)

    async def _send(self, method: str, path: str, **kwargs: Any) -> Answer:
        try:
            r = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as e:
            raise GatewayError(504, "timeout", "The Neermattam gateway did not answer in time.") from e
        except httpx.HTTPError as e:
            raise GatewayError(503, "unreachable", f"The Neermattam gateway could not be reached: {e.__class__.__name__}.") from e
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code >= 400:
            msg, code = "The gateway refused the request.", "http_error"
            retry = None
            if isinstance(body, dict):
                msg = str(body.get("error") or body.get("detail") or msg)
                code = str(body.get("code") or code)
                if isinstance(body.get("retry_after_seconds"), int):
                    retry = body["retry_after_seconds"]
            ra = r.headers.get("Retry-After")
            if retry is None and ra and ra.isdigit():
                retry = int(ra)
            raise GatewayError(r.status_code, code, msg, retry)
        return Answer(r.status_code, body, r.headers)
