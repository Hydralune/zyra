from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urlsplit, urlunsplit

from .errors import BrowserConfigurationError
from .models import BrowserRuntimeConfig, BrowserSessionCommand, stable_digest


SECRET_HEADER_TOKENS = (
    "authorization",
    "cookie",
    "token",
    "secret",
    "password",
    "api-key",
    "api_key",
    "proxy-authorization",
)

SENSITIVE_QUERY_TOKENS = (
    "token",
    "secret",
    "key",
    "password",
    "signature",
    "credential",
)


@dataclass(frozen=True, slots=True)
class NormalizedHeaders:
    transport: Mapping[str, str]
    public: Mapping[str, str]
    names: tuple[str, ...]
    fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "public": dict(self.public),
            "names": list(self.names),
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class NormalizedProxy:
    transport_url: str
    public_url: str
    scheme: str
    host: str
    port: int | None
    authenticated: bool
    fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "public_url": self.public_url,
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "authenticated": self.authenticated,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class BrowserTimeoutPolicy:
    request_seconds: float
    connect_seconds: float
    navigation_seconds: float
    action_seconds: float
    shutdown_seconds: float

    def __post_init__(self) -> None:
        values = (
            self.request_seconds,
            self.connect_seconds,
            self.navigation_seconds,
            self.action_seconds,
            self.shutdown_seconds,
        )
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise BrowserConfigurationError("browser timeout values must be finite and positive")
        if self.request_seconds >= self.action_seconds:
            raise BrowserConfigurationError("CDP request timeout must be below action timeout")
        if self.action_seconds > self.navigation_seconds * 2:
            raise BrowserConfigurationError("action timeout is inconsistent with navigation timeout")

    def to_dict(self) -> dict[str, float]:
        return {
            "request_seconds": self.request_seconds,
            "connect_seconds": self.connect_seconds,
            "navigation_seconds": self.navigation_seconds,
            "action_seconds": self.action_seconds,
            "shutdown_seconds": self.shutdown_seconds,
        }


@dataclass(frozen=True, slots=True)
class BrowserLaunchArguments:
    executable: Path | None
    arguments: tuple[str, ...]
    environment: Mapping[str, str]
    headless: bool
    user_data_dir: Path
    downloads_dir: Path

    def public_dict(self) -> dict[str, Any]:
        return {
            "executable": str(self.executable or ""),
            "arguments": list(self.arguments),
            "environment_names": sorted(self.environment),
            "headless": self.headless,
            "user_data_dir": str(self.user_data_dir),
            "downloads_dir": str(self.downloads_dir),
        }


@dataclass(frozen=True, slots=True)
class BrowserConnectionPolicy:
    endpoint_http_url: str
    endpoint_websocket_url: str
    headers: NormalizedHeaders
    proxy: NormalizedProxy | None
    timeouts: BrowserTimeoutPolicy
    launch: BrowserLaunchArguments
    verify_tls: bool
    local: bool
    fingerprint: str
    warnings: tuple[str, ...] = ()

    @property
    def endpoint_url(self) -> str:
        return self.endpoint_http_url

    def public_dict(self) -> dict[str, Any]:
        return {
            "endpoint_http_url": redact_url(self.endpoint_http_url),
            "endpoint_websocket_url": redact_url(self.endpoint_websocket_url),
            "headers": self.headers.to_dict(),
            "proxy": self.proxy.to_dict() if self.proxy else None,
            "timeouts": self.timeouts.to_dict(),
            "launch": self.launch.public_dict(),
            "verify_tls": self.verify_tls,
            "local": self.local,
            "fingerprint": self.fingerprint,
            "warnings": list(self.warnings),
        }


class BrowserConnectionPolicyRuntime:
    def __init__(
        self,
        config: BrowserRuntimeConfig,
        *,
        allow_insecure_tls: bool = False,
        allow_remote_loopback_proxy: bool = False,
        disabled: bool = False,
    ) -> None:
        self.config = config
        self.allow_insecure_tls = allow_insecure_tls
        self.allow_remote_loopback_proxy = allow_remote_loopback_proxy
        self.disabled = disabled

    def normalize(
        self,
        command: BrowserSessionCommand,
        *,
        profile_root: Path,
        downloads_dir: Path,
        base_arguments: tuple[str, ...] = (),
        runtime_environment: Mapping[str, str] | None = None,
    ) -> BrowserConnectionPolicy:
        if self.disabled:
            raise BrowserConfigurationError("browser connection policy runtime is disabled")
        profile_root = profile_root.expanduser().resolve()
        downloads_dir = downloads_dir.expanduser().resolve()
        self._require_contained(profile_root, self.config.runtime_root, "browser profile")
        self._require_contained(downloads_dir, profile_root, "browser downloads")
        headers = self.normalize_headers(command.headers)
        proxy = self.normalize_proxy(command.proxy_url) if command.proxy_url else None
        http_url, websocket_url, local, endpoint_warnings = self.normalize_endpoint(command.endpoint_url)
        timeouts = self.timeout_policy(command.constraints)
        verify_tls = self._verify_tls(command.constraints, http_url)
        launch = self.launch_arguments(
            command,
            profile_root=profile_root,
            downloads_dir=downloads_dir,
            base_arguments=base_arguments,
            runtime_environment=runtime_environment or {},
        )
        self._validate_conflicts(command, http_url, websocket_url, launch, proxy)
        material = {
            "endpoint_http_url": redact_url(http_url),
            "endpoint_websocket_url": redact_url(websocket_url),
            "header_fingerprint": headers.fingerprint,
            "proxy_fingerprint": proxy.fingerprint if proxy else "",
            "timeouts": timeouts.to_dict(),
            "launch": launch.public_dict(),
            "verify_tls": verify_tls,
            "local": local,
        }
        return BrowserConnectionPolicy(
            endpoint_http_url=http_url,
            endpoint_websocket_url=websocket_url,
            headers=headers,
            proxy=proxy,
            timeouts=timeouts,
            launch=launch,
            verify_tls=verify_tls,
            local=local,
            fingerprint=stable_digest(material),
            warnings=tuple(endpoint_warnings),
        )

    def normalize_headers(self, headers: Mapping[str, Any]) -> NormalizedHeaders:
        transport: dict[str, str] = {}
        public: dict[str, str] = {}
        canonical_names: dict[str, str] = {}
        for raw_name, raw_value in headers.items():
            name = str(raw_name).strip()
            value = str(raw_value)
            if not name:
                raise BrowserConfigurationError("browser header name cannot be empty")
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                raise BrowserConfigurationError(f"browser header name is invalid: {name}")
            if "\r" in value or "\n" in value:
                raise BrowserConfigurationError(f"browser header contains newline: {name}")
            normalized = name.casefold()
            if normalized in canonical_names:
                raise BrowserConfigurationError(f"duplicate browser header after normalization: {name}")
            canonical_names[normalized] = name
            transport[name] = value
            public[name] = "[REDACTED]" if is_secret_header(name) else value
        fingerprint_material = {
            name.casefold(): hashlib.sha256(value.encode("utf-8")).hexdigest()
            for name, value in sorted(transport.items())
        }
        return NormalizedHeaders(
            transport=transport,
            public=public,
            names=tuple(sorted(transport, key=str.casefold)),
            fingerprint=stable_digest(fingerprint_material),
        )

    def normalize_proxy(self, proxy_url: str) -> NormalizedProxy:
        parsed = urlsplit(proxy_url)
        if parsed.scheme.casefold() not in {"http", "https", "socks5", "socks5h"}:
            raise BrowserConfigurationError("browser proxy scheme is unsupported")
        if not parsed.hostname:
            raise BrowserConfigurationError("browser proxy host is required")
        try:
            port = parsed.port
        except ValueError as error:
            raise BrowserConfigurationError("browser proxy port is invalid") from error
        if port is not None and not 1 <= port <= 65535:
            raise BrowserConfigurationError("browser proxy port is out of range")
        host = parsed.hostname.casefold()
        if not self.allow_remote_loopback_proxy and host in {"localhost", "127.0.0.1", "::1"}:
            pass
        public_netloc = host + (f":{port}" if port else "")
        public_url = urlunsplit((parsed.scheme.casefold(), public_netloc, parsed.path, "", ""))
        fingerprint = stable_digest({
            "scheme": parsed.scheme.casefold(),
            "host": host,
            "port": port,
            "username": hashlib.sha256((parsed.username or "").encode()).hexdigest(),
            "password": hashlib.sha256((parsed.password or "").encode()).hexdigest(),
        })
        return NormalizedProxy(
            transport_url=proxy_url,
            public_url=public_url,
            scheme=parsed.scheme.casefold(),
            host=host,
            port=port,
            authenticated=bool(parsed.username or parsed.password),
            fingerprint=fingerprint,
        )

    def normalize_endpoint(self, endpoint_url: str) -> tuple[str, str, bool, list[str]]:
        if not endpoint_url:
            return "", "", True, []
        parsed = urlsplit(endpoint_url)
        scheme = parsed.scheme.casefold()
        if scheme not in {"http", "https", "ws", "wss"}:
            raise BrowserConfigurationError("browser endpoint scheme must be http(s) or ws(s)")
        if not parsed.hostname:
            raise BrowserConfigurationError("browser endpoint host is required")
        if parsed.username or parsed.password:
            raise BrowserConfigurationError("browser endpoint credentials must use headers, not URL userinfo")
        try:
            port = parsed.port
        except ValueError as error:
            raise BrowserConfigurationError("browser endpoint port is invalid") from error
        netloc = parsed.hostname + (f":{port}" if port else "")
        path = parsed.path.rstrip("/")
        warnings: list[str] = []
        if any(token in parsed.query.casefold() for token in SENSITIVE_QUERY_TOKENS):
            warnings.append("endpoint query contains sensitive-looking keys and is redacted from projections")
        if scheme in {"ws", "wss"}:
            websocket = urlunsplit((scheme, netloc, path, parsed.query, ""))
            http_scheme = "https" if scheme == "wss" else "http"
            http = urlunsplit((http_scheme, netloc, "", "", ""))
        else:
            http = urlunsplit((scheme, netloc, path, parsed.query, ""))
            websocket = ""
        local = is_loopback_host(parsed.hostname)
        return http, websocket, local, warnings

    def timeout_policy(self, constraints: Mapping[str, Any]) -> BrowserTimeoutPolicy:
        request = coerce_timeout(constraints.get("cdp_request_timeout"), self.config.request_timeout_seconds)
        connect = coerce_timeout(constraints.get("browser_connect_timeout"), self.config.connect_timeout_seconds)
        navigation = coerce_timeout(constraints.get("browser_navigation_timeout"), max(30.0, request * 3))
        action = coerce_timeout(constraints.get("browser_action_timeout"), max(request + 1.0, min(navigation, request * 2)))
        shutdown = coerce_timeout(constraints.get("browser_shutdown_timeout"), max(5.0, connect))
        return BrowserTimeoutPolicy(
            request_seconds=request,
            connect_seconds=connect,
            navigation_seconds=navigation,
            action_seconds=action,
            shutdown_seconds=shutdown,
        )

    def launch_arguments(
        self,
        command: BrowserSessionCommand,
        *,
        profile_root: Path,
        downloads_dir: Path,
        base_arguments: tuple[str, ...],
        runtime_environment: Mapping[str, str],
    ) -> BrowserLaunchArguments:
        user_data_dir = profile_root / "user-data"
        required = [
            f"--user-data-dir={user_data_dir}",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-sync",
        ]
        if command.headless:
            required.append("--headless=new")
        proxy = command.proxy_url
        if proxy:
            parsed = self.normalize_proxy(proxy)
            required.append(f"--proxy-server={parsed.public_url}")
        custom = command.constraints.get("browser_args") or ()
        if isinstance(custom, str):
            custom = (custom,)
        if not isinstance(custom, (list, tuple)):
            raise BrowserConfigurationError("browser_args must be a sequence")
        arguments = self.merge_arguments((*base_arguments, *required, *(str(item) for item in custom)))
        environment = {str(key): str(value) for key, value in runtime_environment.items()}
        environment["ZYRA_BROWSER_DOWNLOADS"] = str(downloads_dir)
        environment["ZYRA_BROWSER_USER_DATA"] = str(user_data_dir)
        return BrowserLaunchArguments(
            executable=command.executable_path,
            arguments=arguments,
            environment=environment,
            headless=command.headless,
            user_data_dir=user_data_dir,
            downloads_dir=downloads_dir,
        )

    def merge_arguments(self, arguments: tuple[str, ...]) -> tuple[str, ...]:
        singleton: dict[str, str] = {}
        flags: list[str] = []
        disabled_features: list[str] = []
        enabled_features: list[str] = []
        for argument in arguments:
            value = str(argument).strip()
            if not value.startswith("--"):
                raise BrowserConfigurationError(f"browser launch argument must start with --: {value}")
            key, separator, raw_value = value.partition("=")
            if key == "--disable-features" and separator:
                disabled_features.extend(item for item in raw_value.split(",") if item)
                continue
            if key == "--enable-features" and separator:
                enabled_features.extend(item for item in raw_value.split(",") if item)
                continue
            if separator:
                previous = singleton.get(key)
                if previous is not None and previous != value and key in {"--user-data-dir", "--remote-debugging-port", "--proxy-server"}:
                    raise BrowserConfigurationError(f"conflicting browser launch argument: {key}")
                singleton[key] = value
            elif value not in flags:
                flags.append(value)
        merged = [*flags, *singleton.values()]
        if disabled_features:
            merged.append(f"--disable-features={','.join(dict.fromkeys(disabled_features))}")
        if enabled_features:
            merged.append(f"--enable-features={','.join(dict.fromkeys(enabled_features))}")
        return tuple(merged)

    def _verify_tls(self, constraints: Mapping[str, Any], endpoint_url: str) -> bool:
        verify = constraints.get("verify_tls", True) is not False
        if not verify and not self.allow_insecure_tls:
            raise BrowserConfigurationError("insecure browser endpoint TLS requires explicit runtime policy")
        if endpoint_url.startswith("http://"):
            return True
        return verify

    def _validate_conflicts(
        self,
        command: BrowserSessionCommand,
        http_url: str,
        websocket_url: str,
        launch: BrowserLaunchArguments,
        proxy: NormalizedProxy | None,
    ) -> None:
        if (http_url or websocket_url) and command.executable_path is not None:
            raise BrowserConfigurationError("browser command cannot specify both endpoint and executable")
        if not http_url and not websocket_url and launch.executable is None:
            pass
        if proxy and http_url:
            endpoint_host = (urlsplit(http_url).hostname or "").casefold()
            if is_loopback_host(endpoint_host) and not is_loopback_host(proxy.host) and not self.allow_remote_loopback_proxy:
                raise BrowserConfigurationError("remote proxy cannot mediate a loopback CDP endpoint")
        if command.keep_alive and command.constraints.get("ephemeral_profile") is True:
            raise BrowserConfigurationError("keep_alive conflicts with ephemeral profile policy")

    @staticmethod
    def _require_contained(path: Path, root: Path, label: str) -> None:
        try:
            path.relative_to(root.resolve())
        except ValueError as error:
            raise BrowserConfigurationError(f"{label} path is outside runtime root") from error


def is_secret_header(name: str) -> bool:
    normalized = name.casefold().replace("_", "-")
    return any(token in normalized for token in SECRET_HEADER_TOKENS)


def coerce_timeout(value: Any, default: float) -> float:
    if value in (None, ""):
        return float(default)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(parsed) or parsed <= 0:
        return float(default)
    return parsed


def is_loopback_host(host: str) -> bool:
    normalized = host.strip("[]").casefold()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def redact_url(value: str) -> str:
    if not value:
        return ""
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path
    return urlunsplit((parsed.scheme, f"{host}{port}", path, "", ""))
