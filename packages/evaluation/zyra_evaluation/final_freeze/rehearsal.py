"""Bounded clean-machine, offline, restart, and provider-failure rehearsals.

Rehearsal commands are explicit argv arrays.  Shell evaluation is never used,
the working directory is repository-contained, output is redacted and
truncated, and each drill has a deadline.  This keeps the rehearsal usable as
release evidence without turning a JSON plan into an arbitrary shell script.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .common import (
    FindingLedger,
    environment_snapshot,
    object_with_digest,
    parse_timestamp,
    redact,
    require_boolean,
    require_choice,
    require_identity,
    require_integer,
    require_mapping,
    require_sequence,
    require_text,
    safe_relative_path,
    stable_unique,
    utc_now,
)


DRILL_KINDS = (
    "clean-install",
    "offline-startup",
    "process-restart",
    "provider-failure",
    "semantic-health",
    "package-build",
    "submission-verification",
)
MAX_OUTPUT_CHARACTERS = 32_000
SECRET_KEY_PATTERN = re.compile(
    r"(?:token|secret|password|passwd|api[_-]?key|credential)",
    re.IGNORECASE,
)
ROOT_SOURCE_PATTERN = re.compile(
    r"(?:^|[\s\\/])\.\.(?:[\\/])"
    r"(?:claude-code-best|browser-use|OpenHands|opencode|AgentScope|"
    r"langgraph|hermes|oh-my-pi)(?:[\\/]|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RehearsalCommand:
    """One subprocess invocation within a drill."""

    command_id: str
    argv: tuple[str, ...]
    cwd: str
    timeout_seconds: int
    expected_exit_codes: tuple[int, ...] = (0,)
    environment: tuple[tuple[str, str], ...] = ()
    unset_environment: tuple[str, ...] = ()
    must_contain: tuple[str, ...] = ()
    must_not_contain: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: Any, label: str) -> "RehearsalCommand":
        item = require_mapping(value, label)
        argv = tuple(
            require_text(
                entry,
                f"{label}.argv[{index}]",
                maximum=4096,
            )
            for index, entry in enumerate(
                require_sequence(
                    item.get("argv"),
                    f"{label}.argv",
                    minimum=1,
                )
            )
        )
        expected_exit_codes = tuple(
            require_integer(
                entry,
                f"{label}.expected_exit_codes[{index}]",
                minimum=0,
                maximum=255,
            )
            for index, entry in enumerate(
                require_sequence(
                    item.get("expected_exit_codes", [0]),
                    f"{label}.expected_exit_codes",
                    minimum=1,
                )
            )
        )
        environment_mapping = require_mapping(
            item.get("environment", {}),
            f"{label}.environment",
        )
        environment = tuple(
            (
                _environment_key(key, f"{label}.environment"),
                require_text(
                    raw,
                    f"{label}.environment.{key}",
                    allow_empty=True,
                    maximum=8192,
                ),
            )
            for key, raw in sorted(environment_mapping.items())
        )
        unset_environment = tuple(
            _environment_key(entry, f"{label}.unset_environment[{index}]")
            for index, entry in enumerate(
                require_sequence(
                        item.get("unset_environment", []),
                        f"{label}.unset_environment",
                        allow_empty=True,
                )
            )
        )
        return cls(
            command_id=require_identity(
                item.get("command_id"),
                f"{label}.command_id",
            ),
            argv=argv,
            cwd=safe_relative_path(item.get("cwd", "."), f"{label}.cwd"),
            timeout_seconds=require_integer(
                item.get("timeout_seconds", 300),
                f"{label}.timeout_seconds",
                minimum=1,
                maximum=1800,
            ),
            expected_exit_codes=expected_exit_codes,
            environment=environment,
            unset_environment=unset_environment,
            must_contain=tuple(
                require_text(
                    entry,
                    f"{label}.must_contain[{index}]",
                    maximum=4096,
                )
                for index, entry in enumerate(
                    require_sequence(
                        item.get("must_contain", []),
                        f"{label}.must_contain",
                        allow_empty=True,
                    )
                )
            ),
            must_not_contain=tuple(
                require_text(
                    entry,
                    f"{label}.must_not_contain[{index}]",
                    maximum=4096,
                )
                for index, entry in enumerate(
                    require_sequence(
                        item.get("must_not_contain", []),
                        f"{label}.must_not_contain",
                        allow_empty=True,
                    )
                )
            ),
        )

    def to_dict(self, *, redact_environment: bool = True) -> dict[str, Any]:
        environment = dict(self.environment)
        if redact_environment:
            environment = redact(environment)
        return {
            "command_id": self.command_id,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "expected_exit_codes": list(self.expected_exit_codes),
            "environment": environment,
            "unset_environment": list(self.unset_environment),
            "must_contain": list(self.must_contain),
            "must_not_contain": list(self.must_not_contain),
        }


@dataclass(frozen=True, slots=True)
class DrillDefinition:
    """Definition and acceptance conditions for a rehearsal drill."""

    drill_id: str
    kind: str
    owner: str
    description: str
    commands: tuple[RehearsalCommand, ...]
    required: bool = True
    evidence_paths: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: Any, label: str) -> "DrillDefinition":
        item = require_mapping(value, label)
        commands = tuple(
            RehearsalCommand.from_dict(
                entry,
                f"{label}.commands[{index}]",
            )
            for index, entry in enumerate(
                require_sequence(
                    item.get("commands"),
                    f"{label}.commands",
                    minimum=1,
                )
            )
        )
        return cls(
            drill_id=require_identity(
                item.get("drill_id"),
                f"{label}.drill_id",
            ),
            kind=require_choice(
                item.get("kind"),
                f"{label}.kind",
                DRILL_KINDS,
            ),
            owner=require_identity(item.get("owner"), f"{label}.owner"),
            description=require_text(
                item.get("description"),
                f"{label}.description",
                maximum=4096,
            ),
            commands=commands,
            required=require_boolean(
                item.get("required", True),
                f"{label}.required",
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
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "drill_id": self.drill_id,
            "kind": self.kind,
            "owner": self.owner,
            "description": self.description,
            "commands": [command.to_dict() for command in self.commands],
            "required": self.required,
            "evidence_paths": list(self.evidence_paths),
        }


@dataclass(frozen=True, slots=True)
class RehearsalPlan:
    """Ordered set of drills bound to one target commit."""

    target_commit: str
    created_at: str
    drills: tuple[DrillDefinition, ...]
    clean_state_required: bool = True

    @classmethod
    def from_dict(cls, value: Any) -> "RehearsalPlan":
        item = require_mapping(value, "rehearsal_plan")
        return cls(
            target_commit=require_text(
                item.get("target_commit"),
                "rehearsal_plan.target_commit",
                minimum=7,
                maximum=40,
            ).lower(),
            created_at=parse_timestamp(
                item.get("created_at"),
                "rehearsal_plan.created_at",
            )
            .isoformat()
            .replace("+00:00", "Z"),
            drills=tuple(
                DrillDefinition.from_dict(
                    entry,
                    f"rehearsal_plan.drills[{index}]",
                )
                for index, entry in enumerate(
                    require_sequence(
                        item.get("drills"),
                        "rehearsal_plan.drills",
                        minimum=1,
                    )
                )
            ),
            clean_state_required=require_boolean(
                item.get("clean_state_required", True),
                "rehearsal_plan.clean_state_required",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return object_with_digest(
            {
                "schema": "zyra.final-freeze.rehearsal-plan.v1",
                "target_commit": self.target_commit,
                "created_at": self.created_at,
                "clean_state_required": self.clean_state_required,
                "drills": [drill.to_dict() for drill in self.drills],
            }
        )


class RehearsalPolicy:
    """Validate a plan before any command is allowed to execute."""

    REQUIRED_KINDS = {
        "clean-install",
        "offline-startup",
        "process-restart",
        "provider-failure",
        "semantic-health",
        "package-build",
        "submission-verification",
    }

    def validate(
        self,
        plan: RehearsalPlan,
        repository_root: str | Path,
    ) -> dict[str, Any]:
        root = Path(repository_root).resolve()
        ledger = FindingLedger()
        drill_ids: set[str] = set()
        command_ids: set[str] = set()
        kinds: set[str] = set()
        for drill in plan.drills:
            if drill.drill_id in drill_ids:
                ledger.blocker(
                    "duplicate-drill-id",
                    "rehearsal plan contains a duplicate drill id",
                    category="rehearsal",
                    drill_id=drill.drill_id,
                )
            drill_ids.add(drill.drill_id)
            kinds.add(drill.kind)
            for command in drill.commands:
                if command.command_id in command_ids:
                    ledger.blocker(
                        "duplicate-command-id",
                        "rehearsal command ids must be globally unique",
                        category="rehearsal",
                        command_id=command.command_id,
                    )
                command_ids.add(command.command_id)
                self._validate_command(command, root, ledger)
        missing = sorted(self.REQUIRED_KINDS - kinds)
        if missing:
            ledger.blocker(
                "missing-required-drills",
                "rehearsal plan does not cover all release failure modes",
                category="rehearsal",
                missing=missing,
            )
        if not plan.clean_state_required:
            ledger.blocker(
                "clean-state-not-required",
                "final rehearsal must reject dirty repository state",
                category="rehearsal",
            )
        document = {
            "schema": "zyra.final-freeze.rehearsal-policy.v1",
            "valid": ledger.valid,
            "target_commit": plan.target_commit,
            "drill_count": len(plan.drills),
            "command_count": sum(
                len(drill.commands) for drill in plan.drills
            ),
            "covered_kinds": sorted(kinds),
            "findings": ledger.to_dict(),
        }
        return object_with_digest(document)

    def _validate_command(
        self,
        command: RehearsalCommand,
        root: Path,
        ledger: FindingLedger,
    ) -> None:
        cwd = (root / command.cwd).resolve()
        try:
            cwd.relative_to(root)
        except ValueError:
            ledger.blocker(
                "rehearsal-cwd-escape",
                "rehearsal command working directory escapes repository",
                category="rehearsal",
                command_id=command.command_id,
                cwd=command.cwd,
            )
        joined = " ".join(command.argv)
        if ROOT_SOURCE_PATTERN.search(joined):
            ledger.blocker(
                "root-source-runtime-dependency",
                "rehearsal command references a root source repository",
                category="rehearsal",
                command_id=command.command_id,
            )
        for key, value in command.environment:
            if SECRET_KEY_PATTERN.search(key) and value:
                ledger.blocker(
                    "literal-secret-in-rehearsal",
                    "rehearsal plan cannot embed credential values",
                    category="rehearsal",
                    command_id=command.command_id,
                    key=key,
                )
        if any(
            character in command.argv[0]
            for character in ("|", "&", ";", ">", "<")
        ):
            ledger.blocker(
                "shell-syntax-rejected",
                "rehearsal executable contains shell control syntax",
                category="rehearsal",
                command_id=command.command_id,
            )


class RehearsalRunner:
    """Run a validated rehearsal plan and record redacted evidence."""

    def __init__(
        self,
        repository_root: str | Path,
        *,
        base_environment: Mapping[str, str] | None = None,
    ) -> None:
        self.repository_root = Path(repository_root).resolve()
        self.base_environment = dict(base_environment or os.environ)

    def run(
        self,
        plan: RehearsalPlan | dict[str, Any],
        *,
        continue_after_failure: bool = True,
        require_clean: bool | None = None,
    ) -> dict[str, Any]:
        selected = (
            plan if isinstance(plan, RehearsalPlan) else RehearsalPlan.from_dict(plan)
        )
        policy = RehearsalPolicy().validate(
            selected,
            self.repository_root,
        )
        ledger = FindingLedger()
        if not policy["valid"]:
            ledger.blocker(
                "invalid-rehearsal-plan",
                "rehearsal plan failed admission policy",
                category="rehearsal",
                findings=policy["findings"],
            )
        clean_required = (
            selected.clean_state_required
            if require_clean is None
            else bool(require_clean)
        )
        repository = self._repository_state(selected.target_commit, ledger)
        if clean_required and repository["dirty"]:
            ledger.blocker(
                "dirty-rehearsal-repository",
                "clean rehearsal cannot run with uncommitted changes",
                category="rehearsal",
                changed_paths=repository["changed_paths"],
            )
        drill_results: list[dict[str, Any]] = []
        if ledger.valid:
            for drill in selected.drills:
                result = self._run_drill(drill)
                drill_results.append(result)
                if drill.required and not result["valid"]:
                    ledger.blocker(
                        "required-drill-failed",
                        "required final rehearsal drill failed",
                        category="rehearsal",
                        drill_id=drill.drill_id,
                        kind=drill.kind,
                    )
                    if not continue_after_failure:
                        break
                elif not result["valid"]:
                    ledger.warning(
                        "optional-drill-failed",
                        "optional final rehearsal drill failed",
                        category="rehearsal",
                        drill_id=drill.drill_id,
                        kind=drill.kind,
                    )
        required_count = sum(1 for drill in selected.drills if drill.required)
        required_passed = sum(
            1
            for result in drill_results
            if result["required"] and result["valid"]
        )
        document = {
            "schema": "zyra.final-freeze.rehearsal-receipt.v1",
            "valid": ledger.valid,
            "started_from_commit": repository["head_commit"],
            "target_commit": selected.target_commit,
            "repository": repository,
            "environment": environment_snapshot(self.base_environment),
            "policy_digest": policy["digest"],
            "drill_count": len(drill_results),
            "required_count": required_count,
            "required_passed": required_passed,
            "drills": drill_results,
            "findings": ledger.to_dict(),
            "completed_at": utc_now(),
        }
        return object_with_digest(document)

    def _repository_state(
        self,
        expected_commit: str,
        ledger: FindingLedger,
    ) -> dict[str, Any]:
        try:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.repository_root,
                capture_output=True,
                check=True,
                text=True,
                timeout=30,
            ).stdout.strip()
            status = subprocess.run(
                ["git", "status", "--short"],
                cwd=self.repository_root,
                capture_output=True,
                check=True,
                text=True,
                timeout=30,
            ).stdout.splitlines()
        except (OSError, subprocess.SubprocessError) as error:
            ledger.blocker(
                "repository-inspection-failed",
                "could not inspect repository before rehearsal",
                category="rehearsal",
                error=str(error),
            )
            return {
                "head_commit": None,
                "expected_commit": expected_commit,
                "dirty": True,
                "changed_paths": [],
            }
        if not (
            head == expected_commit
            or head.startswith(expected_commit)
            or expected_commit.startswith(head)
        ):
            ledger.blocker(
                "rehearsal-commit-mismatch",
                "rehearsal repository is not at the planned commit",
                category="rehearsal",
                expected=expected_commit,
                actual=head,
            )
        return {
            "head_commit": head,
            "expected_commit": expected_commit,
            "dirty": bool(status),
            "changed_paths": [
                _redacted_status_path(line)
                for line in status[:200]
            ],
        }

    def _run_drill(self, drill: DrillDefinition) -> dict[str, Any]:
        started = time.monotonic()
        command_results: list[dict[str, Any]] = []
        for command in drill.commands:
            result = self._run_command(command)
            command_results.append(result)
            if not result["valid"]:
                break
        valid = (
            len(command_results) == len(drill.commands)
            and all(result["valid"] for result in command_results)
        )
        return object_with_digest(
            {
                "drill_id": drill.drill_id,
                "kind": drill.kind,
                "owner": drill.owner,
                "required": drill.required,
                "valid": valid,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "evidence_paths": list(drill.evidence_paths),
                "commands": command_results,
            }
        )

    def _run_command(self, command: RehearsalCommand) -> dict[str, Any]:
        environment = dict(self.base_environment)
        for key in command.unset_environment:
            environment.pop(key, None)
        environment.update(dict(command.environment))
        cwd = (self.repository_root / command.cwd).resolve()
        started = time.monotonic()
        timed_out = False
        launch_error: str | None = None
        return_code: int | None = None
        stdout = ""
        stderr = ""
        try:
            completed = subprocess.run(
                list(command.argv),
                cwd=cwd,
                env=environment,
                capture_output=True,
                check=False,
                shell=False,
                text=True,
                timeout=command.timeout_seconds,
            )
            return_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as error:
            timed_out = True
            stdout = _coerce_output(error.stdout)
            stderr = _coerce_output(error.stderr)
        except OSError as error:
            launch_error = str(error)
        combined = f"{stdout}\n{stderr}"
        missing = [
            pattern
            for pattern in command.must_contain
            if pattern not in combined
        ]
        forbidden = [
            pattern
            for pattern in command.must_not_contain
            if pattern in combined
        ]
        valid = (
            not timed_out
            and launch_error is None
            and return_code in command.expected_exit_codes
            and not missing
            and not forbidden
        )
        return object_with_digest(
            {
                "command_id": command.command_id,
                "valid": valid,
                "argv": list(command.argv),
                "cwd": command.cwd,
                "timeout_seconds": command.timeout_seconds,
                "expected_exit_codes": list(command.expected_exit_codes),
                "return_code": return_code,
                "timed_out": timed_out,
                "launch_error": launch_error,
                "duration_ms": round(
                    (time.monotonic() - started) * 1000
                ),
                "missing_output_patterns": missing,
                "forbidden_output_patterns": forbidden,
                "stdout": _redacted_output(stdout),
                "stderr": _redacted_output(stderr),
                "output_truncated": (
                    len(stdout) > MAX_OUTPUT_CHARACTERS
                    or len(stderr) > MAX_OUTPUT_CHARACTERS
                ),
                "environment": environment_snapshot(environment),
            }
        )


class RehearsalReceiptVerifier:
    """Verify an existing rehearsal receipt without rerunning commands."""

    REQUIRED_KINDS = RehearsalPolicy.REQUIRED_KINDS

    def verify(
        self,
        receipt: Any,
        *,
        expected_commit: str,
    ) -> dict[str, Any]:
        item = require_mapping(receipt, "rehearsal_receipt")
        ledger = FindingLedger()
        expected = require_text(
            expected_commit,
            "expected_commit",
            minimum=7,
            maximum=40,
        )
        target = item.get("target_commit")
        head = item.get("started_from_commit")
        for field, actual in (
            ("target_commit", target),
            ("started_from_commit", head),
        ):
            if not isinstance(actual, str) or not (
                actual == expected
                or actual.startswith(expected)
                or expected.startswith(actual)
            ):
                ledger.blocker(
                    "rehearsal-receipt-commit-mismatch",
                    "rehearsal receipt is not bound to expected commit",
                    category="rehearsal-verification",
                    field=field,
                    expected=expected,
                    actual=actual,
                )
        drills = require_sequence(
            item.get("drills"),
            "rehearsal_receipt.drills",
        )
        kinds: set[str] = set()
        for index, raw in enumerate(drills):
            drill = require_mapping(
                raw,
                f"rehearsal_receipt.drills[{index}]",
            )
            kind = require_choice(
                drill.get("kind"),
                f"rehearsal_receipt.drills[{index}].kind",
                DRILL_KINDS,
            )
            kinds.add(kind)
            required = require_boolean(
                drill.get("required"),
                f"rehearsal_receipt.drills[{index}].required",
            )
            valid = require_boolean(
                drill.get("valid"),
                f"rehearsal_receipt.drills[{index}].valid",
            )
            if required and not valid:
                ledger.blocker(
                    "required-rehearsal-receipt-failed",
                    "required drill is not passing in rehearsal receipt",
                    category="rehearsal-verification",
                    drill_id=drill.get("drill_id"),
                    kind=kind,
                )
            for command_index, command_raw in enumerate(
                require_sequence(
                    drill.get("commands"),
                    f"rehearsal_receipt.drills[{index}].commands",
                    minimum=1,
                )
            ):
                command = require_mapping(
                    command_raw,
                    (
                        f"rehearsal_receipt.drills[{index}]"
                        f".commands[{command_index}]"
                    ),
                )
                if command.get("timed_out") is True:
                    ledger.blocker(
                        "rehearsal-command-timed-out",
                        "rehearsal receipt contains a timed out command",
                        category="rehearsal-verification",
                        command_id=command.get("command_id"),
                    )
                serialized = str(command)
                if ROOT_SOURCE_PATTERN.search(serialized):
                    ledger.blocker(
                        "rehearsal-receipt-root-source-path",
                        "rehearsal receipt exposes a root source dependency",
                        category="rehearsal-verification",
                        command_id=command.get("command_id"),
                    )
        missing = sorted(self.REQUIRED_KINDS - kinds)
        if missing:
            ledger.blocker(
                "rehearsal-receipt-kinds-missing",
                "rehearsal receipt lacks required drill kinds",
                category="rehearsal-verification",
                missing=missing,
            )
        if item.get("valid") is not True:
            ledger.blocker(
                "rehearsal-receipt-invalid",
                "rehearsal producer did not mark the receipt valid",
                category="rehearsal-verification",
            )
        document = {
            "schema": "zyra.final-freeze.rehearsal-verification.v1",
            "valid": ledger.valid,
            "expected_commit": expected,
            "covered_kinds": sorted(kinds),
            "drill_count": len(drills),
            "findings": ledger.to_dict(),
        }
        return object_with_digest(document)


def _environment_key(value: Any, label: str) -> str:
    key = require_text(value, label, maximum=255)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        raise ValueError(f"{label} is not a valid environment variable name")
    return key


def _coerce_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _redacted_output(value: str) -> str:
    text = value[:MAX_OUTPUT_CHARACTERS]
    text = re.sub(
        r"(?i)(token|secret|password|api[_-]?key)"
        r"(\s*[=:]\s*)([^\s,;]+)",
        r"\1\2<redacted>",
        text,
    )
    text = re.sub(
        r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{12,}\b",
        "<redacted-token>",
        text,
    )
    return text


def _redacted_status_path(value: str) -> str:
    line = value.strip()
    if len(line) <= 2:
        return line
    return line[3:].replace("\\", "/")
