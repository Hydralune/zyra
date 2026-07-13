from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit
from urllib.request import url2pathname

from .integration_models import BrowserActionName, BrowserActionRequest, integration_digest
from .models import BrowserSessionRef, browser_id, browser_now


class BrowserActionAdmissionEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class BrowserActionRisk(StrEnum):
    READ_ONLY = "read_only"
    NAVIGATION = "navigation"
    SCRIPT = "script"
    FILE_READ = "file_read"
    TARGET_CONTROL = "target_control"


class BrowserActionPolicyError(RuntimeError):
    def __init__(self, code: str, message: str, *, field_name: str = "", details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.field_name = field_name
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class BrowserActionTimeoutPolicy:
    minimum_seconds: float = 0.1
    maximum_seconds: float = 120.0
    navigate_seconds: float = 30.0
    screenshot_seconds: float = 30.0
    evaluate_seconds: float = 30.0
    downloads_seconds: float = 45.0
    trace_seconds: float = 15.0
    target_seconds: float = 10.0

    def __post_init__(self) -> None:
        values = (
            self.minimum_seconds,
            self.maximum_seconds,
            self.navigate_seconds,
            self.screenshot_seconds,
            self.evaluate_seconds,
            self.downloads_seconds,
            self.trace_seconds,
            self.target_seconds,
        )
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("browser action timeouts must be finite positive values")
        if self.minimum_seconds > self.maximum_seconds:
            raise ValueError("minimum action timeout exceeds maximum")

    def default_for(self, action: BrowserActionName) -> float:
        return {
            BrowserActionName.NAVIGATE: self.navigate_seconds,
            BrowserActionName.SCREENSHOT: self.screenshot_seconds,
            BrowserActionName.EVALUATE_JS: self.evaluate_seconds,
            BrowserActionName.COLLECT_DOWNLOADS: self.downloads_seconds,
            BrowserActionName.CAPTURE_TRACE: self.trace_seconds,
            BrowserActionName.LIST_TARGETS: self.target_seconds,
            BrowserActionName.FOCUS_TARGET: self.target_seconds,
        }[action]

    def normalize(self, action: BrowserActionName, value: Any) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = self.default_for(action)
        if not math.isfinite(parsed):
            parsed = self.default_for(action)
        return max(self.minimum_seconds, min(self.maximum_seconds, parsed))


@dataclass(frozen=True, slots=True)
class BrowserActionPolicyConfig:
    timeouts: BrowserActionTimeoutPolicy = field(default_factory=BrowserActionTimeoutPolicy)
    allow_file_navigation: bool = True
    allow_loopback_http: bool = True
    max_url_chars: int = 16_384
    max_script_chars: int = 256_000
    max_string_argument_chars: int = 1_000_000
    max_argument_depth: int = 12
    max_argument_items: int = 10_000
    max_settle_seconds: float = 10.0
    max_download_files: int = 256
    allowed_screenshot_formats: tuple[str, ...] = ("png", "jpeg", "webp")
    secret_query_keys: tuple[str, ...] = (
        "access_token", "api_key", "apikey", "auth", "authorization", "code",
        "credential", "key", "password", "secret", "signature", "sig", "token",
    )

    def __post_init__(self) -> None:
        if self.max_url_chars < 256 or self.max_script_chars < 1024:
            raise ValueError("browser action policy size limits are too small")
        if self.max_argument_depth < 2 or self.max_argument_items < 16:
            raise ValueError("browser action policy structural limits are too small")
        if self.max_settle_seconds < 0 or self.max_download_files < 1:
            raise ValueError("browser action policy bounds are invalid")


@dataclass(frozen=True, slots=True)
class BrowserActionAdmission:
    admission_id: str
    request_id: str
    request_fingerprint: str
    browser_session_id: str
    session_revision: int
    action: BrowserActionName
    effect: BrowserActionAdmissionEffect
    risk: BrowserActionRisk
    timeout_seconds: float
    execution_arguments: Mapping[str, Any]
    public_arguments: Mapping[str, Any]
    workspace_root: str = ""
    downloads_root: str = ""
    reason: str = ""
    code: str = ""
    warnings: tuple[str, ...] = ()
    admitted_at: str = field(default_factory=browser_now)

    @property
    def allowed(self) -> bool:
        return self.effect == BrowserActionAdmissionEffect.ALLOW

    def apply(self, request: BrowserActionRequest) -> BrowserActionRequest:
        if not self.allowed:
            raise BrowserActionPolicyError(self.code or "browser_action_policy_denied", self.reason)
        if request.request_id != self.request_id or request.fingerprint != self.request_fingerprint:
            raise BrowserActionPolicyError("browser_action_admission_identity_changed", "action request changed after admission")
        return replace(
            request,
            arguments=dict(self.execution_arguments),
            metadata={
                **dict(request.metadata),
                "browser_action_admission_id": self.admission_id,
                "browser_action_risk": str(self.risk),
                "browser_action_timeout_seconds": self.timeout_seconds,
                "browser_action_public_arguments": dict(self.public_arguments),
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "admission_id": self.admission_id,
            "request_id": self.request_id,
            "request_fingerprint": self.request_fingerprint,
            "browser_session_id": self.browser_session_id,
            "session_revision": self.session_revision,
            "action": str(self.action),
            "effect": str(self.effect),
            "allowed": self.allowed,
            "risk": str(self.risk),
            "timeout_seconds": self.timeout_seconds,
            "public_arguments": dict(self.public_arguments),
            "workspace_root": self.workspace_root,
            "downloads_root": self.downloads_root,
            "reason": self.reason,
            "code": self.code,
            "warnings": list(self.warnings),
            "admitted_at": self.admitted_at,
        }


class BrowserActionAdmissionPolicy:
    """04A admission for actions already bound to one browser session.

    This policy intentionally does not replace the 04C registry, domain policy,
    element security or permission runtime.  It only normalizes the transport
    inputs that must be safe before the existing permission gate and CDP call.
    """

    def __init__(self, config: BrowserActionPolicyConfig | None = None, *, disabled: bool = False) -> None:
        self.config = config or BrowserActionPolicyConfig()
        self.disabled = disabled

    def admit(
        self,
        request: BrowserActionRequest,
        session: BrowserSessionRef,
        *,
        workspace_root: str | Path | None = None,
        downloads_root: str | Path | None = None,
    ) -> BrowserActionAdmission:
        if self.disabled:
            return self._deny(request, session, "browser_action_policy_disabled", "browser action admission policy is disabled")
        identity_error = self._identity_error(request, session)
        if identity_error:
            return self._deny(request, session, identity_error[0], identity_error[1])
        try:
            self._validate_structure(request.arguments)
            workspace = Path(workspace_root).expanduser().resolve() if workspace_root else None
            downloads = Path(downloads_root).expanduser().resolve() if downloads_root else None
            timeout = self.config.timeouts.normalize(request.action, request.arguments.get("timeout_seconds"))
            execution, warnings = self._normalize_arguments(
                request.action,
                request.arguments,
                workspace_root=workspace,
                downloads_root=downloads,
                timeout_seconds=timeout,
            )
            public = redact_action_arguments(execution, secret_query_keys=self.config.secret_query_keys)
            return BrowserActionAdmission(
                admission_id=browser_id("bradmission"),
                request_id=request.request_id,
                request_fingerprint=request.fingerprint,
                browser_session_id=request.browser_session_id,
                session_revision=session.revision,
                action=request.action,
                effect=BrowserActionAdmissionEffect.ALLOW,
                risk=self.risk_for(request.action),
                timeout_seconds=timeout,
                execution_arguments=execution,
                public_arguments=public,
                workspace_root=str(workspace or ""),
                downloads_root=str(downloads or ""),
                warnings=warnings,
            )
        except BrowserActionPolicyError as error:
            return self._deny(request, session, error.code, str(error), details=error.details)

    @staticmethod
    def risk_for(action: BrowserActionName) -> BrowserActionRisk:
        return {
            BrowserActionName.NAVIGATE: BrowserActionRisk.NAVIGATION,
            BrowserActionName.SCREENSHOT: BrowserActionRisk.READ_ONLY,
            BrowserActionName.EVALUATE_JS: BrowserActionRisk.SCRIPT,
            BrowserActionName.COLLECT_DOWNLOADS: BrowserActionRisk.FILE_READ,
            BrowserActionName.CAPTURE_TRACE: BrowserActionRisk.READ_ONLY,
            BrowserActionName.LIST_TARGETS: BrowserActionRisk.READ_ONLY,
            BrowserActionName.FOCUS_TARGET: BrowserActionRisk.TARGET_CONTROL,
        }[action]

    def _identity_error(
        self,
        request: BrowserActionRequest,
        session: BrowserSessionRef,
    ) -> tuple[str, str] | None:
        if session.status != "running":
            return "browser_action_session_not_running", "browser action admission requires a running session"
        if request.browser_session_id != session.session_id:
            return "browser_action_session_mismatch", "action request is bound to another browser session"
        if request.run_id != session.run_id or request.task_id != session.task_id:
            return "browser_action_identity_mismatch", "action request run/task identity does not own the session"
        return None

    def _normalize_arguments(
        self,
        action: BrowserActionName,
        arguments: Mapping[str, Any],
        *,
        workspace_root: Path | None,
        downloads_root: Path | None,
        timeout_seconds: float,
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        normalized = {str(key): self._copy_value(value) for key, value in arguments.items()}
        normalized["timeout_seconds"] = timeout_seconds
        warnings: list[str] = []
        if action == BrowserActionName.NAVIGATE:
            normalized["url"], url_warnings = self._normalize_url(
                str(normalized.get("url") or ""),
                workspace_root=workspace_root,
                downloads_root=downloads_root,
            )
            warnings.extend(url_warnings)
            normalized["settle_seconds"] = self._bounded_float(
                normalized.get("settle_seconds"), 0.0, 0.0, self.config.max_settle_seconds
            )
        elif action == BrowserActionName.SCREENSHOT:
            image_format = str(normalized.get("format") or "png").casefold().replace("jpg", "jpeg")
            if image_format not in self.config.allowed_screenshot_formats:
                raise BrowserActionPolicyError("browser_screenshot_format_denied", "screenshot format is not admitted", field_name="format")
            normalized["format"] = image_format
            normalized["full_page"] = bool(normalized.get("full_page", False))
            if image_format in {"jpeg", "webp"}:
                normalized["quality"] = self._bounded_int(normalized.get("quality"), 90, 1, 100)
            else:
                normalized.pop("quality", None)
        elif action == BrowserActionName.EVALUATE_JS:
            code = str(normalized.get("code") or "")
            if not code.strip():
                raise BrowserActionPolicyError("browser_evaluate_code_missing", "evaluate_js code is empty", field_name="code")
            if len(code) > self.config.max_script_chars:
                raise BrowserActionPolicyError(
                    "browser_evaluate_code_too_large",
                    "evaluate_js code exceeds the 04A transport bound",
                    field_name="code",
                    details={"chars": len(code), "limit": self.config.max_script_chars},
                )
            if "\x00" in code:
                raise BrowserActionPolicyError("browser_evaluate_code_invalid", "evaluate_js code contains NUL", field_name="code")
            normalized["code"] = code
            normalized["await_promise"] = bool(normalized.get("await_promise", True))
            normalized["return_by_value"] = bool(normalized.get("return_by_value", True))
            normalized["user_gesture"] = bool(normalized.get("user_gesture", False))
        elif action == BrowserActionName.COLLECT_DOWNLOADS:
            if downloads_root is None:
                raise BrowserActionPolicyError(
                    "browser_download_root_unavailable",
                    "download collection requires the session-owned downloads root",
                )
            normalized["settle_seconds"] = self._bounded_float(
                normalized.get("settle_seconds"), 0.0, 0.0, self.config.max_settle_seconds
            )
            normalized["max_files"] = self._bounded_int(
                normalized.get("max_files"), 32, 1, self.config.max_download_files
            )
            requested = normalized.get("path") or normalized.get("source_path")
            if requested:
                source = Path(str(requested)).expanduser().resolve()
                self._require_contained(source, downloads_root, "download source")
                normalized["source_path"] = str(source)
                normalized.pop("path", None)
        elif action == BrowserActionName.CAPTURE_TRACE:
            normalized = {"timeout_seconds": timeout_seconds}
        elif action == BrowserActionName.LIST_TARGETS:
            normalized = {"timeout_seconds": timeout_seconds}
        elif action == BrowserActionName.FOCUS_TARGET:
            target_id = str(normalized.get("target_id") or "").strip()
            if not target_id or len(target_id) > 256 or not re.fullmatch(r"[A-Za-z0-9._:-]+", target_id):
                raise BrowserActionPolicyError("browser_target_id_invalid", "focus target id is invalid", field_name="target_id")
            normalized = {
                "target_id": target_id,
                "activate": bool(normalized.get("activate", True)),
                "timeout_seconds": timeout_seconds,
            }
        return normalized, tuple(dict.fromkeys(warnings))

    def _normalize_url(
        self,
        value: str,
        *,
        workspace_root: Path | None,
        downloads_root: Path | None,
    ) -> tuple[str, tuple[str, ...]]:
        url = value.strip()
        if not url or len(url) > self.config.max_url_chars:
            raise BrowserActionPolicyError("browser_navigation_url_invalid", "navigation URL is empty or too long", field_name="url")
        if any(ord(character) < 0x20 for character in url):
            raise BrowserActionPolicyError("browser_navigation_url_control_character", "navigation URL contains control characters")
        parsed = urlsplit(url)
        scheme = parsed.scheme.casefold()
        if parsed.username or parsed.password:
            raise BrowserActionPolicyError("browser_navigation_url_userinfo", "navigation credentials must not be embedded in the URL")
        warnings: list[str] = []
        if scheme in {"http", "https"}:
            if not parsed.hostname:
                raise BrowserActionPolicyError("browser_navigation_host_missing", "http(s) navigation URL requires a host")
            host = parsed.hostname.rstrip(".").casefold()
            try:
                ip = ipaddress.ip_address(host.strip("[]"))
            except ValueError:
                ip = None
            if ip is not None and ip.is_loopback and not self.config.allow_loopback_http:
                raise BrowserActionPolicyError("browser_navigation_loopback_denied", "loopback navigation is disabled")
            if ip is not None and (ip.is_private or ip.is_link_local):
                warnings.append("navigation targets a private or link-local address; 04C domain policy remains authoritative")
            try:
                port = parsed.port
            except ValueError as error:
                raise BrowserActionPolicyError(
                    "browser_navigation_port_invalid",
                    "navigation URL contains an invalid port",
                    field_name="url",
                ) from error
            netloc = parsed.hostname + (f":{port}" if port else "")
            normalized = urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))
            return normalized, tuple(warnings)
        if scheme == "file":
            if not self.config.allow_file_navigation:
                raise BrowserActionPolicyError("browser_file_navigation_denied", "file navigation is disabled")
            path_text = url2pathname(unquote(parsed.path))
            if parsed.netloc:
                path_text = f"//{parsed.netloc}{path_text}"
            source = Path(path_text).expanduser().resolve()
            roots = tuple(root for root in (workspace_root, downloads_root) if root is not None)
            if not roots:
                raise BrowserActionPolicyError("browser_file_navigation_root_missing", "file navigation has no owned root")
            if not any(self._contained(source, root) for root in roots):
                raise BrowserActionPolicyError(
                    "browser_file_navigation_escape",
                    "file navigation escapes workspace/download ownership",
                    details={"source": str(source), "roots": [str(root) for root in roots]},
                )
            if not source.is_file():
                raise BrowserActionPolicyError("browser_file_navigation_missing", "file navigation target does not exist")
            return source.as_uri(), ()
        raise BrowserActionPolicyError(
            "browser_navigation_scheme_denied",
            f"navigation scheme is not admitted by the 04A session policy: {scheme or 'missing'}",
        )

    def _validate_structure(self, arguments: Mapping[str, Any]) -> None:
        count = 0

        def visit(value: Any, depth: int) -> None:
            nonlocal count
            count += 1
            if count > self.config.max_argument_items:
                raise BrowserActionPolicyError("browser_action_arguments_too_many", "browser action arguments exceed item limit")
            if depth > self.config.max_argument_depth:
                raise BrowserActionPolicyError("browser_action_arguments_too_deep", "browser action arguments exceed depth limit")
            if isinstance(value, Mapping):
                for key, item in value.items():
                    if not isinstance(key, str) or not key or len(key) > 256:
                        raise BrowserActionPolicyError("browser_action_argument_key_invalid", "browser action argument key is invalid")
                    visit(item, depth + 1)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    visit(item, depth + 1)
            elif isinstance(value, str):
                if len(value) > self.config.max_string_argument_chars:
                    raise BrowserActionPolicyError("browser_action_argument_string_too_large", "browser action string argument is too large")
            elif not isinstance(value, (int, float, bool, type(None), Path)):
                raise BrowserActionPolicyError(
                    "browser_action_argument_type_invalid",
                    f"browser action argument type is unsupported: {type(value).__name__}",
                )

        visit(arguments, 0)

    def _copy_value(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): self._copy_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._copy_value(item) for item in value]
        if isinstance(value, Path):
            return str(value.expanduser().resolve())
        return value

    def _deny(
        self,
        request: BrowserActionRequest,
        session: BrowserSessionRef,
        code: str,
        reason: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> BrowserActionAdmission:
        public = redact_action_arguments(request.arguments, secret_query_keys=self.config.secret_query_keys)
        return BrowserActionAdmission(
            admission_id=browser_id("bradmission"),
            request_id=request.request_id,
            request_fingerprint=request.fingerprint,
            browser_session_id=request.browser_session_id,
            session_revision=session.revision,
            action=request.action,
            effect=BrowserActionAdmissionEffect.DENY,
            risk=self.risk_for(request.action),
            timeout_seconds=self.config.timeouts.normalize(request.action, request.arguments.get("timeout_seconds")),
            execution_arguments={},
            public_arguments={**public, **({"policy_details": redact_action_arguments(details, secret_query_keys=self.config.secret_query_keys)} if details else {})},
            reason=reason,
            code=code,
        )

    @staticmethod
    def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    @staticmethod
    def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = default
        if not math.isfinite(parsed):
            parsed = default
        return max(minimum, min(maximum, parsed))

    @staticmethod
    def _contained(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @classmethod
    def _require_contained(cls, path: Path, root: Path, label: str) -> None:
        if not cls._contained(path, root):
            raise BrowserActionPolicyError(
                "browser_action_file_escape",
                f"{label} escapes its owned root",
                details={"path": str(path), "root": str(root)},
            )

    def describe(self) -> dict[str, Any]:
        return {
            "runtime_id": "zyra-browser-action-admission-policy",
            "owner_unit": "M1-S04A-02",
            "disabled": self.disabled,
            "actions": [str(item) for item in BrowserActionName],
            "scope": "session-bound transport admission",
            "full_action_registry_owner": "M1-04C",
            "full_security_policy_owner": "M1-04C",
            "permission_owner": "M1-03A",
            "canonical_store_owner": False,
        }


SECRET_KEY_PARTS = frozenset({
    "authorization", "cookie", "password", "passwd", "secret", "token", "api_key",
    "apikey", "credential", "private_key", "access_key", "session_key", "signature",
})


def redact_action_arguments(
    value: Any,
    *,
    secret_query_keys: Sequence[str] = (),
    key: str = "",
    depth: int = 0,
) -> Any:
    if depth > 16:
        return "[DEPTH_LIMIT]"
    normalized_key = key.casefold().replace("-", "_")
    if normalized_key and any(part in normalized_key for part in SECRET_KEY_PARTS):
        return "[REDACTED]"
    if normalized_key in {"code", "expression", "script", "javascript"} and isinstance(value, str):
        return {
            "chars": len(value),
            "sha256": "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest(),
            "content_included": False,
        }
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_action_arguments(
                item,
                secret_query_keys=secret_query_keys,
                key=str(item_key),
                depth=depth + 1,
            )
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            redact_action_arguments(item, secret_query_keys=secret_query_keys, key=key, depth=depth + 1)
            for item in value
        ]
    if isinstance(value, Path):
        return value.name
    if isinstance(value, str):
        if normalized_key in {"url", "uri", "endpoint_url", "source_url"}:
            return redact_url(value, secret_query_keys=secret_query_keys)
        if re.search(r"(?i)(bearer|basic)\s+[A-Za-z0-9._~+/-]{8,}", value):
            return "[REDACTED]"
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def redact_url(value: str, *, secret_query_keys: Sequence[str]) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "[INVALID_URL]"
    secret_keys = {item.casefold().replace("-", "_") for item in secret_query_keys}
    query: list[tuple[str, str]] = []
    for name, item in parse_qsl(parsed.query, keep_blank_values=True):
        normalized = name.casefold().replace("-", "_")
        query.append((name, "[REDACTED]" if normalized in secret_keys or any(part in normalized for part in SECRET_KEY_PARTS) else item))
    hostname = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        return "[INVALID_URL]"
    netloc = hostname + (f":{port}" if port else "")
    return urlunsplit((parsed.scheme, netloc, parsed.path, urlencode(query), ""))
