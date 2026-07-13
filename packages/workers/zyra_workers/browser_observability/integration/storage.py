from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from zyra_core import ArtifactKind, ArtifactRef, EventRecord, EventType
from zyra_runtime import LocalArtifactStore

from ...browser_session.errors import BrowserProfileCorrupt, BrowserProfileError
from ...browser_session.profile_store import (
    BrowserProfileStore,
    BrowserStorageStateReceipt,
)
from ..models import (
    HealthStatus,
    ObservationScope,
    Severity,
    SignalKind,
    WatchdogName,
    WatchdogSignal,
    canonical_json,
    digest_value,
    new_observation_id,
    utc_now,
)
from .contracts import (
    BrowserIntegrationOutput,
    EvidenceSource,
    EvidenceTerminalState,
    RuntimeEvidenceEnvelope,
)


class StorageStateIntegrationError(BrowserProfileError):
    code = "browser_storage_state_integration_error"


class StorageOperation(StrEnum):
    CAPTURE = "capture"
    RESTORE = "restore"
    LOAD = "load"
    SAVE = "save"


class BrowserStorageCdpPort(Protocol):
    def send(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        cdp_session_id: str = "",
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class StorageIntegrationPolicy:
    maximum_cookies: int = 5_000
    maximum_origins: int = 1_000
    maximum_items_per_origin: int = 10_000
    publish_manifest: bool = True
    fail_on_double_corruption: bool = True
    require_restore_readback: bool = True

    def __post_init__(self) -> None:
        if self.maximum_cookies < 0 or self.maximum_origins < 0:
            raise ValueError("storage integration limits must be non-negative")
        if self.maximum_items_per_origin < 0:
            raise ValueError("origin item limit must be non-negative")


@dataclass(frozen=True, slots=True)
class StorageOperationReceipt:
    scope: ObservationScope
    profile_id: str
    operation: StorageOperation
    ok: bool
    storage_receipt: BrowserStorageStateReceipt | None = None
    receipt_id: str = field(
        default_factory=lambda: new_observation_id("browser-storage-operation")
    )
    source_event_ids: tuple[str, ...] = ()
    artifact_id: str = ""
    cookies_applied: int = 0
    origins_applied: int = 0
    restored_from_backup: bool = False
    primary_repaired: bool = False
    error: str = ""
    created_at: str = field(default_factory=utc_now)

    @property
    def digest(self) -> str:
        return digest_value(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": "zyra.browser-observability.storage-operation.v1",
            "receipt_id": self.receipt_id,
            "scope": self.scope.to_dict(),
            "profile_id": self.profile_id,
            "operation": str(self.operation),
            "ok": self.ok,
            "storage_receipt": (
                self.storage_receipt.to_dict() if self.storage_receipt else None
            ),
            "source_event_ids": list(self.source_event_ids),
            "artifact_id": self.artifact_id,
            "cookies_applied": self.cookies_applied,
            "origins_applied": self.origins_applied,
            "restored_from_backup": self.restored_from_backup,
            "primary_repaired": self.primary_repaired,
            "error": self.error,
            "created_at": self.created_at,
            "persistence_owner": "M1-04A/BrowserProfileStore",
            "history_owner": "M1-04D/BrowserHistoryStore",
        }
        if include_digest:
            value["digest"] = digest_value(value)
        return value

    def evidence(self) -> RuntimeEvidenceEnvelope:
        return RuntimeEvidenceEnvelope(
            scope=self.scope,
            source=EvidenceSource.BROWSER_STORAGE,
            event_type=f"storage_{self.operation}",
            payload=self.to_dict(),
            evidence_id=self.receipt_id,
            source_event_id=self.source_event_ids[-1] if self.source_event_ids else "",
            correlation_event_ids=self.source_event_ids,
            terminal_state=(
                EvidenceTerminalState.COMPLETED
                if self.ok
                else EvidenceTerminalState.FAILED
            ),
            retryable=not self.ok,
            artifact_ids=(self.artifact_id,) if self.artifact_id else (),
            metadata={
                "profile_id": self.profile_id,
                "restored_from_backup": self.restored_from_backup,
                "primary_repaired": self.primary_repaired,
            },
        )


class BrowserStorageIntegrationRuntime:
    """Attach CDP capture/restore to the canonical 04A profile store."""

    def __init__(
        self,
        profile_store: BrowserProfileStore,
        artifact_store: LocalArtifactStore,
        *,
        policy: StorageIntegrationPolicy | None = None,
    ) -> None:
        self.profile_store = profile_store
        self.artifact_store = artifact_store
        self.policy = policy or StorageIntegrationPolicy()
        self._receipts: list[StorageOperationReceipt] = []

    def capture(
        self,
        scope: ObservationScope,
        profile_id: str,
        cdp: BrowserStorageCdpPort,
        *,
        source_event_ids: Sequence[str] = (),
    ) -> BrowserIntegrationOutput:
        try:
            state = self._capture_state(cdp)
            storage_receipt = self.profile_store.save_storage_state_with_receipt(
                profile_id,
                state,
            )
            artifact = self._manifest(scope, storage_receipt, StorageOperation.CAPTURE)
            receipt = StorageOperationReceipt(
                scope=scope,
                profile_id=profile_id,
                operation=StorageOperation.CAPTURE,
                ok=True,
                storage_receipt=storage_receipt,
                source_event_ids=tuple(source_event_ids),
                artifact_id=artifact.artifact_id if artifact else "",
                cookies_applied=storage_receipt.cookies,
                origins_applied=storage_receipt.origins,
            )
        except Exception as exc:
            receipt = StorageOperationReceipt(
                scope=scope,
                profile_id=profile_id,
                operation=StorageOperation.CAPTURE,
                ok=False,
                source_event_ids=tuple(source_event_ids),
                error=f"{type(exc).__name__}: {exc}",
            )
            artifact = None
        self._receipts.append(receipt)
        return self._output(receipt, artifact)

    def restore(
        self,
        scope: ObservationScope,
        profile_id: str,
        cdp: BrowserStorageCdpPort,
        *,
        source_event_ids: Sequence[str] = (),
    ) -> BrowserIntegrationOutput:
        try:
            state, storage_receipt = self.profile_store.load_storage_state_with_receipt(
                profile_id
            )
            cookies = tuple(
                item for item in state.get("cookies", ()) if isinstance(item, Mapping)
            )
            origins = tuple(
                item for item in state.get("origins", ()) if isinstance(item, Mapping)
            )
            self._validate_state(cookies, origins)
            cookies_applied = self._restore_cookies(cdp, cookies)
            origins_applied = self._restore_origins(cdp, origins)
            if self.policy.require_restore_readback:
                readback = self._capture_state(cdp)
                self._assert_readback(state, readback)
            artifact = self._manifest(scope, storage_receipt, StorageOperation.RESTORE)
            receipt = StorageOperationReceipt(
                scope=scope,
                profile_id=profile_id,
                operation=StorageOperation.RESTORE,
                ok=True,
                storage_receipt=storage_receipt,
                source_event_ids=tuple(source_event_ids),
                artifact_id=artifact.artifact_id if artifact else "",
                cookies_applied=cookies_applied,
                origins_applied=origins_applied,
                restored_from_backup=storage_receipt.source == "backup",
                primary_repaired=storage_receipt.restored_primary,
            )
        except BrowserProfileCorrupt:
            if self.policy.fail_on_double_corruption:
                raise
            receipt = StorageOperationReceipt(
                scope=scope,
                profile_id=profile_id,
                operation=StorageOperation.RESTORE,
                ok=False,
                source_event_ids=tuple(source_event_ids),
                error="primary and backup storage state are corrupt",
            )
            artifact = None
        except Exception as exc:
            receipt = StorageOperationReceipt(
                scope=scope,
                profile_id=profile_id,
                operation=StorageOperation.RESTORE,
                ok=False,
                source_event_ids=tuple(source_event_ids),
                error=f"{type(exc).__name__}: {exc}",
            )
            artifact = None
        self._receipts.append(receipt)
        return self._output(receipt, artifact)

    def observe_load(
        self,
        scope: ObservationScope,
        profile_id: str,
        *,
        source_event_ids: Sequence[str] = (),
    ) -> BrowserIntegrationOutput:
        try:
            _, storage_receipt = self.profile_store.load_storage_state_with_receipt(
                profile_id
            )
            artifact = self._manifest(scope, storage_receipt, StorageOperation.LOAD)
            receipt = StorageOperationReceipt(
                scope=scope,
                profile_id=profile_id,
                operation=StorageOperation.LOAD,
                ok=True,
                storage_receipt=storage_receipt,
                source_event_ids=tuple(source_event_ids),
                artifact_id=artifact.artifact_id if artifact else "",
                restored_from_backup=storage_receipt.source == "backup",
                primary_repaired=storage_receipt.restored_primary,
            )
        except Exception as exc:
            artifact = None
            receipt = StorageOperationReceipt(
                scope=scope,
                profile_id=profile_id,
                operation=StorageOperation.LOAD,
                ok=False,
                source_event_ids=tuple(source_event_ids),
                error=f"{type(exc).__name__}: {exc}",
            )
        self._receipts.append(receipt)
        return self._output(receipt, artifact)

    def projection(self, *, scope: ObservationScope | None = None) -> dict[str, Any]:
        values = tuple(
            item for item in self._receipts if scope is None or item.scope == scope
        )
        return {
            "schema": "zyra.browser-observability.storage-projection.v1",
            "operation_count": len(values),
            "failure_count": sum(1 for item in values if not item.ok),
            "backup_restore_count": sum(
                1 for item in values if item.restored_from_backup
            ),
            "primary_repair_count": sum(
                1 for item in values if item.primary_repaired
            ),
            "operations": [item.to_dict() for item in values[-100:]],
            "persistence_owner": "M1-04A/BrowserProfileStore",
        }

    def _capture_state(self, cdp: BrowserStorageCdpPort) -> dict[str, Any]:
        cookies_response = cdp.send("Storage.getCookies", {})
        cookies = tuple(
            dict(item)
            for item in cookies_response.get("cookies", ())
            if isinstance(item, Mapping)
        )
        origins_response = cdp.send("Storage.getStorageKeyForFrame", {})
        raw_origins = origins_response.get("origins", ())
        origins = tuple(
            dict(item) for item in raw_origins if isinstance(item, Mapping)
        )
        self._validate_state(cookies, origins)
        return {
            "cookies": list(cookies),
            "origins": list(origins),
            "captured_at": utc_now(),
        }

    def _validate_state(
        self,
        cookies: Sequence[Mapping[str, Any]],
        origins: Sequence[Mapping[str, Any]],
    ) -> None:
        if len(cookies) > self.policy.maximum_cookies:
            raise StorageStateIntegrationError("browser cookie limit exceeded")
        if len(origins) > self.policy.maximum_origins:
            raise StorageStateIntegrationError("browser origin limit exceeded")
        for origin in origins:
            values = origin.get("localStorage") or origin.get("items") or ()
            if isinstance(values, Sequence) and len(values) > self.policy.maximum_items_per_origin:
                raise StorageStateIntegrationError("origin storage item limit exceeded")

    @staticmethod
    def _restore_cookies(
        cdp: BrowserStorageCdpPort,
        cookies: Sequence[Mapping[str, Any]],
    ) -> int:
        if not cookies:
            return 0
        result = cdp.send("Storage.setCookies", {"cookies": [dict(item) for item in cookies]})
        if result.get("error"):
            raise StorageStateIntegrationError(str(result["error"]))
        return len(cookies)

    @staticmethod
    def _restore_origins(
        cdp: BrowserStorageCdpPort,
        origins: Sequence[Mapping[str, Any]],
    ) -> int:
        restored = 0
        for origin in origins:
            origin_name = str(origin.get("origin") or "")
            items = origin.get("localStorage") or origin.get("items") or ()
            if not origin_name:
                continue
            for raw in items if isinstance(items, Sequence) else ():
                if isinstance(raw, Mapping):
                    key = str(raw.get("name") or raw.get("key") or "")
                    value = str(raw.get("value") or "")
                elif isinstance(raw, Sequence) and len(raw) == 2:
                    key, value = str(raw[0]), str(raw[1])
                else:
                    continue
                result = cdp.send(
                    "DOMStorage.setDOMStorageItem",
                    {
                        "storageId": {"securityOrigin": origin_name, "isLocalStorage": True},
                        "key": key,
                        "value": value,
                    },
                )
                if result.get("error"):
                    raise StorageStateIntegrationError(str(result["error"]))
            restored += 1
        return restored

    @staticmethod
    def _assert_readback(
        expected: Mapping[str, Any],
        actual: Mapping[str, Any],
    ) -> None:
        expected_digest = digest_value(
            {
                "cookies": expected.get("cookies") or [],
                "origins": expected.get("origins") or [],
            }
        )
        actual_digest = digest_value(
            {
                "cookies": actual.get("cookies") or [],
                "origins": actual.get("origins") or [],
            }
        )
        if expected_digest != actual_digest:
            raise StorageStateIntegrationError("storage restore readback differs")

    def _manifest(
        self,
        scope: ObservationScope,
        receipt: BrowserStorageStateReceipt,
        operation: StorageOperation,
    ) -> ArtifactRef | None:
        if not self.policy.publish_manifest:
            return None
        content = canonical_json(
            {
                "schema": "zyra.browser-observability.storage-manifest.v1",
                "scope": scope.to_dict(),
                "operation": str(operation),
                "storage_receipt": receipt.to_dict(),
                "contains_storage_values": False,
            }
        ) + "\n"
        return self.artifact_store.write_text(
            run_id=scope.run_id,
            task_id=scope.task_id,
            content=content,
            title="Browser storage-state integrity receipt",
            kind=ArtifactKind.STRUCTURED_DATA,
            extension=".json",
            producer_node_id=scope.node_id or None,
        )

    def _output(
        self,
        receipt: StorageOperationReceipt,
        artifact: ArtifactRef | None,
    ) -> BrowserIntegrationOutput:
        signal = self._signal(receipt)
        return BrowserIntegrationOutput(
            evidence=(receipt.evidence(),),
            signals=(signal,) if signal else (),
            artifacts=(artifact,) if artifact else (),
            events=(self._event(receipt),),
            projection=self.projection(scope=receipt.scope),
        )

    @staticmethod
    def _signal(receipt: StorageOperationReceipt) -> WatchdogSignal | None:
        if receipt.ok and not receipt.restored_from_backup:
            return None
        return WatchdogSignal(
            scope=receipt.scope,
            watchdog=WatchdogName.STORAGE_STATE,
            kind=(
                SignalKind.STORAGE_PERSISTED
                if receipt.ok
                else SignalKind.STORAGE_FAILED
            ),
            status=(HealthStatus.DEGRADED if receipt.ok else HealthStatus.UNHEALTHY),
            severity=(Severity.WARNING if receipt.ok else Severity.ERROR),
            summary=(
                "Browser storage state recovered from backup."
                if receipt.ok
                else receipt.error or "Browser storage state operation failed."
            ),
            sequence=1,
            retryable=not receipt.ok,
            terminal=not receipt.ok,
            evidence_event_ids=receipt.source_event_ids,
            artifact_ids=(receipt.artifact_id,) if receipt.artifact_id else (),
            metadata={
                "storage_receipt_id": receipt.receipt_id,
                "restored_from_backup": receipt.restored_from_backup,
                "primary_repaired": receipt.primary_repaired,
            },
        )

    @staticmethod
    def _event(receipt: StorageOperationReceipt) -> EventRecord:
        return EventRecord(
            run_id=receipt.scope.run_id,
            task_id=receipt.scope.task_id,
            node_id=receipt.scope.node_id or None,
            event_type=(
                EventType.BROWSER_RUNTIME_DIAGNOSTIC
                if receipt.ok
                else EventType.WORKER_HEALTH
            ),
            payload={
                "browser_storage_state": receipt.to_dict(),
                "recovery_planner_owner": "M1-07C",
                "is_recovery_plan": False,
            },
        )
