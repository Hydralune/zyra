from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from .configuration import ConfigurationError, ResolvedConfiguration


_ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_CREDENTIAL_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class CredentialStatus(StrEnum):
    PRESENT = "present"
    MISSING = "missing"
    EMPTY = "empty"
    REVOKED = "revoked"
    EXPIRED = "expired"


class CredentialError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        credential_id: str = "",
        environment_name: str = "",
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.credential_id = credential_id
        self.environment_name = environment_name
        self.retryable = retryable
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.credential-error/v1",
            "code": self.code,
            "message": str(self),
            "credential_id": self.credential_id,
            "environment_name": self.environment_name,
            "retryable": self.retryable,
            "details": {
                key: value
                for key, value in self.details.items()
                if not _sensitive_name(str(key))
            },
        }


@dataclass(frozen=True, slots=True)
class CredentialRequirement:
    credential_id: str
    environment_name: str
    required: bool
    feature: str

    def __post_init__(self) -> None:
        if not _CREDENTIAL_ID.fullmatch(self.credential_id):
            raise CredentialError(
                "credential_id_invalid",
                f"invalid credential id: {self.credential_id!r}",
                credential_id=self.credential_id,
            )
        if not _ENVIRONMENT_NAME.fullmatch(self.environment_name):
            raise CredentialError(
                "credential_environment_invalid",
                f"invalid credential environment variable: {self.environment_name!r}",
                credential_id=self.credential_id,
                environment_name=self.environment_name,
            )


@dataclass(frozen=True, slots=True)
class CredentialPresence:
    credential_id: str
    environment_name: str
    required: bool
    feature: str
    status: CredentialStatus
    fingerprint: str
    length: int
    observed_at_ns: int

    @property
    def available(self) -> bool:
        return self.status is CredentialStatus.PRESENT

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.credential-presence/v1",
            "credential_id": self.credential_id,
            "environment_name": self.environment_name,
            "required": self.required,
            "feature": self.feature,
            "status": self.status.value,
            "available": self.available,
            "fingerprint": self.fingerprint,
            "length": self.length,
            "observed_at_ns": self.observed_at_ns,
            "material_exposed": False,
        }


@dataclass(frozen=True, slots=True)
class CredentialPresenceReport:
    configuration_digest: str
    entries: tuple[CredentialPresence, ...]
    observed_at_ns: int

    @property
    def ready(self) -> bool:
        return all(item.available or not item.required for item in self.entries)

    @property
    def blockers(self) -> tuple[str, ...]:
        return tuple(
            item.credential_id
            for item in self.entries
            if item.required and not item.available
        )

    def require_ready(self) -> None:
        blockers = self.blockers
        if blockers:
            raise CredentialError(
                "required_credentials_missing",
                "required credential bindings are unavailable",
                retryable=True,
                details={"credential_ids": list(blockers)},
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.credential-presence-report/v1",
            "configuration_digest": self.configuration_digest,
            "ready": self.ready,
            "blockers": list(self.blockers),
            "entries": [item.to_dict() for item in self.entries],
            "observed_at_ns": self.observed_at_ns,
            "credential_material_persisted": False,
        }


@dataclass(frozen=True, slots=True)
class CredentialLease:
    lease_id: str
    credential_id: str
    version: int
    fingerprint: str
    issued_at_ns: int
    expires_at_ns: int
    generation: int

    @property
    def expired(self) -> bool:
        return time.time_ns() >= self.expires_at_ns

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.credential-lease/v1",
            "lease_id": self.lease_id,
            "credential_id": self.credential_id,
            "version": self.version,
            "fingerprint": self.fingerprint,
            "issued_at_ns": self.issued_at_ns,
            "expires_at_ns": self.expires_at_ns,
            "generation": self.generation,
            "material_exposed": False,
        }


@dataclass(slots=True)
class _SecretSlot:
    credential_id: str
    environment_name: str
    value: bytearray
    fingerprint: str
    version: int
    generation: int
    loaded_at_ns: int
    revoked: bool = False

    def erase(self) -> None:
        for index in range(len(self.value)):
            self.value[index] = 0
        self.revoked = True
        self.generation += 1


class CredentialPresenceRuntime:
    _FEATURE_REQUIREMENTS = MappingProxyType(
        {
            "provider": "provider_dispatch",
            "mcp": "mcp",
            "edge": "edge_worker",
        }
    )

    def __init__(
        self,
        configuration: ResolvedConfiguration,
        *,
        environ: Mapping[str, str] | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.configuration = configuration
        self._environment = dict(os.environ if environ is None else environ)
        self._clock_ns = clock_ns

    def requirements(self) -> tuple[CredentialRequirement, ...]:
        required = self.configuration.get("credentials.required", {})
        optional = self.configuration.get("credentials.optional", {})
        if not isinstance(required, Mapping) or not isinstance(optional, Mapping):
            raise ConfigurationError(
                "credential_configuration_invalid",
                "credential bindings must be mappings",
                path="credentials",
            )
        features = self.configuration.get("features", {})
        result: list[CredentialRequirement] = []
        seen: set[str] = set()
        for required_flag, source in ((True, required), (False, optional)):
            for credential_id, environment_name in sorted(source.items()):
                if credential_id in seen:
                    raise CredentialError(
                        "credential_binding_duplicate",
                        f"credential binding is both required and optional: {credential_id}",
                        credential_id=credential_id,
                    )
                seen.add(credential_id)
                feature = self._FEATURE_REQUIREMENTS.get(credential_id, "")
                enabled = bool(features.get(feature, False)) if feature else required_flag
                result.append(
                    CredentialRequirement(
                        credential_id=str(credential_id),
                        environment_name=str(environment_name),
                        required=bool(required_flag and enabled),
                        feature=feature,
                    )
                )
        return tuple(result)

    def inspect(self) -> CredentialPresenceReport:
        observed_at_ns = self._clock_ns()
        entries: list[CredentialPresence] = []
        for requirement in self.requirements():
            raw = self._environment.get(requirement.environment_name)
            if raw is None:
                status = CredentialStatus.MISSING
                fingerprint = ""
                length = 0
            elif not isinstance(raw, str) or not raw.strip():
                status = CredentialStatus.EMPTY
                fingerprint = ""
                length = 0
            else:
                encoded = raw.encode("utf-8")
                status = CredentialStatus.PRESENT
                fingerprint = _fingerprint(encoded)
                length = len(encoded)
            entries.append(
                CredentialPresence(
                    credential_id=requirement.credential_id,
                    environment_name=requirement.environment_name,
                    required=requirement.required,
                    feature=requirement.feature,
                    status=status,
                    fingerprint=fingerprint,
                    length=length,
                    observed_at_ns=observed_at_ns,
                )
            )
        return CredentialPresenceReport(
            configuration_digest=self.configuration.digest,
            entries=tuple(entries),
            observed_at_ns=observed_at_ns,
        )


class EnvironmentSecretProvisioner:
    def __init__(
        self,
        requirements: tuple[CredentialRequirement, ...],
        *,
        environ: Mapping[str, str] | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self._requirements = {item.credential_id: item for item in requirements}
        self._environment = dict(os.environ if environ is None else environ)
        self._clock_ns = clock_ns
        self._lock = threading.RLock()
        self._slots: dict[str, _SecretSlot] = {}
        self._leases: dict[str, CredentialLease] = {}
        self._generation = 1
        self._closed = False

    def provision(self, credential_id: str) -> CredentialPresence:
        requirement = self._require_requirement(credential_id)
        raw = self._environment.get(requirement.environment_name)
        now = self._clock_ns()
        if raw is None:
            raise CredentialError(
                "credential_environment_missing",
                f"credential environment variable is missing: {requirement.environment_name}",
                credential_id=credential_id,
                environment_name=requirement.environment_name,
                retryable=True,
            )
        if not isinstance(raw, str) or not raw.strip():
            raise CredentialError(
                "credential_environment_empty",
                f"credential environment variable is empty: {requirement.environment_name}",
                credential_id=credential_id,
                environment_name=requirement.environment_name,
                retryable=True,
            )
        encoded = bytearray(raw.encode("utf-8"))
        fingerprint = _fingerprint(encoded)
        with self._lock:
            self._assert_open()
            previous = self._slots.get(credential_id)
            if previous is not None and not previous.revoked:
                if hmac.compare_digest(previous.fingerprint, fingerprint):
                    version = previous.version
                else:
                    previous.erase()
                    version = previous.version + 1
            else:
                version = 1 if previous is None else previous.version + 1
            self._generation += 1
            self._slots[credential_id] = _SecretSlot(
                credential_id=credential_id,
                environment_name=requirement.environment_name,
                value=encoded,
                fingerprint=fingerprint,
                version=version,
                generation=self._generation,
                loaded_at_ns=now,
            )
            self._invalidate_leases(credential_id)
        return CredentialPresence(
            credential_id=credential_id,
            environment_name=requirement.environment_name,
            required=requirement.required,
            feature=requirement.feature,
            status=CredentialStatus.PRESENT,
            fingerprint=fingerprint,
            length=len(encoded),
            observed_at_ns=now,
        )

    def provision_required(self) -> tuple[CredentialPresence, ...]:
        results: list[CredentialPresence] = []
        failures: list[CredentialError] = []
        for requirement in self._requirements.values():
            if not requirement.required:
                continue
            try:
                results.append(self.provision(requirement.credential_id))
            except CredentialError as error:
                failures.append(error)
        if failures:
            for result in results:
                self.revoke(result.credential_id)
            raise CredentialError(
                "credential_provision_batch_failed",
                "one or more required credentials could not be provisioned",
                retryable=any(item.retryable for item in failures),
                details={
                    "failures": [item.to_dict() for item in failures],
                },
            )
        return tuple(results)

    def issue_lease(
        self,
        credential_id: str,
        *,
        ttl_seconds: float = 60.0,
    ) -> CredentialLease:
        if ttl_seconds <= 0 or ttl_seconds > 3600:
            raise CredentialError(
                "credential_lease_ttl_invalid",
                "credential lease ttl must be in (0, 3600]",
                credential_id=credential_id,
            )
        now = self._clock_ns()
        with self._lock:
            self._assert_open()
            slot = self._slots.get(credential_id)
            if slot is None or slot.revoked:
                raise CredentialError(
                    "credential_not_provisioned",
                    f"credential is not provisioned: {credential_id}",
                    credential_id=credential_id,
                    retryable=True,
                )
            lease = CredentialLease(
                lease_id="credential-lease-" + secrets.token_urlsafe(18),
                credential_id=credential_id,
                version=slot.version,
                fingerprint=slot.fingerprint,
                issued_at_ns=now,
                expires_at_ns=now + int(ttl_seconds * 1_000_000_000),
                generation=slot.generation,
            )
            self._leases[lease.lease_id] = lease
            return lease

    def use(
        self,
        lease: CredentialLease,
        callback: Callable[[memoryview], Any],
    ) -> Any:
        with self._lock:
            self._assert_open()
            registered = self._leases.get(lease.lease_id)
            if registered != lease:
                raise CredentialError(
                    "credential_lease_unknown",
                    "credential lease is unknown or was invalidated",
                    credential_id=lease.credential_id,
                )
            now = self._clock_ns()
            if now >= lease.expires_at_ns:
                self._leases.pop(lease.lease_id, None)
                raise CredentialError(
                    "credential_lease_expired",
                    "credential lease expired",
                    credential_id=lease.credential_id,
                    retryable=True,
                )
            slot = self._slots.get(lease.credential_id)
            if (
                slot is None
                or slot.revoked
                or slot.version != lease.version
                or slot.generation != lease.generation
                or not hmac.compare_digest(slot.fingerprint, lease.fingerprint)
            ):
                self._leases.pop(lease.lease_id, None)
                raise CredentialError(
                    "credential_lease_stale",
                    "credential lease does not match the current provisioned generation",
                    credential_id=lease.credential_id,
                    retryable=True,
                )
            view = memoryview(slot.value).toreadonly()
            try:
                return callback(view)
            finally:
                view.release()

    def rotate(self, credential_id: str, value: str) -> CredentialPresence:
        requirement = self._require_requirement(credential_id)
        if not isinstance(value, str) or not value.strip():
            raise CredentialError(
                "credential_rotation_value_invalid",
                "rotated credential must be non-empty text",
                credential_id=credential_id,
            )
        environment = dict(self._environment)
        environment[requirement.environment_name] = value
        self._environment = environment
        return self.provision(credential_id)

    def revoke(self, credential_id: str) -> bool:
        with self._lock:
            slot = self._slots.get(credential_id)
            if slot is None or slot.revoked:
                return False
            slot.erase()
            self._generation += 1
            self._invalidate_leases(credential_id)
            return True

    def status(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            result: list[dict[str, Any]] = []
            for credential_id, requirement in sorted(self._requirements.items()):
                slot = self._slots.get(credential_id)
                result.append(
                    {
                        "credential_id": credential_id,
                        "environment_name": requirement.environment_name,
                        "required": requirement.required,
                        "feature": requirement.feature,
                        "provisioned": slot is not None and not slot.revoked,
                        "revoked": bool(slot.revoked) if slot is not None else False,
                        "version": slot.version if slot is not None else 0,
                        "generation": slot.generation if slot is not None else 0,
                        "fingerprint": slot.fingerprint if slot is not None else "",
                        "material_exposed": False,
                    }
                )
            return tuple(result)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            for slot in self._slots.values():
                slot.erase()
            self._leases.clear()
            self._generation += 1
            self._closed = True

    def _require_requirement(self, credential_id: str) -> CredentialRequirement:
        requirement = self._requirements.get(credential_id)
        if requirement is None:
            raise CredentialError(
                "credential_binding_unknown",
                f"credential binding is not configured: {credential_id}",
                credential_id=credential_id,
            )
        return requirement

    def _invalidate_leases(self, credential_id: str) -> None:
        invalid = [
            lease_id
            for lease_id, lease in self._leases.items()
            if lease.credential_id == credential_id
        ]
        for lease_id in invalid:
            self._leases.pop(lease_id, None)

    def _assert_open(self) -> None:
        if self._closed:
            raise CredentialError(
                "credential_provisioner_closed",
                "credential provisioner is closed",
            )


def _fingerprint(value: bytes | bytearray) -> str:
    return "sha256:" + hashlib.sha256(bytes(value)).hexdigest()


def _sensitive_name(value: str) -> bool:
    normalized = value.casefold()
    return any(
        token in normalized
        for token in (
            "secret",
            "password",
            "token",
            "authorization",
            "private_key",
            "api_key",
            "material",
            "value",
        )
    )
