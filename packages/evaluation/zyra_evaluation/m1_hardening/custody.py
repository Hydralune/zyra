from __future__ import annotations

import ast
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .contracts import (
    EvidencePointer,
    Finding,
    GateResult,
    GateStatus,
    Severity,
    StateCustodyEntry,
)


class M1StateCustodyMap:
    def __init__(self, project_root: str | Path) -> None:
        self.root = Path(project_root).resolve()
        self._text_cache: dict[Path, str] = {}
        self._symbol_cache: dict[Path, set[str]] = {}

    def evaluate(
        self,
        entries: Sequence[StateCustodyEntry],
        *,
        required_families: Iterable[str] = (),
        runtime_required_families: Iterable[str] = (),
        task: Mapping[str, Any] | None = None,
        events: Sequence[Mapping[str, Any]] = (),
    ) -> GateResult:
        result = GateResult(
            gate_id="m1-state-custody",
            status=GateStatus.NOT_RUN,
            summary="Canonical state owner, persistence, restore and event-correlation audit.",
        )
        families: dict[str, list[StateCustodyEntry]] = defaultdict(list)
        for entry in entries:
            families[entry.state_family].append(entry)
            findings, evidence = self._audit_entry(entry)
            result.findings.extend(findings)
            result.evidence.extend(evidence)
        result.findings.extend(self._audit_required(required_families, families))
        result.findings.extend(self._audit_duplicate_owners(families))
        result.findings.extend(self._audit_store_overlap(entries))
        result.findings.extend(self._audit_restore_chains(entries))
        result.findings.extend(
            self._audit_runtime_projection(
                entries,
                task or {},
                events,
                required_families=set(runtime_required_families),
            )
        )
        result.findings.extend(self._audit_event_causality(entries, events))
        result.findings.extend(self._audit_derivative_boundaries(entries))

        result.metrics.update(
            {
                "state_family_count": len(families),
                "entry_count": len(entries),
                "owner_count": len({entry.canonical_owner for entry in entries}),
                "store_path_count": len({path for entry in entries for path in entry.store_paths}),
                "restore_entry_count": sum(len(entry.restore_entries) for entry in entries),
                "event_type_count": len({event for entry in entries for event in entry.event_types}),
                "entries": [entry.to_dict() for entry in entries],
            }
        )
        return result.finish()

    def _audit_entry(self, entry: StateCustodyEntry) -> tuple[list[Finding], list[EvidencePointer]]:
        findings: list[Finding] = []
        evidence: list[EvidencePointer] = []
        if not entry.canonical_owner:
            findings.append(
                Finding(
                    code="custody.owner_missing",
                    severity=Severity.BLOCKER,
                    summary="State family has no canonical owner.",
                    capability=entry.state_family,
                )
            )
        for category, values in (
            ("schema", entry.schema_paths),
            ("store", entry.store_paths),
        ):
            if not values:
                findings.append(
                    Finding(
                        code=f"custody.{category}_missing",
                        severity=Severity.BLOCKER,
                        summary=f"State family has no {category} path.",
                        capability=entry.state_family,
                    )
                )
            for raw in values:
                path = self._resolve(raw)
                if path is None or not path.exists():
                    findings.append(
                        Finding(
                            code=f"custody.{category}_path_missing",
                            severity=Severity.BLOCKER,
                            summary=f"Declared {category} path does not exist.",
                            capability=entry.state_family,
                            location=raw,
                        )
                    )
                else:
                    evidence.append(
                        EvidencePointer(
                            kind=f"state_{category}",
                            location=path.relative_to(self.root).as_posix(),
                            summary=f"{entry.state_family} {category} is inside Zyra.",
                        )
                    )
        for category, values in (
            ("write", entry.write_entries),
            ("read", entry.read_entries),
            ("restore", entry.restore_entries),
        ):
            if not values:
                findings.append(
                    Finding(
                        code=f"custody.{category}_entry_missing",
                        severity=Severity.BLOCKER if category in {"write", "restore"} else Severity.ERROR,
                        summary=f"State family has no {category} entry.",
                        capability=entry.state_family,
                    )
                )
                continue
            for value in values:
                if not self._entry_exists(value, (*entry.store_paths, *entry.schema_paths)):
                    findings.append(
                        Finding(
                            code=f"custody.{category}_entry_unresolved",
                            severity=Severity.ERROR,
                            summary=f"Declared {category} entry was not found in the owner source.",
                            capability=entry.state_family,
                            location=value,
                        )
                    )
                else:
                    evidence.append(
                        EvidencePointer(
                            kind=f"state_{category}_entry",
                            location=value,
                            summary=f"{entry.state_family} {category} entry is present.",
                        )
                    )
        if not entry.revision_fields:
            findings.append(
                Finding(
                    code="custody.revision_field_missing",
                    severity=Severity.ERROR,
                    summary="State family cannot prove before/after revision changes.",
                    capability=entry.state_family,
                )
            )
        if not entry.correlation_fields:
            findings.append(
                Finding(
                    code="custody.correlation_field_missing",
                    severity=Severity.ERROR,
                    summary="State family has no run/task/causation correlation field.",
                    capability=entry.state_family,
                )
            )
        if not entry.idempotency_fields:
            findings.append(
                Finding(
                    code="custody.idempotency_field_missing",
                    severity=Severity.WARNING,
                    summary="State family has no explicit idempotency or stable request identity.",
                    capability=entry.state_family,
                )
            )
        return findings, evidence

    @staticmethod
    def _audit_required(
        required: Iterable[str],
        families: Mapping[str, Sequence[StateCustodyEntry]],
    ) -> list[Finding]:
        return [
            Finding(
                code="custody.required_family_missing",
                severity=Severity.BLOCKER,
                summary="Required M1 state family is absent from the custody map.",
                capability=family,
            )
            for family in sorted(set(required) - set(families))
        ]

    @staticmethod
    def _audit_duplicate_owners(
        families: Mapping[str, Sequence[StateCustodyEntry]],
    ) -> list[Finding]:
        findings: list[Finding] = []
        for family, entries in families.items():
            owners = {entry.canonical_owner for entry in entries if entry.canonical_owner}
            if len(owners) > 1:
                findings.append(
                    Finding(
                        code="custody.multiple_canonical_owners",
                        severity=Severity.BLOCKER,
                        summary="A state family declares multiple canonical owners.",
                        capability=family,
                        detail=", ".join(sorted(owners)),
                    )
                )
            if len(entries) > 1 and len(owners) == 1:
                findings.append(
                    Finding(
                        code="custody.duplicate_family_records",
                        severity=Severity.WARNING,
                        summary="A state family has duplicate custody records.",
                        capability=family,
                    )
                )
        return findings

    def _audit_store_overlap(self, entries: Sequence[StateCustodyEntry]) -> list[Finding]:
        by_path: dict[str, list[StateCustodyEntry]] = defaultdict(list)
        for entry in entries:
            for raw in entry.store_paths:
                path = self._resolve(raw)
                key = str(path or raw).lower()
                by_path[key].append(entry)
        findings: list[Finding] = []
        for path, owners in by_path.items():
            families = {entry.state_family for entry in owners}
            canonical = {entry.canonical_owner for entry in owners}
            if len(families) > 1 and len(canonical) > 1:
                findings.append(
                    Finding(
                        code="custody.store_path_shared_by_owners",
                        severity=Severity.ERROR,
                        summary="One store path is claimed by multiple canonical owners.",
                        detail=f"families={','.join(sorted(families))}; owners={','.join(sorted(canonical))}",
                        location=path,
                    )
                )
        return findings

    def _audit_restore_chains(self, entries: Sequence[StateCustodyEntry]) -> list[Finding]:
        findings: list[Finding] = []
        for entry in entries:
            store_text = "\n".join(self._read_path(raw) for raw in entry.store_paths)
            restore_text = "\n".join(self._read_entry_file(raw) for raw in entry.restore_entries)
            combined = f"{store_text}\n{restore_text}".lower()
            if not combined.strip():
                continue
            persistence_markers = ("write", "save", "append", "commit", "insert", "upsert", "persist")
            restore_markers = ("restore", "load", "resume", "replay", "recover", "snapshot")
            if not any(marker in combined for marker in persistence_markers):
                findings.append(
                    Finding(
                        code="custody.persistence_semantics_unseen",
                        severity=Severity.ERROR,
                        summary="Store source has no visible persistence/commit semantic marker.",
                        capability=entry.state_family,
                    )
                )
            if not any(marker in combined for marker in restore_markers):
                findings.append(
                    Finding(
                        code="custody.restore_semantics_unseen",
                        severity=Severity.ERROR,
                        summary="Owner source has no visible restore/resume/replay semantic marker.",
                        capability=entry.state_family,
                    )
                )
            if entry.state_family in {"permission", "worker_lease", "recovery", "graph_topology", "provider_backend"}:
                fence_markers = ("idempot", "fence", "revision", "expected_version", "compare", "conflict")
                if not any(marker in combined for marker in fence_markers):
                    findings.append(
                        Finding(
                            code="custody.concurrent_write_guard_unseen",
                            severity=Severity.BLOCKER,
                            summary="High-risk state owner lacks visible revision/idempotency/fence semantics.",
                            capability=entry.state_family,
                        )
                    )
        return findings

    def _audit_runtime_projection(
        self,
        entries: Sequence[StateCustodyEntry],
        task: Mapping[str, Any],
        events: Sequence[Mapping[str, Any]],
        *,
        required_families: set[str],
    ) -> list[Finding]:
        if not task and not events:
            return []
        findings: list[Finding] = []
        task_text = repr(task).lower()
        event_types = Counter(str(event.get("event_type") or "").lower() for event in events)
        event_text = "\n".join(repr(event).lower() for event in events)
        for entry in entries:
            expected_events = set(entry.event_types)
            observed = {
                expected
                for expected in expected_events
                if expected.lower() in event_types or self._runtime_family_observed(entry.state_family, expected, event_text, events)
            }
            if expected_events and not observed:
                severity = Severity.ERROR if entry.state_family in required_families else Severity.INFO
                findings.append(
                    Finding(
                        code="custody.runtime_event_unobserved",
                        severity=severity,
                        summary="No declared owner event type was observed in the task trace.",
                        capability=entry.state_family,
                        detail=", ".join(sorted(expected_events)),
                    )
                )
            owner_tokens = {token.lower() for token in re.split(r"[^A-Za-z0-9_]+", entry.canonical_owner) if len(token) >= 4}
            if owner_tokens and not any(token in task_text for token in owner_tokens):
                findings.append(
                    Finding(
                        code="custody.owner_not_projected",
                        severity=Severity.INFO,
                        summary="Task projection does not name the canonical owner; event evidence remains authoritative.",
                        capability=entry.state_family,
                    )
                )
        return findings

    @staticmethod
    def _runtime_family_observed(
        family: str,
        expected_event: str,
        event_text: str,
        events: Sequence[Mapping[str, Any]],
    ) -> bool:
        if expected_event.lower() in event_text:
            return True
        markers = {
            "task_session": ("task_created", "requirement_change", "control_command", "session"),
            "runtime_event": ("event_id", "event_type", "tool", "artifact", "permission"),
            "permission": ("permission", "approval", "suspended", "denied"),
            "workspace": ("artifact", "workspace", "export", "file"),
            "memory": ("memory", "compact", "retrieval"),
            "worker_lease": ("lease", "worker_admitted", "heartbeat"),
            "provider_backend": ("provider", "backend_route", "model_request"),
            "graph_topology": ("topology", "graph_delta", "branch_delta"),
            "recovery": ("recovery", "retry", "resume", "replan"),
            "skill_invocation": ("skill_invoked", "skill_completed", "skill_failed"),
            "browser_session": ("browser_started", "browser_action", "browser_crashed"),
            "code_index": ("code_index", "symbol", "index_invalidated"),
        }
        if family == "runtime_event":
            return bool(events)
        return any(marker in event_text for marker in markers.get(family, ()))

    @staticmethod
    def _families_implied_by_task(task_text: str) -> set[str]:
        mapping = {
            "permission": ("permission", "approval"),
            "session": ("session", "query"),
            "memory": ("memory", "compact"),
            "worker_lease": ("worker", "lease"),
            "provider_backend": ("provider", "backend"),
            "graph_topology": ("graph", "topology"),
            "recovery": ("recovery", "fault"),
            "artifact": ("artifact",),
            "runtime_event": ("event",),
        }
        return {family for family, tokens in mapping.items() if any(token in task_text for token in tokens)}

    @staticmethod
    def _audit_event_causality(
        entries: Sequence[StateCustodyEntry],
        events: Sequence[Mapping[str, Any]],
    ) -> list[Finding]:
        if not events:
            return []
        known_types = {event_type for entry in entries for event_type in entry.event_types}
        findings: list[Finding] = []
        seen_ids: set[str] = set()
        for index, event in enumerate(events):
            event_type = str(event.get("event_type") or "")
            event_id = str(event.get("event_id") or "")
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            causation = str(
                event.get("causation_id")
                or payload.get("causation_id")
                or payload.get("cause_event_id")
                or payload.get("request_id")
                or ""
            )
            if event_id:
                if event_id in seen_ids:
                    findings.append(
                        Finding(
                            code="custody.duplicate_event_id",
                            severity=Severity.BLOCKER,
                            summary="Task trace contains a duplicate canonical event id.",
                            detail=event_id,
                            location=f"events[{index}]",
                        )
                    )
                seen_ids.add(event_id)
            if event_type in known_types and index > 0 and not causation:
                findings.append(
                    Finding(
                        code="custody.event_causation_missing",
                        severity=Severity.WARNING,
                        summary="A state-owner event has no causation/request correlation.",
                        detail=event_type,
                        location=f"events[{index}]",
                    )
                )
        return findings

    @staticmethod
    def _audit_derivative_boundaries(entries: Sequence[StateCustodyEntry]) -> list[Finding]:
        findings: list[Finding] = []
        owner_by_family = {entry.state_family: entry.canonical_owner for entry in entries}
        for entry in entries:
            for consumer in entry.derivative_consumers:
                if consumer == entry.canonical_owner:
                    continue
                if consumer in entry.forbidden_owners:
                    findings.append(
                        Finding(
                            code="custody.forbidden_derivative_consumer",
                            severity=Severity.BLOCKER,
                            summary="A forbidden runtime is listed as a derivative state consumer.",
                            capability=entry.state_family,
                            detail=consumer,
                        )
                    )
            for forbidden in entry.forbidden_owners:
                if owner_by_family.get(entry.state_family) == forbidden:
                    findings.append(
                        Finding(
                            code="custody.forbidden_canonical_owner",
                            severity=Severity.BLOCKER,
                            summary="A rejected/reference runtime owns canonical state.",
                            capability=entry.state_family,
                            detail=forbidden,
                        )
                    )
        return findings

    def _entry_exists(self, entry: str, fallback_paths: Sequence[str]) -> bool:
        path_part, symbol = self._split_entry(entry)
        paths = [path_part] if path_part else list(fallback_paths)
        for raw in paths:
            path = self._resolve(raw)
            if path is None or not path.exists():
                continue
            candidates = [path] if path.is_file() else [item for item in path.rglob("*") if item.is_file()]
            if not symbol:
                return bool(candidates)
            if any(symbol in self._symbols(candidate) or symbol.rsplit(".", 1)[-1] in self._symbols(candidate) for candidate in candidates):
                return True
        return False

    @staticmethod
    def _split_entry(entry: str) -> tuple[str, str]:
        if "::" in entry:
            return tuple(part.strip() for part in entry.rsplit("::", 1))  # type: ignore[return-value]
        return "", entry.strip()

    def _symbols(self, path: Path) -> set[str]:
        cached = self._symbol_cache.get(path)
        if cached is not None:
            return cached
        text = self._text(path)
        symbols: set[str] = set()
        if path.suffix in {".py", ".pyi"}:
            try:
                tree = ast.parse(text)
            except SyntaxError:
                tree = None
            if tree is not None:
                symbols.update(
                    node.name
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                )
        else:
            symbols.update(
                re.findall(r"\b(?:class|function|interface|type|const)\s+([A-Za-z_$][A-Za-z0-9_$]*)", text)
            )
        self._symbol_cache[path] = symbols
        return symbols

    def _read_path(self, raw: str) -> str:
        path = self._resolve(raw)
        if path is None or not path.exists():
            return ""
        if path.is_file():
            return self._text(path)
        return "\n".join(
            self._text(item)
            for item in path.rglob("*")
            if item.is_file() and item.suffix in {".py", ".ts", ".tsx", ".rs"}
        )

    def _read_entry_file(self, entry: str) -> str:
        path_part, _symbol = self._split_entry(entry)
        return self._read_path(path_part) if path_part else ""

    def _text(self, path: Path) -> str:
        if path not in self._text_cache:
            try:
                self._text_cache[path] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                self._text_cache[path] = ""
        return self._text_cache[path]

    def _resolve(self, raw: str) -> Path | None:
        if not raw:
            return None
        path = Path(raw)
        if not path.is_absolute():
            path = self.root / path
        try:
            resolved = path.resolve(strict=False)
        except OSError:
            return None
        return resolved if resolved.is_relative_to(self.root) else None
