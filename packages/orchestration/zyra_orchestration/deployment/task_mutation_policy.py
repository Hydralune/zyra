from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from zyra_runtime.sandbox_gateway.canonical import canonical_logical_path


TASK_MUTATION_POLICY_SCHEMA = "zyra.task-mutation-policy/v1"

_TEST_PATH_COMPONENTS = frozenset(
    {"test", "tests", "spec", "specs", "__tests__"}
)
_TEST_FILE = re.compile(
    r"(?:^test[_-].+|.+[_-]test|.+\.(?:test|spec))"
    r"(?:\.[A-Za-z0-9]+)?$",
    re.IGNORECASE,
)
_TEST_COMMAND = re.compile(
    r"(?:^|\s)(?:"
    r"pytest|py\.test|unittest|jest|vitest|mocha|phpunit|rspec|"
    r"cargo\s+test|go\s+test|dotnet\s+test|mvn(?:\.cmd)?\s+test|"
    r"gradle(?:w)?\s+test|npm(?:\.cmd)?\s+(?:run\s+)?test|"
    r"pnpm(?:\.cmd)?\s+(?:run\s+)?test|yarn(?:\.cmd)?\s+(?:run\s+)?test|"
    r"bun\s+(?:run\s+)?test"
    r")(?:\s|$)",
    re.IGNORECASE,
)


class TaskMutationPolicyViolation(RuntimeError):
    """A task-scoped workspace invariant rejected a physical mutation."""

    def __init__(self, reason: str, *, logical_path: str = "") -> None:
        super().__init__(reason)
        self.logical_path = logical_path


@dataclass(frozen=True, slots=True)
class TaskMutationPolicy:
    protected_source_roots: tuple[str, ...] = ()
    required_pre_mutation_evidence: tuple[str, ...] = ()
    protect_existing_test_files: bool = False
    inherit_across_execution_lineage: bool = False
    enabled: bool = False
    schema: str = TASK_MUTATION_POLICY_SCHEMA

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "TaskMutationPolicy":
        payload = dict(value or {})
        roots: list[str] = []
        for raw in payload.get("protected_source_roots") or ():
            canonical = canonical_logical_path(str(raw), allow_root=False)
            if canonical not in roots:
                roots.append(canonical)
        evidence = tuple(
            dict.fromkeys(
                str(item).strip()
                for item in payload.get("required_pre_mutation_evidence") or ()
                if str(item).strip()
            )
        )
        requested = bool(
            roots
            or evidence
            or payload.get("protect_existing_test_files") is True
        )
        return cls(
            protected_source_roots=tuple(roots),
            required_pre_mutation_evidence=evidence,
            protect_existing_test_files=(
                payload.get("protect_existing_test_files") is True
            ),
            inherit_across_execution_lineage=(
                payload.get("inherit_across_execution_lineage") is True
            ),
            enabled=payload.get("enabled") is True and requested,
        )

    @property
    def baseline_required(self) -> bool:
        return "existing_test_baseline" in self.required_pre_mutation_evidence

    def protects_path(self, logical_path: str) -> bool:
        canonical = canonical_logical_path(logical_path, allow_root=False)
        return any(
            canonical == root or canonical.startswith(f"{root}/")
            for root in self.protected_source_roots
        )

    def is_existing_test_path(self, logical_path: str) -> bool:
        canonical = canonical_logical_path(logical_path, allow_root=False)
        parts = tuple(part.casefold() for part in canonical.split("/"))
        return bool(
            any(part in _TEST_PATH_COMPONENTS for part in parts[:-1])
            or _TEST_FILE.fullmatch(parts[-1])
        )


class TaskMutationPolicyGuard:
    """Durable task-lineage owner for ordered workspace mutation policy."""

    def __init__(
        self,
        policy: TaskMutationPolicy,
        *,
        state_root: str | Path,
    ) -> None:
        self.policy = policy
        self.state_root = Path(state_root).resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_root / "state.json"
        self.receipt_path = self.state_root / "receipts.jsonl"
        self._lock = threading.RLock()
        self._state = self._load_state()

    @property
    def enabled(self) -> bool:
        return self.policy.enabled

    @property
    def baseline_satisfied(self) -> bool:
        with self._lock:
            return bool(self._state.get("existing_test_baseline"))

    def assert_mutation(self, logical_path: str, *, existed: bool) -> None:
        if not self.enabled:
            return
        canonical = canonical_logical_path(logical_path, allow_root=False)
        reason = self.denial_reason(canonical, existed=existed)
        if not reason:
            return
        self._record(
            "mutation_denied",
            logical_path=canonical,
            reason=reason,
            baseline_satisfied=self.baseline_satisfied,
        )
        raise TaskMutationPolicyViolation(reason, logical_path=canonical)

    def denial_reason(self, logical_path: str, *, existed: bool) -> str:
        if not self.enabled:
            return ""
        canonical = canonical_logical_path(logical_path, allow_root=False)
        if self.policy.protects_path(canonical):
            return (
                f"task mutation policy protects source input {canonical}; "
                "read it and write the isolated working/output copy elsewhere"
            )
        if self.policy.baseline_required and not self.baseline_satisfied:
            return (
                "task mutation policy requires a completed existing-test baseline "
                f"receipt before changing {canonical}"
            )
        if (
            existed
            and self.policy.protect_existing_test_files
            and self.policy.is_existing_test_path(canonical)
        ):
            return (
                f"task mutation policy protects existing test file {canonical}; "
                "add a new root-cause test instead of rewriting it"
            )
        return ""

    def prohibited_delta(
        self,
        before: Mapping[str, Mapping[str, Any]],
        after: Mapping[str, Mapping[str, Any]],
        *,
        baseline_satisfied_before: bool,
    ) -> tuple[str, ...]:
        if not self.enabled:
            return ()
        before_paths = set(before)
        after_paths = set(after)
        changed = (before_paths ^ after_paths) | {
            path
            for path in before_paths & after_paths
            if before[path].get("sha256") != after[path].get("sha256")
        }
        prohibited: list[str] = []
        for path in sorted(changed):
            if self.policy.baseline_required and not baseline_satisfied_before:
                prohibited.append(path)
                continue
            if self.policy.protects_path(path):
                prohibited.append(path)
                continue
            if (
                path in before_paths
                and self.policy.protect_existing_test_files
                and self.policy.is_existing_test_path(path)
            ):
                prohibited.append(path)
        return tuple(prohibited)

    def observe_command(
        self,
        *,
        executable: str,
        argv: Sequence[str],
        metadata: Mapping[str, Any],
        termination: str,
        return_code: int | None,
        command_id: str,
    ) -> bool:
        if not self.enabled or not self.policy.baseline_required:
            return False
        if self.baseline_satisfied:
            return False
        verification = _truthy(metadata.get("progressive_verification_driving"))
        if (
            not verification
            or termination != "exited"
            or return_code is None
            or not _is_test_command(executable, argv)
        ):
            return False
        command_digest = hashlib.sha256(
            json.dumps(
                [str(executable), *(str(item) for item in argv)],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with self._lock:
            self._state["existing_test_baseline"] = {
                "command_id": str(command_id),
                "command_digest": f"sha256:{command_digest}",
                "return_code": int(return_code),
                "observed_at": time.time(),
            }
            self._persist_state()
        self._record(
            "pre_mutation_evidence_observed",
            evidence="existing_test_baseline",
            command_id=str(command_id),
            command_digest=f"sha256:{command_digest}",
            return_code=int(return_code),
        )
        return True

    def record_shell_denial(
        self,
        *,
        command_id: str,
        prohibited_paths: Sequence[str],
    ) -> None:
        self._record(
            "shell_mutation_reverted",
            command_id=str(command_id),
            prohibited_path_count=len(tuple(prohibited_paths)),
            prohibited_path_digests=[
                "sha256:" + hashlib.sha256(path.encode("utf-8")).hexdigest()
                for path in prohibited_paths
            ],
        )

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            state = {
                "schema": "zyra.task-mutation-policy-state/v1",
                "policy_schema": self.policy.schema,
                "existing_test_baseline": None,
            }
            self._write_json(state)
            return state
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("task mutation policy state is unreadable") from error
        if not isinstance(value, Mapping):
            raise RuntimeError("task mutation policy state must be an object")
        return dict(value)

    def _persist_state(self) -> None:
        self._write_json(self._state)

    def _write_json(self, value: Mapping[str, Any]) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                dict(value),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)

    def _record(self, event: str, **payload: Any) -> None:
        record = {
            "schema": "zyra.task-mutation-policy-receipt/v1",
            "event": str(event),
            "observed_at": time.time(),
            **payload,
        }
        rendered = json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._lock:
            with self.receipt_path.open("a", encoding="utf-8") as handle:
                handle.write(rendered + "\n")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _is_test_command(executable: str, argv: Sequence[str]) -> bool:
    rendered = " ".join(
        (str(executable).strip(), *(str(item).strip() for item in argv))
    ).replace("\\", "/")
    return bool(_TEST_COMMAND.search(rendered))


__all__ = [
    "TASK_MUTATION_POLICY_SCHEMA",
    "TaskMutationPolicy",
    "TaskMutationPolicyGuard",
    "TaskMutationPolicyViolation",
]
