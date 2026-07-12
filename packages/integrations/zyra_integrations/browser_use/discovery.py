from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


class DiscoveryError(RuntimeError):
    def __init__(self, code: str, message: str, *, endpoint: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.endpoint = endpoint
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class RedactedHeaders:
    transport: Mapping[str, str]
    public: Mapping[str, str]

    @classmethod
    def build(cls, headers: Mapping[str, Any] | None) -> "RedactedHeaders":
        transport: dict[str, str] = {}
        public: dict[str, str] = {}
        for raw_name, raw_value in (headers or {}).items():
            name = str(raw_name).strip()
            if not name:
                continue
            value = str(raw_value)
            transport[name] = value
            normalized = name.casefold().replace("-", "_")
            secret = any(
                token in normalized
                for token in ("authorization", "cookie", "token", "secret", "api_key", "proxy_authorization")
            )
            public[name] = "[REDACTED]" if secret else value
        return cls(transport=transport, public=public)


@dataclass(frozen=True, slots=True)
class BrowserEndpoint:
    http_url: str
    websocket_url: str = ""
    headers: RedactedHeaders = field(default_factory=lambda: RedactedHeaders({}, {}))
    proxy_url: str = ""
    verify_tls: bool = True

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlparse(self.http_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("browser endpoint must be an absolute http(s) URL")
        if self.websocket_url:
            ws = urllib.parse.urlparse(self.websocket_url)
            if ws.scheme not in {"ws", "wss"} or not ws.hostname:
                raise ValueError("browser websocket endpoint must be ws(s)")

    @property
    def base_url(self) -> str:
        return self.http_url.rstrip("/")

    def public_dict(self) -> dict[str, Any]:
        return {
            "http_url": self.http_url,
            "websocket_url": _redact_url(self.websocket_url),
            "headers": dict(self.headers.public),
            "proxy_url": _redact_url(self.proxy_url),
            "verify_tls": self.verify_tls,
        }


@dataclass(frozen=True, slots=True)
class BrowserTargetDescriptor:
    target_id: str
    target_type: str
    title: str
    url: str
    websocket_url: str
    devtools_frontend_url: str = ""
    attached: bool = False
    opener_id: str = ""

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "BrowserTargetDescriptor":
        return cls(
            target_id=str(value.get("id") or value.get("targetId") or ""),
            target_type=str(value.get("type") or "other"),
            title=str(value.get("title") or ""),
            url=str(value.get("url") or ""),
            websocket_url=str(value.get("webSocketDebuggerUrl") or ""),
            devtools_frontend_url=str(value.get("devtoolsFrontendUrl") or ""),
            attached=bool(value.get("attached")),
            opener_id=str(value.get("openerId") or ""),
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "target_type": self.target_type,
            "title": self.title,
            "url": self.url,
            "websocket_url": _redact_url(self.websocket_url),
            "attached": self.attached,
            "opener_id": self.opener_id,
        }


@dataclass(frozen=True, slots=True)
class DiscoverySnapshot:
    endpoint: BrowserEndpoint
    browser_name: str
    protocol_version: str
    user_agent: str
    v8_version: str
    webkit_version: str
    websocket_url: str
    targets: tuple[BrowserTargetDescriptor, ...]

    def page_targets(self) -> tuple[BrowserTargetDescriptor, ...]:
        return tuple(target for target in self.targets if target.target_type == "page")

    def public_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint.public_dict(),
            "browser_name": self.browser_name,
            "protocol_version": self.protocol_version,
            "user_agent": self.user_agent,
            "v8_version": self.v8_version,
            "webkit_version": self.webkit_version,
            "websocket_url": _redact_url(self.websocket_url),
            "targets": [target.public_dict() for target in self.targets],
        }


class BrowserEndpointDiscovery:
    """Discovers browser and target websocket endpoints over the CDP HTTP API."""

    def __init__(self, endpoint: BrowserEndpoint, *, timeout_seconds: float = 5.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("discovery timeout must be positive")
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self._opener = self._build_opener(endpoint)

    def discover(self) -> DiscoverySnapshot:
        version = self._request_json("GET", "/json/version")
        if not isinstance(version, Mapping):
            raise DiscoveryError("invalid_version_payload", "CDP /json/version returned a non-object")
        targets_value = self._request_json("GET", "/json/list")
        if not isinstance(targets_value, Sequence) or isinstance(targets_value, (str, bytes, bytearray)):
            raise DiscoveryError("invalid_target_payload", "CDP /json/list returned a non-array")
        targets = tuple(
            BrowserTargetDescriptor.from_json(item)
            for item in targets_value
            if isinstance(item, Mapping)
        )
        websocket_url = str(version.get("webSocketDebuggerUrl") or self.endpoint.websocket_url)
        if not websocket_url:
            raise DiscoveryError("websocket_endpoint_missing", "browser websocket debugger URL is missing")
        return DiscoverySnapshot(
            endpoint=self.endpoint,
            browser_name=str(version.get("Browser") or ""),
            protocol_version=str(version.get("Protocol-Version") or ""),
            user_agent=str(version.get("User-Agent") or ""),
            v8_version=str(version.get("V8-Version") or ""),
            webkit_version=str(version.get("WebKit-Version") or ""),
            websocket_url=websocket_url,
            targets=targets,
        )

    def list_targets(self) -> tuple[BrowserTargetDescriptor, ...]:
        payload = self._request_json("GET", "/json/list")
        if not isinstance(payload, list):
            raise DiscoveryError("invalid_target_payload", "CDP target list is invalid")
        return tuple(BrowserTargetDescriptor.from_json(item) for item in payload if isinstance(item, Mapping))

    def create_blank_target(self) -> BrowserTargetDescriptor:
        errors: list[DiscoveryError] = []
        for method, suffix in (("PUT", "/json/new?about:blank"), ("GET", "/json/new?about:blank")):
            try:
                payload = self._request_json(method, suffix)
            except DiscoveryError as error:
                errors.append(error)
                continue
            if isinstance(payload, Mapping):
                target = BrowserTargetDescriptor.from_json(payload)
                if target.target_id:
                    return target
        reason = errors[-1] if errors else None
        raise DiscoveryError(
            "blank_target_creation_failed",
            str(reason or "browser rejected blank target creation"),
            retryable=bool(reason and reason.retryable),
        )

    def activate_target(self, target_id: str) -> bool:
        if not target_id:
            raise ValueError("target id is required")
        payload = self._request_json("GET", f"/json/activate/{urllib.parse.quote(target_id, safe='')}")
        return payload == "Target activated" or bool(payload)

    def close_target(self, target_id: str) -> bool:
        if not target_id:
            raise ValueError("target id is required")
        payload = self._request_json("GET", f"/json/close/{urllib.parse.quote(target_id, safe='')}")
        return payload == "Target is closing" or bool(payload)

    def _request_json(self, method: str, suffix: str) -> Any:
        url = f"{self.endpoint.base_url}{suffix}"
        request = urllib.request.Request(url=url, method=method, headers=dict(self.endpoint.headers.transport))
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                raw = response.read(8 * 1024 * 1024)
        except urllib.error.HTTPError as error:
            retryable = error.code >= 500 or error.code in {408, 425, 429}
            raise DiscoveryError(
                "http_error",
                f"browser discovery HTTP {error.code}",
                endpoint=_redact_url(url),
                retryable=retryable,
            ) from error
        except urllib.error.URLError as error:
            raise DiscoveryError(
                "connection_error",
                f"browser discovery connection failed: {error.reason}",
                endpoint=_redact_url(url),
                retryable=True,
            ) from error
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DiscoveryError("invalid_encoding", "browser discovery payload is not UTF-8") from error
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text.strip()

    @staticmethod
    def _build_opener(endpoint: BrowserEndpoint) -> urllib.request.OpenerDirector:
        handlers: list[Any] = []
        if endpoint.proxy_url:
            handlers.append(urllib.request.ProxyHandler({"http": endpoint.proxy_url, "https": endpoint.proxy_url}))
        if not endpoint.verify_tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=context))
        return urllib.request.build_opener(*handlers)


def _redact_url(value: str) -> str:
    if not value:
        return ""
    parsed = urllib.parse.urlsplit(value)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{host}{port}"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
