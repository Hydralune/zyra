from __future__ import annotations

import json
import re
import subprocess
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .causality import CausalityResult
from .model import (
    AuditCatalog,
    AuditSection,
    Disposition,
    EvidencePointer,
    EvidenceStatus,
    Finding,
    RequirementEvidence,
    RuleSwitches,
    Severity,
    content_digest,
    deduplicate_findings,
    finding,
    section,
    stable_digest,
)
from .ownership import OwnershipResult
from .reachability import CodeGraph, ReachabilityResult


ALLOWED_M3_OWNERS = frozenset({"M3-01B", "M3-02A", "M3-02B", "M3-03"})
LIVE_SIGNAL_PATTERN = re.compile(
    r"(?i)\b(?:live|scenario|experiment|run_id|scenario_id|experiment_id|"
    r"effective_step|canonical_event|transition|human_intervention|"
    r"dispatch|provider|model|artifact|metric|fault|recovery)\b"
)
MUTATION_SIGNAL_PATTERN = re.compile(
    r"(?i)\b(?:mutation|state|route|placement|tool|permission|compact|restore|"
    r"fault|recovery|artifact|metric|commit|checkpoint)\b"
)
CONFIG_SIGNAL_PATTERN = re.compile(
    r"(?i)\b(?:sealed|policy|provider|model|edge|cloud|local|scheduler|"
    r"topology|memory|compact|recovery|trace|artifact|scenario|experiment)\b"
)


@dataclass(frozen=True, slots=True)
class RequirementObservation:
    requirement_id: str
    status: str
    owner_domains_valid: bool
    invalid_owner_domains: tuple[str, ...]
    default_entries_valid: bool
    invalid_default_entries: tuple[str, ...]
    live_evidence_valid: bool
    invalid_live_evidence: tuple[str, ...]
    event_links_valid: bool
    invalid_event_links: tuple[str, ...]
    mutation_refs_valid: bool
    invalid_mutation_refs: tuple[str, ...]
    artifacts_valid: bool
    invalid_artifacts: tuple[str, ...]
    metrics_valid: bool
    invalid_metrics: tuple[str, ...]
    tests_valid: bool
    invalid_tests: tuple[str, ...]
    commits_valid: bool
    invalid_commits: tuple[str, ...]
    configs_valid: bool
    invalid_configs: tuple[str, ...]
    m3_owner_valid: bool
    evidence_digests: Mapping[str, str]

    @property
    def valid(self) -> bool:
        return (
            self.status == EvidenceStatus.VERIFIED.value
            and self.owner_domains_valid
            and self.default_entries_valid
            and self.live_evidence_valid
            and self.event_links_valid
            and self.mutation_refs_valid
            and self.artifacts_valid
            and self.metrics_valid
            and self.tests_valid
            and self.commits_valid
            and self.configs_valid
            and self.m3_owner_valid
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "status": self.status,
            "owner_domains_valid": self.owner_domains_valid,
            "invalid_owner_domains": list(self.invalid_owner_domains),
            "default_entries_valid": self.default_entries_valid,
            "invalid_default_entries": list(self.invalid_default_entries),
            "live_evidence_valid": self.live_evidence_valid,
            "invalid_live_evidence": list(self.invalid_live_evidence),
            "event_links_valid": self.event_links_valid,
            "invalid_event_links": list(self.invalid_event_links),
            "mutation_refs_valid": self.mutation_refs_valid,
            "invalid_mutation_refs": list(self.invalid_mutation_refs),
            "artifacts_valid": self.artifacts_valid,
            "invalid_artifacts": list(self.invalid_artifacts),
            "metrics_valid": self.metrics_valid,
            "invalid_metrics": list(self.invalid_metrics),
            "tests_valid": self.tests_valid,
            "invalid_tests": list(self.invalid_tests),
            "commits_valid": self.commits_valid,
            "invalid_commits": list(self.invalid_commits),
            "configs_valid": self.configs_valid,
            "invalid_configs": list(self.invalid_configs),
            "m3_owner_valid": self.m3_owner_valid,
            "evidence_digests": dict(sorted(self.evidence_digests.items())),
            "valid": self.valid,
        }


@dataclass(frozen=True, slots=True)
class RequirementAuditResult:
    observations: tuple[RequirementObservation, ...]
    section: AuditSection

    @property
    def valid_requirement_ids(self) -> frozenset[str]:
        return frozenset(
            item.requirement_id for item in self.observations if item.valid
        )


class RequirementEvidenceAuditor:
    def __init__(
        self,
        project_root: str | Path,
        *,
        switches: RuleSwitches | None = None,
    ) -> None:
        self.root = Path(project_root).resolve(strict=False)
        self.switches = switches or RuleSwitches()
        self._bytes_cache: dict[str, bytes | None] = {}
        self._head = self._git_revision("HEAD")

    def audit(
        self,
        catalog: AuditCatalog,
        ownership: OwnershipResult,
        reachability: ReachabilityResult,
        causality: CausalityResult,
    ) -> RequirementAuditResult:
        if not self.switches.requirements:
            return RequirementAuditResult(
                observations=(),
                section=section(
                    "requirement_runtime_evidence",
                    metrics={
                        "rule_enabled": False,
                        "requirement_count": len(catalog.requirements),
                    },
                ),
            )
        observations: list[RequirementObservation] = []
        findings: list[Finding] = []
        evidence: list[EvidencePointer] = []
        valid_domains = ownership.valid_domains
        valid_entries = reachability.reachable_entry_ids
        valid_events = causality.valid_link_ids
        for requirement in catalog.requirements:
            observation = self._observe(
                requirement,
                reachability.graph,
                valid_domains,
                valid_entries,
                valid_events,
            )
            observations.append(observation)
            findings.extend(self._findings(requirement, observation))
            evidence.extend(
                EvidencePointer(
                    kind="requirement_runtime_evidence",
                    path=path,
                    digest=digest,
                    attributes={
                        "requirement_id": requirement.requirement_id,
                        "status": requirement.status.value,
                        "valid": observation.valid,
                    },
                )
                for path, digest in sorted(observation.evidence_digests.items())
            )
        findings.extend(self._coverage(catalog, observations))
        metrics = {
            "rule_enabled": True,
            "requirement_count": len(observations),
            "verified_count": sum(
                item.status == EvidenceStatus.VERIFIED.value
                for item in observations
            ),
            "valid_count": sum(item.valid for item in observations),
            "invalid_count": sum(not item.valid for item in observations),
            "status_counts": dict(
                sorted(Counter(item.status for item in observations).items())
            ),
            "m3_owner_counts": dict(
                sorted(
                    Counter(
                        item.m3_owner for item in catalog.requirements
                    ).items()
                )
            ),
            "evidence_digest": stable_digest(
                [item.to_dict() for item in observations]
            ),
        }
        return RequirementAuditResult(
            observations=tuple(observations),
            section=section(
                "requirement_runtime_evidence",
                metrics=metrics,
                findings=findings,
                evidence=evidence,
                records=(item.to_dict() for item in observations),
            ),
        )

    def _observe(
        self,
        requirement: RequirementEvidence,
        graph: CodeGraph,
        valid_domains: frozenset[str],
        valid_entries: frozenset[str],
        valid_events: frozenset[str],
    ) -> RequirementObservation:
        invalid_owner_domains = tuple(
            item for item in requirement.owner_domains if item not in valid_domains
        )
        invalid_default_entries = tuple(
            item
            for item in requirement.default_entry_ids
            if item not in valid_entries
        )
        invalid_live_evidence = tuple(
            path
            for path in requirement.live_evidence_paths
            if not self._valid_live_evidence(path, requirement.requirement_id)
        )
        invalid_event_links = tuple(
            item for item in requirement.event_links if item not in valid_events
        )
        invalid_mutation_refs = tuple(
            item.key
            for item in requirement.mutation_refs
            if not graph.resolve_ref(item).valid
            or not self._mutation_source_valid(item.path, item.symbol)
        )
        invalid_artifacts = tuple(
            path
            for path in requirement.artifact_paths
            if not self._valid_materialized_evidence(path, metric=False)
        )
        invalid_metrics = tuple(
            path
            for path in requirement.metric_paths
            if not self._valid_materialized_evidence(path, metric=True)
        )
        invalid_tests = tuple(
            path for path in requirement.test_paths if not self._valid_test(path)
        )
        invalid_commits = tuple(
            item for item in requirement.commits if not self._valid_commit(item)
        )
        invalid_configs = tuple(
            path for path in requirement.config_paths if not self._valid_config(path)
        )
        evidence_paths = {
            *requirement.live_evidence_paths,
            *requirement.artifact_paths,
            *requirement.metric_paths,
            *requirement.test_paths,
            *requirement.config_paths,
        }
        digests = {
            path: content_digest(payload)
            for path in sorted(evidence_paths)
            for payload in (self._read_bytes(path),)
            if payload is not None
        }
        return RequirementObservation(
            requirement_id=requirement.requirement_id,
            status=requirement.status.value,
            owner_domains_valid=not invalid_owner_domains,
            invalid_owner_domains=invalid_owner_domains,
            default_entries_valid=not invalid_default_entries,
            invalid_default_entries=invalid_default_entries,
            live_evidence_valid=not invalid_live_evidence,
            invalid_live_evidence=invalid_live_evidence,
            event_links_valid=not invalid_event_links,
            invalid_event_links=invalid_event_links,
            mutation_refs_valid=not invalid_mutation_refs,
            invalid_mutation_refs=invalid_mutation_refs,
            artifacts_valid=not invalid_artifacts,
            invalid_artifacts=invalid_artifacts,
            metrics_valid=not invalid_metrics,
            invalid_metrics=invalid_metrics,
            tests_valid=not invalid_tests,
            invalid_tests=invalid_tests,
            commits_valid=not invalid_commits,
            invalid_commits=invalid_commits,
            configs_valid=not invalid_configs,
            invalid_configs=invalid_configs,
            m3_owner_valid=requirement.m3_owner in ALLOWED_M3_OWNERS,
            evidence_digests=digests,
        )

    def _findings(
        self,
        requirement: RequirementEvidence,
        observation: RequirementObservation,
    ) -> tuple[Finding, ...]:
        findings: list[Finding] = []
        if requirement.status is not EvidenceStatus.VERIFIED:
            findings.append(
                self._blocker(
                    "requirement_evidence_not_verified",
                    (
                        f"{requirement.requirement_id} evidence status is "
                        f"{requirement.status.value}, not verified."
                    ),
                    requirement,
                    remediation=(
                        "Close the assigned M3 owner with real runtime/live evidence "
                        "before freeze."
                    ),
                    attributes={"status": requirement.status.value},
                )
            )
        checks = (
            (
                observation.invalid_owner_domains,
                "requirement_owner_domain_invalid",
                "canonical owner domain",
            ),
            (
                observation.invalid_default_entries,
                "requirement_default_entry_invalid",
                "reachable default entry",
            ),
            (
                observation.invalid_live_evidence,
                "requirement_live_evidence_invalid",
                "live run evidence",
            ),
            (
                observation.invalid_event_links,
                "requirement_event_link_invalid",
                "event-to-mutation link",
            ),
            (
                observation.invalid_mutation_refs,
                "requirement_mutation_ref_invalid",
                "semantic mutation reference",
            ),
            (
                observation.invalid_artifacts,
                "requirement_artifact_invalid",
                "artifact evidence",
            ),
            (
                observation.invalid_metrics,
                "requirement_metric_invalid",
                "raw metric evidence",
            ),
            (
                observation.invalid_tests,
                "requirement_test_invalid",
                "behavior test",
            ),
            (
                observation.invalid_commits,
                "requirement_commit_invalid",
                "Git commit",
            ),
            (
                observation.invalid_configs,
                "requirement_config_invalid",
                "configuration evidence",
            ),
        )
        for invalid, code, label in checks:
            if not invalid:
                continue
            findings.append(
                self._blocker(
                    code,
                    (
                        f"{requirement.requirement_id} has invalid {label} "
                        f"bindings: {', '.join(invalid)}."
                    ),
                    requirement,
                    remediation=(
                        f"Bind the requirement to real {label} evidence at the "
                        "exact target commit."
                    ),
                    attributes={"invalid": list(invalid)},
                )
            )
        if not observation.m3_owner_valid:
            findings.append(
                self._blocker(
                    "requirement_m3_owner_invalid",
                    (
                        f"{requirement.requirement_id} has invalid M3 closure "
                        f"owner {requirement.m3_owner!r}."
                    ),
                    requirement,
                    remediation=(
                        "Assign M3-01B, M3-02A, M3-02B, or M3-03 as the "
                        "remaining evidence owner."
                    ),
                    attributes={"m3_owner": requirement.m3_owner},
                )
            )
        return deduplicate_findings(findings)

    def _coverage(
        self,
        catalog: AuditCatalog,
        observations: Sequence[RequirementObservation],
    ) -> tuple[Finding, ...]:
        observed = {item.requirement_id for item in observations}
        findings: list[Finding] = []
        for requirement_id in sorted(set(catalog.required_requirements) - observed):
            placeholder = next(
                (
                    item
                    for item in catalog.requirements
                    if item.requirement_id == requirement_id
                ),
                None,
            )
            if placeholder is not None:
                continue
            findings.append(
                finding(
                    "required_competition_evidence_unmapped",
                    (
                        f"Required competition identity has no evidence graph: "
                        f"{requirement_id}."
                    ),
                    "requirements",
                    severity=Severity.BLOCKER,
                    requirement_id=requirement_id,
                    owner_unit="M3-03",
                    disposition=Disposition.BLOCK_RELEASE,
                    default_path_impact=(
                        "Submission evidence cannot be traced to runtime behavior."
                    ),
                    remediation=(
                        "Add owner/default/live/event/mutation/artifact/metric/"
                        "test/commit/config/M3-owner bindings."
                    ),
                )
            )
        return deduplicate_findings(findings)

    def _valid_live_evidence(
        self,
        relative: str,
        requirement_id: str,
    ) -> bool:
        payload = self._read_bytes(relative)
        if payload is None or not payload:
            return False
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            return False
        if not LIVE_SIGNAL_PATTERN.search(text):
            return False
        if Path(relative).suffix.casefold() == ".json":
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                return False
            if not isinstance(decoded, (Mapping, Sequence)):
                return False
            flattened = flatten_json_strings(decoded, maximum=25000)
            if (
                requirement_id not in flattened
                and requirement_id.replace("REQ-", "") not in flattened
                and requirement_id.replace("SCORE-", "") not in flattened
            ):
                generic_bundle = any(
                    marker in flattened
                    for marker in (
                        "requirement_score",
                        "competition_evidence",
                        "requirement_evidence",
                        "verified_requirements",
                    )
                )
                if not generic_bundle:
                    return False
        return True

    def _valid_materialized_evidence(
        self,
        relative: str,
        *,
        metric: bool,
    ) -> bool:
        payload = self._read_bytes(relative)
        if payload is None or not payload:
            return False
        suffix = Path(relative).suffix.casefold()
        if suffix in {".json", ".jsonl", ".csv", ".tsv", ".md", ".txt"}:
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                return not metric
            pattern = (
                re.compile(
                    r"(?i)\b(?:p50|p95|variance|latency|cost|token|rate|count|"
                    r"metric|sample|utilization|entropy|density|mttr)\b"
                )
                if metric
                else re.compile(
                    r"(?i)\b(?:artifact|digest|sha256|manifest|deliver|report|"
                    r"patch|archive|output|result)\b"
                )
            )
            return pattern.search(text) is not None
        return not metric

    def _valid_test(self, relative: str) -> bool:
        payload = self._read_bytes(relative)
        if payload is None:
            return False
        text = payload.decode("utf-8", errors="replace")
        path = relative.casefold()
        test_path = (
            "/tests/" in f"/{path}"
            or Path(path).name.startswith("test_")
            or ".test." in path
            or ".spec." in path
        )
        behavior = bool(
            re.search(
                r"\b(?:assert|expect|pytest|unittest|test\(|describe\(|it\()",
                text,
            )
        )
        return test_path and behavior

    def _valid_commit(self, revision: str) -> bool:
        if not self._head:
            return False
        selected = self._git_revision(revision)
        if not selected:
            return False
        completed = subprocess.run(
            ["git", "merge-base", "--is-ancestor", selected, self._head],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return completed.returncode == 0

    def _valid_config(self, relative: str) -> bool:
        payload = self._read_bytes(relative)
        if payload is None or not payload:
            return False
        text = payload.decode("utf-8", errors="replace")
        return CONFIG_SIGNAL_PATTERN.search(text) is not None

    def _mutation_source_valid(self, relative: str, symbol: str) -> bool:
        payload = self._read_bytes(relative)
        if payload is None:
            return False
        text = payload.decode("utf-8", errors="replace")
        token = symbol.rsplit(".", 1)[-1]
        return (
            (not token or token in text)
            and MUTATION_SIGNAL_PATTERN.search(text) is not None
        )

    def _read_bytes(self, relative: str) -> bytes | None:
        normalized = relative.replace("\\", "/")
        if normalized in self._bytes_cache:
            return self._bytes_cache[normalized]
        selected = (self.root / normalized).resolve(strict=False)
        try:
            selected.relative_to(self.root)
            payload = selected.read_bytes()
        except (ValueError, OSError):
            payload = None
        self._bytes_cache[normalized] = payload
        return payload

    def _git_revision(self, value: str) -> str:
        completed = subprocess.run(
            ["git", "rev-parse", "--verify", f"{value}^{{commit}}"],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        selected = completed.stdout.strip().casefold()
        return (
            selected
            if completed.returncode == 0
            and re.fullmatch(r"[0-9a-f]{40,64}", selected)
            else ""
        )

    @staticmethod
    def _blocker(
        code: str,
        message: str,
        requirement: RequirementEvidence,
        *,
        remediation: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> Finding:
        return finding(
            code,
            message,
            "requirements",
            severity=Severity.BLOCKER,
            requirement_id=requirement.requirement_id,
            owner_unit=requirement.m3_owner,
            disposition=Disposition.BLOCK_RELEASE,
            default_path_impact=(
                "Competition evidence cannot be reproduced from canonical "
                "runtime facts at the frozen commit."
            ),
            remediation=remediation,
            attributes=dict(attributes or {}),
        )


def flatten_json_strings(value: Any, *, maximum: int) -> str:
    selected: list[str] = []
    stack = [value]
    while stack and sum(len(item) for item in selected) < maximum:
        item = stack.pop()
        if isinstance(item, Mapping):
            for key, child in item.items():
                selected.append(str(key))
                stack.append(child)
        elif isinstance(item, Sequence) and not isinstance(
            item, (str, bytes, bytearray)
        ):
            stack.extend(reversed(item))
        elif isinstance(item, (str, int, float, bool)):
            selected.append(str(item))
    return "\n".join(selected)[:maximum]
