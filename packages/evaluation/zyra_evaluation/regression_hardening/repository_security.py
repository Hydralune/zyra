from __future__ import annotations

import re
import shlex
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from .contracts import (
    AssertionSeverity,
    CaseExecutionBuffer,
    FailureKind,
    ObservationKind,
    stable_digest,
)


class RepositoryRisk(StrEnum):
    READ_ONLY = "read_only"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    DESTRUCTIVE = "destructive"
    UNKNOWN = "unknown"


class FrictionEffect(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PatchPhase(StrEnum):
    READ = "read"
    PREPARED = "prepared"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class GitAssessment:
    argv: tuple[str, ...]
    command: str
    risk: RepositoryRisk
    required_effect: FrictionEffect
    reason: str
    dirty_worktree_sensitive: bool
    remote_effect: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "command": self.command,
            "risk": self.risk.value,
            "required_effect": self.required_effect.value,
            "reason": self.reason,
            "dirty_worktree_sensitive": self.dirty_worktree_sensitive,
            "remote_effect": self.remote_effect,
        }


class GitCommandClassifier:
    _READ_ONLY = {
        "status",
        "diff",
        "log",
        "show",
        "branch:list",
        "remote:get-url",
        "rev-parse",
        "ls-files",
        "grep",
    }
    _DESTRUCTIVE = {
        "reset:--hard",
        "clean:-f",
        "clean:-fd",
        "clean:-fdx",
        "checkout:--",
        "restore:--source",
        "branch:-d-force",
        "push:--force",
        "push:--force-with-lease",
        "rebase",
        "filter-branch",
    }
    _REMOTE = {"push", "fetch", "pull", "clone", "submodule"}

    def classify(self, command: str | Sequence[str]) -> GitAssessment:
        argv = self._argv(command)
        if not argv or argv[0].casefold() != "git":
            return GitAssessment(
                argv=argv,
                command="",
                risk=RepositoryRisk.UNKNOWN,
                required_effect=FrictionEffect.DENY,
                reason="not a Git command",
                dirty_worktree_sensitive=False,
                remote_effect=False,
            )
        args = self._strip_global_options(argv[1:])
        if not args:
            return GitAssessment(
                argv=argv,
                command="",
                risk=RepositoryRisk.UNKNOWN,
                required_effect=FrictionEffect.DENY,
                reason="Git subcommand is missing",
                dirty_worktree_sensitive=False,
                remote_effect=False,
            )
        subcommand = args[0].casefold()
        options = {item.casefold() for item in args[1:] if item.startswith("-")}
        key = self._key(subcommand, args[1:])
        remote = subcommand in self._REMOTE
        dirty_sensitive = subcommand in {
            "checkout",
            "restore",
            "reset",
            "clean",
            "rebase",
            "merge",
            "cherry-pick",
            "apply",
            "am",
        }
        if key in self._DESTRUCTIVE or (
            subcommand == "clean"
            and any("f" in option.lstrip("-") for option in options)
        ):
            return GitAssessment(
                argv=argv,
                command=key,
                risk=RepositoryRisk.DESTRUCTIVE,
                required_effect=FrictionEffect.DENY,
                reason="destructive Git operation is denied by default",
                dirty_worktree_sensitive=dirty_sensitive,
                remote_effect=remote,
            )
        if key in self._READ_ONLY or subcommand in self._READ_ONLY:
            return GitAssessment(
                argv=argv,
                command=key,
                risk=RepositoryRisk.READ_ONLY,
                required_effect=FrictionEffect.ALLOW,
                reason="bounded read-only Git inspection",
                dirty_worktree_sensitive=False,
                remote_effect=False,
            )
        if subcommand == "push":
            return GitAssessment(
                argv=argv,
                command=key,
                risk=RepositoryRisk.HIGH,
                required_effect=FrictionEffect.ASK,
                reason="push changes remote repository state",
                dirty_worktree_sensitive=False,
                remote_effect=True,
            )
        if subcommand in {"commit", "tag", "branch", "switch", "merge", "cherry-pick"}:
            return GitAssessment(
                argv=argv,
                command=key,
                risk=RepositoryRisk.MEDIUM,
                required_effect=FrictionEffect.ASK,
                reason="operation mutates repository history or refs",
                dirty_worktree_sensitive=dirty_sensitive,
                remote_effect=False,
            )
        if subcommand in {"add", "apply", "am", "stash", "restore", "checkout"}:
            return GitAssessment(
                argv=argv,
                command=key,
                risk=RepositoryRisk.MEDIUM,
                required_effect=FrictionEffect.ASK,
                reason="operation mutates index or worktree",
                dirty_worktree_sensitive=True,
                remote_effect=False,
            )
        if subcommand in {"fetch", "pull", "clone", "submodule"}:
            return GitAssessment(
                argv=argv,
                command=key,
                risk=RepositoryRisk.MEDIUM,
                required_effect=FrictionEffect.ASK,
                reason="operation contacts a remote and may update refs/worktree",
                dirty_worktree_sensitive=subcommand == "pull",
                remote_effect=True,
            )
        return GitAssessment(
            argv=argv,
            command=key,
            risk=RepositoryRisk.UNKNOWN,
            required_effect=FrictionEffect.DENY,
            reason="unknown Git operation fails closed",
            dirty_worktree_sensitive=dirty_sensitive,
            remote_effect=remote,
        )

    @staticmethod
    def _argv(command: str | Sequence[str]) -> tuple[str, ...]:
        if isinstance(command, str):
            try:
                return tuple(shlex.split(command, posix=False))
            except ValueError:
                return ()
        return tuple(str(item) for item in command)

    @staticmethod
    def _strip_global_options(args: Sequence[str]) -> tuple[str, ...]:
        result: list[str] = []
        skip_next = False
        options_with_value = {
            "-c",
            "-C",
            "--git-dir",
            "--work-tree",
            "--namespace",
            "--exec-path",
        }
        for item in args:
            if skip_next:
                skip_next = False
                continue
            if not result and item in options_with_value:
                skip_next = True
                continue
            if not result and any(
                item.startswith(prefix + "=")
                for prefix in options_with_value
                if prefix.startswith("--")
            ):
                continue
            if not result and item.startswith("-"):
                continue
            result.append(item)
        return tuple(result)

    @staticmethod
    def _key(subcommand: str, args: Sequence[str]) -> str:
        lowered = [item.casefold() for item in args]
        if subcommand == "reset" and "--hard" in lowered:
            return "reset:--hard"
        if subcommand == "checkout" and "--" in lowered:
            return "checkout:--"
        if subcommand == "restore" and any(
            item == "--source" or item.startswith("--source=")
            for item in lowered
        ):
            return "restore:--source"
        if subcommand == "branch" and any(item in {"-D", "-d"} for item in args):
            return "branch:-d-force" if "-D" in args else "branch:-d"
        if subcommand == "push" and "--force-with-lease" in lowered:
            return "push:--force-with-lease"
        if subcommand == "push" and any(
            item in {"--force", "-f"} for item in lowered
        ):
            return "push:--force"
        if subcommand == "clean":
            options = "".join(item.lstrip("-") for item in lowered if item.startswith("-"))
            if "f" in options:
                return "clean:-" + "".join(
                    character for character in "fdx" if character in options
                )
        if subcommand == "branch" and "--list" in lowered:
            return "branch:list"
        if subcommand == "remote" and lowered[:1] == ["get-url"]:
            return "remote:get-url"
        return subcommand


@dataclass(frozen=True, slots=True)
class PatchReceiptObservation:
    transaction_id: str
    workspace_id: str
    path: str
    phase: PatchPhase
    expected_digest: str
    observed_before_digest: str
    observed_after_digest: str
    owner_epoch_before: int
    owner_epoch_after: int
    idempotency_key: str
    causation_id: str
    policy_effect: FrictionEffect
    error_code: str = ""
    rollback_digest: str = ""
    history_id: str = ""
    dirty_paths: tuple[str, ...] = ()
    atomic: bool = False
    committed: bool = False

    @property
    def stale(self) -> bool:
        return bool(
            self.expected_digest
            and self.expected_digest != self.observed_before_digest
        )

    @property
    def changed(self) -> bool:
        return self.observed_before_digest != self.observed_after_digest

    def to_dict(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "workspace_id": self.workspace_id,
            "path": self.path,
            "phase": self.phase.value,
            "expected_digest": self.expected_digest,
            "observed_before_digest": self.observed_before_digest,
            "observed_after_digest": self.observed_after_digest,
            "owner_epoch_before": self.owner_epoch_before,
            "owner_epoch_after": self.owner_epoch_after,
            "idempotency_key": self.idempotency_key,
            "causation_id": self.causation_id,
            "policy_effect": self.policy_effect.value,
            "error_code": self.error_code,
            "rollback_digest": self.rollback_digest,
            "history_id": self.history_id,
            "dirty_paths": list(self.dirty_paths),
            "atomic": self.atomic,
            "committed": self.committed,
            "stale": self.stale,
            "changed": self.changed,
        }


@dataclass(frozen=True, slots=True)
class RepositoryFinding:
    code: str
    transaction_id: str
    path: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "transaction_id": self.transaction_id,
            "path": self.path,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RepositorySecurityReport:
    patches: tuple[PatchReceiptObservation, ...]
    git_assessments: tuple[GitAssessment, ...]
    observed_effects: tuple[FrictionEffect, ...]
    findings: tuple[RepositoryFinding, ...]
    phase_counts: Mapping[str, int]
    risk_counts: Mapping[str, int]
    digest: str

    @property
    def valid(self) -> bool:
        required_phases = {
            PatchPhase.READ,
            PatchPhase.PREPARED,
            PatchPhase.COMMITTED,
            PatchPhase.ROLLED_BACK,
            PatchPhase.REJECTED,
        }
        observed_phases = {item.phase for item in self.patches}
        return not self.findings and required_phases.issubset(observed_phases)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.m3-repository-security/v1",
            "valid": self.valid,
            "patches": [item.to_dict() for item in self.patches],
            "git_assessments": [item.to_dict() for item in self.git_assessments],
            "observed_effects": [item.value for item in self.observed_effects],
            "findings": [item.to_dict() for item in self.findings],
            "phase_counts": dict(sorted(self.phase_counts.items())),
            "risk_counts": dict(sorted(self.risk_counts.items())),
            "digest": self.digest,
        }


class RepositorySecurityAuditor:
    def audit(
        self,
        patches: Sequence[PatchReceiptObservation],
        git_commands: Sequence[str | Sequence[str]],
        observed_effects: Sequence[FrictionEffect | str],
    ) -> RepositorySecurityReport:
        classifier = GitCommandClassifier()
        assessments = tuple(classifier.classify(item) for item in git_commands)
        effects = tuple(FrictionEffect(item) for item in observed_effects)
        findings: list[RepositoryFinding] = []
        by_transaction: dict[str, list[PatchReceiptObservation]] = {}
        for patch in patches:
            by_transaction.setdefault(patch.transaction_id, []).append(patch)
            findings.extend(self._patch_findings(patch))
        findings.extend(self._transaction_findings(by_transaction))
        for index, assessment in enumerate(assessments):
            if index >= len(effects):
                findings.append(
                    RepositoryFinding(
                        code="git_effect_missing",
                        transaction_id="",
                        path="",
                        reason=f"no observed policy effect for {' '.join(assessment.argv)}",
                    )
                )
                continue
            if effects[index] is not assessment.required_effect:
                findings.append(
                    RepositoryFinding(
                        code="git_friction_mismatch",
                        transaction_id="",
                        path="",
                        reason=(
                            f"{' '.join(assessment.argv)} expected "
                            f"{assessment.required_effect.value}, observed "
                            f"{effects[index].value}"
                        ),
                    )
                )
        if len(effects) > len(assessments):
            findings.append(
                RepositoryFinding(
                    code="orphan_git_effect",
                    transaction_id="",
                    path="",
                    reason="observed policy effects exceed classified commands",
                )
            )
        phase_counts = Counter(item.phase.value for item in patches)
        risk_counts = Counter(item.risk.value for item in assessments)
        material = {
            "patches": [item.to_dict() for item in patches],
            "git": [item.to_dict() for item in assessments],
            "effects": [item.value for item in effects],
            "findings": [item.to_dict() for item in findings],
        }
        return RepositorySecurityReport(
            patches=tuple(patches),
            git_assessments=assessments,
            observed_effects=effects,
            findings=tuple(findings),
            phase_counts=dict(phase_counts),
            risk_counts=dict(risk_counts),
            digest=stable_digest(material),
        )

    def mutation_campaign(
        self,
        patches: Sequence[PatchReceiptObservation],
        git_commands: Sequence[str | Sequence[str]],
        observed_effects: Sequence[FrictionEffect | str],
    ) -> Mapping[str, RepositorySecurityReport]:
        baseline = self.audit(patches, git_commands, observed_effects)
        committed = next(
            item for item in patches if item.phase is PatchPhase.COMMITTED
        )
        stale_commit = replace(
            committed,
            transaction_id=f"{committed.transaction_id}-stale",
            expected_digest="sha256:expected",
            observed_before_digest="sha256:changed",
            committed=True,
            error_code="",
        )
        non_atomic = replace(
            committed,
            transaction_id=f"{committed.transaction_id}-atomic",
            atomic=False,
        )
        missing_history = replace(
            committed,
            transaction_id=f"{committed.transaction_id}-history",
            history_id="",
        )
        dirty_bypass = replace(
            committed,
            transaction_id=f"{committed.transaction_id}-dirty",
            dirty_paths=("unrelated-user-change.py",),
            policy_effect=FrictionEffect.ALLOW,
        )
        replay = replace(
            committed,
            transaction_id=f"{committed.transaction_id}-replay",
            idempotency_key="",
        )
        return {
            "baseline": baseline,
            "stale_commit": self.audit(
                (*patches, stale_commit),
                git_commands,
                observed_effects,
            ),
            "non_atomic": self.audit(
                (*patches, non_atomic),
                git_commands,
                observed_effects,
            ),
            "missing_history": self.audit(
                (*patches, missing_history),
                git_commands,
                observed_effects,
            ),
            "dirty_bypass": self.audit(
                (*patches, dirty_bypass),
                git_commands,
                observed_effects,
            ),
            "replay_unfenced": self.audit(
                (*patches, replay),
                git_commands,
                observed_effects,
            ),
            "destructive_allowed": self.audit(
                patches,
                (*git_commands, "git reset --hard"),
                (*observed_effects, FrictionEffect.ALLOW),
            ),
        }

    @staticmethod
    def _patch_findings(
        patch: PatchReceiptObservation,
    ) -> list[RepositoryFinding]:
        findings: list[RepositoryFinding] = []

        def add(code: str, reason: str) -> None:
            findings.append(
                RepositoryFinding(
                    code=code,
                    transaction_id=patch.transaction_id,
                    path=patch.path,
                    reason=reason,
                )
            )

        if not patch.workspace_id or not patch.path:
            add("patch_identity_missing", "workspace/path identity is missing")
        if patch.phase in {
            PatchPhase.PREPARED,
            PatchPhase.COMMITTED,
            PatchPhase.ROLLED_BACK,
        } and not patch.expected_digest:
            add("read_before_write_missing", "patch lacks expected base digest")
        if patch.phase is PatchPhase.COMMITTED:
            if patch.stale:
                add("stale_patch_committed", "stale base digest was committed")
            if not patch.atomic:
                add("non_atomic_commit", "patch commit lacks atomic transaction evidence")
            if not patch.history_id:
                add("history_missing", "committed patch lacks file history identity")
            if not patch.idempotency_key:
                add("idempotency_missing", "committed patch lacks idempotency fence")
            if patch.owner_epoch_after <= patch.owner_epoch_before:
                add("owner_epoch_not_advanced", "commit did not advance owner epoch")
            if not patch.changed:
                add("commit_without_change", "committed patch did not change content")
            if patch.dirty_paths and patch.policy_effect is FrictionEffect.ALLOW:
                add(
                    "dirty_worktree_bypass",
                    "dirty unrelated paths were present without ask/deny friction",
                )
        if patch.phase is PatchPhase.ROLLED_BACK:
            if not patch.rollback_digest:
                add("rollback_digest_missing", "rollback lacks restored digest")
            if patch.rollback_digest != patch.observed_before_digest:
                add("rollback_mismatch", "rollback did not restore the previous digest")
            if not patch.history_id:
                add("rollback_history_missing", "rollback lacks history identity")
        if patch.phase is PatchPhase.REJECTED:
            if not patch.error_code:
                add("rejection_code_missing", "rejected patch lacks stable error code")
            if patch.changed or patch.committed:
                add("rejection_mutated_state", "rejected patch changed content/state")
        if patch.causation_id == "":
            add("causation_missing", "patch observation lacks causation identity")
        return findings

    @staticmethod
    def _transaction_findings(
        transactions: Mapping[str, Sequence[PatchReceiptObservation]],
    ) -> list[RepositoryFinding]:
        findings: list[RepositoryFinding] = []
        for transaction_id, values in transactions.items():
            phases = [item.phase for item in values]
            terminal = {
                item
                for item in phases
                if item in {
                    PatchPhase.COMMITTED,
                    PatchPhase.ROLLED_BACK,
                    PatchPhase.REJECTED,
                }
            }
            if len(terminal) > 1:
                findings.append(
                    RepositoryFinding(
                        code="transaction_multiple_terminal_states",
                        transaction_id=transaction_id,
                        path="",
                        reason=f"terminal phases conflict: {[item.value for item in terminal]}",
                    )
                )
            sequence = {
                PatchPhase.READ: 1,
                PatchPhase.PREPARED: 2,
                PatchPhase.COMMITTED: 3,
                PatchPhase.ROLLED_BACK: 3,
                PatchPhase.REJECTED: 3,
            }
            numeric = [sequence[item] for item in phases]
            if numeric != sorted(numeric):
                findings.append(
                    RepositoryFinding(
                        code="transaction_phase_regression",
                        transaction_id=transaction_id,
                        path="",
                        reason=f"phase order regressed: {[item.value for item in phases]}",
                    )
                )
        return findings


def patch_observations_from_mappings(
    values: Iterable[Mapping[str, Any]],
) -> tuple[PatchReceiptObservation, ...]:
    result: list[PatchReceiptObservation] = []
    for value in values:
        result.append(
            PatchReceiptObservation(
                transaction_id=str(value.get("transaction_id") or ""),
                workspace_id=str(value.get("workspace_id") or ""),
                path=str(value.get("path") or value.get("logical_path") or ""),
                phase=PatchPhase(str(value.get("phase") or "")),
                expected_digest=str(value.get("expected_digest") or ""),
                observed_before_digest=str(
                    value.get("observed_before_digest")
                    or value.get("before_digest")
                    or ""
                ),
                observed_after_digest=str(
                    value.get("observed_after_digest")
                    or value.get("after_digest")
                    or ""
                ),
                owner_epoch_before=int(value.get("owner_epoch_before") or 0),
                owner_epoch_after=int(value.get("owner_epoch_after") or 0),
                idempotency_key=str(value.get("idempotency_key") or ""),
                causation_id=str(value.get("causation_id") or ""),
                policy_effect=FrictionEffect(str(value.get("policy_effect") or "deny")),
                error_code=str(value.get("error_code") or ""),
                rollback_digest=str(value.get("rollback_digest") or ""),
                history_id=str(value.get("history_id") or ""),
                dirty_paths=tuple(str(item) for item in value.get("dirty_paths") or ()),
                atomic=bool(value.get("atomic")),
                committed=bool(value.get("committed")),
            )
        )
    return tuple(result)


def evaluate_repository_security(
    patches: Sequence[PatchReceiptObservation],
    git_commands: Sequence[str | Sequence[str]],
    effects: Sequence[FrictionEffect | str],
) -> CaseExecutionBuffer:
    reports = RepositorySecurityAuditor().mutation_campaign(
        patches,
        git_commands,
        effects,
    )
    buffer = CaseExecutionBuffer()
    baseline = reports["baseline"]
    base_observation = buffer.observe(
        "repository-security.baseline",
        ObservationKind.SECURITY,
        "patch-git-progressive-friction",
        "verified" if baseline.valid else "invalid",
        attributes={
            "report_digest": baseline.digest,
            "phase_counts": baseline.phase_counts,
            "risk_counts": baseline.risk_counts,
        },
    )
    buffer.assert_that(
        "repository-security.baseline-valid",
        baseline.valid,
        "patch lifecycle and Git friction baseline must pass",
        evidence=(base_observation.observation_id,),
        failure_kind=FailureKind.SECURITY,
    )
    for name, report in reports.items():
        if name == "baseline":
            continue
        observation = buffer.observe(
            f"repository-security.{name}",
            ObservationKind.MUTATION,
            f"repository-negative:{name}",
            "mutation-rejected" if not report.valid else "mutation-accepted",
            attributes={
                "report_digest": report.digest,
                "finding_codes": [item.code for item in report.findings],
            },
        )
        buffer.assert_that(
            f"repository-security.reject-{name}",
            not report.valid,
            f"{name} repository-security mutation must be rejected",
            severity=AssertionSeverity.BLOCKER,
            evidence=(observation.observation_id,),
            failure_kind=FailureKind.SECURITY,
        )
    return buffer


__all__ = [
    "FrictionEffect",
    "GitAssessment",
    "GitCommandClassifier",
    "PatchPhase",
    "PatchReceiptObservation",
    "RepositoryFinding",
    "RepositoryRisk",
    "RepositorySecurityAuditor",
    "RepositorySecurityReport",
    "evaluate_repository_security",
    "patch_observations_from_mappings",
]
