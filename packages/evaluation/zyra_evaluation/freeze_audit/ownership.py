from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import (
    AuditCatalog,
    AuditSection,
    Disposition,
    EvidencePointer,
    Finding,
    OwnerContract,
    RuleSwitches,
    Severity,
    SourceRef,
    StateRole,
    deduplicate_findings,
    finding,
    section,
)
from .python_graph import PythonFileFacts, PythonGraphResult
from .reachability import CodeGraph, ReferenceObservation
from .script_graph import ScriptFileFacts, ScriptGraphResult


WRITE_SIGNAL_PATTERN = re.compile(
    r"(?i)\b(?:commit|save|store|persist|write|append|insert|update|upsert|"
    r"replace|delete|mutate|transition|compare_and_swap|set_[a-z0-9_]+)\b"
)
READ_ONLY_SIGNAL_PATTERN = re.compile(
    r"(?i)\b(?:read[-_ ]?only|projection|reconstructible|derived|cache|view[-_ ]?local)\b"
)
CANONICAL_SIGNAL_PATTERN = re.compile(
    r"(?i)\b(?:canonical|authoritative|durable|source[-_ ]of[-_ ]truth)\b"
)
DISABLE_SIGNAL_PATTERN = re.compile(
    r"(?i)\b(?:disable|disconnect|owner[-_ ]loss|fail[-_ ]closed|unavailable)\b"
)


@dataclass(frozen=True, slots=True)
class OwnerReferenceRecord:
    domain: str
    role: str
    reference: SourceRef
    observation: ReferenceObservation
    write_signal: bool
    persistent_signal: bool
    canonical_claim: bool
    read_only_claim: bool

    @property
    def valid(self) -> bool:
        if not self.reference.required and not self.observation.exists:
            return True
        if not self.observation.valid:
            return False
        if self.role in {
            StateRole.STORE.value,
            StateRole.WRITER.value,
            StateRole.CHECKPOINT.value,
            StateRole.RECOVERY.value,
        }:
            return self.write_signal or self.persistent_signal
        if self.role in {
            StateRole.PROJECTION.value,
            StateRole.CACHE.value,
            StateRole.FALLBACK.value,
        }:
            return not self.canonical_claim
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "role": self.role,
            "reference": self.reference.to_dict(),
            "observation": self.observation.to_dict(),
            "write_signal": self.write_signal,
            "persistent_signal": self.persistent_signal,
            "canonical_claim": self.canonical_claim,
            "read_only_claim": self.read_only_claim,
            "valid": self.valid,
        }


@dataclass(frozen=True, slots=True)
class OwnershipResult:
    records: tuple[OwnerReferenceRecord, ...]
    section: AuditSection

    @property
    def valid_domains(self) -> frozenset[str]:
        grouped: dict[str, list[OwnerReferenceRecord]] = defaultdict(list)
        for item in self.records:
            grouped[item.domain].append(item)
        return frozenset(
            domain for domain, records in grouped.items() if all(item.valid for item in records)
        )


class OwnershipAuditor:
    def __init__(
        self,
        project_root: str | Path,
        *,
        switches: RuleSwitches | None = None,
    ) -> None:
        self.root = Path(project_root).resolve(strict=False)
        self.switches = switches or RuleSwitches()
        self._source_cache: dict[str, str] = {}

    def audit(
        self,
        catalog: AuditCatalog,
        graph: CodeGraph,
        python: PythonGraphResult,
        script: ScriptGraphResult,
    ) -> OwnershipResult:
        if not self.switches.ownership:
            return OwnershipResult(
                records=(),
                section=section(
                    "state_ownership",
                    metrics={
                        "rule_enabled": False,
                        "domain_count": len(catalog.owners),
                    },
                ),
            )
        python_by_path = python.by_path
        script_by_path = script.by_path
        records: list[OwnerReferenceRecord] = []
        findings: list[Finding] = []
        evidence: list[EvidencePointer] = []
        for contract in catalog.owners:
            contract_records = self._records_for_contract(
                contract,
                graph,
                python_by_path,
                script_by_path,
            )
            records.extend(contract_records)
            findings.extend(self._findings_for_records(contract, contract_records))
            findings.extend(self._validate_contract_tests(contract))
            findings.extend(self._scan_forbidden_claims(contract))
            findings.extend(
                self._scan_undeclared_canonical_candidates(
                    contract,
                    python,
                    script,
                )
            )
            evidence.extend(
                EvidencePointer(
                    kind=f"state_{item.role}",
                    path=item.reference.path,
                    symbol=item.reference.symbol,
                    attributes={
                        "domain": contract.domain,
                        "valid": item.valid,
                        "write_signal": item.write_signal,
                        "persistent_signal": item.persistent_signal,
                        "canonical_claim": item.canonical_claim,
                    },
                )
                for item in contract_records
            )
        findings.extend(self._validate_unique_contract_authority(catalog))
        metrics = {
            "rule_enabled": True,
            "domain_count": len(catalog.owners),
            "reference_count": len(records),
            "valid_reference_count": sum(item.valid for item in records),
            "invalid_reference_count": sum(not item.valid for item in records),
            "role_counts": dict(
                sorted(Counter(item.role for item in records).items())
            ),
            "owner_count": sum(item.role == StateRole.OWNER.value for item in records),
            "store_count": sum(item.role == StateRole.STORE.value for item in records),
            "writer_count": sum(item.role == StateRole.WRITER.value for item in records),
            "projection_count": sum(
                item.role == StateRole.PROJECTION.value for item in records
            ),
            "cache_count": sum(item.role == StateRole.CACHE.value for item in records),
            "canonical_claim_count": sum(item.canonical_claim for item in records),
        }
        return OwnershipResult(
            records=tuple(records),
            section=section(
                "state_ownership",
                metrics=metrics,
                findings=findings,
                evidence=evidence,
                records=(item.to_dict() for item in records),
            ),
        )

    def _records_for_contract(
        self,
        contract: OwnerContract,
        graph: CodeGraph,
        python_by_path: Mapping[str, PythonFileFacts],
        script_by_path: Mapping[str, ScriptFileFacts],
    ) -> tuple[OwnerReferenceRecord, ...]:
        references: list[tuple[str, SourceRef]] = [
            (StateRole.OWNER.value, contract.owner),
            (StateRole.STORE.value, contract.store),
            *((StateRole.WRITER.value, item) for item in contract.writers),
            *((StateRole.PROJECTION.value, item) for item in contract.projections),
            *((StateRole.CACHE.value, item) for item in contract.caches),
            (StateRole.CHECKPOINT.value, contract.checkpoint),
            (StateRole.RECOVERY.value, contract.recovery),
            *((StateRole.FALLBACK.value, item) for item in contract.fallback_refs),
        ]
        records: list[OwnerReferenceRecord] = []
        for role, reference in references:
            observation = graph.resolve_ref(reference)
            source = self._read(reference.path)
            python_fact = python_by_path.get(reference.path)
            script_fact = script_by_path.get(reference.path)
            write_signal = self._write_signal(
                reference,
                source,
                python_fact,
                script_fact,
            )
            persistent_signal = self._persistent_signal(
                reference,
                python_fact,
                script_fact,
            )
            selected_text = self._symbol_text(reference, source)
            records.append(
                OwnerReferenceRecord(
                    domain=contract.domain,
                    role=role,
                    reference=reference,
                    observation=observation,
                    write_signal=write_signal,
                    persistent_signal=persistent_signal,
                    canonical_claim=bool(
                        CANONICAL_SIGNAL_PATTERN.search(selected_text)
                    ),
                    read_only_claim=bool(
                        READ_ONLY_SIGNAL_PATTERN.search(selected_text)
                    ),
                )
            )
        return tuple(records)

    def _findings_for_records(
        self,
        contract: OwnerContract,
        records: Sequence[OwnerReferenceRecord],
    ) -> tuple[Finding, ...]:
        findings: list[Finding] = []
        for record in records:
            common = {
                "domain": contract.domain,
                "path": record.reference.path,
                "owner_unit": "M3-01B",
                "disposition": Disposition.BLOCK_RELEASE,
                "default_path_impact": (
                    f"{contract.domain} cannot prove its {record.role} authority."
                ),
            }
            if not record.observation.exists and record.reference.required:
                findings.append(
                    finding(
                        "state_owner_reference_missing",
                        (
                            f"{contract.domain} {record.role} reference is missing: "
                            f"{record.reference.key}."
                        ),
                        "ownership",
                        severity=Severity.BLOCKER,
                        remediation=(
                            "Restore the executable owner boundary or update the "
                            "catalog to the actual canonical implementation."
                        ),
                        **common,
                    )
                )
                continue
            if not record.observation.executable and record.reference.required:
                findings.append(
                    finding(
                        "state_owner_reference_nonexecutable",
                        (
                            f"{contract.domain} {record.role} has no executable "
                            f"behavior: {record.reference.key}."
                        ),
                        "ownership",
                        severity=Severity.BLOCKER,
                        remediation=(
                            "Use a real runtime symbol rather than a protocol, DTO, "
                            "manifest, report, fixture, or static declaration."
                        ),
                        **common,
                    )
                )
            if not record.observation.production and record.reference.required:
                findings.append(
                    finding(
                        "state_owner_reference_nonproduction",
                        (
                            f"{contract.domain} {record.role} is only present in a "
                            "test/demo/report/source-pool path."
                        ),
                        "ownership",
                        severity=Severity.BLOCKER,
                        remediation="Move authority to packaged production code.",
                        **common,
                    )
                )
            if (
                record.role
                in {
                    StateRole.STORE.value,
                    StateRole.WRITER.value,
                    StateRole.CHECKPOINT.value,
                    StateRole.RECOVERY.value,
                }
                and record.observation.valid
                and not (record.write_signal or record.persistent_signal)
            ):
                findings.append(
                    finding(
                        "state_write_semantic_signal_missing",
                        (
                            f"{contract.domain} {record.role} exists but exposes no "
                            f"observable state mutation: {record.reference.key}."
                        ),
                        "ownership",
                        severity=Severity.BLOCKER,
                        remediation=(
                            "Point at the real transaction/write/recovery method and "
                            "prove persistent mutation behavior."
                        ),
                        **common,
                    )
                )
            if (
                record.role
                in {
                    StateRole.PROJECTION.value,
                    StateRole.CACHE.value,
                    StateRole.FALLBACK.value,
                }
                and record.canonical_claim
            ):
                findings.append(
                    finding(
                        "projection_cache_fallback_claims_owner",
                        (
                            f"{contract.domain} {record.role} claims canonical "
                            f"authority: {record.reference.key}."
                        ),
                        "ownership",
                        severity=Severity.BLOCKER,
                        remediation=(
                            "Remove canonical writes/claims or make the component the "
                            "single cataloged owner and demote the previous owner."
                        ),
                        **common,
                    )
                )
        return deduplicate_findings(findings)

    def _validate_contract_tests(
        self,
        contract: OwnerContract,
    ) -> tuple[Finding, ...]:
        findings: list[Finding] = []
        owner_tokens = {
            contract.owner.symbol,
            contract.owner.symbol.rsplit(".", 1)[-1],
            Path(contract.owner.path).stem,
        }
        for test_path in contract.tests:
            source = self._read(test_path)
            if not source:
                findings.append(
                    self._contract_blocker(
                        "state_owner_test_missing",
                        f"State-owner behavior test is missing: {test_path}.",
                        contract,
                        path=test_path,
                        remediation="Restore a real behavior test for this state domain.",
                    )
                )
                continue
            if not any(token and token in source for token in owner_tokens):
                findings.append(
                    self._contract_blocker(
                        "state_owner_test_not_bound",
                        (
                            f"Test does not name or exercise the cataloged owner: "
                            f"{test_path}."
                        ),
                        contract,
                        path=test_path,
                        remediation=(
                            "Bind the test to the owner class/module and assert a "
                            "real state transition."
                        ),
                    )
                )
            if not (
                DISABLE_SIGNAL_PATTERN.search(source)
                or any(probe in source for probe in contract.disable_probes)
            ):
                findings.append(
                    self._contract_blocker(
                        "state_owner_disable_probe_missing",
                        (
                            f"Test has no disconnect/disable assertion for "
                            f"{contract.domain}: {test_path}."
                        ),
                        contract,
                        path=test_path,
                        remediation=(
                            "Add an owner-disable or owner-loss test that prevents "
                            "fallback success."
                        ),
                    )
                )
        return deduplicate_findings(findings)

    def _scan_forbidden_claims(
        self,
        contract: OwnerContract,
    ) -> tuple[Finding, ...]:
        if not contract.forbidden_owner_claims:
            return ()
        findings: list[Finding] = []
        for path in self._production_source_paths():
            text = self._read(path)
            if not text:
                continue
            for claim in contract.forbidden_owner_claims:
                if claim.casefold() not in text.casefold():
                    continue
                if path in {contract.owner.path, contract.store.path}:
                    continue
                line = next(
                    (
                        index
                        for index, value in enumerate(text.splitlines(), start=1)
                        if claim.casefold() in value.casefold()
                    ),
                    0,
                )
                findings.append(
                    self._contract_blocker(
                        "forbidden_alternate_owner_claim",
                        (
                            f"Production source makes a forbidden alternate-owner "
                            f"claim for {contract.domain}: {claim!r}."
                        ),
                        contract,
                        path=path,
                        line=line,
                        remediation=(
                            "Remove the alternate canonical writer or update one "
                            "unit-level owner-transfer decision before implementation."
                        ),
                        attributes={"claim": claim},
                    )
                )
        return deduplicate_findings(findings)

    def _scan_undeclared_canonical_candidates(
        self,
        contract: OwnerContract,
        python: PythonGraphResult,
        script: ScriptGraphResult,
    ) -> tuple[Finding, ...]:
        declared_paths = {
            item.path
            for item in (
                contract.owner,
                contract.store,
                *contract.writers,
                *contract.projections,
                *contract.caches,
                contract.checkpoint,
                contract.recovery,
                *contract.fallback_refs,
            )
        }
        owner_token = contract.owner.symbol.rsplit(".", 1)[-1].casefold()
        store_token = contract.store.symbol.rsplit(".", 1)[-1].casefold()
        domain_tokens = {
            item
            for item in re.split(r"[_./-]+", contract.domain.casefold())
            if len(item) >= 5
        }
        findings: list[Finding] = []
        candidates: list[tuple[str, int, str]] = []
        for file_fact in python.files:
            if file_fact.path in declared_paths or not file_fact.production:
                continue
            for line in file_fact.owner_claim_lines:
                source = self._line(file_fact.path, line).casefold()
                if (
                    owner_token in source
                    or store_token in source
                    or any(token in source for token in domain_tokens)
                ):
                    candidates.append((file_fact.path, line, source.strip()))
        for file_fact in script.files:
            if file_fact.path in declared_paths or not file_fact.production:
                continue
            for line in file_fact.owner_claim_lines:
                source = self._line(file_fact.path, line).casefold()
                if (
                    owner_token in source
                    or store_token in source
                    or any(token in source for token in domain_tokens)
                ):
                    candidates.append((file_fact.path, line, source.strip()))
        for path, line, source in sorted(candidates):
            findings.append(
                self._contract_blocker(
                    "undeclared_canonical_owner_candidate",
                    (
                        f"Production source outside the custody contract claims "
                        f"canonical authority for {contract.domain}."
                    ),
                    contract,
                    path=path,
                    line=line,
                    remediation=(
                        "Classify the component as owner/store/projection/cache/"
                        "fallback or remove the competing authority claim."
                    ),
                    attributes={"excerpt": source[:240]},
                )
            )
        return deduplicate_findings(findings)

    def _validate_unique_contract_authority(
        self,
        catalog: AuditCatalog,
    ) -> tuple[Finding, ...]:
        findings: list[Finding] = []
        domains = Counter(item.domain for item in catalog.owners)
        for domain, count in sorted(domains.items()):
            if count == 1:
                continue
            findings.append(
                finding(
                    "duplicate_canonical_state_owner",
                    f"State domain has {count} canonical owner contracts: {domain}.",
                    "ownership",
                    severity=Severity.BLOCKER,
                    domain=domain,
                    owner_unit="M3-01B",
                    disposition=Disposition.BLOCK_RELEASE,
                    default_path_impact=(
                        "Writes, restore and recovery cannot be resolved deterministically."
                    ),
                    remediation="Retain one authoritative owner contract.",
                    attributes={"count": count},
                )
            )
        entry_authority: dict[str, list[str]] = defaultdict(list)
        for owner_contract in catalog.owners:
            for entry in owner_contract.entries:
                entry_authority[entry.entry_id].append(owner_contract.domain)
        for entry_id, domains_for_entry in sorted(entry_authority.items()):
            if len(domains_for_entry) <= 1:
                continue
            findings.append(
                finding(
                    "default_entry_has_multiple_state_authorities",
                    (
                        f"One default entry identity is assigned to multiple state "
                        f"domains without separate trace identities: {entry_id}."
                    ),
                    "ownership",
                    severity=Severity.BLOCKER,
                    domain=",".join(domains_for_entry),
                    owner_unit="M3-01B",
                    disposition=Disposition.BLOCK_RELEASE,
                    remediation=(
                        "Use unique entry identities per state trace or define an "
                        "explicit composite root outside canonical ownership."
                    ),
                    attributes={"entry_id": entry_id, "domains": domains_for_entry},
                )
            )
        return deduplicate_findings(findings)

    @staticmethod
    def _write_signal(
        reference: SourceRef,
        source: str,
        python_fact: PythonFileFacts | None,
        script_fact: ScriptFileFacts | None,
    ) -> bool:
        selected = source
        if reference.symbol:
            selected = OwnershipAuditor._symbol_text(reference, source)
        if WRITE_SIGNAL_PATTERN.search(selected):
            return True
        if python_fact is not None:
            scope = reference.symbol.rsplit(".", 1)[-1]
            return any(
                item.write_like
                and (
                    not reference.symbol
                    or scope in item.scope
                    or scope in item.qualified_name
                )
                for item in python_fact.calls
            )
        if script_fact is not None:
            scope = reference.symbol.rsplit(".", 1)[-1]
            return any(
                item.write_like
                and (
                    not reference.symbol
                    or scope in item.scope
                    or scope in item.callee
                )
                for item in script_fact.calls
            )
        return False

    @staticmethod
    def _persistent_signal(
        reference: SourceRef,
        python_fact: PythonFileFacts | None,
        script_fact: ScriptFileFacts | None,
    ) -> bool:
        scope = reference.symbol.rsplit(".", 1)[-1]
        if python_fact is not None:
            return any(
                item.persistent
                and (
                    not reference.symbol
                    or scope in item.scope
                    or scope in item.target
                )
                for item in python_fact.assignments
            )
        if script_fact is not None:
            return any(
                item.persistent
                and (
                    not reference.symbol
                    or scope in item.scope
                    or scope in item.target
                )
                for item in script_fact.assignments
            )
        return False

    @staticmethod
    def _symbol_text(reference: SourceRef, source: str) -> str:
        if not reference.symbol or not source:
            return source
        name = re.escape(reference.symbol.rsplit(".", 1)[-1])
        pattern = re.compile(
            rf"(?m)^(?P<indent>\s*)(?:export\s+)?(?:class|def|async\s+def|"
            rf"function|const|let|var)\s+{name}\b"
        )
        match = pattern.search(source)
        if match is None:
            return source
        start = match.start()
        indentation = len(match.group("indent"))
        lines = source[start:].splitlines()
        selected = [lines[0]]
        for line in lines[1:]:
            if line.strip() and len(line) - len(line.lstrip()) <= indentation:
                if re.match(
                    r"\s*(?:export\s+)?(?:class|def|async\s+def|function|const|let|var)\b",
                    line,
                ):
                    break
            selected.append(line)
            if len(selected) >= 600:
                break
        return "\n".join(selected)

    def _production_source_paths(self) -> tuple[str, ...]:
        paths: list[str] = []
        for prefix in ("apps", "packages", "scripts", "skills"):
            root = self.root / prefix
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix.casefold() not in {
                    ".py",
                    ".ts",
                    ".tsx",
                    ".js",
                    ".jsx",
                }:
                    continue
                relative = path.relative_to(self.root).as_posix()
                folded = relative.casefold().split("/")
                if any(
                    part in {
                        "tests",
                        "test",
                        "fixtures",
                        "mocks",
                        "vendor",
                        "vendor-runtimes",
                    }
                    for part in folded
                ):
                    continue
                paths.append(relative)
        return tuple(sorted(paths))

    def _read(self, relative: str) -> str:
        normalized = relative.replace("\\", "/")
        if normalized in self._source_cache:
            return self._source_cache[normalized]
        path = (self.root / normalized).resolve(strict=False)
        try:
            path.relative_to(self.root)
            text = path.read_text(encoding="utf-8")
        except (ValueError, OSError, UnicodeDecodeError):
            text = ""
        self._source_cache[normalized] = text
        return text

    def _line(self, relative: str, line: int) -> str:
        lines = self._read(relative).splitlines()
        return lines[line - 1] if 0 < line <= len(lines) else ""

    @staticmethod
    def _contract_blocker(
        code: str,
        message: str,
        contract: OwnerContract,
        *,
        path: str = "",
        line: int = 0,
        remediation: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> Finding:
        return finding(
            code,
            message,
            "ownership",
            severity=Severity.BLOCKER,
            domain=contract.domain,
            path=path,
            line=line,
            owner_unit="M3-01B",
            disposition=Disposition.BLOCK_RELEASE,
            default_path_impact=(
                f"Canonical {contract.domain} state may be ambiguous, "
                "non-durable, or fallback-owned."
            ),
            remediation=remediation,
            attributes=dict(attributes or {}),
        )
