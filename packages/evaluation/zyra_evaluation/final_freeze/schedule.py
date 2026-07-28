"""Deadline-aware release schedule and feature-freeze admission policy.

The schedule is an executable contract rather than a prose calendar.  Every
gate has an owner, an immutable acceptance definition, an evidence location,
and a transition policy.  The implementation deliberately refuses silent
waivers for blocking gates: a missed gate becomes a release blocker until it
is supplied with passing evidence or the whole final-freeze decision is
re-issued.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from zyra_evaluation.freeze_reporting.canonical import digest, normalize

from .common import (
    FindingLedger,
    object_with_digest,
    parse_timestamp,
    require_boolean,
    require_choice,
    require_identity,
    require_mapping,
    require_sequence,
    require_text,
    safe_relative_path,
    stable_unique,
    utc_now,
)


FINAL_SUBMISSION_DATE = date(2026, 9, 15)
GATE_STATES = (
    "pending",
    "ready",
    "running",
    "passed",
    "failed",
    "blocked",
)
TERMINAL_GATE_STATES = {"passed", "failed", "blocked"}

ALLOWED_TRANSITIONS = {
    "pending": {"ready", "blocked"},
    "ready": {"running", "blocked"},
    "running": {"passed", "failed", "blocked"},
    "failed": {"ready", "blocked"},
    "blocked": {"ready"},
    "passed": set(),
}


@dataclass(frozen=True, slots=True)
class GateDefinition:
    """Immutable definition for one deadline gate."""

    gate_id: str
    title: str
    owner: str
    due_at: str
    acceptance: tuple[str, ...]
    evidence_paths: tuple[str, ...]
    blocking: bool = True
    depends_on: tuple[str, ...] = ()
    description: str = ""

    @classmethod
    def from_dict(cls, value: Any, label: str) -> "GateDefinition":
        item = require_mapping(value, label)
        acceptance = tuple(
            require_text(entry, f"{label}.acceptance[{index}]", maximum=4096)
            for index, entry in enumerate(
                require_sequence(
                    item.get("acceptance"),
                    f"{label}.acceptance",
                    minimum=1,
                )
            )
        )
        evidence_paths = tuple(
            safe_relative_path(entry, f"{label}.evidence_paths[{index}]")
            for index, entry in enumerate(
                require_sequence(
                    item.get("evidence_paths"),
                    f"{label}.evidence_paths",
                    minimum=1,
                )
            )
        )
        dependencies = tuple(
            require_identity(entry, f"{label}.depends_on[{index}]")
            for index, entry in enumerate(
                require_sequence(
                    item.get("depends_on", []),
                    f"{label}.depends_on",
                    allow_empty=True,
                )
            )
        )
        due_at = parse_timestamp(item.get("due_at"), f"{label}.due_at")
        return cls(
            gate_id=require_identity(item.get("gate_id"), f"{label}.gate_id"),
            title=require_text(item.get("title"), f"{label}.title"),
            owner=require_identity(item.get("owner"), f"{label}.owner"),
            due_at=due_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            acceptance=acceptance,
            evidence_paths=evidence_paths,
            blocking=require_boolean(item.get("blocking"), f"{label}.blocking"),
            depends_on=dependencies,
            description=require_text(
                item.get("description", ""),
                f"{label}.description",
                allow_empty=True,
                maximum=4096,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "title": self.title,
            "owner": self.owner,
            "due_at": self.due_at,
            "blocking": self.blocking,
            "depends_on": list(self.depends_on),
            "acceptance": list(self.acceptance),
            "evidence_paths": list(self.evidence_paths),
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class GateState:
    """Recorded state for a gate at a specific review instant."""

    gate_id: str
    state: str
    recorded_at: str
    evidence_paths: tuple[str, ...] = ()
    note: str = ""

    @classmethod
    def from_dict(cls, value: Any, label: str) -> "GateState":
        item = require_mapping(value, label)
        recorded = parse_timestamp(
            item.get("recorded_at"),
            f"{label}.recorded_at",
        )
        return cls(
            gate_id=require_identity(item.get("gate_id"), f"{label}.gate_id"),
            state=require_choice(
                item.get("state"),
                f"{label}.state",
                GATE_STATES,
            ),
            recorded_at=recorded.astimezone(UTC).isoformat().replace(
                "+00:00",
                "Z",
            ),
            evidence_paths=tuple(
                safe_relative_path(entry, f"{label}.evidence_paths[{index}]")
                for index, entry in enumerate(
                    require_sequence(
                        item.get("evidence_paths", []),
                        f"{label}.evidence_paths",
                        allow_empty=True,
                    )
                )
            ),
            note=require_text(
                item.get("note", ""),
                f"{label}.note",
                allow_empty=True,
                maximum=4096,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "state": self.state,
            "recorded_at": self.recorded_at,
            "evidence_paths": list(self.evidence_paths),
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class ChangeRequest:
    """One post-freeze change proposed for admission."""

    change_id: str
    summary: str
    category: str
    risk: str
    owner: str
    requested_at: str
    evidence_paths: tuple[str, ...]
    affects_submission: bool
    rollback_plan: str

    @classmethod
    def from_dict(cls, value: Any, label: str) -> "ChangeRequest":
        item = require_mapping(value, label)
        requested = parse_timestamp(
            item.get("requested_at"),
            f"{label}.requested_at",
        )
        return cls(
            change_id=require_identity(
                item.get("change_id"),
                f"{label}.change_id",
            ),
            summary=require_text(item.get("summary"), f"{label}.summary"),
            category=require_choice(
                item.get("category"),
                f"{label}.category",
                (
                    "release-blocker-fix",
                    "security-fix",
                    "evidence-repair",
                    "documentation",
                    "feature",
                    "optimization",
                    "refactor",
                ),
            ),
            risk=require_choice(
                item.get("risk"),
                f"{label}.risk",
                ("low", "medium", "high", "critical"),
            ),
            owner=require_identity(item.get("owner"), f"{label}.owner"),
            requested_at=requested.astimezone(UTC).isoformat().replace(
                "+00:00",
                "Z",
            ),
            evidence_paths=tuple(
                safe_relative_path(entry, f"{label}.evidence_paths[{index}]")
                for index, entry in enumerate(
                    require_sequence(
                        item.get("evidence_paths"),
                        f"{label}.evidence_paths",
                        minimum=1,
                    )
                )
            ),
            affects_submission=require_boolean(
                item.get("affects_submission"),
                f"{label}.affects_submission",
            ),
            rollback_plan=require_text(
                item.get("rollback_plan"),
                f"{label}.rollback_plan",
                maximum=4096,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_id": self.change_id,
            "summary": self.summary,
            "category": self.category,
            "risk": self.risk,
            "owner": self.owner,
            "requested_at": self.requested_at,
            "evidence_paths": list(self.evidence_paths),
            "affects_submission": self.affects_submission,
            "rollback_plan": self.rollback_plan,
        }


class ReleaseSchedule:
    """Validate schedule definitions and produce deadline decisions."""

    def __init__(
        self,
        definitions: list[GateDefinition],
        states: list[GateState],
        *,
        submission_date: date = FINAL_SUBMISSION_DATE,
    ) -> None:
        self.definitions = list(definitions)
        self.states = list(states)
        self.submission_date = submission_date

    @classmethod
    def from_dict(cls, value: Any) -> "ReleaseSchedule":
        document = require_mapping(value, "release_schedule")
        definitions = [
            GateDefinition.from_dict(entry, f"release_schedule.gates[{index}]")
            for index, entry in enumerate(
                require_sequence(
                    document.get("gates"),
                    "release_schedule.gates",
                    minimum=1,
                )
            )
        ]
        states = [
            GateState.from_dict(entry, f"release_schedule.states[{index}]")
            for index, entry in enumerate(
                require_sequence(
                    document.get("states"),
                    "release_schedule.states",
                    minimum=1,
                )
            )
        ]
        submission = date.fromisoformat(
            require_text(
                document.get("submission_date"),
                "release_schedule.submission_date",
            )
        )
        return cls(definitions, states, submission_date=submission)

    @classmethod
    def default(
        cls,
        *,
        recorded_at: str | None = None,
        evidence_root: str = "docs/reviews/evidence/M3-S03-02",
    ) -> "ReleaseSchedule":
        """Create the committed first-stage-to-submission countdown."""

        stamp = recorded_at or utc_now()
        specs = [
            (
                "stage-freeze",
                "Freeze first-stage implementation and evidence",
                "release-owner",
                date(2026, 7, 28),
                (),
                (
                    "M3-S03-02 final verifier passes",
                    "the evidence commit is immutable and named",
                ),
                "final-freeze-receipt.json",
            ),
            (
                "rc1",
                "Build release candidate 1",
                "submission-owner",
                date(2026, 9, 1),
                ("stage-freeze",),
                (
                    "manifest and checksums independently verify",
                    "submission naming and material inventory pass",
                ),
                "submission-verification.json",
            ),
            (
                "clean-rehearsal",
                "Complete rehearsal and feature freeze",
                "rehearsal-owner",
                date(2026, 9, 8),
                ("rc1",),
                (
                    "offline installation path is explicit",
                    "restart and provider-failure drills pass",
                    "feature freeze admission policy is active",
                ),
                "rehearsal-receipt.json",
            ),
            (
                "dual-review",
                "Complete independent dual-review sign-off",
                "review-owner",
                date(2026, 9, 12),
                ("clean-rehearsal",),
                (
                    "two distinct reviewers attest to the same manifest",
                    "no unresolved first-stage blocker exists",
                ),
                "dual-review-receipt.json",
            ),
            (
                "submission-lock",
                "Lock the exact submission artifacts",
                "release-owner",
                date(2026, 9, 12),
                ("dual-review",),
                (
                    "archive digests equal the reviewed candidate",
                    "last-mile instructions are executable",
                ),
                "submission-lock.json",
            ),
            (
                "submit",
                "Submit before the official deadline",
                "submission-owner",
                date(2026, 9, 15),
                ("submission-lock",),
                (
                    "submission confirmation is retained",
                    "submitted digests equal the locked manifest",
                ),
                "submission-confirmation.json",
            ),
        ]
        definitions: list[GateDefinition] = []
        states: list[GateState] = []
        for (
            gate_id,
            title,
            owner,
            due_date,
            dependencies,
            acceptance,
            evidence_name,
        ) in specs:
            due = datetime.combine(
                due_date,
                datetime.min.time(),
                tzinfo=UTC,
            )
            definitions.append(
                GateDefinition(
                    gate_id=gate_id,
                    title=title,
                    owner=owner,
                    due_at=due.isoformat().replace("+00:00", "Z"),
                    acceptance=tuple(acceptance),
                    evidence_paths=(f"{evidence_root}/{evidence_name}",),
                    blocking=True,
                    depends_on=tuple(dependencies),
                    description=(
                        f"Fixed first-stage release gate for "
                        f"{due_date.isoformat()}."
                    ),
                )
            )
            states.append(
                GateState(
                    gate_id=gate_id,
                    state="pending",
                    recorded_at=stamp,
                )
            )
        return cls(definitions, states)

    def evaluate(
        self,
        *,
        now: str | None = None,
        require_all_passed: bool = False,
    ) -> dict[str, Any]:
        ledger = FindingLedger()
        instant = parse_timestamp(now or utc_now(), "schedule.now")
        definitions = self._definitions_by_id(ledger)
        states = self._states_by_id(ledger)
        self._validate_submission_date(ledger)
        self._validate_dependencies(definitions, ledger)
        rows: list[dict[str, Any]] = []
        for gate_id in sorted(definitions):
            definition = definitions[gate_id]
            state = states.get(gate_id)
            row = self._evaluate_gate(
                definition,
                state,
                definitions,
                states,
                instant,
                ledger,
                require_all_passed=require_all_passed,
            )
            rows.append(row)
        for gate_id in sorted(set(states) - set(definitions)):
            ledger.blocker(
                "unknown-gate-state",
                "a state record names an undefined schedule gate",
                category="schedule",
                gate_id=gate_id,
            )
        document = {
            "schema": "zyra.final-freeze.release-schedule.v1",
            "valid": ledger.valid,
            "evaluated_at": instant.astimezone(UTC).isoformat().replace(
                "+00:00",
                "Z",
            ),
            "submission_date": self.submission_date.isoformat(),
            "days_to_submission": (
                self.submission_date - instant.date()
            ).days,
            "require_all_passed": require_all_passed,
            "gates": rows,
            "findings": ledger.to_dict(),
        }
        return object_with_digest(document)

    def transition(
        self,
        gate_id: str,
        next_state: str,
        *,
        recorded_at: str | None = None,
        evidence_paths: tuple[str, ...] = (),
        note: str = "",
    ) -> "ReleaseSchedule":
        identity = require_identity(gate_id, "gate_id")
        desired = require_choice(next_state, "next_state", GATE_STATES)
        definitions = {item.gate_id: item for item in self.definitions}
        states = {item.gate_id: item for item in self.states}
        if identity not in definitions:
            raise ValueError(f"unknown release gate: {identity}")
        if identity not in states:
            raise ValueError(f"missing state for release gate: {identity}")
        current = states[identity]
        if desired not in ALLOWED_TRANSITIONS[current.state]:
            raise ValueError(
                f"invalid release gate transition: "
                f"{current.state} -> {desired}"
            )
        if desired == "passed" and not evidence_paths:
            raise ValueError("passing a release gate requires evidence")
        if desired in {"ready", "running", "passed"}:
            unmet = [
                dependency
                for dependency in definitions[identity].depends_on
                if states.get(dependency) is None
                or states[dependency].state != "passed"
            ]
            if unmet:
                raise ValueError(
                    f"release gate dependencies have not passed: {unmet}"
                )
        states[identity] = GateState(
            gate_id=identity,
            state=desired,
            recorded_at=parse_timestamp(
                recorded_at or utc_now(),
                "recorded_at",
            )
            .astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            evidence_paths=tuple(
                safe_relative_path(path, "evidence_path")
                for path in evidence_paths
            ),
            note=require_text(
                note,
                "note",
                allow_empty=True,
                maximum=4096,
            ),
        )
        return ReleaseSchedule(
            list(self.definitions),
            [states[key] for key in sorted(states)],
            submission_date=self.submission_date,
        )

    def to_dict(self) -> dict[str, Any]:
        document = {
            "schema": "zyra.final-freeze.release-schedule-input.v1",
            "submission_date": self.submission_date.isoformat(),
            "gates": [item.to_dict() for item in self.definitions],
            "states": [item.to_dict() for item in self.states],
        }
        return object_with_digest(document)

    def _definitions_by_id(
        self,
        ledger: FindingLedger,
    ) -> dict[str, GateDefinition]:
        values: dict[str, GateDefinition] = {}
        for definition in self.definitions:
            if definition.gate_id in values:
                ledger.blocker(
                    "duplicate-gate-definition",
                    "release schedule contains a duplicate gate id",
                    category="schedule",
                    gate_id=definition.gate_id,
                )
            values[definition.gate_id] = definition
        if not values:
            ledger.blocker(
                "missing-gates",
                "release schedule must define at least one gate",
                category="schedule",
            )
        return values

    def _states_by_id(
        self,
        ledger: FindingLedger,
    ) -> dict[str, GateState]:
        values: dict[str, GateState] = {}
        for state in self.states:
            if state.gate_id in values:
                ledger.blocker(
                    "duplicate-gate-state",
                    "release schedule contains duplicate state records",
                    category="schedule",
                    gate_id=state.gate_id,
                )
            values[state.gate_id] = state
        return values

    def _validate_submission_date(self, ledger: FindingLedger) -> None:
        if self.submission_date != FINAL_SUBMISSION_DATE:
            ledger.blocker(
                "submission-date-mismatch",
                "schedule does not use the official 2026-09-15 deadline",
                category="schedule",
                expected=FINAL_SUBMISSION_DATE.isoformat(),
                actual=self.submission_date.isoformat(),
            )

    def _validate_dependencies(
        self,
        definitions: dict[str, GateDefinition],
        ledger: FindingLedger,
    ) -> None:
        for gate_id, definition in definitions.items():
            for dependency in definition.depends_on:
                if dependency == gate_id:
                    ledger.blocker(
                        "self-dependent-gate",
                        "release gate cannot depend on itself",
                        category="schedule",
                        gate_id=gate_id,
                    )
                elif dependency not in definitions:
                    ledger.blocker(
                        "unknown-gate-dependency",
                        "release gate names an undefined dependency",
                        category="schedule",
                        gate_id=gate_id,
                        dependency=dependency,
                    )
                elif parse_timestamp(
                    definitions[dependency].due_at,
                    "dependency.due_at",
                ) > parse_timestamp(definition.due_at, "gate.due_at"):
                    ledger.blocker(
                        "dependency-due-after-gate",
                        "dependency is due after its dependent gate",
                        category="schedule",
                        gate_id=gate_id,
                        dependency=dependency,
                    )
        self._detect_cycles(definitions, ledger)

    def _detect_cycles(
        self,
        definitions: dict[str, GateDefinition],
        ledger: FindingLedger,
    ) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(gate_id: str, trail: list[str]) -> None:
            if gate_id in visiting:
                cycle = trail[trail.index(gate_id) :] + [gate_id]
                ledger.blocker(
                    "cyclic-gate-dependencies",
                    "release schedule dependency graph contains a cycle",
                    category="schedule",
                    cycle=cycle,
                )
                return
            if gate_id in visited:
                return
            visiting.add(gate_id)
            for dependency in definitions[gate_id].depends_on:
                if dependency in definitions:
                    visit(dependency, [*trail, dependency])
            visiting.remove(gate_id)
            visited.add(gate_id)

        for gate_id in sorted(definitions):
            visit(gate_id, [gate_id])

    def _evaluate_gate(
        self,
        definition: GateDefinition,
        state: GateState | None,
        definitions: dict[str, GateDefinition],
        states: dict[str, GateState],
        instant: datetime,
        ledger: FindingLedger,
        *,
        require_all_passed: bool,
    ) -> dict[str, Any]:
        due = parse_timestamp(definition.due_at, "gate.due_at")
        if state is None:
            ledger.blocker(
                "missing-gate-state",
                "release gate has no state record",
                category="schedule",
                gate_id=definition.gate_id,
            )
            state_name = "missing"
            evidence_paths: list[str] = []
            recorded_at = None
        else:
            state_name = state.state
            evidence_paths = stable_unique(state.evidence_paths)
            recorded_at = state.recorded_at
        overdue = instant > due and state_name != "passed"
        if overdue and definition.blocking:
            ledger.blocker(
                "overdue-blocking-gate",
                "blocking release gate is overdue and has not passed",
                category="schedule",
                gate_id=definition.gate_id,
                due_at=definition.due_at,
                state=state_name,
            )
        if require_all_passed and definition.blocking and state_name != "passed":
            ledger.blocker(
                "unpassed-blocking-gate",
                "final submission requires every blocking gate to pass",
                category="schedule",
                gate_id=definition.gate_id,
                state=state_name,
            )
        if state_name == "passed":
            if not evidence_paths:
                ledger.blocker(
                    "passed-gate-without-evidence",
                    "a passed release gate has no evidence",
                    category="schedule",
                    gate_id=definition.gate_id,
                )
            missing_declared = sorted(
                set(definition.evidence_paths) - set(evidence_paths)
            )
            if missing_declared:
                ledger.blocker(
                    "passed-gate-missing-declared-evidence",
                    "passed release gate omits declared acceptance evidence",
                    category="schedule",
                    gate_id=definition.gate_id,
                    missing=missing_declared,
                )
        dependencies = []
        for dependency in definition.depends_on:
            dependency_state = states.get(dependency)
            dependencies.append(
                {
                    "gate_id": dependency,
                    "state": (
                        dependency_state.state
                        if dependency_state is not None
                        else "missing"
                    ),
                    "due_at": (
                        definitions[dependency].due_at
                        if dependency in definitions
                        else None
                    ),
                }
            )
        return {
            **definition.to_dict(),
            "state": state_name,
            "recorded_at": recorded_at,
            "recorded_evidence_paths": evidence_paths,
            "overdue": overdue,
            "terminal": state_name in TERMINAL_GATE_STATES,
            "dependency_states": dependencies,
        }


class ChangeAdmissionPolicy:
    """Fail-closed policy for changes proposed after first-stage freeze."""

    ALLOWED_CATEGORIES = {
        "release-blocker-fix",
        "security-fix",
        "evidence-repair",
        "documentation",
    }

    def evaluate(
        self,
        request: ChangeRequest | dict[str, Any],
        *,
        freeze_active: bool = True,
        first_stage_blockers: int = 0,
    ) -> dict[str, Any]:
        item = (
            request
            if isinstance(request, ChangeRequest)
            else ChangeRequest.from_dict(request, "change_request")
        )
        ledger = FindingLedger()
        if first_stage_blockers < 0:
            ledger.blocker(
                "invalid-blocker-count",
                "first-stage blocker count cannot be negative",
                category="change-admission",
                count=first_stage_blockers,
            )
        if freeze_active and item.category not in self.ALLOWED_CATEGORIES:
            ledger.blocker(
                "post-freeze-feature-rejected",
                "feature, optimization, and refactor work is closed after freeze",
                category="change-admission",
                change_id=item.change_id,
                requested_category=item.category,
            )
        if item.risk in {"high", "critical"}:
            ledger.blocker(
                "high-risk-post-freeze-change",
                "high-risk changes require a new freeze decision",
                category="change-admission",
                change_id=item.change_id,
                risk=item.risk,
            )
        if item.affects_submission and item.category == "documentation":
            ledger.warning(
                "submission-document-change",
                "submission-facing documentation must rebuild checksums",
                category="change-admission",
                change_id=item.change_id,
            )
        if (
            first_stage_blockers > 0
            and item.category not in {"release-blocker-fix", "security-fix"}
        ):
            ledger.blocker(
                "change-does-not-close-blocker",
                "non-blocker change cannot be admitted while blockers remain",
                category="change-admission",
                change_id=item.change_id,
                first_stage_blockers=first_stage_blockers,
            )
        decision = {
            "schema": "zyra.final-freeze.change-admission.v1",
            "change": item.to_dict(),
            "freeze_active": freeze_active,
            "first_stage_blockers": first_stage_blockers,
            "decision": "admit" if ledger.valid else "reject",
            "required_actions": self._required_actions(item, ledger.valid),
            "findings": ledger.to_dict(),
        }
        return object_with_digest(decision)

    def _required_actions(
        self,
        request: ChangeRequest,
        admitted: bool,
    ) -> list[str]:
        if not admitted:
            return [
                "do not mutate the frozen submission candidate",
                "resolve blockers or issue a new final-freeze decision",
            ]
        actions = [
            "run tests directly affected by the change",
            "re-run the M3-03 incremental critical review",
            "rebuild and independently verify the submission manifest",
        ]
        if request.affects_submission:
            actions.extend(
                [
                    "invalidate prior reviewer attestations",
                    "regenerate submission checksums",
                ]
            )
        if request.category in {"release-blocker-fix", "security-fix"}:
            actions.append("record the fixed blocker and regression evidence")
        return stable_unique(actions)


def schedule_summary(schedule: ReleaseSchedule, *, now: str) -> dict[str, Any]:
    """Return a compact operator projection without weakening validation."""

    receipt = schedule.evaluate(now=now)
    gates = require_sequence(receipt["gates"], "schedule_receipt.gates")
    counts: dict[str, int] = {}
    next_gates: list[dict[str, Any]] = []
    for raw in gates:
        gate = require_mapping(raw, "schedule_receipt.gate")
        state = require_text(gate.get("state"), "schedule_receipt.gate.state")
        counts[state] = counts.get(state, 0) + 1
        if state != "passed":
            next_gates.append(
                {
                    "gate_id": gate["gate_id"],
                    "title": gate["title"],
                    "due_at": gate["due_at"],
                    "owner": gate["owner"],
                    "state": state,
                    "overdue": gate["overdue"],
                }
            )
    next_gates.sort(key=lambda item: (item["due_at"], item["gate_id"]))
    summary = {
        "schema": "zyra.final-freeze.release-schedule-summary.v1",
        "valid": receipt["valid"],
        "submission_date": receipt["submission_date"],
        "days_to_submission": receipt["days_to_submission"],
        "state_counts": normalize(counts),
        "next_gate": next_gates[0] if next_gates else None,
        "remaining_gates": next_gates,
        "schedule_digest": receipt["digest"],
    }
    return object_with_digest(summary)
