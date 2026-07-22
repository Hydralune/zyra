from __future__ import annotations

import ast
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .contracts import Finding, GateResult, GateStatus, Severity


@dataclass(frozen=True, slots=True)
class SourceResponsibility:
    responsibility_id: str
    paths: tuple[str, ...]
    required_markers: tuple[tuple[str, ...], ...]
    forbidden_markers: tuple[str, ...] = ()
    required_runtime_events: tuple[str, ...] = ()
    downstream_effects: tuple[str, ...] = ()


class ResponsibilitySourceInspector:
    def __init__(self, project_root: str | Path) -> None:
        self.root = Path(project_root).resolve()
        self._cache: dict[Path, str] = {}

    def evaluate(
        self,
        gate_id: str,
        summary: str,
        responsibilities: Sequence[SourceResponsibility],
        *,
        events: Sequence[Mapping[str, Any]] = (),
        final_completion: bool = False,
    ) -> GateResult:
        result = GateResult(gate_id=gate_id, status=GateStatus.NOT_RUN, summary=summary)
        event_text, event_types = self._event_view(events)
        responsibility_metrics: list[dict[str, Any]] = []
        for responsibility in responsibilities:
            files = self._files(responsibility.paths)
            text = "\n".join(self._text(path) for path in files)
            missing_groups = [
                group for group in responsibility.required_markers
                if not any(re.search(marker, text, flags=re.IGNORECASE | re.MULTILINE) for marker in group)
            ]
            forbidden_hits = [
                marker
                for marker in responsibility.forbidden_markers
                if re.search(marker, text, flags=re.IGNORECASE | re.MULTILINE)
            ]
            missing_events = [
                pattern
                for pattern in responsibility.required_runtime_events
                if not self._event_pattern(pattern, event_text, event_types)
            ]
            missing_effects = [
                pattern
                for pattern in responsibility.downstream_effects
                if not self._event_pattern(pattern, event_text, event_types)
            ]
            if not files:
                result.add(
                    Finding(
                        code=f"{gate_id}.source_missing",
                        severity=Severity.BLOCKER,
                        summary="Cross-cutting owner source is missing.",
                        capability=responsibility.responsibility_id,
                        detail=", ".join(responsibility.paths),
                    )
                )
            for group in missing_groups:
                result.add(
                    Finding(
                        code=f"{gate_id}.source_semantic_missing",
                        severity=Severity.ERROR,
                        summary="Owner source lacks a required behavior semantic.",
                        capability=responsibility.responsibility_id,
                        detail=" OR ".join(group),
                    )
                )
            for marker in forbidden_hits:
                result.add(
                    Finding(
                        code=f"{gate_id}.forbidden_semantic",
                        severity=Severity.BLOCKER,
                        summary="Owner source contains a forbidden behavior marker.",
                        capability=responsibility.responsibility_id,
                        detail=marker,
                    )
                )
            for pattern in missing_events:
                result.add(
                    Finding(
                        code=f"{gate_id}.runtime_event_missing",
                        severity=Severity.BLOCKER if final_completion else Severity.WARNING,
                        summary="No real runtime event proves the required behavior.",
                        capability=responsibility.responsibility_id,
                        detail=pattern,
                    )
                )
            for pattern in missing_effects:
                result.add(
                    Finding(
                        code=f"{gate_id}.semantic_effect_missing",
                        severity=Severity.BLOCKER if final_completion else Severity.WARNING,
                        summary="No downstream runtime effect proves the cross-cutting capability changes behavior.",
                        capability=responsibility.responsibility_id,
                        detail=pattern,
                    )
                )
            responsibility_metrics.append(
                {
                    "responsibility_id": responsibility.responsibility_id,
                    "source_files": [path.relative_to(self.root).as_posix() for path in files],
                    "missing_source_semantics": [list(group) for group in missing_groups],
                    "forbidden_hits": forbidden_hits,
                    "missing_runtime_events": missing_events,
                    "missing_downstream_effects": missing_effects,
                }
            )
        result.metrics.update(
            {
                "responsibility_count": len(responsibilities),
                "event_count": len(events),
                "event_type_counts": dict(sorted(event_types.items())),
                "responsibilities": responsibility_metrics,
            }
        )
        if not events and not final_completion:
            result.limitations.append("Static owner audit ran; live semantic-effect evidence remains open.")
        return result.finish(default_partial=not final_completion)

    def _files(self, paths: Sequence[str]) -> list[Path]:
        found: set[Path] = set()
        for raw in paths:
            candidate = (self.root / raw).resolve()
            if not candidate.is_relative_to(self.root) or not candidate.exists():
                continue
            values = [candidate] if candidate.is_file() else candidate.rglob("*")
            for path in values:
                if path.is_file() and path.suffix in {".py", ".pyi", ".ts", ".tsx", ".js", ".rs"}:
                    found.add(path)
        return sorted(found)

    def _text(self, path: Path) -> str:
        if path not in self._cache:
            try:
                self._cache[path] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                self._cache[path] = ""
        return self._cache[path]

    @staticmethod
    def _event_view(events: Sequence[Mapping[str, Any]]) -> tuple[str, Counter[str]]:
        texts: list[str] = []
        types: Counter[str] = Counter()
        for event in events:
            event_type = str(event.get("event_type") or event.get("type") or "")
            types[event_type] += 1
            texts.append(repr(event))
        return "\n".join(texts), types

    @staticmethod
    def _event_pattern(pattern: str, event_text: str, event_types: Mapping[str, int]) -> bool:
        return any(re.search(pattern, event_type, flags=re.IGNORECASE) for event_type in event_types) or bool(
            re.search(pattern, event_text, flags=re.IGNORECASE | re.MULTILINE)
        )


class PatchGitGate:
    def __init__(self, project_root: str | Path) -> None:
        self.inspector = ResponsibilitySourceInspector(project_root)

    def evaluate(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        final_completion: bool = False,
    ) -> GateResult:
        responsibilities = (
            SourceResponsibility(
                responsibility_id="read-before-write",
                paths=(
                    "packages/runtime/claude-runtime/src",
                    "packages/workspace/zyra_workspace",
                    "packages/workers/zyra_workers",
                ),
                required_markers=(
                    (r"read[_A-Za-z]*before[_A-Za-z]*write", r"base(?:_|\s)*(?:hash|digest)", r"expected[_A-Za-z]*(?:hash|revision)"),
                    (r"mtime", r"modified[_A-Za-z]*time", r"stat\s*\("),
                    (r"stale", r"conflict", r"compare[_A-Za-z]*and[_A-Za-z]*swap"),
                ),
                required_runtime_events=(r"(?:file|patch).*(?:read|precondition|validated)",),
                downstream_effects=(r"(?:patch|file).*(?:committed|written|applied)",),
            ),
            SourceResponsibility(
                responsibility_id="patch-transaction",
                paths=(
                    "packages/runtime/claude-runtime/src",
                    "packages/workspace/zyra_workspace",
                    "packages/workers/zyra_workers",
                ),
                required_markers=(
                    (r"transaction", r"atomic", r"temporary[_A-Za-z]*file"),
                    (r"rollback", r"rewind", r"file[_A-Za-z]*history", r"restore"),
                    (r"patch", r"apply[_A-Za-z]*edit", r"hashline"),
                ),
                required_runtime_events=(r"patch|file_history|workspace",),
                downstream_effects=(r"artifact|checkpoint|rollback|rewind",),
            ),
            SourceResponsibility(
                responsibility_id="dirty-worktree-protection",
                paths=("packages/workspace/zyra_workspace", "packages/runtime/claude-runtime/src"),
                required_markers=(
                    (r"dirty", r"worktree[_A-Za-z]*changes", r"git[_A-Za-z]*status"),
                    (r"destructive", r"reset[_A-Za-z]*hard", r"force"),
                    (r"permission", r"approval", r"deny"),
                ),
                required_runtime_events=(r"permission|workspace|git",),
                downstream_effects=(r"denied|blocked|approval|recovery",),
            ),
        )
        result = self.inspector.evaluate(
            "patch-git",
            "Patch/Git read-before-write, stale guard, transaction, rollback and destructive-operation policy.",
            responsibilities,
            events=events,
            final_completion=final_completion,
        )
        result.findings.extend(self._destructive_git_findings(events))
        return result.finish(default_partial=not final_completion)

    @staticmethod
    def _destructive_git_findings(events: Sequence[Mapping[str, Any]]) -> list[Finding]:
        findings: list[Finding] = []
        destructive = re.compile(r"\bgit\s+(?:reset\s+--hard|clean\s+-[a-z]*f|checkout\s+--|push\s+--force|branch\s+-D)\b", re.IGNORECASE)
        for index, event in enumerate(events):
            text = repr(event)
            if not destructive.search(text):
                continue
            lowered = text.lower()
            protected = any(token in lowered for token in ("denied", "approval", "permission", "blocked", "recovery"))
            if not protected:
                findings.append(
                    Finding(
                        code="patch-git.destructive_git_unprotected",
                        severity=Severity.BLOCKER,
                        summary="Destructive Git operation has no deny/approval/recovery evidence.",
                        location=f"events[{index}]",
                    )
                )
        return findings


class DenyPolicyGate:
    def __init__(self, project_root: str | Path) -> None:
        self.inspector = ResponsibilitySourceInspector(project_root)

    def evaluate(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        final_completion: bool = False,
    ) -> GateResult:
        responsibilities = (
            SourceResponsibility(
                responsibility_id="progressive-friction",
                paths=(
                    "packages/runtime/claude-runtime/src/permission",
                    "packages/runtime/zyra_runtime/permission",
                    "packages/runtime/zyra_runtime/sandbox_gateway",
                ),
                required_markers=(
                    (r"low[_A-Za-z]*(?:risk|read)", r"read[_A-Za-z]*only", r"safe[_A-Za-z]*allow"),
                    (r"high[_A-Za-z]*risk", r"unknown", r"destructive"),
                    (r"allow",),
                    (r"ask",),
                    (r"deny",),
                ),
                required_runtime_events=(r"permission|policy",),
                downstream_effects=(r"tool|gateway|recovery",),
            ),
            SourceResponsibility(
                responsibility_id="deny-recovery-signal",
                paths=(
                    "packages/runtime/claude-runtime/src",
                    "packages/runtime/zyra_runtime/permission",
                    "packages/scheduler/zyra_scheduler/recovery_runtime",
                ),
                required_markers=(
                    (r"replan", r"recovery"),
                    (r"denial", r"deny"),
                    (r"causation", r"request[_A-Za-z]*id", r"tool[_A-Za-z]*use[_A-Za-z]*id"),
                ),
                required_runtime_events=(r"denied|permission",),
                downstream_effects=(r"recovery|replan|reroute|abort",),
            ),
        )
        result = self.inspector.evaluate(
            "deny-policy",
            "Progressive-friction permission policy and causally linked denial recovery.",
            responsibilities,
            events=events,
            final_completion=final_completion,
        )
        result.findings.extend(self._friction_findings(events))
        result.findings.extend(self._denial_recovery_findings(events))
        return result.finish(default_partial=not final_completion)

    @staticmethod
    def _friction_findings(events: Sequence[Mapping[str, Any]]) -> list[Finding]:
        findings: list[Finding] = []
        for index, event in enumerate(events):
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            decision = payload.get("permission_decision") if isinstance(payload.get("permission_decision"), Mapping) else payload
            risk = str(decision.get("risk") or decision.get("risk_level") or "").lower()
            effect = str(decision.get("effect") or decision.get("decision") or "").lower()
            tool = str(decision.get("tool_name") or decision.get("capability") or "")
            if risk in {"low", "read_only", "safe"} and effect in {"ask", "pending", "approval_required"}:
                findings.append(
                    Finding(
                        code="deny-policy.low_risk_interrupted",
                        severity=Severity.ERROR,
                        summary="Low-risk deterministic read/search was interrupted by approval.",
                        detail=tool,
                        location=f"events[{index}]",
                    )
                )
            if risk in {"high", "critical", "unknown", "destructive"} and effect in {"allow", "allowed"}:
                findings.append(
                    Finding(
                        code="deny-policy.high_risk_allowed",
                        severity=Severity.BLOCKER,
                        summary="High-risk/unknown action bypassed ask/deny policy.",
                        detail=tool,
                        location=f"events[{index}]",
                    )
                )
        return findings

    @staticmethod
    def _denial_recovery_findings(events: Sequence[Mapping[str, Any]]) -> list[Finding]:
        denied: dict[str, int] = {}
        recovered: set[str] = set()
        for index, event in enumerate(events):
            text = repr(event).lower()
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            correlation = str(
                payload.get("request_id")
                or payload.get("permission_request_id")
                or payload.get("causation_id")
                or event.get("causation_id")
                or ""
            )
            if correlation and any(token in text for token in ("denied", "effect': 'deny", 'effect": "deny')):
                denied[correlation] = index
            if correlation and any(token in text for token in ("recovery", "replan", "reroute", "safely_failed", "aborted")):
                recovered.add(correlation)
        return [
            Finding(
                code="deny-policy.denial_without_recovery",
                severity=Severity.BLOCKER,
                summary="Denied request has no correlated recovery/replan signal.",
                detail=request_id,
                location=f"events[{index}]",
            )
            for request_id, index in denied.items()
            if request_id not in recovered
        ]


class SecretPromptInjectionGate:
    _SECRET_PATTERNS = (
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
        re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*['\"]?[^\s,'\"]{8,}"),
    )

    def __init__(self, project_root: str | Path) -> None:
        self.inspector = ResponsibilitySourceInspector(project_root)

    def evaluate(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        final_completion: bool = False,
    ) -> GateResult:
        responsibilities = (
            SourceResponsibility(
                responsibility_id="provenance-trust",
                paths=(
                    "packages/runtime/claude-runtime/src",
                    "packages/runtime/zyra_runtime",
                    "packages/memory/zyra_memory",
                    "packages/code_index/zyra_code_index",
                ),
                required_markers=(
                    (r"provenance", r"source[_A-Za-z]*(?:kind|ref|id)"),
                    (r"trust", r"trusted", r"untrusted"),
                    (r"redact", r"secret", r"credential[_A-Za-z]*handle"),
                ),
                required_runtime_events=(r"context|memory|tool|index",),
                downstream_effects=(r"redact|blocked|rejected|provenance|trust",),
            ),
            SourceResponsibility(
                responsibility_id="memory-write-guard",
                paths=("packages/memory/zyra_memory", "packages/memory/curator-state-machine/src"),
                required_markers=(
                    (r"write[_A-Za-z]*guard", r"validation", r"admission"),
                    (r"prompt[_A-Za-z]*inject", r"untrusted", r"instruction"),
                    (r"reject", r"quarantine", r"redact"),
                ),
                required_runtime_events=(r"memory.*(?:candidate|accepted|rejected|committed)",),
                downstream_effects=(r"memory.*(?:rejected|committed)|write.*blocked",),
            ),
            SourceResponsibility(
                responsibility_id="untrusted-rule-mutation-guard",
                paths=(
                    "packages/runtime/claude-runtime/src/permission",
                    "packages/runtime/zyra_runtime/permission",
                    "packages/runtime/zyra_runtime/sandbox_gateway",
                ),
                required_markers=(
                    (r"untrusted", r"trust[_A-Za-z]*level"),
                    (r"rule", r"permission"),
                    (r"deny", r"reject", r"cannot", r"forbid"),
                ),
                required_runtime_events=(r"permission|policy",),
                downstream_effects=(r"deny|blocked|rejected",),
            ),
        )
        result = self.inspector.evaluate(
            "secrets-prompt-injection",
            "Provenance/trust, redaction, memory-write and untrusted rule-mutation guards.",
            responsibilities,
            events=events,
            final_completion=final_completion,
        )
        result.findings.extend(self._secret_leak_findings(events))
        result.findings.extend(self._untrusted_mutation_findings(events))
        return result.finish(default_partial=not final_completion)

    def _secret_leak_findings(self, events: Sequence[Mapping[str, Any]]) -> list[Finding]:
        findings: list[Finding] = []
        for index, event in enumerate(events):
            text = repr(event)
            for pattern in self._SECRET_PATTERNS:
                match = pattern.search(text)
                if not match:
                    continue
                findings.append(
                    Finding(
                        code="secrets-prompt-injection.secret_in_event",
                        severity=Severity.BLOCKER,
                        summary="Event/trace payload appears to contain an unredacted secret.",
                        detail=self._redacted_preview(match.group(0)),
                        location=f"events[{index}]",
                    )
                )
        return findings

    @staticmethod
    def _untrusted_mutation_findings(events: Sequence[Mapping[str, Any]]) -> list[Finding]:
        findings: list[Finding] = []
        for index, event in enumerate(events):
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            trust = str(payload.get("trust") or payload.get("trust_level") or "").lower()
            mutation = str(payload.get("mutation_kind") or payload.get("operation") or "").lower()
            outcome = str(payload.get("status") or payload.get("effect") or "").lower()
            if trust in {"untrusted", "external", "remote_content"} and any(
                token in mutation for token in ("permission", "rule", "policy", "allowlist", "system_prompt")
            ) and outcome not in {"denied", "rejected", "blocked", "quarantined"}:
                findings.append(
                    Finding(
                        code="secrets-prompt-injection.untrusted_rule_mutation",
                        severity=Severity.BLOCKER,
                        summary="Untrusted content mutated a permission/policy/system-instruction boundary.",
                        detail=f"mutation={mutation}; outcome={outcome}",
                        location=f"events[{index}]",
                    )
                )
        return findings

    @staticmethod
    def _redacted_preview(value: str) -> str:
        if len(value) <= 8:
            return "<redacted>"
        return value[:4] + "…<redacted>…" + value[-2:]


class CodeIndexGate:
    def __init__(self, project_root: str | Path) -> None:
        self.inspector = ResponsibilitySourceInspector(project_root)

    def evaluate(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        final_completion: bool = False,
    ) -> GateResult:
        responsibilities = (
            SourceResponsibility(
                responsibility_id="search-symbol-fallback",
                paths=("packages/code_index/zyra_code_index", "apps/api/zyra_api/main.py"),
                required_markers=(
                    (r"glob", r"path[_A-Za-z]*match"),
                    (r"grep", r"search"),
                    (r"lsp", r"symbol"),
                    (r"fallback", r"ripgrep", r"text[_A-Za-z]*search"),
                ),
                required_runtime_events=(r"code_index|index",),
                downstream_effects=(r"context|patch|test|recovery",),
            ),
            SourceResponsibility(
                responsibility_id="budget-permission",
                paths=("packages/code_index/zyra_code_index", "packages/runtime/zyra_runtime/permission"),
                required_markers=(
                    (r"budget", r"limit", r"maximum"),
                    (r"permission", r"authorize", r"allowed"),
                    (r"workspace", r"root", r"scope"),
                ),
                required_runtime_events=(r"code_index|permission",),
                downstream_effects=(r"budget|denied|allowed|truncated",),
            ),
            SourceResponsibility(
                responsibility_id="incremental-update",
                paths=("packages/code_index/zyra_code_index",),
                required_markers=(
                    (r"incremental", r"invalidate", r"reconcile"),
                    (r"mtime", r"digest", r"hash", r"revision"),
                    (r"update", r"rebuild"),
                ),
                required_runtime_events=(r"code_index.*(?:invalidate|reconcile|rebuild|update)",),
                downstream_effects=(r"search|context|select[_-]?tests|recovery",),
            ),
        )
        result = self.inspector.evaluate(
            "code-index",
            "Code-index search/symbol fallback, budgets, permission and incremental semantic effects.",
            responsibilities,
            events=events,
            final_completion=final_completion,
        )
        effects = self._effect_matrix(events)
        result.metrics["downstream_effect_matrix"] = effects
        if final_completion and not any(effects.values()):
            result.add(
                Finding(
                    code="code-index.no_downstream_effect",
                    severity=Severity.BLOCKER,
                    summary="Code index did not affect context, patch, test selection or recovery.",
                )
            )
        elif not any(effects.values()):
            result.add(
                Finding(
                    code="code-index.no_downstream_effect",
                    severity=Severity.WARNING,
                    summary="No live downstream code-index effect was observed in this foundation trace.",
                )
            )
        result.findings.extend(self._fallback_mask_findings(events))
        return result.finish(default_partial=not final_completion)

    @staticmethod
    def _effect_matrix(events: Sequence[Mapping[str, Any]]) -> dict[str, bool]:
        matrix = {"context": False, "patch": False, "test_selection": False, "recovery": False}
        index_refs: set[str] = set()
        for event in events:
            text = repr(event).lower()
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            if "code_index" in text or "code-index" in text:
                for key in ("request_id", "result_id", "index_revision", "causation_id"):
                    if payload.get(key):
                        index_refs.add(str(payload[key]))
            correlated = bool(index_refs & {str(value) for value in payload.values() if isinstance(value, (str, int))})
            if "context" in text and (correlated or "code_index" in text):
                matrix["context"] = True
            if "patch" in text and (correlated or "code_index" in text):
                matrix["patch"] = True
            if any(token in text for token in ("select_tests", "test_selection", "selected_tests")) and (correlated or "code_index" in text):
                matrix["test_selection"] = True
            if "recovery" in text and (correlated or "code_index" in text):
                matrix["recovery"] = True
        return matrix

    @staticmethod
    def _fallback_mask_findings(events: Sequence[Mapping[str, Any]]) -> list[Finding]:
        findings: list[Finding] = []
        for index, event in enumerate(events):
            text = repr(event).lower()
            if "code_index" not in text and "code-index" not in text:
                continue
            if any(token in text for token in ("mock_fallback", "fixture_fallback", "legacy_fallback")):
                findings.append(
                    Finding(
                        code="code-index.fallback_masked",
                        severity=Severity.BLOCKER,
                        summary="Code-index failure was masked by mock/fixture/legacy fallback.",
                        location=f"events[{index}]",
                    )
                )
        return findings


class CrossCuttingGateSuite:
    def __init__(self, project_root: str | Path) -> None:
        self.patch_git = PatchGitGate(project_root)
        self.deny_policy = DenyPolicyGate(project_root)
        self.secrets = SecretPromptInjectionGate(project_root)
        self.code_index = CodeIndexGate(project_root)

    def evaluate(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        final_completion: bool = False,
    ) -> tuple[GateResult, ...]:
        return (
            self.patch_git.evaluate(events, final_completion=final_completion),
            self.deny_policy.evaluate(events, final_completion=final_completion),
            self.secrets.evaluate(events, final_completion=final_completion),
            self.code_index.evaluate(events, final_completion=final_completion),
        )
