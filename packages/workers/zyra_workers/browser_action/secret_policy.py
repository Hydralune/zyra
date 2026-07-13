from __future__ import annotations

import hashlib
import hmac
import re
import struct
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .models import digest_value, stable_id
from .network_policy import CanonicalUrl, canonical_host, canonicalize_url


class SecretPolicyError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


class SecretPurpose(StrEnum):
    FORM_INPUT = "form_input"
    AUTHENTICATION = "authentication"
    TOTP = "totp"
    HEADER = "header"
    CLIPBOARD = "clipboard"


class SecretKind(StrEnum):
    TEXT = "text"
    PASSWORD = "password"
    TOKEN = "token"
    TOTP_SEED = "totp_seed"


@dataclass(frozen=True, slots=True)
class SecretDescriptor:
    key: str
    kind: SecretKind
    allowed_hosts: tuple[str, ...]
    allowed_purposes: frozenset[SecretPurpose]
    value_length: int
    version: int = 1
    allow_subdomains: bool = False

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", self.key):
            raise ValueError("invalid browser secret key")
        object.__setattr__(self, "allowed_hosts", tuple(canonical_host(item) for item in self.allowed_hosts))
        object.__setattr__(self, "allowed_purposes", frozenset(self.allowed_purposes))
        if not self.allowed_hosts or not self.allowed_purposes or self.value_length < 1 or self.version < 1:
            raise ValueError("browser secret descriptor scope is incomplete")

    @property
    def descriptor_digest(self) -> str:
        return digest_value(self.public_dict())

    def host_allowed(self, host: str) -> bool:
        candidate = canonical_host(host)
        for allowed in self.allowed_hosts:
            if candidate == allowed:
                return True
            if self.allow_subdomains and candidate.endswith("." + allowed):
                return True
        return False

    def public_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": str(self.kind),
            "allowed_hosts": list(self.allowed_hosts),
            "allowed_purposes": sorted(str(item) for item in self.allowed_purposes),
            "value_length": self.value_length,
            "version": self.version,
            "allow_subdomains": self.allow_subdomains,
        }


@dataclass(frozen=True, slots=True)
class SecretReference:
    key: str
    purpose: SecretPurpose
    path: str
    placeholder: str
    descriptor_digest: str

    @property
    def reference_id(self) -> str:
        return stable_id("brsecretref", self.key, self.purpose, self.path, self.descriptor_digest)

    def public_dict(self) -> dict[str, Any]:
        return {
            "reference_id": self.reference_id,
            "key": self.key,
            "purpose": str(self.purpose),
            "path": self.path,
            "placeholder_hash": digest_value(self.placeholder),
            "descriptor_digest": self.descriptor_digest,
        }


@dataclass(frozen=True, slots=True)
class SecretReceipt:
    receipt_id: str
    action_id: str
    origin: str
    references: tuple[SecretReference, ...]
    public_arguments: Mapping[str, Any]
    arguments_shape_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "references", tuple(self.references))
        object.__setattr__(self, "public_arguments", dict(self.public_arguments))

    @property
    def binding_digest(self) -> str:
        return digest_value(self.public_dict())

    def public_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "action_id": self.action_id,
            "origin": self.origin,
            "references": [item.public_dict() for item in self.references],
            "public_arguments": dict(self.public_arguments),
            "arguments_shape_digest": self.arguments_shape_digest,
        }


@dataclass(frozen=True, slots=True)
class SecretLease:
    receipt_id: str
    values: Mapping[str, str] = field(repr=False)
    resolved_arguments: Mapping[str, Any] = field(repr=False)
    value_fingerprints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", dict(self.values))
        object.__setattr__(self, "resolved_arguments", dict(self.resolved_arguments))
        object.__setattr__(self, "value_fingerprints", tuple(self.value_fingerprints))

    def public_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "resolved_keys": sorted(self.values),
            "value_fingerprints": list(self.value_fingerprints),
        }


class SecretProvider(Protocol):
    def describe(self, key: str) -> SecretDescriptor | None: ...

    def resolve(self, key: str) -> str: ...


class InMemorySecretProvider:
    """Small provider used by workers/tests; values are never returned by inventory."""

    def __init__(self, values: Mapping[str, tuple[str, SecretDescriptor]]) -> None:
        self._values = dict(values)
        self._resolve_counts: dict[str, int] = {}
        self._lock = threading.Lock()
        for key, (value, descriptor) in self._values.items():
            if key != descriptor.key or len(value) != descriptor.value_length:
                raise ValueError("secret provider descriptor does not match its value")

    def describe(self, key: str) -> SecretDescriptor | None:
        selected = self._values.get(key)
        return selected[1] if selected else None

    def resolve(self, key: str) -> str:
        selected = self._values.get(key)
        if selected is None:
            raise SecretPolicyError("secret_missing", f"secret {key!r} is unavailable")
        with self._lock:
            self._resolve_counts[key] = self._resolve_counts.get(key, 0) + 1
        return selected[0]

    def resolve_count(self, key: str) -> int:
        return self._resolve_counts.get(key, 0)

    def public_inventory(self) -> tuple[dict[str, Any], ...]:
        return tuple(descriptor.public_dict() for _value, descriptor in self._values.values())


class BrowserSecretPolicy:
    """Secret placeholder binding and post-grant materialization.

    Preflight validates key/origin/purpose and rewrites placeholders into
    non-secret reference records.  It intentionally never calls ``resolve``.
    The executor may call ``materialize`` once, after permission consumption
    and after a fresh selector/origin revalidation.
    """

    _EXACT = re.compile(r"^(?:<secret>(?P<xml>[A-Za-z][A-Za-z0-9_.-]{0,127})</secret>|\$\{secret:(?P<brace>[A-Za-z][A-Za-z0-9_.-]{0,127})\})$")
    _ANY = re.compile(r"<secret>(?P<xml>[A-Za-z][A-Za-z0-9_.-]{0,127})</secret>|\$\{secret:(?P<brace>[A-Za-z][A-Za-z0-9_.-]{0,127})\}")

    def __init__(self, provider: SecretProvider, *, maximum_references: int = 32) -> None:
        if maximum_references < 1:
            raise ValueError("maximum_references must be positive")
        self.provider = provider
        self.maximum_references = maximum_references
        self._consumed: set[str] = set()
        self._lock = threading.Lock()

    def preflight(
        self,
        *,
        action_id: str,
        arguments: Mapping[str, Any],
        target_url: str,
        purpose_by_path: Mapping[str, SecretPurpose] | None = None,
        default_purpose: SecretPurpose = SecretPurpose.FORM_INPUT,
    ) -> SecretReceipt:
        url = canonicalize_url(target_url)
        purposes = dict(purpose_by_path or {})
        references: list[SecretReference] = []
        public = self._bind_value(arguments, "$", url, purposes, default_purpose, references)
        if len(references) > self.maximum_references:
            raise SecretPolicyError("secret_reference_limit", "browser action contains too many secret references")
        shape = shape_of(public)
        receipt_id = stable_id(
            "brsecret",
            action_id,
            url.origin,
            [reference.public_dict() for reference in references],
            shape,
        )
        return SecretReceipt(
            receipt_id=receipt_id,
            action_id=action_id,
            origin=url.origin,
            references=tuple(references),
            public_arguments=public,
            arguments_shape_digest=digest_value(shape),
        )

    def revalidate(self, receipt: SecretReceipt, *, target_url: str) -> None:
        current = canonicalize_url(target_url)
        if current.origin != receipt.origin:
            raise SecretPolicyError(
                "secret_origin_changed",
                "browser secret destination origin changed after approval",
                details={"approved_origin": receipt.origin, "current_origin": current.origin},
            )
        for reference in receipt.references:
            descriptor = self.provider.describe(reference.key)
            if descriptor is None or descriptor.descriptor_digest != reference.descriptor_digest:
                raise SecretPolicyError("secret_descriptor_changed", "browser secret descriptor changed after approval")
            if not descriptor.host_allowed(current.host) or reference.purpose not in descriptor.allowed_purposes:
                raise SecretPolicyError("secret_scope_changed", "browser secret no longer matches origin or purpose")

    def materialize(self, receipt: SecretReceipt, *, target_url: str) -> SecretLease:
        self.revalidate(receipt, target_url=target_url)
        with self._lock:
            if receipt.receipt_id in self._consumed:
                raise SecretPolicyError("secret_receipt_replayed", "browser secret receipt can be resolved only once")
            self._consumed.add(receipt.receipt_id)
        values: dict[str, str] = {}
        fingerprints: list[str] = []
        try:
            for reference in receipt.references:
                if reference.key not in values:
                    descriptor = self.provider.describe(reference.key)
                    if descriptor is None:
                        raise SecretPolicyError("secret_missing", f"secret {reference.key!r} is unavailable")
                    raw = self.provider.resolve(reference.key)
                    value = totp_code(raw) if descriptor.kind == SecretKind.TOTP_SEED else raw
                    if not value:
                        raise SecretPolicyError("secret_empty", f"secret {reference.key!r} resolved to an empty value")
                    values[reference.key] = value
                    fingerprints.append(secret_fingerprint(value))
            resolved = replace_references(receipt.public_arguments, receipt.references, values)
            return SecretLease(receipt.receipt_id, values, resolved, tuple(fingerprints))
        except Exception:
            for key in tuple(values):
                values[key] = ""
            raise

    def _bind_value(
        self,
        value: Any,
        path: str,
        url: CanonicalUrl,
        purposes: Mapping[str, SecretPurpose],
        default_purpose: SecretPurpose,
        references: list[SecretReference],
    ) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): self._bind_value(item, f"{path}.{key}", url, purposes, default_purpose, references)
                for key, item in value.items()
            }
        if isinstance(value, list | tuple):
            return [
                self._bind_value(item, f"{path}[{index}]", url, purposes, default_purpose, references)
                for index, item in enumerate(value)
            ]
        if not isinstance(value, str):
            return value
        matches = tuple(self._ANY.finditer(value))
        if not matches:
            return value
        if len(matches) != 1 or matches[0].span() != (0, len(value)):
            raise SecretPolicyError(
                "secret_interpolation_denied",
                "secret placeholders must occupy the complete argument value",
                details={"path": path},
            )
        key = matches[0].group("xml") or matches[0].group("brace")
        descriptor = self.provider.describe(key)
        if descriptor is None:
            raise SecretPolicyError("secret_missing", f"secret {key!r} is unavailable", details={"path": path})
        purpose = purposes.get(path, default_purpose)
        if purpose not in descriptor.allowed_purposes:
            raise SecretPolicyError(
                "secret_purpose_denied",
                f"secret {key!r} cannot be used for {purpose}",
                details={"path": path},
            )
        if not descriptor.host_allowed(url.host):
            raise SecretPolicyError(
                "secret_domain_denied",
                f"secret {key!r} is not scoped to destination host",
                details={"path": path, "destination_host": url.host},
            )
        reference = SecretReference(
            key=key,
            purpose=purpose,
            path=path,
            placeholder=value,
            descriptor_digest=descriptor.descriptor_digest,
        )
        references.append(reference)
        return {"$zyra_secret_ref": reference.reference_id, "key": key, "purpose": str(purpose)}


class SecretRedactor:
    """Recursive output/event/artifact scrubber for resolved values and credentials."""

    DEFAULT_KEY_PATTERN = re.compile(
        r"(?:secret|password|passwd|token|authorization|cookie|credential|api[_-]?key|session[_-]?key)",
        re.IGNORECASE,
    )
    URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s:]+(?::[^/@\s]*)?@", re.IGNORECASE)
    BEARER = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9+/_.=-]{4,}", re.IGNORECASE)
    LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")

    def __init__(self, values: Sequence[str] = (), *, marker: str = "[REDACTED]") -> None:
        self.marker = marker
        self._values = tuple(sorted((item for item in values if item), key=len, reverse=True))

    def redact(self, value: Any, *, path: str = "$") -> Any:
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, item in value.items():
                name = str(key)
                if self.DEFAULT_KEY_PATTERN.search(name):
                    result[name] = self.marker
                else:
                    result[name] = self.redact(item, path=f"{path}.{name}")
            return result
        if isinstance(value, list | tuple | set | frozenset):
            return [self.redact(item, path=f"{path}[]") for item in value]
        if isinstance(value, bytes):
            try:
                return self._redact_text(value.decode("utf-8"))
            except UnicodeDecodeError:
                return f"[BINARY:{len(value)}]"
        if isinstance(value, str):
            return self._redact_text(value)
        return value

    def assert_clean(self, value: Any) -> None:
        rendered = repr(value)
        for secret in self._values:
            if secret and secret in rendered:
                raise SecretPolicyError("secret_redaction_failure", "secret value remains in public browser output")

    def _redact_text(self, value: str) -> str:
        result = value
        for secret in self._values:
            result = result.replace(secret, self.marker)
        result = self.URL_CREDENTIALS.sub(lambda match: match.group("scheme") + self.marker + "@", result)
        result = self.BEARER.sub(lambda match: match.group(1) + " " + self.marker, result)
        return result


def replace_references(public: Any, references: Sequence[SecretReference], values: Mapping[str, str]) -> Any:
    by_id = {reference.reference_id: reference for reference in references}

    def replace(value: Any) -> Any:
        if isinstance(value, Mapping):
            reference_id = value.get("$zyra_secret_ref")
            if isinstance(reference_id, str):
                reference = by_id.get(reference_id)
                if reference is None or reference.key not in values:
                    raise SecretPolicyError("secret_reference_invalid", "browser secret reference is not bound")
                return values[reference.key]
            return {str(key): replace(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [replace(item) for item in value]
        return value

    return replace(public)


def shape_of(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): shape_of(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, list | tuple):
        return [shape_of(item) for item in value]
    if isinstance(value, str):
        return "string"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if value is None:
        return "null"
    return type(value).__name__


def secret_fingerprint(value: str) -> str:
    return "secret-sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def totp_code(seed: str, *, timestamp: int | None = None, digits: int = 6, period: int = 30) -> str:
    """Generate RFC 6238 SHA-1 TOTP without exposing the seed in state."""
    import base64

    normalized = re.sub(r"\s+", "", seed).upper()
    try:
        key = base64.b32decode(normalized, casefold=True)
    except Exception as exc:
        raise SecretPolicyError("totp_seed_invalid", "TOTP seed is not valid base32") from exc
    counter = int(timestamp if timestamp is not None else time.time()) // period
    payload = struct.pack(">Q", counter)
    digest = hmac.new(key, payload, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10**digits)).zfill(digits)
