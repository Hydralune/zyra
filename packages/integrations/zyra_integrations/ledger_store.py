from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ledger_models import (
    AuditDisposition,
    InternalizationLedgerEntry,
    LedgerLifecycle,
    LedgerQuery,
    MainPathStatus,
    MigrationStrategy,
    to_jsonable,
)


SEED_FILENAME = "internalization_ledger_seed.json"


@dataclass(slots=True)
class LedgerMutation:
    action: str
    ledger_id: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


@dataclass(slots=True)
class LedgerSummary:
    total_entries: int
    by_source_repo: dict[str, int] = field(default_factory=dict)
    by_owner_unit: dict[str, int] = field(default_factory=dict)
    by_lifecycle: dict[str, int] = field(default_factory=dict)
    by_main_path_status: dict[str, int] = field(default_factory=dict)
    by_strategy: dict[str, int] = field(default_factory=dict)
    active_or_internalized: int = 0
    planned_or_candidate: int = 0
    blocked_or_rejected: int = 0
    primary_target_prefixes: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


class InternalizationLedger:
    def __init__(self, entries: list[InternalizationLedgerEntry] | None = None) -> None:
        self._entries: dict[str, InternalizationLedgerEntry] = {}
        self._mutations: list[LedgerMutation] = []
        for entry in entries or []:
            self.add(entry)

    @property
    def mutations(self) -> list[LedgerMutation]:
        return list(self._mutations)

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        return iter(self.entries())

    def entries(self) -> list[InternalizationLedgerEntry]:
        return sorted(
            self._entries.values(),
            key=lambda entry: (entry.owner_unit, entry.source_repo.lower(), entry.capability_name.lower(), entry.source_path),
        )

    def ids(self) -> list[str]:
        return [entry.ledger_id for entry in self.entries()]

    def add(self, entry: InternalizationLedgerEntry) -> None:
        if entry.ledger_id in self._entries:
            raise ValueError(f"Duplicate ledger_id: {entry.ledger_id}")
        self._entries[entry.ledger_id] = entry
        self._mutations.append(LedgerMutation(action="add", ledger_id=entry.ledger_id, after=entry.to_dict()))

    def upsert(self, entry: InternalizationLedgerEntry) -> LedgerMutation:
        before = self._entries.get(entry.ledger_id)
        mutation = LedgerMutation(
            action="update" if before is not None else "add",
            ledger_id=entry.ledger_id,
            before=before.to_dict() if before is not None else None,
            after=entry.to_dict(),
        )
        self._entries[entry.ledger_id] = entry
        self._mutations.append(mutation)
        return mutation

    def remove(self, ledger_id: str) -> LedgerMutation:
        before = self._entries.pop(ledger_id)
        mutation = LedgerMutation(action="remove", ledger_id=ledger_id, before=before.to_dict(), after=None)
        self._mutations.append(mutation)
        return mutation

    def get(self, ledger_id: str) -> InternalizationLedgerEntry | None:
        return self._entries.get(ledger_id)

    def require(self, ledger_id: str) -> InternalizationLedgerEntry:
        entry = self.get(ledger_id)
        if entry is None:
            raise KeyError(f"Unknown ledger entry: {ledger_id}")
        return entry

    def query(self, query: LedgerQuery) -> list[InternalizationLedgerEntry]:
        matches = [entry for entry in self.entries() if query.matches(entry)]
        return matches[: max(0, query.limit)]

    def by_source_repo(self, source_repo: str) -> list[InternalizationLedgerEntry]:
        return self.query(LedgerQuery(source_repo=source_repo, limit=10_000))

    def by_owner_unit(self, owner_unit: str) -> list[InternalizationLedgerEntry]:
        return self.query(LedgerQuery(owner_unit=owner_unit, limit=10_000))

    def by_target_prefix(self, prefix: str) -> list[InternalizationLedgerEntry]:
        return [
            entry
            for entry in self.entries()
            if any(target.startswith(prefix) for target in entry.target_paths)
        ]

    def validate_unique_source_targets(self) -> list[str]:
        errors: list[str] = []
        seen: dict[tuple[str, str, str], str] = {}
        for entry in self.entries():
            key = (entry.source_repo, entry.source_path, entry.capability_name)
            existing = seen.get(key)
            if existing:
                errors.append(f"{entry.ledger_id} duplicates source/capability tuple already used by {existing}")
            else:
                seen[key] = entry.ledger_id
        return errors

    def validate_schema(self) -> list[str]:
        errors: list[str] = []
        for entry in self.entries():
            for error in entry.validate():
                errors.append(f"{entry.ledger_id}: {error}")
        errors.extend(self.validate_unique_source_targets())
        return errors

    def summary(self) -> LedgerSummary:
        by_source = Counter(entry.source_repo for entry in self.entries())
        by_owner = Counter(entry.owner_unit or "unassigned" for entry in self.entries())
        by_lifecycle = Counter(str(entry.lifecycle) for entry in self.entries())
        by_status = Counter(str(entry.main_path_status) for entry in self.entries())
        by_strategy = Counter(str(entry.migration_strategy) for entry in self.entries())
        prefixes: Counter[str] = Counter()
        for entry in self.entries():
            primary = entry.primary_target_path
            prefix = "/".join(primary.split("/")[:2]) if primary else "missing"
            prefixes[prefix] += 1
        active = sum(
            1
            for entry in self.entries()
            if entry.lifecycle in {LedgerLifecycle.ACTIVE, LedgerLifecycle.INTERNALIZED, LedgerLifecycle.PRODUCTIZED}
        )
        planned = sum(
            1
            for entry in self.entries()
            if entry.lifecycle in {LedgerLifecycle.CANDIDATE, LedgerLifecycle.PLANNED, LedgerLifecycle.IN_PROGRESS}
        )
        blocked = sum(
            1
            for entry in self.entries()
            if entry.lifecycle in {LedgerLifecycle.DEFERRED, LedgerLifecycle.REJECTED}
            or entry.main_path_status in {MainPathStatus.BLOCKED, MainPathStatus.REJECTED}
        )
        return LedgerSummary(
            total_entries=len(self),
            by_source_repo=dict(sorted(by_source.items())),
            by_owner_unit=dict(sorted(by_owner.items())),
            by_lifecycle=dict(sorted(by_lifecycle.items())),
            by_main_path_status=dict(sorted(by_status.items())),
            by_strategy=dict(sorted(by_strategy.items())),
            active_or_internalized=active,
            planned_or_candidate=planned,
            blocked_or_rejected=blocked,
            primary_target_prefixes=dict(sorted(prefixes.items())),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "kind": "zyra.internalization_ledger",
            "summary": self.summary().to_dict(),
            "entries": [entry.to_dict() for entry in self.entries()],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, normalize_current_policy: bool = False) -> "InternalizationLedger":
        entries = [
            InternalizationLedgerEntry.from_dict(item)
            for item in _entry_payloads(data)
        ]
        if normalize_current_policy:
            from .ledger_migrations import normalize_ledger_for_current_policy

            entries = normalize_ledger_for_current_policy(entries)
        ledger = cls(entries)
        ledger._mutations.clear()
        return ledger

    @classmethod
    def load(cls, path: str | Path, *, normalize_current_policy: bool = False) -> "InternalizationLedger":
        target = Path(path)
        data = _read_structured_file(target)
        return cls.from_dict(data, normalize_current_policy=normalize_current_policy)

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_structured_file(target, self.to_dict())
        return target

    def save_filtered(self, path: str | Path, query: LedgerQuery) -> Path:
        return InternalizationLedger(self.query(query)).save(path)

    def disposition(self) -> AuditDisposition:
        errors = self.validate_schema()
        if errors:
            return AuditDisposition.FAILING
        if any(entry.lifecycle == LedgerLifecycle.REJECTED or entry.main_path_status == MainPathStatus.BLOCKED for entry in self.entries()):
            return AuditDisposition.WARNING
        return AuditDisposition.PASSING


def project_ledger_path(project_root: Path) -> Path:
    configured = os.environ.get("ZYRA_INTEGRATION_LEDGER")
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else project_root / path
    return project_root / "tmp" / "internalization_ledger.json"


def package_seed_path() -> Path:
    return Path(__file__).resolve().parent / "data" / SEED_FILENAME


def load_seed_ledger() -> InternalizationLedger:
    return InternalizationLedger.load(package_seed_path(), normalize_current_policy=True)


def load_project_ledger(project_root: Path, *, bootstrap: bool = True) -> InternalizationLedger:
    path = project_ledger_path(project_root)
    if path.exists():
        return InternalizationLedger.load(path, normalize_current_policy=True)
    ledger = load_seed_ledger()
    if bootstrap:
        ledger.save(path)
    return ledger


def save_project_ledger(project_root: Path, ledger: InternalizationLedger) -> Path:
    return ledger.save(project_ledger_path(project_root))


def merge_ledgers(base: InternalizationLedger, incoming: InternalizationLedger) -> InternalizationLedger:
    merged = InternalizationLedger(base.entries())
    for entry in incoming.entries():
        merged.upsert(entry)
    merged._mutations.clear()
    return merged


def parse_query(params: dict[str, Any]) -> LedgerQuery:
    return LedgerQuery(
        source_repo=str(params.get("source_repo") or params.get("repo") or ""),
        owner_unit=str(params.get("owner_unit") or params.get("unit") or ""),
        milestone=str(params.get("milestone") or ""),
        lifecycle=_optional_enum(LedgerLifecycle, params.get("lifecycle")),
        main_path_status=_optional_enum(MainPathStatus, params.get("main_path_status") or params.get("status")),
        migration_strategy=_optional_enum(MigrationStrategy, params.get("migration_strategy") or params.get("strategy")),
        target_contains=str(params.get("target_contains") or params.get("target") or ""),
        capability_contains=str(params.get("capability_contains") or params.get("q") or ""),
        tag=str(params.get("tag") or ""),
        limit=_positive_int(params.get("limit"), default=200),
    )


def grouped_by_owner_unit(ledger: InternalizationLedger) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in ledger.entries():
        grouped[entry.owner_unit or "unassigned"].append(entry.to_dict())
    return dict(sorted(grouped.items()))


def _entry_payloads(data: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(data.get("entries"), list):
        return [dict(item) for item in data["entries"] if isinstance(item, dict)]
    if isinstance(data.get("ledger"), list):
        return [dict(item) for item in data["ledger"] if isinstance(item, dict)]
    if isinstance(data, list):
        return [dict(item) for item in data if isinstance(item, dict)]
    return []


def _read_structured_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {"entries": []}
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"{path} is not valid JSON-compatible YAML. Zyra intentionally writes YAML as the JSON subset "
            "so the ledger stays dependency-free."
        ) from error


def _write_structured_file(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")


def _optional_enum(enum_type: type[Any], value: Any) -> Any | None:
    if value in (None, ""):
        return None
    try:
        return enum_type(str(value))
    except ValueError:
        return None


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
