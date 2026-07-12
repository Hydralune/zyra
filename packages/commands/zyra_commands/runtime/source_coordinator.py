from __future__ import annotations

"""Atomic dynamic command source coordination.

Built-ins, MCP prompts, skills, plugins and project workflows all project into
one ControlCommandRegistry.  Providers never become command state owners; they
only publish immutable descriptors that point back to their canonical runtime.
"""

import copy
import hashlib
import json
import os
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso

from .registry import ControlCommandRegistry
from .schemas import (
    CommandAvailability,
    CommandConcurrency,
    CommandExposure,
    CommandKind,
    CommandMutationScope,
    CommandRegistrySnapshot,
    CommandSource,
    CommandSourceKind,
    ControlCommandDescriptor,
)


class CommandSourceError(RuntimeError):
    pass


class CommandSourceDisabled(CommandSourceError):
    pass


class CommandSourceConflict(CommandSourceError):
    pass


class SourceRefreshStatus(StrEnum):
    DISCOVERED = "discovered"
    APPLIED = "applied"
    UNCHANGED = "unchanged"
    FAILED_LAST_GOOD_RETAINED = "failed_last_good_retained"
    REMOVED = "removed"


class CommandSourceProvider(Protocol):
    @property
    def source_id(self) -> str: ...

    @property
    def source_kind(self) -> CommandSourceKind: ...

    def revision(self) -> str: ...

    def descriptors(self) -> Sequence[ControlCommandDescriptor | Mapping[str, Any] | Any]: ...


@dataclass(frozen=True, slots=True)
class ProviderState:
    source_id: str
    source_kind: CommandSourceKind
    revision: str
    descriptor_count: int
    descriptor_digest: str
    enabled: bool = True
    last_error: str = ""
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_kind": self.source_kind.value,
            "revision": self.revision,
            "descriptor_count": self.descriptor_count,
            "descriptor_digest": self.descriptor_digest,
            "enabled": self.enabled,
            "last_error": self.last_error,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProviderState":
        return cls(
            source_id=str(value.get("source_id") or ""),
            source_kind=CommandSourceKind(str(value.get("source_kind") or CommandSourceKind.PROJECT.value)),
            revision=str(value.get("revision") or ""),
            descriptor_count=max(0, int(value.get("descriptor_count") or 0)),
            descriptor_digest=str(value.get("descriptor_digest") or ""),
            enabled=bool(value.get("enabled", True)),
            last_error=str(value.get("last_error") or ""),
            updated_at=str(value.get("updated_at") or now_iso()),
        )


@dataclass(frozen=True, slots=True)
class CommandSourceRefreshReceipt:
    refresh_id: str
    status: SourceRefreshStatus
    source_id: str
    source_kind: CommandSourceKind
    provider_revision: str
    registry_generation_before: int
    registry_generation_after: int
    registry_digest_before: str
    registry_digest_after: str
    descriptor_count: int
    collision_count: int
    changed_names: tuple[str, ...] = ()
    shadowed_names: tuple[str, ...] = ()
    error_code: str = ""
    error_message: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    @property
    def changed(self) -> bool:
        return self.registry_digest_before != self.registry_digest_after

    def to_dict(self) -> dict[str, Any]:
        return {
            "refresh_id": self.refresh_id,
            "status": self.status.value,
            "source_id": self.source_id,
            "source_kind": self.source_kind.value,
            "provider_revision": self.provider_revision,
            "registry_generation_before": self.registry_generation_before,
            "registry_generation_after": self.registry_generation_after,
            "registry_digest_before": self.registry_digest_before,
            "registry_digest_after": self.registry_digest_after,
            "descriptor_count": self.descriptor_count,
            "collision_count": self.collision_count,
            "changed_names": list(self.changed_names),
            "shadowed_names": list(self.shadowed_names),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "metadata": _safe_mapping(self.metadata),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CommandSourceRefreshReceipt":
        return cls(
            refresh_id=str(value.get("refresh_id") or new_id("cmdrefresh")),
            status=SourceRefreshStatus(str(value.get("status") or SourceRefreshStatus.DISCOVERED.value)),
            source_id=str(value.get("source_id") or ""),
            source_kind=CommandSourceKind(str(value.get("source_kind") or CommandSourceKind.PROJECT.value)),
            provider_revision=str(value.get("provider_revision") or ""),
            registry_generation_before=max(0, int(value.get("registry_generation_before") or 0)),
            registry_generation_after=max(0, int(value.get("registry_generation_after") or 0)),
            registry_digest_before=str(value.get("registry_digest_before") or ""),
            registry_digest_after=str(value.get("registry_digest_after") or ""),
            descriptor_count=max(0, int(value.get("descriptor_count") or 0)),
            collision_count=max(0, int(value.get("collision_count") or 0)),
            changed_names=tuple(str(item) for item in value.get("changed_names") or ()),
            shadowed_names=tuple(str(item) for item in value.get("shadowed_names") or ()),
            error_code=str(value.get("error_code") or ""),
            error_message=str(value.get("error_message") or ""),
            metadata=_safe_mapping(value.get("metadata")),
            created_at=str(value.get("created_at") or now_iso()),
        )


class CallableCommandSourceProvider:
    def __init__(
        self,
        source_id: str,
        source_kind: CommandSourceKind,
        loader: Callable[[], Sequence[Any]],
        *,
        revision_loader: Callable[[], str] | None = None,
        descriptor_mapper: Callable[[Any], ControlCommandDescriptor] | None = None,
    ) -> None:
        self._source_id = str(source_id)
        self._source_kind = source_kind
        self.loader = loader
        self.revision_loader = revision_loader
        self.descriptor_mapper = descriptor_mapper

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def source_kind(self) -> CommandSourceKind:
        return self._source_kind

    def revision(self) -> str:
        if self.revision_loader is None:
            return "dynamic"
        return str(self.revision_loader())

    def descriptors(self) -> Sequence[ControlCommandDescriptor]:
        values = self.loader()
        return tuple(
            self.descriptor_mapper(item) if self.descriptor_mapper else coerce_dynamic_descriptor(
                item,
                source_id=self.source_id,
                source_kind=self.source_kind,
                source_revision=self.revision(),
            )
            for item in values
        )


class CommandRegistryCoordinator:
    schema = "zyra.command-source-coordinator/v1"

    def __init__(
        self,
        registry: ControlCommandRegistry,
        state_path: str | Path,
        *,
        event_sink: Callable[[EventRecord], None] | None = None,
        disabled: bool = False,
        maximum_receipts: int = 2048,
    ) -> None:
        self.registry = registry
        self.state_path = Path(state_path).resolve()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.event_sink = event_sink
        self.disabled = bool(disabled)
        self.maximum_receipts = max(64, int(maximum_receipts))
        self._lock = RLock()
        self._providers: dict[str, CommandSourceProvider] = {}
        self._provider_states: dict[str, ProviderState] = {}
        self._last_good: dict[str, tuple[ControlCommandDescriptor, ...]] = {}
        self._receipts: list[CommandSourceRefreshReceipt] = []
        self._load()

    def register(self, provider: CommandSourceProvider) -> None:
        self._require_enabled()
        source_id = str(provider.source_id).strip()
        if not source_id:
            raise ValueError("command source provider id is required")
        with self._lock:
            existing = self._providers.get(source_id)
            if existing is not None and existing is not provider:
                raise CommandSourceConflict(f"command source provider already registered: {source_id}")
            self._providers[source_id] = provider

    def unregister(self, source_id: str, *, remove_projection: bool = True) -> CommandSourceRefreshReceipt | None:
        self._require_enabled()
        with self._lock:
            provider = self._providers.pop(source_id, None)
            if provider is None and source_id not in self._provider_states:
                return None
            before = self.registry.snapshot()
            after = self.registry.remove_source(source_id) if remove_projection else before
            self._provider_states.pop(source_id, None)
            self._last_good.pop(source_id, None)
            receipt = self._receipt(
                status=SourceRefreshStatus.REMOVED,
                source_id=source_id,
                source_kind=(provider.source_kind if provider else CommandSourceKind.PROJECT),
                provider_revision="",
                before=before,
                after=after,
                descriptor_count=0,
            )
            self._record(receipt)
            return receipt

    def refresh(self, source_id: str) -> CommandSourceRefreshReceipt:
        self._require_enabled()
        with self._lock:
            provider = self._providers.get(source_id)
            if provider is None:
                raise CommandSourceConflict(f"command source provider not registered: {source_id}")
            before = self.registry.snapshot()
            previous_names = {
                item.canonical_name for item in self._last_good.get(source_id, ())
            }
            revision = ""
            try:
                revision = str(provider.revision())
                candidate = tuple(self._validate_descriptor(item, provider) for item in provider.descriptors())
                self._validate_unique(candidate)
                descriptor_digest = _digest([item.to_dict() for item in candidate])
                after = self.registry.replace_source(source_id, candidate)
                current_names = {item.canonical_name for item in candidate}
                status = SourceRefreshStatus.UNCHANGED if before.digest == after.digest else SourceRefreshStatus.APPLIED
                state = ProviderState(
                    source_id=source_id,
                    source_kind=provider.source_kind,
                    revision=revision,
                    descriptor_count=len(candidate),
                    descriptor_digest=descriptor_digest,
                )
                self._provider_states[source_id] = state
                self._last_good[source_id] = copy.deepcopy(candidate)
                receipt = self._receipt(
                    status=status,
                    source_id=source_id,
                    source_kind=provider.source_kind,
                    provider_revision=revision,
                    before=before,
                    after=after,
                    descriptor_count=len(candidate),
                    changed_names=tuple(sorted(previous_names.symmetric_difference(current_names))),
                )
            except Exception as error:
                after = self.registry.snapshot()
                previous = self._provider_states.get(source_id)
                if previous is not None:
                    self._provider_states[source_id] = replace(
                        previous,
                        last_error=f"{type(error).__name__}: {error}",
                        updated_at=now_iso(),
                    )
                receipt = self._receipt(
                    status=SourceRefreshStatus.FAILED_LAST_GOOD_RETAINED,
                    source_id=source_id,
                    source_kind=provider.source_kind,
                    provider_revision=revision,
                    before=before,
                    after=after,
                    descriptor_count=len(self._last_good.get(source_id, ())),
                    error_code=type(error).__name__,
                    error_message=str(error),
                )
            self._record(receipt)
            return receipt

    def refresh_all(self) -> tuple[CommandSourceRefreshReceipt, ...]:
        self._require_enabled()
        return tuple(self.refresh(source_id) for source_id in sorted(self._providers))

    def provider_states(self) -> tuple[ProviderState, ...]:
        with self._lock:
            return tuple(copy.deepcopy(item) for item in sorted(self._provider_states.values(), key=lambda value: value.source_id))

    def receipts(self, *, limit: int = 100) -> tuple[CommandSourceRefreshReceipt, ...]:
        with self._lock:
            selected = self._receipts[-max(1, min(int(limit), self.maximum_receipts)) :]
            return tuple(copy.deepcopy(item) for item in selected)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            body = {
                "schema": self.schema,
                "owner": "M1-03D CommandRegistryCoordinator",
                "registry": self.registry.snapshot().to_dict(),
                "providers": [item.to_dict() for item in sorted(self._provider_states.values(), key=lambda value: value.source_id)],
                "last_good": {
                    key: [item.to_dict() for item in values]
                    for key, values in sorted(self._last_good.items())
                },
                "receipts": [item.to_dict() for item in self._receipts],
            }
            return {**body, "checksum": _digest(body)}

    def _record(self, receipt: CommandSourceRefreshReceipt) -> None:
        self._receipts.append(copy.deepcopy(receipt))
        self._receipts = self._receipts[-self.maximum_receipts :]
        self._persist()
        if self.event_sink is not None:
            event_type = getattr(EventType, "COMMAND_REGISTRY_REFRESHED", getattr(EventType, "COMMAND_COMPLETED", "command_completed"))
            self.event_sink(EventRecord(
                run_id="",
                task_id="",
                event_type=event_type,
                payload={
                    "schema": "zyra.command-registry-refresh-event/v1",
                    "receipt": receipt.to_dict(),
                    "state_owner": "ControlCommandRegistry",
                },
            ))

    def _receipt(
        self,
        *,
        status: SourceRefreshStatus,
        source_id: str,
        source_kind: CommandSourceKind,
        provider_revision: str,
        before: CommandRegistrySnapshot,
        after: CommandRegistrySnapshot,
        descriptor_count: int,
        changed_names: tuple[str, ...] = (),
        error_code: str = "",
        error_message: str = "",
    ) -> CommandSourceRefreshReceipt:
        shadows = tuple(sorted({item.name for item in after.collisions if item.shadowed_source == source_id}))
        return CommandSourceRefreshReceipt(
            refresh_id=new_id("cmdrefresh"),
            status=status,
            source_id=source_id,
            source_kind=source_kind,
            provider_revision=provider_revision,
            registry_generation_before=before.generation,
            registry_generation_after=after.generation,
            registry_digest_before=before.digest,
            registry_digest_after=after.digest,
            descriptor_count=descriptor_count,
            collision_count=len(after.collisions),
            changed_names=changed_names,
            shadowed_names=shadows,
            error_code=error_code,
            error_message=error_message,
            metadata={
                "last_good_retained": status is SourceRefreshStatus.FAILED_LAST_GOOD_RETAINED,
                "generation_stable_if_unchanged": True,
            },
        )

    @staticmethod
    def _validate_descriptor(item: Any, provider: CommandSourceProvider) -> ControlCommandDescriptor:
        descriptor = item if isinstance(item, ControlCommandDescriptor) else coerce_dynamic_descriptor(
            item,
            source_id=provider.source_id,
            source_kind=provider.source_kind,
            source_revision=provider.revision(),
        )
        if descriptor.source.source_id != provider.source_id:
            raise CommandSourceConflict("descriptor source id differs from provider")
        if descriptor.source.kind is not provider.source_kind:
            raise CommandSourceConflict("descriptor source kind differs from provider")
        if not descriptor.handler_id or descriptor.handler_id == "dynamic.command":
            raise CommandSourceConflict("dynamic command requires an explicit canonical owner handler")
        return copy.deepcopy(descriptor)

    @staticmethod
    def _validate_unique(values: Sequence[ControlCommandDescriptor]) -> None:
        names = [item.canonical_name for item in values]
        if len(names) != len(set(names)):
            raise CommandSourceConflict("provider published duplicate canonical command names")
        aliases = []
        for item in values:
            aliases.extend(item.aliases)
        if len(aliases) != len(set(aliases)):
            raise CommandSourceConflict("provider published duplicate aliases")

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CommandSourceError(f"cannot load command source coordinator state: {error}") from error
        if not isinstance(value, Mapping):
            raise CommandSourceError("command source coordinator root must be an object")
        body = {key: copy.deepcopy(item) for key, item in value.items() if key != "checksum"}
        expected = str(value.get("checksum") or "")
        if expected and expected != _digest(body):
            raise CommandSourceError("command source coordinator checksum mismatch")
        for item in value.get("providers") or ():
            if isinstance(item, Mapping):
                state = ProviderState.from_dict(item)
                self._provider_states[state.source_id] = state
        raw_last_good = value.get("last_good")
        if isinstance(raw_last_good, Mapping):
            for source_id, items in raw_last_good.items():
                descriptors = []
                for item in items or ():
                    if not isinstance(item, Mapping):
                        continue
                    descriptors.append(coerce_dynamic_descriptor(
                        item,
                        source_id=str(source_id),
                        source_kind=CommandSourceKind(str(_mapping(item.get("source")).get("kind") or CommandSourceKind.PROJECT.value)),
                        source_revision=str(_mapping(item.get("source")).get("version") or "restored"),
                    ))
                self._last_good[str(source_id)] = tuple(descriptors)
                if descriptors:
                    self.registry.replace_source(str(source_id), descriptors)
        self._receipts = [
            CommandSourceRefreshReceipt.from_dict(item)
            for item in value.get("receipts") or ()
            if isinstance(item, Mapping)
        ][-self.maximum_receipts :]

    def _persist(self) -> None:
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.state_path)

    def _require_enabled(self) -> None:
        if self.disabled:
            raise CommandSourceDisabled("CommandRegistryCoordinator is disabled")


def coerce_dynamic_descriptor(
    value: Any,
    *,
    source_id: str,
    source_kind: CommandSourceKind,
    source_revision: str,
) -> ControlCommandDescriptor:
    if isinstance(value, ControlCommandDescriptor):
        return value
    raw = _object_mapping(value)
    source_raw = _mapping(raw.get("source"))
    name = str(raw.get("canonical_name") or raw.get("command_name") or raw.get("name") or "")
    if not name:
        raise CommandSourceConflict("dynamic command has no name")
    if not name.startswith("/"):
        name = "/" + name
    metadata = _safe_mapping(raw.get("metadata"))
    handler = str(raw.get("handler_id") or metadata.get("handler_id") or _default_handler(source_kind))
    if source_kind is CommandSourceKind.MCP:
        scope = CommandMutationScope.MCP
        concurrency = CommandConcurrency.SESSION_SERIAL
    elif source_kind in {CommandSourceKind.SKILL, CommandSourceKind.PLUGIN}:
        scope = CommandMutationScope.SKILL_PLUGIN
        concurrency = CommandConcurrency.SESSION_SERIAL
    else:
        scope = CommandMutationScope(str(raw.get("mutation_scope") or CommandMutationScope.READ_ONLY.value))
        concurrency = CommandConcurrency(str(raw.get("concurrency") or CommandConcurrency.READ_ONLY_PARALLEL.value))
    aliases_raw = raw.get("aliases") or ()
    aliases = tuple(str(item) for item in aliases_raw) if isinstance(aliases_raw, Sequence) and not isinstance(aliases_raw, (str, bytes)) else ()
    return ControlCommandDescriptor(
        canonical_name=name,
        description=str(raw.get("description") or raw.get("purpose") or name),
        source=CommandSource(
            kind=source_kind,
            source_id=source_id,
            version=str(source_revision or source_raw.get("version") or "dynamic"),
            source_paths=tuple(str(item) for item in source_raw.get("source_paths") or ()),
        ),
        handler_id=handler,
        aliases=aliases,
        argument_schema=_mapping(raw.get("argument_schema")),
        argument_hint=str(raw.get("argument_hint") or ""),
        kind=CommandKind(str(raw.get("kind") or CommandKind.PROMPT.value)),
        mutation_scope=scope,
        concurrency=concurrency,
        immediate=bool(raw.get("immediate", scope is CommandMutationScope.READ_ONLY)),
        category=str(raw.get("category") or metadata.get("category") or "extensions"),
        permission_action=str(raw.get("permission_action") or metadata.get("permission_action") or "inspect"),
        availability=CommandAvailability(
            enabled=bool(raw.get("enabled", True)),
            reason=str(raw.get("disabled_reason") or ""),
        ),
        exposure=CommandExposure(
            local=bool(raw.get("local", True)),
            api=bool(raw.get("api", True)),
            remote=bool(raw.get("remote", True)),
            agent=bool(raw.get("agent", False)),
        ),
        metadata={
            **metadata,
            "dynamic_source": True,
            "canonical_owner_required": True,
        },
    )


def _default_handler(kind: CommandSourceKind) -> str:
    return {
        CommandSourceKind.MCP: "mcp.prompt",
        CommandSourceKind.SKILL: "skill.command",
        CommandSourceKind.PLUGIN: "skill_plugin.command",
        CommandSourceKind.PROJECT: "workflow.command",
        CommandSourceKind.OPENCODE_ADAPTED: "workflow.command",
    }.get(kind, "")


def _object_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    for name in ("safe_dict", "to_dict"):
        method = getattr(value, name, None)
        if callable(method):
            selected = method()
            if isinstance(selected, Mapping):
                return dict(selected)
    result = {}
    for name in ("name", "purpose", "description", "aliases", "metadata", "source"):
        if hasattr(value, name):
            result[name] = getattr(value, name)
    return result


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _safe_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result = {}
    for key, item in value.items():
        name = str(key)
        if any(token in name.casefold() for token in ("secret", "token", "password", "authorization", "credential")):
            result[name] = "<redacted>"
        elif isinstance(item, Mapping):
            result[name] = _safe_mapping(item)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            result[name] = [_safe_value(entry) for entry in item]
        else:
            result[name] = _safe_value(item)
    return result


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return _safe_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_safe_value(item) for item in value]
    return str(value)


def _digest(value: Any) -> str:
    payload = json.dumps(_safe_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


__all__ = [
    "CallableCommandSourceProvider",
    "CommandRegistryCoordinator",
    "CommandSourceConflict",
    "CommandSourceDisabled",
    "CommandSourceError",
    "CommandSourceProvider",
    "CommandSourceRefreshReceipt",
    "ProviderState",
    "SourceRefreshStatus",
    "coerce_dynamic_descriptor",
]
