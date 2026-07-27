from __future__ import annotations

import json
import socket
import time
from collections.abc import Mapping
from http.client import HTTPConnection, HTTPResponse
from typing import Any
from urllib.parse import urlencode, urlparse

from .errors import NodeProtocolError, ProcessUnavailable


class ProductHttpClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 15.0,
        api_version: str = "1.0",
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or not parsed.hostname or not parsed.port:
            raise ValueError("product HTTP base URL requires explicit http host and port")
        self.base_url = base_url.rstrip("/")
        self.host = parsed.hostname
        self.port = parsed.port
        self.timeout_seconds = timeout_seconds
        self.api_version = api_version

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        query: Mapping[str, Any] | None = None,
        accepted_statuses: tuple[int, ...] = (200,),
        timeout_seconds: float | None = None,
        idempotency_key: str = "",
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        target = path if path.startswith("/") else f"/{path}"
        if query:
            target += "?" + urlencode(
                {
                    str(key): str(value)
                    for key, value in query.items()
                    if value is not None
                }
            )
        body = json.dumps(
            dict(payload or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Accept": "application/json",
            "X-Zyra-Api-Version": self.api_version,
            "X-Zyra-Client": "deployment-semantic-health",
            "X-Zyra-Client-Version": "1.0.0",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        connection = HTTPConnection(
            self.host,
            self.port,
            timeout=timeout_seconds or self.timeout_seconds,
        )
        try:
            connection.request(method.upper(), target, body=body, headers=headers)
            response = connection.getresponse()
            response_body = self._read_response(response)
            response_headers = {
                str(key): str(value) for key, value in response.getheaders()
            }
        except (ConnectionError, OSError, TimeoutError, socket.timeout) as error:
            raise ProcessUnavailable(
                "product_http_unreachable",
                "Zyra product endpoint is unavailable",
                operation=f"{method.upper()} {target}",
                retryable=True,
                details={
                    "base_url": self.base_url,
                    "error": f"{type(error).__name__}: {error}",
                },
            ) from error
        finally:
            connection.close()
        try:
            value = json.loads(response_body.decode("utf-8")) if response_body else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise NodeProtocolError(
                "product_http_json_invalid",
                "Zyra product endpoint returned invalid JSON",
                operation=f"{method.upper()} {target}",
                details={"status": response.status},
            ) from error
        if not isinstance(value, dict):
            raise NodeProtocolError(
                "product_http_shape_invalid",
                "Zyra product endpoint returned a non-object JSON value",
                operation=f"{method.upper()} {target}",
                details={"status": response.status},
            )
        if response.status not in accepted_statuses:
            raise NodeProtocolError(
                str(value.get("error") or "product_http_rejected"),
                str(value.get("message") or f"product endpoint returned HTTP {response.status}"),
                operation=f"{method.upper()} {target}",
                retryable=response.status >= 500,
                details={"status": response.status, "response": value},
            )
        return response.status, value, response_headers

    @staticmethod
    def _read_response(response: HTTPResponse) -> bytes:
        raw = response.getheader("Content-Length")
        if raw is None:
            value = response.read(16 * 1024 * 1024 + 1)
        else:
            try:
                length = int(raw)
            except ValueError as error:
                raise NodeProtocolError(
                    "product_content_length_invalid",
                    "product endpoint returned invalid Content-Length",
                    operation="read_response",
                ) from error
            if length < 0 or length > 16 * 1024 * 1024:
                raise NodeProtocolError(
                    "product_response_too_large",
                    "product endpoint response exceeds the semantic health limit",
                    operation="read_response",
                )
            value = response.read(length)
        if len(value) > 16 * 1024 * 1024:
            raise NodeProtocolError(
                "product_response_too_large",
                "product endpoint response exceeds the semantic health limit",
                operation="read_response",
            )
        return value

    def get(
        self,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        accepted_statuses: tuple[int, ...] = (200,),
    ) -> dict[str, Any]:
        return self.request(
            "GET",
            path,
            {},
            query=query,
            accepted_statuses=accepted_statuses,
        )[1]

    def post(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        accepted_statuses: tuple[int, ...] = (200,),
        idempotency_key: str = "",
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            path,
            payload,
            accepted_statuses=accepted_statuses,
            idempotency_key=idempotency_key,
            timeout_seconds=timeout_seconds,
        )[1]

    def wait_ready(
        self,
        *,
        path: str = "/health",
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last_error: BaseException | None = None
        while time.monotonic() < deadline:
            try:
                value = self.get(path)
                if (
                    value.get("ready") is True
                    or value.get("ok") is True
                    or value.get("status") in {"ok", "ready", "degraded"}
                ):
                    return value
            except BaseException as error:
                last_error = error
            time.sleep(0.1)
        raise ProcessUnavailable(
            "product_http_readiness_timeout",
            "Zyra product endpoint did not become ready before the deadline",
            operation=f"wait_ready {path}",
            retryable=True,
            details={
                "base_url": self.base_url,
                "timeout_seconds": timeout_seconds,
                "last_error": (
                    f"{type(last_error).__name__}: {last_error}"
                    if last_error
                    else ""
                ),
            },
        )


__all__ = ["ProductHttpClient"]
