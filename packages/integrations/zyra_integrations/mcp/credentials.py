from __future__ import annotations

"""Credential custody primitives for the Zyra-owned MCP runtime.

The rest of the MCP runtime stores only :class:`CredentialReference` values.
Raw OAuth, XAA, bearer, and client-secret material crosses this module's
boundary only through explicit ``reveal_*`` calls.  File persistence uses an
atomic replace, restrictive permissions where the platform supports them,
and a small cross-process lease so concurrent refreshes cannot interleave.

File permissions are defense in depth, not secret protection.  The default
vault therefore uses Windows DPAPI (current-user scope, with credential
identity as authenticated optional entropy).  Platforms without an available
authenticated OS protector fail closed unless a deployment injects a secure
``SecretCodec``.  ``IdentitySecretCodec`` remains available only as an
explicit test/development choice.
"""

import base64
import ctypes
import hashlib
import json
import os
import secrets as random_secrets
import stat
import threading
import time
from collections.abc import Callable, Iterator, Mapping, MutableMapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, TypeAlias, runtime_checkable

if os.name == "nt":  # pragma: no branch - selected once for this process.
    from ctypes import wintypes


JsonScalar: TypeAlias = None | bool | int | float | str
PublicValue: TypeAlias = JsonScalar | list["PublicValue"] | dict[str, "PublicValue"]


class CredentialError(RuntimeError):
    """Base error for credential custody failures."""


class CredentialNotFound(CredentialError):
    """The requested opaque credential reference does not exist."""


class CredentialCorrupt(CredentialError):
    """Credential bytes or metadata failed integrity validation."""


class CredentialConflict(CredentialError):
    """A compare-and-swap revision did not match the current record."""


class CredentialLeaseTimeout(CredentialError):
    """Another process retained the credential mutation lease too long."""


class CredentialPermissionError(CredentialError):
    """Strict permission verification found a credential exposure risk."""


class CredentialProtectionUnavailable(CredentialError):
    """No authenticated credential protector is available for this platform."""


class CredentialProtectionError(CredentialError):
    """The platform credential protector could not protect secret material."""


class CredentialKind(StrEnum):
    OAUTH = "oauth"
    XAA = "xaa"
    BEARER = "bearer"
    CLIENT = "client"
    CUSTOM = "custom"


def _utc_timestamp(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CredentialError(f"credential value is not canonical JSON: {type(exc).__name__}") from exc
    return text.encode("utf-8")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


_SENSITIVE_FRAGMENTS = (
    "authorization",
    "bearer",
    "client-secret",
    "client_secret",
    "cookie",
    "credential",
    "id-token",
    "id_token",
    "password",
    "private-key",
    "private_key",
    "refresh-token",
    "refresh_token",
    "secret",
    "token",
)

_SAFE_SECRET_SHAPED_NAMES = {
    "access-token-digest",
    "client-secret-present",
    "content-digest",
    "credential-reference",
    "has-access-token",
    "has-client-secret",
    "has-id-token",
    "has-refresh-token",
    "id-token-digest",
    "raw-secret-included",
    "raw-token-included",
    "refresh-token-digest",
    "secret-fields",
    "token-digest",
    "token-type",
}


def is_sensitive_name(name: str) -> bool:
    normalized = str(name).strip().casefold().replace("_", "-")
    if normalized in _SAFE_SECRET_SHAPED_NAMES:
        return False
    return any(fragment.replace("_", "-") in normalized for fragment in _SENSITIVE_FRAGMENTS)


def redact_public_value(value: Any, *, replacement: str = "<redacted>") -> PublicValue:
    """Return a JSON-safe value with every secret-shaped field removed.

    This function is used before state, events, logs, or API payloads receive
    credential-adjacent data.  ``SecretValue`` instances are always redacted,
    even under a benign key.
    """

    if isinstance(value, SecretValue):
        return replacement
    if isinstance(value, Mapping):
        output: dict[str, PublicValue] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            output[key] = replacement if is_sensitive_name(key) else redact_public_value(item, replacement=replacement)
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        return [redact_public_value(item, replacement=replacement) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary:{len(value)} bytes>"
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return "<non-finite>"
        return value
    if hasattr(value, "safe_dict") and callable(value.safe_dict):
        return redact_public_value(value.safe_dict(), replacement=replacement)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return redact_public_value(value.to_dict(), replacement=replacement)
    return f"<{type(value).__name__}>"


def validate_public_attributes(value: Mapping[str, Any] | None) -> dict[str, PublicValue]:
    """Validate metadata that may safely leave credential custody.

    Secret-shaped keys are rejected instead of silently redacted at write
    time.  Silent redaction would make callers believe important material was
    persisted when it was actually discarded.
    """

    output: dict[str, PublicValue] = {}
    for raw_key, item in (value or {}).items():
        key = str(raw_key)
        if is_sensitive_name(key):
            raise CredentialError(f"public credential attribute {key!r} is secret-shaped")
        if isinstance(item, SecretValue):
            raise CredentialError(f"public credential attribute {key!r} contains SecretValue")
        selected = redact_public_value(item)
        if selected == "<redacted>":
            raise CredentialError(f"public credential attribute {key!r} was redacted")
        output[key] = selected
    return output


class SecretValue:
    """Mutable secret bytes with redacted display and explicit revelation.

    Python cannot guarantee that every interpreter copy is wiped, but using a
    bytearray lets the owner clear the primary buffer and prevents accidental
    ``repr``/``str``/dataclass serialization leakage.
    """

    __slots__ = ("_buffer", "_destroyed")

    def __init__(self, value: str | bytes | bytearray | memoryview | "SecretValue") -> None:
        if isinstance(value, SecretValue):
            data = value.reveal_bytes()
        elif isinstance(value, str):
            data = value.encode("utf-8")
        elif isinstance(value, (bytes, bytearray, memoryview)):
            data = bytes(value)
        else:
            raise TypeError("secret values must be text or bytes")
        self._buffer = bytearray(data)
        self._destroyed = False

    def __repr__(self) -> str:
        return "SecretValue(<destroyed>)" if self._destroyed else "SecretValue(<redacted>)"

    def __str__(self) -> str:
        return "<destroyed>" if self._destroyed else "<redacted>"

    def __bool__(self) -> bool:
        return not self._destroyed and bool(self._buffer)

    def __len__(self) -> int:
        return 0 if self._destroyed else len(self._buffer)

    @property
    def destroyed(self) -> bool:
        return self._destroyed

    def reveal_bytes(self) -> bytes:
        if self._destroyed:
            raise CredentialError("secret value has been destroyed")
        return bytes(self._buffer)

    def reveal_text(self, encoding: str = "utf-8") -> str:
        try:
            return self.reveal_bytes().decode(encoding)
        except UnicodeDecodeError as exc:
            raise CredentialError("secret is not valid text") from exc

    def fingerprint(self) -> str:
        return _sha256(self.reveal_bytes())

    def copy(self) -> "SecretValue":
        return SecretValue(self.reveal_bytes())

    def constant_time_equals(self, other: str | bytes | "SecretValue") -> bool:
        candidate = other.reveal_bytes() if isinstance(other, SecretValue) else (
            other.encode("utf-8") if isinstance(other, str) else bytes(other)
        )
        return random_secrets.compare_digest(self.reveal_bytes(), candidate)

    def destroy(self) -> None:
        if self._destroyed:
            return
        for index in range(len(self._buffer)):
            self._buffer[index] = 0
        self._buffer.clear()
        self._destroyed = True

    def __enter__(self) -> "SecretValue":
        if self._destroyed:
            raise CredentialError("cannot enter a destroyed secret")
        return self

    def __exit__(self, *_: object) -> None:
        self.destroy()


def coerce_secret(value: str | bytes | bytearray | memoryview | SecretValue) -> SecretValue:
    return value.copy() if isinstance(value, SecretValue) else SecretValue(value)


@dataclass(frozen=True, slots=True)
class CredentialReference:
    namespace: str
    server_id: str
    config_fingerprint: str
    kind: CredentialKind = CredentialKind.OAUTH

    def __post_init__(self) -> None:
        for label, value, maximum in (
            ("namespace", self.namespace, 128),
            ("server_id", self.server_id, 512),
            ("config_fingerprint", self.config_fingerprint, 256),
        ):
            if not isinstance(value, str) or not value.strip():
                raise CredentialError(f"{label} is required")
            if len(value) > maximum:
                raise CredentialError(f"{label} exceeds {maximum} characters")
        if not isinstance(self.kind, CredentialKind):
            object.__setattr__(self, "kind", CredentialKind(str(self.kind)))

    @property
    def opaque_id(self) -> str:
        material = _canonical_json_bytes(
            {
                "namespace": self.namespace,
                "server_id": self.server_id,
                "config_fingerprint": self.config_fingerprint,
                "kind": str(self.kind),
            }
        )
        return f"cred:{self.namespace}:{self.kind}:{hashlib.sha256(material).hexdigest()}"

    @property
    def storage_name(self) -> str:
        return hashlib.sha256(self.opaque_id.encode("utf-8")).hexdigest() + ".credential"

    def safe_dict(self) -> dict[str, PublicValue]:
        return {
            "credential_reference": self.opaque_id,
            "namespace": self.namespace,
            "server_id": self.server_id,
            "config_fingerprint": self.config_fingerprint,
            "kind": str(self.kind),
        }


@dataclass(frozen=True, slots=True)
class CredentialMetadata:
    reference: CredentialReference
    revision: int
    created_at: str
    updated_at: str
    expires_at: str = ""
    content_digest: str = ""
    secret_fields: tuple[str, ...] = ()
    public_attributes: Mapping[str, PublicValue] = field(default_factory=dict)
    external_version: str = ""

    @property
    def expired_at(self) -> float | None:
        return _parse_timestamp(self.expires_at)

    def is_expired(self, now: float | None = None) -> bool:
        expires = self.expired_at
        return expires is not None and expires <= (time.time() if now is None else now)

    def safe_dict(self) -> dict[str, PublicValue]:
        return {
            **self.reference.safe_dict(),
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "content_digest": self.content_digest,
            "secret_fields": list(self.secret_fields),
            "public_attributes": dict(self.public_attributes),
            "external_version": self.external_version,
            "raw_secret_included": False,
        }


class CredentialEnvelope:
    """A credential read result whose display surface remains redacted."""

    __slots__ = ("metadata", "_secrets")

    def __init__(self, metadata: CredentialMetadata, secrets: Mapping[str, SecretValue]) -> None:
        self.metadata = metadata
        self._secrets = dict(secrets)

    def __repr__(self) -> str:
        return f"CredentialEnvelope(reference={self.metadata.reference.opaque_id!r}, fields={sorted(self._secrets)!r})"

    def field_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._secrets))

    def has(self, name: str) -> bool:
        return name in self._secrets and bool(self._secrets[name])

    def secret(self, name: str, *, required: bool = True) -> SecretValue | None:
        selected = self._secrets.get(name)
        if selected is None:
            if required:
                raise CredentialNotFound(f"credential field {name!r} is unavailable")
            return None
        return selected.copy()

    def reveal_text(self, name: str, *, default: str | None = None) -> str:
        selected = self._secrets.get(name)
        if selected is None:
            if default is not None:
                return default
            raise CredentialNotFound(f"credential field {name!r} is unavailable")
        return selected.reveal_text()

    def secret_fingerprints(self) -> dict[str, str]:
        return {name: secret.fingerprint() for name, secret in self._secrets.items()}

    def safe_dict(self) -> dict[str, PublicValue]:
        return self.metadata.safe_dict()

    def close(self) -> None:
        for secret in self._secrets.values():
            secret.destroy()
        self._secrets.clear()

    def __enter__(self) -> "CredentialEnvelope":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@runtime_checkable
class SecretCodec(Protocol):
    codec_id: str

    def encode(self, plaintext: bytes, *, associated_data: bytes) -> bytes: ...

    def decode(self, encoded: bytes, *, associated_data: bytes) -> bytes: ...


class IdentitySecretCodec:
    """Insecure identity codec for explicit test/development use only.

    Passing this codec is an affirmative opt-in.  It is never selected by
    :class:`FileCredentialVault` and must not be used for production secrets.
    """

    codec_id = "identity-insecure-dev-v1"
    production_safe = False

    def encode(self, plaintext: bytes, *, associated_data: bytes) -> bytes:
        del associated_data
        return bytes(plaintext)

    def decode(self, encoded: bytes, *, associated_data: bytes) -> bytes:
        del associated_data
        return bytes(encoded)


if os.name == "nt":

    class _DpapiDataBlob(ctypes.Structure):
        _fields_ = (
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        )


class WindowsDpapiSecretCodec:
    """Authenticated, current-user Windows DPAPI credential protection.

    DPAPI binds ciphertext to the current Windows user.  Zyra additionally
    supplies a digest of the opaque credential reference as optional entropy,
    so copying ciphertext to another credential record fails authentication.
    """

    codec_id = "windows-dpapi-current-user-v1"
    production_safe = True
    _MAGIC = b"ZYRA-DPAPI\x01"
    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    def __init__(self) -> None:
        if os.name != "nt":
            raise CredentialProtectionUnavailable(
                "Windows DPAPI credential protection is unavailable on this platform"
            )
        try:
            self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._crypt32.CryptProtectData.argtypes = (
                ctypes.POINTER(_DpapiDataBlob),
                wintypes.LPCWSTR,
                ctypes.POINTER(_DpapiDataBlob),
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(_DpapiDataBlob),
            )
            self._crypt32.CryptProtectData.restype = wintypes.BOOL
            self._crypt32.CryptUnprotectData.argtypes = (
                ctypes.POINTER(_DpapiDataBlob),
                ctypes.POINTER(wintypes.LPWSTR),
                ctypes.POINTER(_DpapiDataBlob),
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(_DpapiDataBlob),
            )
            self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
            self._kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
            self._kernel32.LocalFree.restype = wintypes.HLOCAL
        except (AttributeError, OSError) as exc:
            raise CredentialProtectionUnavailable(
                "Windows DPAPI credential protection could not be initialized"
            ) from exc

    @staticmethod
    def _blob(value: bytes) -> tuple[Any, Any]:
        selected = bytes(value)
        buffer = (ctypes.c_ubyte * max(1, len(selected)))()
        if selected:
            ctypes.memmove(buffer, selected, len(selected))
        blob = _DpapiDataBlob(
            cbData=len(selected),
            pbData=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        return blob, buffer

    @staticmethod
    def _entropy(associated_data: bytes) -> bytes:
        return b"zyra:mcp:credential:v1\x00" + hashlib.sha256(associated_data).digest()

    def _free(self, pointer: Any) -> None:
        if pointer:
            self._kernel32.LocalFree(ctypes.cast(pointer, ctypes.c_void_p))

    def encode(self, plaintext: bytes, *, associated_data: bytes) -> bytes:
        source, source_buffer = self._blob(plaintext)
        entropy, entropy_buffer = self._blob(self._entropy(associated_data))
        output = _DpapiDataBlob()
        if not self._crypt32.CryptProtectData(
            ctypes.byref(source),
            None,
            ctypes.byref(entropy),
            None,
            None,
            self._CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output),
        ):
            error_code = ctypes.get_last_error()
            raise CredentialProtectionError(
                f"Windows DPAPI protect failed (error {error_code})"
            )
        try:
            protected = ctypes.string_at(output.pbData, output.cbData)
        finally:
            self._free(output.pbData)
        return self._MAGIC + protected

    def decode(self, encoded: bytes, *, associated_data: bytes) -> bytes:
        selected = bytes(encoded)
        if not selected.startswith(self._MAGIC):
            raise CredentialCorrupt("credential payload is not Windows DPAPI ciphertext")
        source, source_buffer = self._blob(selected[len(self._MAGIC) :])
        entropy, entropy_buffer = self._blob(self._entropy(associated_data))
        output = _DpapiDataBlob()
        description = wintypes.LPWSTR()
        if not self._crypt32.CryptUnprotectData(
            ctypes.byref(source),
            ctypes.byref(description),
            ctypes.byref(entropy),
            None,
            None,
            self._CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output),
        ):
            # ERROR_INVALID_DATA is deliberately not exposed as a distinct
            # oracle: a tampered, copied, or wrong-user ciphertext is corrupt.
            raise CredentialCorrupt("Windows DPAPI credential authentication failed")
        # Keep the ctypes input allocations alive until after the native call.
        del source_buffer, entropy_buffer
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            self._free(output.pbData)
            self._free(description)


class _UnavailableSecretCodec:
    """Lazy fail-closed protector preserving credential-free MCP startup."""

    codec_id = "authenticated-protector-unavailable-v1"
    production_safe = False

    @staticmethod
    def _unavailable() -> CredentialProtectionUnavailable:
        return CredentialProtectionUnavailable(
            "credential persistence requires an authenticated OS, keychain, or KMS SecretCodec"
        )

    def encode(self, plaintext: bytes, *, associated_data: bytes) -> bytes:
        del plaintext, associated_data
        raise self._unavailable()

    def decode(self, encoded: bytes, *, associated_data: bytes) -> bytes:
        del encoded, associated_data
        raise self._unavailable()


def _default_secret_codec(*, platform_name: str | None = None) -> SecretCodec:
    selected_platform = os.name if platform_name is None else platform_name
    if selected_platform == "nt":
        return WindowsDpapiSecretCodec()
    # Runtime construction and credential-free in-process MCP remain usable
    # in Linux cleanrooms.  The first secret read/write fails explicitly.
    return _UnavailableSecretCodec()


@runtime_checkable
class CredentialVault(Protocol):
    def put(
        self,
        reference: CredentialReference,
        secrets: Mapping[str, str | bytes | bytearray | memoryview | SecretValue],
        *,
        public_attributes: Mapping[str, Any] | None = None,
        expires_at: str = "",
        expected_revision: int | None = None,
    ) -> CredentialMetadata: ...

    def get(self, reference: CredentialReference) -> CredentialEnvelope: ...

    def metadata(self, reference: CredentialReference) -> CredentialMetadata: ...

    def delete(self, reference: CredentialReference, *, expected_revision: int | None = None) -> bool: ...

    def exists(self, reference: CredentialReference) -> bool: ...

    def external_version(self, reference: CredentialReference) -> str: ...


_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}


def _process_lock(key: str) -> threading.RLock:
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.RLock())


class _ExclusiveFileLease:
    """Small O_EXCL lease protecting mutations across local processes."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float],
        timeout: float,
        stale_after: float,
    ) -> None:
        self.path = path
        self.clock = clock
        self.timeout = timeout
        self.stale_after = stale_after
        self._owned = False

    def _try_reap_stale(self) -> None:
        try:
            age = self.clock() - self.path.stat().st_mtime
        except OSError:
            return
        if age <= self.stale_after:
            return
        try:
            self.path.unlink()
        except OSError:
            return

    def acquire(self) -> None:
        deadline = time.monotonic() + self.timeout
        payload = _canonical_json_bytes({"pid": os.getpid(), "created_at": self.clock()})
        while True:
            try:
                descriptor = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                self._try_reap_stale()
                if time.monotonic() >= deadline:
                    raise CredentialLeaseTimeout("credential mutation lease timed out")
                time.sleep(0.01)
                continue
            except OSError as exc:
                raise CredentialError(f"credential lease create failed: {type(exc).__name__}") from exc
            try:
                os.write(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._owned = True
            return

    def release(self) -> None:
        if not self._owned:
            return
        self._owned = False
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise CredentialError(f"credential lease cleanup failed: {type(exc).__name__}") from exc

    def __enter__(self) -> "_ExclusiveFileLease":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class FileCredentialVault:
    """Atomic file-backed credential vault with opaque file names.

    The vault is intentionally independent from the ordinary MCP state store.
    Its ``safe_snapshot`` returns metadata only, while ``get`` is an explicit
    custody operation.  A custom ``SecretCodec`` can bind the same interface
    to platform encryption.
    """

    SCHEMA_VERSION = 1

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        codec: SecretCodec | None = None,
        clock: Callable[[], float] = time.time,
        lease_timeout: float = 5.0,
        stale_lease_after: float = 120.0,
        strict_permissions: bool = False,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        # Never silently downgrade credential protection.  Tests and local
        # development that intentionally store plaintext must opt in with an
        # explicit ``IdentitySecretCodec()`` instance.
        self.codec = codec if codec is not None else _default_secret_codec()
        self.clock = clock
        self.lease_timeout = max(0.05, float(lease_timeout))
        self.stale_lease_after = max(self.lease_timeout, float(stale_lease_after))
        self.strict_permissions = bool(strict_permissions)
        self._lock_root = self.root / ".locks"
        self._ensure_directories()

    def _ensure_directories(self) -> None:
        for path in (self.root, self._lock_root):
            path.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(path, stat.S_IRWXU)
            except OSError:
                if self.strict_permissions and os.name != "nt":
                    raise CredentialPermissionError("unable to restrict credential directory permissions")

    def _path(self, reference: CredentialReference) -> Path:
        return self.root / reference.storage_name

    def _lease_path(self, reference: CredentialReference) -> Path:
        return self._lock_root / (reference.storage_name + ".lock")

    @contextmanager
    def _mutation_guard(self, reference: CredentialReference) -> Iterator[None]:
        key = str(self._path(reference))
        with _process_lock(key):
            with _ExclusiveFileLease(
                self._lease_path(reference),
                clock=self.clock,
                timeout=self.lease_timeout,
                stale_after=self.stale_lease_after,
            ):
                yield

    @staticmethod
    def _associated_data(reference: CredentialReference) -> bytes:
        return reference.opaque_id.encode("utf-8")

    def _atomic_write(self, destination: Path, payload: bytes) -> None:
        temporary = destination.with_name(
            f".{destination.name}.tmp-{os.getpid()}-{random_secrets.token_hex(8)}"
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(
                str(temporary),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                stat.S_IRUSR | stat.S_IWUSR,
            )
            view = memoryview(payload)
            written = 0
            while written < len(view):
                count = os.write(descriptor, view[written:])
                if count <= 0:
                    raise OSError("credential write made no progress")
                written += count
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            try:
                os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                if self.strict_permissions and os.name != "nt":
                    raise CredentialPermissionError("unable to restrict credential file permissions")
            os.replace(temporary, destination)
            self._fsync_directory()
        except Exception:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _fsync_directory(self) -> None:
        if os.name == "nt":
            return
        descriptor: int | None = None
        try:
            descriptor = os.open(str(self.root), os.O_RDONLY)
            os.fsync(descriptor)
        except OSError:
            return
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _file_version(self, path: Path) -> str:
        try:
            info = path.stat()
        except FileNotFoundError:
            return ""
        return f"stat:{info.st_mtime_ns}:{info.st_size}"

    def _load_document(self, reference: CredentialReference) -> dict[str, Any]:
        path = self._path(reference)
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise CredentialNotFound(reference.opaque_id) from exc
        except OSError as exc:
            raise CredentialError(f"credential read failed: {type(exc).__name__}") from exc
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialCorrupt("credential document is not valid JSON") from exc
        if not isinstance(document, dict):
            raise CredentialCorrupt("credential document must be an object")
        if document.get("schema_version") != self.SCHEMA_VERSION:
            raise CredentialCorrupt("unsupported credential schema version")
        if document.get("credential_reference") != reference.opaque_id:
            raise CredentialCorrupt("credential reference mismatch")
        if document.get("codec") != self.codec.codec_id:
            raise CredentialCorrupt("credential codec mismatch")
        return document

    def _metadata_from_document(
        self,
        reference: CredentialReference,
        document: Mapping[str, Any],
    ) -> CredentialMetadata:
        try:
            revision = int(document["revision"])
            created_at = str(document["created_at"])
            updated_at = str(document["updated_at"])
            expires_at = str(document.get("expires_at") or "")
            content_digest = str(document["payload_digest"])
            secret_fields = tuple(sorted(str(item) for item in document.get("secret_fields", ())))
            public = validate_public_attributes(document.get("public_attributes") or {})
        except (KeyError, TypeError, ValueError) as exc:
            raise CredentialCorrupt("credential metadata is invalid") from exc
        if revision <= 0:
            raise CredentialCorrupt("credential revision must be positive")
        if not content_digest.startswith("sha256:"):
            raise CredentialCorrupt("credential payload digest is invalid")
        return CredentialMetadata(
            reference=reference,
            revision=revision,
            created_at=created_at,
            updated_at=updated_at,
            expires_at=expires_at,
            content_digest=content_digest,
            secret_fields=secret_fields,
            public_attributes=public,
            external_version=self._file_version(self._path(reference)),
        )

    def _decode_secrets(self, reference: CredentialReference, document: Mapping[str, Any]) -> dict[str, SecretValue]:
        encoded = document.get("payload")
        if not isinstance(encoded, str):
            raise CredentialCorrupt("credential payload is missing")
        try:
            protected = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise CredentialCorrupt("credential payload is not valid base64") from exc
        if _sha256(protected) != document.get("payload_digest"):
            raise CredentialCorrupt("credential payload integrity check failed")
        try:
            plaintext = self.codec.decode(protected, associated_data=self._associated_data(reference))
            values = json.loads(plaintext.decode("utf-8"))
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialCorrupt(f"credential payload decode failed: {type(exc).__name__}") from exc
        if not isinstance(values, dict):
            raise CredentialCorrupt("credential secret payload must be an object")
        output: dict[str, SecretValue] = {}
        try:
            for raw_name, raw_value in values.items():
                name = str(raw_name)
                if not name or len(name) > 256:
                    raise CredentialCorrupt("credential field name is invalid")
                if not isinstance(raw_value, str):
                    raise CredentialCorrupt("credential secret values must be text")
                output[name] = SecretValue(raw_value)
        except Exception:
            for value in output.values():
                value.destroy()
            raise
        expected = tuple(sorted(str(item) for item in document.get("secret_fields", ())))
        if tuple(sorted(output)) != expected:
            for value in output.values():
                value.destroy()
            raise CredentialCorrupt("credential field manifest mismatch")
        return output

    def put(
        self,
        reference: CredentialReference,
        secrets: Mapping[str, str | bytes | bytearray | memoryview | SecretValue],
        *,
        public_attributes: Mapping[str, Any] | None = None,
        expires_at: str = "",
        expected_revision: int | None = None,
    ) -> CredentialMetadata:
        if not secrets:
            raise CredentialError("at least one credential secret is required")
        normalized: dict[str, str] = {}
        for raw_name, raw_value in secrets.items():
            name = str(raw_name).strip()
            if not name or len(name) > 256:
                raise CredentialError("credential field name is invalid")
            selected = coerce_secret(raw_value)
            try:
                normalized[name] = selected.reveal_text()
            finally:
                selected.destroy()
        public = validate_public_attributes(public_attributes)
        if expires_at and _parse_timestamp(expires_at) is None:
            raise CredentialError("expires_at must be an ISO-8601 timestamp")
        with self._mutation_guard(reference):
            path = self._path(reference)
            previous: dict[str, Any] | None
            try:
                previous = self._load_document(reference)
            except CredentialNotFound:
                previous = None
            current_revision = int(previous["revision"]) if previous is not None else 0
            if expected_revision is not None and current_revision != expected_revision:
                raise CredentialConflict(
                    f"credential revision conflict: expected {expected_revision}, current {current_revision}"
                )
            now = _utc_timestamp(self.clock)
            plaintext = _canonical_json_bytes(normalized)
            protected = self.codec.encode(plaintext, associated_data=self._associated_data(reference))
            document = {
                "schema_version": self.SCHEMA_VERSION,
                "credential_reference": reference.opaque_id,
                "codec": self.codec.codec_id,
                "revision": current_revision + 1,
                "created_at": str(previous.get("created_at")) if previous else now,
                "updated_at": now,
                "expires_at": expires_at,
                "payload": base64.b64encode(protected).decode("ascii"),
                "payload_digest": _sha256(protected),
                "secret_fields": sorted(normalized),
                "public_attributes": public,
            }
            self._atomic_write(path, _canonical_json_bytes(document))
            reloaded = self._load_document(reference)
            return self._metadata_from_document(reference, reloaded)

    def get(self, reference: CredentialReference) -> CredentialEnvelope:
        with _process_lock(str(self._path(reference))):
            document = self._load_document(reference)
            metadata = self._metadata_from_document(reference, document)
            values = self._decode_secrets(reference, document)
            return CredentialEnvelope(metadata, values)

    def metadata(self, reference: CredentialReference) -> CredentialMetadata:
        with _process_lock(str(self._path(reference))):
            document = self._load_document(reference)
            return self._metadata_from_document(reference, document)

    def exists(self, reference: CredentialReference) -> bool:
        return self._path(reference).is_file()

    def external_version(self, reference: CredentialReference) -> str:
        return self._file_version(self._path(reference))

    def delete(self, reference: CredentialReference, *, expected_revision: int | None = None) -> bool:
        with self._mutation_guard(reference):
            path = self._path(reference)
            try:
                document = self._load_document(reference)
            except CredentialNotFound:
                return False
            current_revision = int(document["revision"])
            if expected_revision is not None and expected_revision != current_revision:
                raise CredentialConflict(
                    f"credential revision conflict: expected {expected_revision}, current {current_revision}"
                )
            try:
                path.unlink()
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise CredentialError(f"credential deletion failed: {type(exc).__name__}") from exc
            self._fsync_directory()
            return True

    def update(
        self,
        reference: CredentialReference,
        transform: Callable[[dict[str, str], CredentialMetadata], Mapping[str, str | bytes | SecretValue]],
        *,
        public_attributes: Mapping[str, Any] | None = None,
        expires_at: str | None = None,
    ) -> CredentialMetadata:
        with self.get(reference) as envelope:
            current = {name: envelope.reveal_text(name) for name in envelope.field_names()}
            changed = transform(dict(current), envelope.metadata)
            selected_public = envelope.metadata.public_attributes if public_attributes is None else public_attributes
            selected_expiry = envelope.metadata.expires_at if expires_at is None else expires_at
            return self.put(
                reference,
                changed,
                public_attributes=selected_public,
                expires_at=selected_expiry,
                expected_revision=envelope.metadata.revision,
            )

    def list_metadata(self, *, kind: CredentialKind | None = None) -> tuple[CredentialMetadata, ...]:
        records: list[CredentialMetadata] = []
        for path in sorted(self.root.glob("*.credential")):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            opaque = document.get("credential_reference") if isinstance(document, dict) else None
            if not isinstance(opaque, str):
                continue
            # File names are deliberately irreversible.  A safe inventory can
            # only include entries whose reference was supplied by a caller;
            # therefore generic list returns no reconstructed identities.
            # The document is still scanned to detect malformed files during
            # permission audits without exposing embedded identifiers.
            del opaque
        del kind
        return tuple(records)

    def purge_expired(self, references: Sequence[CredentialReference], *, now: float | None = None) -> int:
        selected_now = self.clock() if now is None else now
        removed = 0
        for reference in references:
            try:
                metadata = self.metadata(reference)
            except CredentialNotFound:
                continue
            if metadata.is_expired(selected_now) and self.delete(reference, expected_revision=metadata.revision):
                removed += 1
        return removed

    def permission_report(self) -> dict[str, PublicValue]:
        issues: list[str] = []
        if os.name != "nt":
            for directory in (self.root, self._lock_root):
                try:
                    mode = stat.S_IMODE(directory.stat().st_mode)
                except OSError:
                    issues.append("directory_unreadable")
                    continue
                if mode & (stat.S_IRWXG | stat.S_IRWXO):
                    issues.append("directory_permissions_too_broad")
            for path in self.root.glob("*.credential"):
                try:
                    mode = stat.S_IMODE(path.stat().st_mode)
                except OSError:
                    issues.append("credential_unreadable")
                    continue
                if mode & (stat.S_IRWXG | stat.S_IRWXO):
                    issues.append("credential_permissions_too_broad")
        report: dict[str, PublicValue] = {
            "backend": "file",
            "root_digest": _sha256(str(self.root).encode("utf-8")),
            "codec": self.codec.codec_id,
            "issues": sorted(set(issues)),
            "strict": self.strict_permissions,
            "healthy": not issues,
        }
        if issues and self.strict_permissions:
            raise CredentialPermissionError("credential vault permissions are too broad")
        return report

    def safe_snapshot(self, references: Sequence[CredentialReference]) -> dict[str, PublicValue]:
        records: list[PublicValue] = []
        for reference in references:
            try:
                records.append(self.metadata(reference).safe_dict())
            except CredentialNotFound:
                records.append({**reference.safe_dict(), "missing": True, "raw_secret_included": False})
        return {
            "backend": "file",
            "records": records,
            "raw_secret_included": False,
        }


class MemoryCredentialVault:
    """In-process vault for deterministic embedding and unit tests.

    It obeys the same redaction and revision contracts as the file backend but
    is intentionally non-durable.  Production callers should use it only when
    a process-local credential provider already owns persistence.
    """

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._lock = threading.RLock()
        self._records: dict[str, tuple[CredentialMetadata, dict[str, SecretValue]]] = {}
        self._generation = 0

    def put(
        self,
        reference: CredentialReference,
        secrets: Mapping[str, str | bytes | bytearray | memoryview | SecretValue],
        *,
        public_attributes: Mapping[str, Any] | None = None,
        expires_at: str = "",
        expected_revision: int | None = None,
    ) -> CredentialMetadata:
        if not secrets:
            raise CredentialError("at least one credential secret is required")
        public = validate_public_attributes(public_attributes)
        if expires_at and _parse_timestamp(expires_at) is None:
            raise CredentialError("expires_at must be an ISO-8601 timestamp")
        with self._lock:
            previous = self._records.get(reference.opaque_id)
            current_revision = previous[0].revision if previous else 0
            if expected_revision is not None and current_revision != expected_revision:
                raise CredentialConflict(
                    f"credential revision conflict: expected {expected_revision}, current {current_revision}"
                )
            now = _utc_timestamp(self.clock)
            values = {str(name): coerce_secret(value) for name, value in secrets.items()}
            payload_digest = _sha256(
                _canonical_json_bytes({name: value.reveal_text() for name, value in values.items()})
            )
            self._generation += 1
            metadata = CredentialMetadata(
                reference=reference,
                revision=current_revision + 1,
                created_at=previous[0].created_at if previous else now,
                updated_at=now,
                expires_at=expires_at,
                content_digest=payload_digest,
                secret_fields=tuple(sorted(values)),
                public_attributes=public,
                external_version=f"memory:{self._generation}",
            )
            if previous:
                for value in previous[1].values():
                    value.destroy()
            self._records[reference.opaque_id] = (metadata, values)
            return metadata

    def get(self, reference: CredentialReference) -> CredentialEnvelope:
        with self._lock:
            record = self._records.get(reference.opaque_id)
            if record is None:
                raise CredentialNotFound(reference.opaque_id)
            metadata, values = record
            copied = {name: value.copy() for name, value in values.items()}
            return CredentialEnvelope(metadata, copied)

    def metadata(self, reference: CredentialReference) -> CredentialMetadata:
        with self._lock:
            record = self._records.get(reference.opaque_id)
            if record is None:
                raise CredentialNotFound(reference.opaque_id)
            return record[0]

    def exists(self, reference: CredentialReference) -> bool:
        with self._lock:
            return reference.opaque_id in self._records

    def external_version(self, reference: CredentialReference) -> str:
        try:
            return self.metadata(reference).external_version
        except CredentialNotFound:
            return ""

    def delete(self, reference: CredentialReference, *, expected_revision: int | None = None) -> bool:
        with self._lock:
            record = self._records.get(reference.opaque_id)
            if record is None:
                return False
            if expected_revision is not None and record[0].revision != expected_revision:
                raise CredentialConflict(
                    f"credential revision conflict: expected {expected_revision}, current {record[0].revision}"
                )
            self._records.pop(reference.opaque_id, None)
            for value in record[1].values():
                value.destroy()
            self._generation += 1
            return True

    def safe_snapshot(self) -> dict[str, PublicValue]:
        with self._lock:
            return {
                "backend": "memory",
                "records": [metadata.safe_dict() for metadata, _ in self._records.values()],
                "raw_secret_included": False,
            }


class VaultCredentialProvider:
    """Namespaced facade used by :class:`McpAuthRuntime`."""

    def __init__(self, vault: CredentialVault, *, namespace: str = "mcp") -> None:
        if not namespace or len(namespace) > 128:
            raise CredentialError("credential namespace is invalid")
        self.vault = vault
        self.namespace = namespace

    def reference(
        self,
        server_id: str,
        config_fingerprint: str,
        kind: CredentialKind = CredentialKind.OAUTH,
    ) -> CredentialReference:
        return CredentialReference(self.namespace, server_id, config_fingerprint, kind)

    def save(
        self,
        server_id: str,
        config_fingerprint: str,
        kind: CredentialKind,
        secrets: Mapping[str, str | bytes | SecretValue],
        *,
        public_attributes: Mapping[str, Any] | None = None,
        expires_at: str = "",
        expected_revision: int | None = None,
    ) -> CredentialMetadata:
        return self.vault.put(
            self.reference(server_id, config_fingerprint, kind),
            secrets,
            public_attributes=public_attributes,
            expires_at=expires_at,
            expected_revision=expected_revision,
        )

    def load(
        self,
        server_id: str,
        config_fingerprint: str,
        kind: CredentialKind = CredentialKind.OAUTH,
    ) -> CredentialEnvelope:
        return self.vault.get(self.reference(server_id, config_fingerprint, kind))

    def delete(
        self,
        server_id: str,
        config_fingerprint: str,
        kind: CredentialKind = CredentialKind.OAUTH,
    ) -> bool:
        return self.vault.delete(self.reference(server_id, config_fingerprint, kind))

    def external_version(
        self,
        server_id: str,
        config_fingerprint: str,
        kind: CredentialKind = CredentialKind.OAUTH,
    ) -> str:
        return self.vault.external_version(self.reference(server_id, config_fingerprint, kind))


__all__ = [
    "CredentialConflict",
    "CredentialCorrupt",
    "CredentialEnvelope",
    "CredentialError",
    "CredentialKind",
    "CredentialLeaseTimeout",
    "CredentialMetadata",
    "CredentialNotFound",
    "CredentialPermissionError",
    "CredentialProtectionError",
    "CredentialProtectionUnavailable",
    "CredentialReference",
    "CredentialVault",
    "FileCredentialVault",
    "IdentitySecretCodec",
    "MemoryCredentialVault",
    "SecretCodec",
    "SecretValue",
    "VaultCredentialProvider",
    "WindowsDpapiSecretCodec",
    "coerce_secret",
    "is_sensitive_name",
    "redact_public_value",
    "validate_public_attributes",
]
