from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Self


IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")
COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")
SYMBOL_PATTERN = re.compile(
    r"^[A-Za-z_$][A-Za-z0-9_$]*(?:[.:][A-Za-z_$][A-Za-z0-9_$]*)*$"
)
REQUIREMENT_PATTERN = re.compile(r"^(?:REQ|SCORE)-[A-Z0-9-]+$")


class AuditMode(StrEnum):
    INVENTORY = "inventory"
    CANDIDATE = "candidate"
    FREEZE = "freeze"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"

    @property
    def rank(self) -> int:
        return {
            Severity.INFO: 0,
            Severity.WARNING: 1,
            Severity.ERROR: 2,
            Severity.BLOCKER: 3,
        }[self]


class Disposition(StrEnum):
    ACCEPT = "accept"
    TRACK = "track"
    REMOVE = "remove"
    ABSORB = "absorb"
    REWIRE = "rewire"
    ADD_EVIDENCE = "add_evidence"
    BLOCK_RELEASE = "block_release"


class Language(StrEnum):
    PYTHON = "python"
    TYPESCRIPT = "typescript"
    TSX = "tsx"
    JAVASCRIPT = "javascript"
    JSX = "jsx"
    RUST = "rust"
    NATIVE = "native"
    CONFIG = "config"
    TEXT = "text"


class NodeKind(StrEnum):
    FILE = "file"
    MODULE = "module"
    SYMBOL = "symbol"
    ENTRYPOINT = "entrypoint"
    ROUTE = "route"
    EVENT = "event"
    MUTATION = "mutation"
    ARTIFACT = "artifact"
    METRIC = "metric"
    TEST = "test"


class EdgeKind(StrEnum):
    CONTAINS = "contains"
    IMPORTS = "imports"
    CALLS = "calls"
    ROUTES = "routes"
    BOOTS = "boots"
    EMITS = "emits"
    MUTATES = "mutates"
    MATERIALIZES = "materializes"
    VERIFIES = "verifies"
    DECLARED = "declared"


class EntrySurface(StrEnum):
    CLI = "cli"
    API = "api"
    WEB = "web"
    WORKER = "worker"


class StateRole(StrEnum):
    OWNER = "owner"
    STORE = "store"
    WRITER = "writer"
    PROJECTION = "projection"
    CACHE = "cache"
    CHECKPOINT = "checkpoint"
    RECOVERY = "recovery"
    FALLBACK = "fallback"


class EffectKind(StrEnum):
    STATE_MUTATION = "state_mutation"
    ROUTE = "route"
    PLACEMENT = "placement"
    TOOL = "tool"
    PERMISSION = "permission"
    COMPACT = "compact"
    RESTORE = "restore"
    FAULT = "fault"
    RECOVERY = "recovery"
    ARTIFACT = "artifact"
    METRIC = "metric"


class EvidenceStatus(StrEnum):
    VERIFIED = "verified"
    PARTIAL = "partial"
    PLANNED = "planned"
    MISSING = "missing"


class LineBucket(StrEnum):
    PRODUCTION = "production"
    TEST = "test"
    GENERATED = "generated"
    DATA = "data"
    DOCS = "docs"
    VENDOR_LIKE = "vendor_like"
    ADAPTER_ONLY = "adapter_only"
    MOCK_FIXTURE = "mock_fixture"
    RUNTIME_ASSET = "runtime_asset"
    OTHER = "other"


class CatalogError(ValueError):
    pass


def stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def stable_digest(value: Any, *, prefix: str = "sha256") -> str:
    digest = hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def content_digest(value: bytes | str, *, prefix: str = "sha256") -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return f"{prefix}:{hashlib.sha256(payload).hexdigest()}"


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, StrEnum):
        return value.value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def required_text(
    value: Any,
    *,
    field_name: str,
    minimum: int = 1,
    maximum: int = 4096,
) -> str:
    if not isinstance(value, str):
        raise CatalogError(f"{field_name} must be a string")
    normalized = value.strip()
    if len(normalized) < minimum:
        raise CatalogError(f"{field_name} must contain at least {minimum} characters")
    if len(normalized) > maximum:
        raise CatalogError(f"{field_name} exceeds {maximum} characters")
    if "\x00" in normalized:
        raise CatalogError(f"{field_name} contains a null byte")
    return normalized


def optional_text(
    value: Any,
    *,
    field_name: str,
    maximum: int = 4096,
) -> str:
    if value is None:
        return ""
    return required_text(
        value,
        field_name=field_name,
        minimum=0,
        maximum=maximum,
    )


def identity(value: Any, *, field_name: str) -> str:
    normalized = required_text(value, field_name=field_name, maximum=256)
    if not IDENTITY_PATTERN.fullmatch(normalized):
        raise CatalogError(f"{field_name} has an invalid identity: {normalized!r}")
    return normalized


def symbol(value: Any, *, field_name: str, allow_empty: bool = False) -> str:
    normalized = optional_text(value, field_name=field_name, maximum=256)
    if not normalized and allow_empty:
        return ""
    if not SYMBOL_PATTERN.fullmatch(normalized):
        raise CatalogError(f"{field_name} has an invalid symbol: {normalized!r}")
    return normalized


def revision(value: Any, *, field_name: str, allow_empty: bool = False) -> str:
    normalized = optional_text(value, field_name=field_name, maximum=64)
    if not normalized and allow_empty:
        return ""
    if not COMMIT_PATTERN.fullmatch(normalized):
        raise CatalogError(f"{field_name} must be a hexadecimal Git revision")
    return normalized.casefold()


def requirement_id(value: Any, *, field_name: str) -> str:
    normalized = required_text(value, field_name=field_name, maximum=64).upper()
    if not REQUIREMENT_PATTERN.fullmatch(normalized):
        raise CatalogError(f"{field_name} is not a requirement or score identity")
    return normalized


def enum_value(enum_type: type[StrEnum], value: Any, *, field_name: str) -> Any:
    normalized = required_text(value, field_name=field_name).casefold()
    try:
        return enum_type(normalized)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise CatalogError(f"{field_name} must be one of: {allowed}") from exc


def repository_path(
    value: Any,
    *,
    field_name: str,
    allow_empty: bool = False,
) -> str:
    normalized = optional_text(value, field_name=field_name, maximum=1024)
    if not normalized and allow_empty:
        return ""
    normalized = normalized.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or normalized.startswith(("/", "~")):
        raise CatalogError(f"{field_name} must be repository relative")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise CatalogError(f"{field_name} contains an unsafe segment")
    if ":" in path.parts[0]:
        raise CatalogError(f"{field_name} must not contain a drive prefix")
    return path.as_posix()


def string_tuple(
    value: Any,
    *,
    field_name: str,
    allow_empty: bool = False,
    identities: bool = False,
    paths: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise CatalogError(f"{field_name} must be an array")
    normalized: list[str] = []
    for index, item in enumerate(value):
        item_name = f"{field_name}[{index}]"
        if identities:
            selected = identity(item, field_name=item_name)
        elif paths:
            selected = repository_path(item, field_name=item_name)
        else:
            selected = required_text(item, field_name=item_name)
        if selected not in normalized:
            normalized.append(selected)
    if not normalized and not allow_empty:
        raise CatalogError(f"{field_name} cannot be empty")
    return tuple(normalized)


def mapping(value: Any, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CatalogError(f"{field_name} must be an object")
    return value


def resolve_within(root: Path, relative: str) -> Path:
    base = root.resolve(strict=False)
    candidate = (base / repository_path(relative, field_name="path")).resolve(
        strict=False
    )
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise CatalogError(f"path escapes project root: {relative}") from exc
    return candidate


def relative_to(root: Path, path: Path) -> str:
    base = root.resolve(strict=False)
    selected = path.resolve(strict=False)
    try:
        return selected.relative_to(base).as_posix()
    except ValueError:
        return selected.as_posix()


@dataclass(frozen=True, slots=True)
class RuleSwitches:
    catalog: bool = True
    ownership: bool = True
    reachability: bool = True
    causality: bool = True
    source_risks: bool = True
    effective_lines: bool = True
    requirements: bool = True

    def disabled(self, name: str) -> Self:
        if name not in self.__dataclass_fields__:
            raise ValueError(f"unknown rule group: {name}")
        return replace(self, **{name: False})

    def enabled(self) -> tuple[str, ...]:
        return tuple(
            name for name in self.__dataclass_fields__ if getattr(self, name)
        )

    def to_dict(self) -> dict[str, bool]:
        return {
            name: bool(getattr(self, name))
            for name in self.__dataclass_fields__
        }


@dataclass(frozen=True, slots=True)
class SourceRef:
    path: str
    symbol: str = ""
    language: Language = Language.PYTHON
    role: StateRole = StateRole.WRITER
    selector: str = ""
    required: bool = True

    @classmethod
    def parse(
        cls,
        raw: Any,
        *,
        field_name: str,
        default_role: StateRole = StateRole.WRITER,
        required: bool = True,
    ) -> Self:
        value = mapping(raw, field_name=field_name)
        return cls(
            path=repository_path(value.get("path"), field_name=f"{field_name}.path"),
            symbol=symbol(
                value.get("symbol"),
                field_name=f"{field_name}.symbol",
                allow_empty=True,
            ),
            language=enum_value(
                Language,
                value.get("language", Language.PYTHON.value),
                field_name=f"{field_name}.language",
            ),
            role=enum_value(
                StateRole,
                value.get("role", default_role.value),
                field_name=f"{field_name}.role",
            ),
            selector=optional_text(
                value.get("selector"),
                field_name=f"{field_name}.selector",
                maximum=512,
            ),
            required=bool(value.get("required", required)),
        )

    @property
    def key(self) -> str:
        return f"{self.path}#{self.symbol}" if self.symbol else self.path

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "symbol": self.symbol,
            "language": self.language.value,
            "role": self.role.value,
            "selector": self.selector,
            "required": self.required,
        }


@dataclass(frozen=True, slots=True)
class EntrypointSpec:
    entry_id: str
    surface: EntrySurface
    reference: SourceRef
    command: str = ""
    route: str = ""
    method: str = ""
    default: bool = True
    trace: tuple[SourceRef, ...] = ()

    @classmethod
    def parse(cls, raw: Any, *, field_name: str) -> Self:
        value = mapping(raw, field_name=field_name)
        surface = enum_value(
            EntrySurface,
            value.get("surface"),
            field_name=f"{field_name}.surface",
        )
        trace_raw = value.get("trace", ())
        if not isinstance(trace_raw, Sequence) or isinstance(
            trace_raw, (str, bytes, bytearray)
        ):
            raise CatalogError(f"{field_name}.trace must be an array")
        trace = tuple(
            SourceRef.parse(
                item,
                field_name=f"{field_name}.trace[{index}]",
                default_role=StateRole.WRITER,
            )
            for index, item in enumerate(trace_raw)
        )
        result = cls(
            entry_id=identity(
                value.get("entry_id"), field_name=f"{field_name}.entry_id"
            ),
            surface=surface,
            reference=SourceRef.parse(
                value.get("reference"),
                field_name=f"{field_name}.reference",
                default_role=StateRole.WRITER,
            ),
            command=optional_text(
                value.get("command"),
                field_name=f"{field_name}.command",
                maximum=512,
            ),
            route=optional_text(
                value.get("route"),
                field_name=f"{field_name}.route",
                maximum=512,
            ),
            method=optional_text(
                value.get("method"),
                field_name=f"{field_name}.method",
                maximum=16,
            ).upper(),
            default=bool(value.get("default", True)),
            trace=trace,
        )
        result.validate()
        return result

    def validate(self) -> None:
        if self.surface is EntrySurface.CLI and not self.command:
            raise CatalogError(f"CLI entry {self.entry_id} requires command")
        if self.surface is EntrySurface.API:
            if not self.route or not self.route.startswith("/"):
                raise CatalogError(f"API entry {self.entry_id} requires absolute route")
            if self.method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                raise CatalogError(f"API entry {self.entry_id} has invalid method")
        if self.surface in {EntrySurface.WEB, EntrySurface.WORKER}:
            if not self.reference.symbol and not self.reference.selector:
                raise CatalogError(
                    f"{self.surface.value} entry {self.entry_id} needs symbol or selector"
                )
        if self.default and not self.trace:
            raise CatalogError(f"default entry {self.entry_id} requires a trace")

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "surface": self.surface.value,
            "reference": self.reference.to_dict(),
            "command": self.command,
            "route": self.route,
            "method": self.method,
            "default": self.default,
            "trace": [item.to_dict() for item in self.trace],
        }


@dataclass(frozen=True, slots=True)
class EventMutationSpec:
    link_id: str
    event_name: str
    producer: SourceRef
    mutation: SourceRef
    effect_kind: EffectKind
    artifact: SourceRef | None = None
    metric: SourceRef | None = None
    verifier: SourceRef | None = None
    required_attributes: tuple[str, ...] = ()

    @classmethod
    def parse(cls, raw: Any, *, field_name: str) -> Self:
        value = mapping(raw, field_name=field_name)

        def optional_ref(name: str, kind: NodeKind) -> SourceRef | None:
            selected = value.get(name)
            if selected is None:
                return None
            default_role = (
                StateRole.WRITER
                if kind in {NodeKind.ARTIFACT, NodeKind.METRIC}
                else StateRole.RECOVERY
            )
            return SourceRef.parse(
                selected,
                field_name=f"{field_name}.{name}",
                default_role=default_role,
            )

        result = cls(
            link_id=identity(
                value.get("link_id"), field_name=f"{field_name}.link_id"
            ),
            event_name=required_text(
                value.get("event_name"),
                field_name=f"{field_name}.event_name",
                maximum=256,
            ),
            producer=SourceRef.parse(
                value.get("producer"),
                field_name=f"{field_name}.producer",
                default_role=StateRole.WRITER,
            ),
            mutation=SourceRef.parse(
                value.get("mutation"),
                field_name=f"{field_name}.mutation",
                default_role=StateRole.WRITER,
            ),
            effect_kind=enum_value(
                EffectKind,
                value.get("effect_kind"),
                field_name=f"{field_name}.effect_kind",
            ),
            artifact=optional_ref("artifact", NodeKind.ARTIFACT),
            metric=optional_ref("metric", NodeKind.METRIC),
            verifier=optional_ref("verifier", NodeKind.TEST),
            required_attributes=string_tuple(
                value.get("required_attributes", ()),
                field_name=f"{field_name}.required_attributes",
                allow_empty=True,
                identities=True,
            ),
        )
        if (
            result.effect_kind is EffectKind.ARTIFACT
            and result.artifact is None
        ):
            raise CatalogError(f"{field_name} artifact effect requires artifact")
        if result.effect_kind is EffectKind.METRIC and result.metric is None:
            raise CatalogError(f"{field_name} metric effect requires metric")
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "link_id": self.link_id,
            "event_name": self.event_name,
            "producer": self.producer.to_dict(),
            "mutation": self.mutation.to_dict(),
            "effect_kind": self.effect_kind.value,
            "artifact": self.artifact.to_dict() if self.artifact else None,
            "metric": self.metric.to_dict() if self.metric else None,
            "verifier": self.verifier.to_dict() if self.verifier else None,
            "required_attributes": list(self.required_attributes),
        }


@dataclass(frozen=True, slots=True)
class OwnerContract:
    domain: str
    description: str
    owner: SourceRef
    store: SourceRef
    writers: tuple[SourceRef, ...]
    projections: tuple[SourceRef, ...]
    caches: tuple[SourceRef, ...]
    checkpoint: SourceRef
    recovery: SourceRef
    entries: tuple[EntrypointSpec, ...]
    events: tuple[EventMutationSpec, ...]
    tests: tuple[str, ...]
    disable_probes: tuple[str, ...]
    fallback_refs: tuple[SourceRef, ...]
    forbidden_owner_claims: tuple[str, ...]
    shared_owner_group: str = ""

    @classmethod
    def parse(cls, raw: Any, *, field_name: str) -> Self:
        value = mapping(raw, field_name=field_name)

        def refs(
            name: str,
            *,
            default_role: StateRole,
            allow_empty: bool,
        ) -> tuple[SourceRef, ...]:
            selected = value.get(name, ())
            if not isinstance(selected, Sequence) or isinstance(
                selected, (str, bytes, bytearray)
            ):
                raise CatalogError(f"{field_name}.{name} must be an array")
            result = tuple(
                SourceRef.parse(
                    item,
                    field_name=f"{field_name}.{name}[{index}]",
                    default_role=default_role,
                )
                for index, item in enumerate(selected)
            )
            if not result and not allow_empty:
                raise CatalogError(f"{field_name}.{name} cannot be empty")
            return result

        entries_raw = value.get("entries", ())
        events_raw = value.get("events", ())
        if not isinstance(entries_raw, Sequence) or isinstance(
            entries_raw, (str, bytes, bytearray)
        ):
            raise CatalogError(f"{field_name}.entries must be an array")
        if not isinstance(events_raw, Sequence) or isinstance(
            events_raw, (str, bytes, bytearray)
        ):
            raise CatalogError(f"{field_name}.events must be an array")
        result = cls(
            domain=identity(value.get("domain"), field_name=f"{field_name}.domain"),
            description=required_text(
                value.get("description"),
                field_name=f"{field_name}.description",
                minimum=24,
            ),
            owner=SourceRef.parse(
                value.get("owner"),
                field_name=f"{field_name}.owner",
                default_role=StateRole.OWNER,
            ),
            store=SourceRef.parse(
                value.get("store"),
                field_name=f"{field_name}.store",
                default_role=StateRole.STORE,
            ),
            writers=refs(
                "writers",
                default_role=StateRole.WRITER,
                allow_empty=False,
            ),
            projections=refs(
                "projections",
                default_role=StateRole.PROJECTION,
                allow_empty=True,
            ),
            caches=refs(
                "caches",
                default_role=StateRole.CACHE,
                allow_empty=True,
            ),
            checkpoint=SourceRef.parse(
                value.get("checkpoint"),
                field_name=f"{field_name}.checkpoint",
                default_role=StateRole.CHECKPOINT,
            ),
            recovery=SourceRef.parse(
                value.get("recovery"),
                field_name=f"{field_name}.recovery",
                default_role=StateRole.RECOVERY,
            ),
            entries=tuple(
                EntrypointSpec.parse(
                    item,
                    field_name=f"{field_name}.entries[{index}]",
                )
                for index, item in enumerate(entries_raw)
            ),
            events=tuple(
                EventMutationSpec.parse(
                    item,
                    field_name=f"{field_name}.events[{index}]",
                )
                for index, item in enumerate(events_raw)
            ),
            tests=string_tuple(
                value.get("tests"),
                field_name=f"{field_name}.tests",
                paths=True,
            ),
            disable_probes=string_tuple(
                value.get("disable_probes"),
                field_name=f"{field_name}.disable_probes",
                identities=True,
            ),
            fallback_refs=refs(
                "fallback_refs",
                default_role=StateRole.FALLBACK,
                allow_empty=True,
            ),
            forbidden_owner_claims=string_tuple(
                value.get("forbidden_owner_claims", ()),
                field_name=f"{field_name}.forbidden_owner_claims",
                allow_empty=True,
            ),
            shared_owner_group=optional_text(
                value.get("shared_owner_group"),
                field_name=f"{field_name}.shared_owner_group",
                maximum=128,
            ),
        )
        result.validate()
        return result

    def validate(self) -> None:
        if self.owner.role is not StateRole.OWNER:
            raise CatalogError(f"{self.domain} owner must use owner role")
        if self.store.role is not StateRole.STORE:
            raise CatalogError(f"{self.domain} store must use store role")
        if any(item.role is not StateRole.WRITER for item in self.writers):
            raise CatalogError(f"{self.domain} writers must use writer role")
        if any(item.role is not StateRole.PROJECTION for item in self.projections):
            raise CatalogError(f"{self.domain} projections must use projection role")
        if any(item.role is not StateRole.CACHE for item in self.caches):
            raise CatalogError(f"{self.domain} caches must use cache role")
        if self.checkpoint.role is not StateRole.CHECKPOINT:
            raise CatalogError(f"{self.domain} checkpoint role is invalid")
        if self.recovery.role is not StateRole.RECOVERY:
            raise CatalogError(f"{self.domain} recovery role is invalid")
        if not any(item.default for item in self.entries):
            raise CatalogError(f"{self.domain} requires a default entry")
        identities = [item.entry_id for item in self.entries]
        if len(identities) != len(set(identities)):
            raise CatalogError(f"{self.domain} has duplicate entry identities")
        links = [item.link_id for item in self.events]
        if len(links) != len(set(links)):
            raise CatalogError(f"{self.domain} has duplicate event link identities")
        canonical = {self.owner.key, self.store.key}
        overlap = canonical & {
            item.key for item in (*self.projections, *self.caches, *self.fallback_refs)
        }
        if overlap:
            raise CatalogError(
                f"{self.domain} promotes projection/cache/fallback to owner: "
                + ", ".join(sorted(overlap))
            )

    def all_refs(self) -> tuple[SourceRef, ...]:
        refs: list[SourceRef] = [
            self.owner,
            self.store,
            *self.writers,
            *self.projections,
            *self.caches,
            self.checkpoint,
            self.recovery,
            *self.fallback_refs,
        ]
        for entry in self.entries:
            refs.append(entry.reference)
            refs.extend(entry.trace)
        for event in self.events:
            refs.extend((event.producer, event.mutation))
            refs.extend(
                item
                for item in (event.artifact, event.metric, event.verifier)
                if item is not None
            )
        selected: dict[str, SourceRef] = {}
        for item in refs:
            selected.setdefault(item.key, item)
        return tuple(selected.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "description": self.description,
            "owner": self.owner.to_dict(),
            "store": self.store.to_dict(),
            "writers": [item.to_dict() for item in self.writers],
            "projections": [item.to_dict() for item in self.projections],
            "caches": [item.to_dict() for item in self.caches],
            "checkpoint": self.checkpoint.to_dict(),
            "recovery": self.recovery.to_dict(),
            "entries": [item.to_dict() for item in self.entries],
            "events": [item.to_dict() for item in self.events],
            "tests": list(self.tests),
            "disable_probes": list(self.disable_probes),
            "fallback_refs": [item.to_dict() for item in self.fallback_refs],
            "forbidden_owner_claims": list(self.forbidden_owner_claims),
            "shared_owner_group": self.shared_owner_group,
        }


@dataclass(frozen=True, slots=True)
class RequirementEvidence:
    requirement_id: str
    description: str
    status: EvidenceStatus
    owner_domains: tuple[str, ...]
    default_entry_ids: tuple[str, ...]
    live_evidence_paths: tuple[str, ...]
    event_links: tuple[str, ...]
    mutation_refs: tuple[SourceRef, ...]
    artifact_paths: tuple[str, ...]
    metric_paths: tuple[str, ...]
    test_paths: tuple[str, ...]
    commits: tuple[str, ...]
    config_paths: tuple[str, ...]
    m3_owner: str

    @classmethod
    def parse(cls, raw: Any, *, field_name: str) -> Self:
        value = mapping(raw, field_name=field_name)
        mutation_raw = value.get("mutation_refs", ())
        if not isinstance(mutation_raw, Sequence) or isinstance(
            mutation_raw, (str, bytes, bytearray)
        ):
            raise CatalogError(f"{field_name}.mutation_refs must be an array")
        result = cls(
            requirement_id=requirement_id(
                value.get("requirement_id"),
                field_name=f"{field_name}.requirement_id",
            ),
            description=required_text(
                value.get("description"),
                field_name=f"{field_name}.description",
                minimum=12,
            ),
            status=enum_value(
                EvidenceStatus,
                value.get("status"),
                field_name=f"{field_name}.status",
            ),
            owner_domains=string_tuple(
                value.get("owner_domains"),
                field_name=f"{field_name}.owner_domains",
                identities=True,
            ),
            default_entry_ids=string_tuple(
                value.get("default_entry_ids"),
                field_name=f"{field_name}.default_entry_ids",
                identities=True,
            ),
            live_evidence_paths=string_tuple(
                value.get("live_evidence_paths"),
                field_name=f"{field_name}.live_evidence_paths",
                paths=True,
            ),
            event_links=string_tuple(
                value.get("event_links"),
                field_name=f"{field_name}.event_links",
                identities=True,
            ),
            mutation_refs=tuple(
                SourceRef.parse(
                    item,
                    field_name=f"{field_name}.mutation_refs[{index}]",
                    default_role=StateRole.WRITER,
                )
                for index, item in enumerate(mutation_raw)
            ),
            artifact_paths=string_tuple(
                value.get("artifact_paths"),
                field_name=f"{field_name}.artifact_paths",
                allow_empty=True,
                paths=True,
            ),
            metric_paths=string_tuple(
                value.get("metric_paths"),
                field_name=f"{field_name}.metric_paths",
                allow_empty=True,
                paths=True,
            ),
            test_paths=string_tuple(
                value.get("test_paths"),
                field_name=f"{field_name}.test_paths",
                paths=True,
            ),
            commits=tuple(
                revision(
                    item,
                    field_name=f"{field_name}.commits[{index}]",
                )
                for index, item in enumerate(
                    value.get("commits")
                    if isinstance(value.get("commits"), Sequence)
                    and not isinstance(value.get("commits"), (str, bytes, bytearray))
                    else ()
                )
            ),
            config_paths=string_tuple(
                value.get("config_paths"),
                field_name=f"{field_name}.config_paths",
                allow_empty=True,
                paths=True,
            ),
            m3_owner=identity(
                value.get("m3_owner"), field_name=f"{field_name}.m3_owner"
            ),
        )
        if not result.commits:
            raise CatalogError(f"{field_name}.commits cannot be empty")
        if result.status is EvidenceStatus.VERIFIED and (
            not result.artifact_paths and not result.metric_paths
        ):
            raise CatalogError(
                f"{result.requirement_id} verified evidence needs artifact or metric"
            )
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "description": self.description,
            "status": self.status.value,
            "owner_domains": list(self.owner_domains),
            "default_entry_ids": list(self.default_entry_ids),
            "live_evidence_paths": list(self.live_evidence_paths),
            "event_links": list(self.event_links),
            "mutation_refs": [item.to_dict() for item in self.mutation_refs],
            "artifact_paths": list(self.artifact_paths),
            "metric_paths": list(self.metric_paths),
            "test_paths": list(self.test_paths),
            "commits": list(self.commits),
            "config_paths": list(self.config_paths),
            "m3_owner": self.m3_owner,
        }


@dataclass(frozen=True, slots=True)
class AuditCatalog:
    schema: str
    owners: tuple[OwnerContract, ...]
    requirements: tuple[RequirementEvidence, ...]
    required_domains: tuple[str, ...]
    required_requirements: tuple[str, ...]
    catalog_digest: str

    @property
    def owner_by_domain(self) -> Mapping[str, OwnerContract]:
        return {item.domain: item for item in self.owners}

    @property
    def requirement_by_id(self) -> Mapping[str, RequirementEvidence]:
        return {item.requirement_id: item for item in self.requirements}

    @property
    def entry_by_id(self) -> Mapping[str, EntrypointSpec]:
        result: dict[str, EntrypointSpec] = {}
        for owner_contract in self.owners:
            for entry in owner_contract.entries:
                result[entry.entry_id] = entry
        return result

    @property
    def event_by_id(self) -> Mapping[str, EventMutationSpec]:
        result: dict[str, EventMutationSpec] = {}
        for owner_contract in self.owners:
            for event in owner_contract.events:
                result[event.link_id] = event
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "required_domains": list(self.required_domains),
            "required_requirements": list(self.required_requirements),
            "owners": [item.to_dict() for item in self.owners],
            "requirements": [item.to_dict() for item in self.requirements],
            "catalog_digest": self.catalog_digest,
        }


@dataclass(frozen=True, slots=True)
class GraphNode:
    node_id: str
    kind: NodeKind
    language: Language
    path: str
    symbol: str = ""
    executable: bool = True
    attributes: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ref_key(self) -> str:
        return f"{self.path}#{self.symbol}" if self.symbol else self.path

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind.value,
            "language": self.language.value,
            "path": self.path,
            "symbol": self.symbol,
            "executable": self.executable,
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True, slots=True)
class GraphEdge:
    source: str
    target: str
    kind: EdgeKind
    path: str = ""
    line: int = 0
    verified: bool = True
    attributes: Mapping[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return stable_digest(
            {
                "source": self.source,
                "target": self.target,
                "kind": self.kind.value,
                "path": self.path,
                "line": self.line,
            }
        ).split(":", 1)[1][:24]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "kind": self.kind.value,
            "path": self.path,
            "line": self.line,
            "verified": self.verified,
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True, slots=True)
class EvidencePointer:
    kind: str
    path: str
    line: int = 0
    symbol: str = ""
    digest: str = ""
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "line": self.line,
            "symbol": self.symbol,
            "digest": self.digest,
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    message: str
    rule_group: str
    severity: Severity
    domain: str = ""
    requirement_id: str = ""
    path: str = ""
    line: int = 0
    owner_unit: str = "M3-01B"
    disposition: Disposition = Disposition.TRACK
    default_path_impact: str = ""
    remediation: str = ""
    evidence: tuple[EvidencePointer, ...] = ()
    attributes: Mapping[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return (
            self.severity is Severity.BLOCKER
            or self.disposition is Disposition.BLOCK_RELEASE
        )

    @property
    def fingerprint(self) -> str:
        payload = {
            "code": self.code,
            "domain": self.domain,
            "requirement_id": self.requirement_id,
            "path": self.path,
            "line": self.line,
            "attributes": dict(self.attributes),
        }
        return stable_digest(payload).split(":", 1)[1][:24]

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "code": self.code,
            "message": self.message,
            "rule_group": self.rule_group,
            "severity": self.severity.value,
            "blocking": self.blocking,
            "domain": self.domain,
            "requirement_id": self.requirement_id,
            "path": self.path,
            "line": self.line,
            "owner_unit": self.owner_unit,
            "disposition": self.disposition.value,
            "default_path_impact": self.default_path_impact,
            "remediation": self.remediation,
            "evidence": [item.to_dict() for item in self.evidence],
            "attributes": dict(self.attributes),
        }


def finding(
    code: str,
    message: str,
    rule_group: str,
    *,
    severity: Severity = Severity.ERROR,
    **kwargs: Any,
) -> Finding:
    return Finding(
        code=identity(code, field_name="finding.code"),
        message=required_text(message, field_name="finding.message"),
        rule_group=identity(rule_group, field_name="finding.rule_group"),
        severity=severity,
        **kwargs,
    )


def deduplicate_findings(items: Iterable[Finding]) -> tuple[Finding, ...]:
    selected: dict[str, Finding] = {}
    for item in items:
        prior = selected.get(item.fingerprint)
        if prior is None or item.severity.rank > prior.severity.rank:
            selected[item.fingerprint] = item
    return tuple(
        sorted(
            selected.values(),
            key=lambda item: (
                -item.severity.rank,
                item.owner_unit,
                item.code,
                item.domain,
                item.requirement_id,
                item.path,
                item.line,
                item.fingerprint,
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class AuditSection:
    name: str
    metrics: Mapping[str, Any]
    findings: tuple[Finding, ...] = ()
    evidence: tuple[EvidencePointer, ...] = ()
    records: tuple[Mapping[str, Any], ...] = ()

    @property
    def valid(self) -> bool:
        return not any(item.blocking for item in self.findings)

    @property
    def digest(self) -> str:
        return stable_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "valid": self.valid,
            "metrics": dict(self.metrics),
            "findings": [item.to_dict() for item in self.findings],
            "evidence": [item.to_dict() for item in self.evidence],
            "records": [dict(item) for item in self.records],
        }


def section(
    name: str,
    *,
    metrics: Mapping[str, Any] | None = None,
    findings: Iterable[Finding] = (),
    evidence: Iterable[EvidencePointer] = (),
    records: Iterable[Mapping[str, Any]] = (),
) -> AuditSection:
    return AuditSection(
        name=identity(name, field_name="section.name"),
        metrics=dict(metrics or {}),
        findings=deduplicate_findings(findings),
        evidence=tuple(evidence),
        records=tuple(dict(item) for item in records),
    )


def findings_by_owner_unit(
    findings: Iterable[Finding],
) -> Mapping[str, tuple[Finding, ...]]:
    grouped: dict[str, list[Finding]] = defaultdict(list)
    for item in findings:
        grouped[item.owner_unit].append(item)
    return {
        key: deduplicate_findings(value)
        for key, value in sorted(grouped.items())
    }


def parse_source_ref_array(
    value: Any,
    *,
    field_name: str,
    default_role: StateRole,
    allow_empty: bool = False,
) -> tuple[SourceRef, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise CatalogError(f"{field_name} must be an array")
    result = tuple(
        SourceRef.parse(
            item,
            field_name=f"{field_name}[{index}]",
            default_role=default_role,
        )
        for index, item in enumerate(value)
    )
    if not result and not allow_empty:
        raise CatalogError(f"{field_name} cannot be empty")
    return result
