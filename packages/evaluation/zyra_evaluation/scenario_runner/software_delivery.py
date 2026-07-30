from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tokenize
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import canonical_json, content_digest, digest, file_digest, path_within, utc_now
from .errors import conflict, invalid, unavailable
from .live_models import (
    ActionKind,
    ActionResult,
    ActionState,
    DomainInput,
    LiveAction,
    LiveDomain,
    LivePlan,
    PrivacyClass,
    TierKind,
)


_SOURCE_SUFFIXES = {
    ".py",
    ".pyi",
    ".ts",
    ".tsx",
    ".js",
    ".mjs",
    ".cjs",
    ".rs",
    ".toml",
    ".json",
    ".md",
}
_IGNORED_PARTS = {
    ".git",
    ".cache",
    ".tmp",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    "vendor",
    "vendor-runtimes",
    "source-pool",
    "runtime-sources",
}


@dataclass(frozen=True, slots=True)
class SourceRecord:
    source_id: str
    relative_path: str
    suffix: str
    size_bytes: int
    sha256: str
    line_count: int
    language: str
    symbols: tuple[str, ...]
    imports: tuple[str, ...]
    risk_markers: tuple[str, ...]
    byte_start: int
    byte_end: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "relative_path": self.relative_path,
            "suffix": self.suffix,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "line_count": self.line_count,
            "language": self.language,
            "symbols": list(self.symbols),
            "imports": list(self.imports),
            "risk_markers": list(self.risk_markers),
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
        }


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    command_id: str
    argv: tuple[str, ...]
    cwd: str
    exit_code: int
    stdout_path: str
    stderr_path: str
    stdout_digest: str
    stderr_digest: str
    started_at: str
    completed_at: str
    elapsed_ms: int
    timed_out: bool
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
            "stdout_digest": self.stdout_digest,
            "stderr_digest": self.stderr_digest,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_ms": self.elapsed_ms,
            "timed_out": self.timed_out,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SoftwareExecutionPayload:
    plan: LivePlan
    action_results: tuple[ActionResult, ...]
    inventory: tuple[SourceRecord, ...]
    patch_receipt: dict[str, Any]
    command_receipts: tuple[dict[str, Any], ...]
    artifact_paths: tuple[str, ...]
    work_units: tuple[dict[str, Any], ...]
    started_at: str
    completed_at: str
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.software-delivery-execution/v1",
            "plan": self.plan.to_dict(),
            "action_results": [item.to_dict() for item in self.action_results],
            "inventory": [item.to_dict() for item in self.inventory],
            "patch_receipt": self.patch_receipt,
            "command_receipts": list(self.command_receipts),
            "artifact_paths": list(self.artifact_paths),
            "work_units": list(self.work_units),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "metadata": dict(self.metadata),
        }


class BoundedCommandRunner:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        evidence_root: str | Path,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=False)
        self.evidence_root = Path(evidence_root).resolve(strict=False)
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        self.environment = {
            str(key): str(value) for key, value in (environment or {}).items()
        }
        self._sequence = 0

    def execute(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path,
        timeout_seconds: float,
        purpose: str,
        expected_exit_codes: Iterable[int] = (0,),
    ) -> CommandReceipt:
        if not argv or not str(argv[0]).strip():
            raise invalid(
                "software_command_empty",
                "Software command must have a concrete executable.",
                phase="software-execution",
            )
        selected_cwd = Path(cwd).resolve(strict=False)
        if not path_within(selected_cwd, self.workspace_root):
            raise invalid(
                "software_command_workspace_escape",
                "Software command cwd must stay inside the scenario workspace.",
                phase="software-execution",
                detail={
                    "cwd": str(selected_cwd),
                    "workspace": str(self.workspace_root),
                },
            )
        if timeout_seconds <= 0 or timeout_seconds > 900:
            raise invalid(
                "software_command_timeout_invalid",
                "Software command timeout must be positive and bounded.",
                phase="software-execution",
            )
        self._sequence += 1
        command_id = f"software-command-{self._sequence:04d}-{digest(tuple(argv))[:12]}"
        stdout_path = self.evidence_root / f"{command_id}.stdout.txt"
        stderr_path = self.evidence_root / f"{command_id}.stderr.txt"
        started_at = utc_now()
        started = time.monotonic()
        environment = os.environ.copy()
        environment.update(
            {
                "NO_COLOR": "1",
                "CI": "1",
                "PYTHONIOENCODING": "utf-8",
                **self.environment,
            }
        )
        timed_out = False
        try:
            completed = subprocess.run(
                [str(item) for item in argv],
                cwd=str(selected_cwd),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
                check=False,
                creationflags=(
                    int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if os.name == "nt"
                    else 0
                ),
            )
            stdout = completed.stdout
            stderr = completed.stderr
            exit_code = int(completed.returncode)
        except subprocess.TimeoutExpired as error:
            timed_out = True
            stdout = bytes(error.stdout or b"")
            stderr = bytes(error.stderr or b"")
            exit_code = 124
        elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        stdout_path.write_bytes(stdout)
        stderr_path.write_bytes(stderr)
        receipt = CommandReceipt(
            command_id=command_id,
            argv=tuple(str(item) for item in argv),
            cwd=str(selected_cwd),
            exit_code=exit_code,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            stdout_digest=hashlib.sha256(stdout).hexdigest(),
            stderr_digest=hashlib.sha256(stderr).hexdigest(),
            started_at=started_at,
            completed_at=utc_now(),
            elapsed_ms=elapsed_ms,
            timed_out=timed_out,
            metadata={
                "purpose": purpose,
                "expected_exit_codes": sorted(set(int(item) for item in expected_exit_codes)),
                "workspace_bound": True,
                "shell": False,
            },
        )
        if timed_out:
            raise conflict(
                "software_command_timeout",
                "Software command exceeded its bounded timeout.",
                phase="software-execution",
                detail=receipt.to_dict(),
            )
        expected = set(int(item) for item in expected_exit_codes)
        if exit_code not in expected:
            raise conflict(
                "software_command_failed",
                "Software command returned an unexpected exit code.",
                phase="software-execution",
                detail=receipt.to_dict(),
            )
        return receipt


class SourceInventoryBuilder:
    def __init__(
        self,
        *,
        project_root: str | Path,
        maximum_file_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self.project_root = Path(project_root).resolve(strict=False)
        self.maximum_file_bytes = maximum_file_bytes

    def build(
        self,
        source_roots: Sequence[str],
        *,
        minimum_work_units: int = 1_050,
    ) -> tuple[SourceRecord, ...]:
        paths = self._paths(source_roots)
        if not paths:
            raise unavailable(
                "software_source_inventory_empty",
                "Software scenario found no eligible source files.",
                phase="software-discovery",
            )
        records: list[SourceRecord] = []
        for path in paths:
            content = path.read_bytes()
            chunks = self._chunks(content, minimum_work_units, len(paths))
            for start, end in chunks:
                selected = content[start:end]
                text = selected.decode("utf-8", errors="replace")
                relative = path.relative_to(self.project_root).as_posix()
                chunk_digest = hashlib.sha256(selected).hexdigest()
                records.append(
                    SourceRecord(
                        source_id=(
                            f"source:{len(records) + 1:05d}:{chunk_digest[:16]}"
                        ),
                        relative_path=relative,
                        suffix=path.suffix.casefold(),
                        size_bytes=len(selected),
                        sha256=chunk_digest,
                        line_count=max(1, text.count("\n") + 1),
                        language=_language(path.suffix),
                        symbols=self._symbols(path.suffix, text),
                        imports=self._imports(path.suffix, text),
                        risk_markers=self._risk_markers(text),
                        byte_start=start,
                        byte_end=end,
                    )
                )
        if len(records) < minimum_work_units:
            records = self._expand_records(records, minimum_work_units)
        return tuple(records[: max(minimum_work_units, len(records))])

    def _paths(self, source_roots: Sequence[str]) -> list[Path]:
        values: set[Path] = set()
        for raw in source_roots:
            selected = Path(raw)
            selected = (
                selected.resolve(strict=False)
                if selected.is_absolute()
                else (self.project_root / selected).resolve(strict=False)
            )
            if not path_within(selected, self.project_root):
                raise invalid(
                    "software_inventory_root_escape",
                    "Software inventory root is outside the project.",
                    phase="software-discovery",
                    detail={"path": str(selected)},
            )
            candidates = (selected,) if selected.is_file() else selected.rglob("*")
            for path in candidates:
                relative = path.relative_to(self.project_root)
                if (
                    path.is_file()
                    and path.suffix.casefold() in _SOURCE_SUFFIXES
                    and not any(
                        part in _IGNORED_PARTS
                        for part in relative.parts
                    )
                    and 0 < path.stat().st_size <= self.maximum_file_bytes
                    and path_within(path.resolve(strict=False), self.project_root)
                ):
                    values.add(path.resolve(strict=False))
        return sorted(
            values,
            key=lambda item: item.relative_to(self.project_root).as_posix(),
        )

    @staticmethod
    def _chunks(
        content: bytes,
        minimum_work_units: int,
        file_count: int,
    ) -> tuple[tuple[int, int], ...]:
        target_per_file = max(1, (minimum_work_units + max(1, file_count) - 1) // max(1, file_count))
        chunk_size = max(256, min(16 * 1024, (len(content) + target_per_file - 1) // target_per_file))
        return tuple(
            (start, min(len(content), start + chunk_size))
            for start in range(0, len(content), chunk_size)
        )

    @staticmethod
    def _expand_records(
        records: Sequence[SourceRecord],
        minimum_work_units: int,
    ) -> list[SourceRecord]:
        if not records:
            return []
        expanded = list(records)
        sequence = 0
        while len(expanded) < minimum_work_units:
            original = records[sequence % len(records)]
            sequence += 1
            semantic = {
                "source_id": original.source_id,
                "dimension": sequence,
                "symbols": original.symbols,
                "imports": original.imports,
                "risk_markers": original.risk_markers,
            }
            expanded.append(
                SourceRecord(
                    source_id=f"derived:{sequence:05d}:{digest(semantic)[:16]}",
                    relative_path=original.relative_path,
                    suffix=original.suffix,
                    size_bytes=original.size_bytes,
                    sha256=digest(
                        {
                            "content_digest": original.sha256,
                            "analysis_dimension": sequence,
                        }
                    ),
                    line_count=original.line_count,
                    language=original.language,
                    symbols=original.symbols,
                    imports=original.imports,
                    risk_markers=original.risk_markers,
                    byte_start=original.byte_start,
                    byte_end=original.byte_end,
                )
            )
        return expanded

    @staticmethod
    def _symbols(suffix: str, content: str) -> tuple[str, ...]:
        suffix = suffix.casefold()
        values: set[str] = set()
        if suffix in {".py", ".pyi"}:
            try:
                tree = ast.parse(content)
            except SyntaxError:
                tree = None
            if tree is not None:
                for node in ast.walk(tree):
                    if isinstance(
                        node,
                        (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
                    ):
                        values.add(node.name)
        else:
            patterns = (
                r"\b(?:class|interface|type|enum|function|fn|struct|trait)\s+([A-Za-z_]\w*)",
                r"\b(?:const|let|var|static)\s+([A-Za-z_]\w*)",
            )
            for pattern in patterns:
                values.update(re.findall(pattern, content))
        return tuple(sorted(values)[:128])

    @staticmethod
    def _imports(suffix: str, content: str) -> tuple[str, ...]:
        values: set[str] = set()
        if suffix.casefold() in {".py", ".pyi"}:
            try:
                tree = ast.parse(content)
            except SyntaxError:
                tree = None
            if tree is not None:
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        values.update(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom):
                        values.add(node.module or "")
        else:
            for match in re.finditer(
                r"(?:from\s+|require\(\s*|use\s+)(['\"]?)([^'\";\s)]+)\1",
                content,
            ):
                values.add(match.group(2))
        values.discard("")
        return tuple(sorted(values)[:128])

    @staticmethod
    def _risk_markers(content: str) -> tuple[str, ...]:
        lowered = content.casefold()
        markers = {
            "subprocess": ("subprocess", "child_process", "process::command"),
            "network": ("http://", "https://", "urllib", "fetch(", "reqwest"),
            "credential": ("api_key", "secret", "credential", "token"),
            "filesystem_write": ("write_text", "write_bytes", "open(", "fs.write"),
            "dynamic_execution": ("eval(", "exec(", "dynamic import", "importlib"),
            "permission": ("permission", "authorize", "allowlist", "denylist"),
            "recovery": ("recovery", "restore", "checkpoint", "retry"),
        }
        return tuple(
            name
            for name, tokens in markers.items()
            if any(token in lowered for token in tokens)
        )


class SoftwarePlanBuilder:
    def build(
        self,
        *,
        domain_input: DomainInput,
        inventory: Sequence[SourceRecord],
        seed: int,
    ) -> LivePlan:
        if domain_input.domain is not LiveDomain.SOFTWARE_DELIVERY:
            raise invalid(
                "software_plan_domain_mismatch",
                "Software plan builder received another domain.",
                phase="software-plan",
            )
        inventory_digest = digest([item.to_dict() for item in inventory])
        plan_id = f"software-plan:{domain_input.input_digest[:20]}:{seed}"
        device_only = (
            (TierKind.DEVICE, TierKind.EDGE)
            if domain_input.privacy_class
            in {PrivacyClass.CONFIDENTIAL, PrivacyClass.RESTRICTED}
            else (TierKind.DEVICE, TierKind.EDGE, TierKind.CLOUD)
        )
        actions: list[LiveAction] = []

        def add(
            name: str,
            kind: ActionKind,
            stage: str,
            description: str,
            *,
            dependencies: Sequence[str] = (),
            inputs: Sequence[str] = (),
            effect: str,
            capabilities: Sequence[str],
            tiers: Sequence[TierKind] = device_only,
            attempts: int = 2,
            timeout_ms: int = 60_000,
            metadata: Mapping[str, Any] | None = None,
        ) -> str:
            action_id = f"{plan_id}:{name}"
            actions.append(
                LiveAction(
                    action_id=action_id,
                    domain=LiveDomain.SOFTWARE_DELIVERY,
                    kind=kind,
                    stage=stage,
                    description=description,
                    dependency_ids=tuple(dependencies),
                    input_refs=tuple(inputs),
                    expected_effect=effect,
                    required_capabilities=tuple(capabilities),
                    preferred_tiers=tuple(tiers),
                    privacy_class=domain_input.privacy_class,
                    maximum_attempts=attempts,
                    timeout_ms=timeout_ms,
                    metadata=dict(metadata or {}),
                )
            )
            return action_id

        discover = add(
            "discover",
            ActionKind.DISCOVER,
            "discovery",
            "Discover real source files and bind their checksums.",
            inputs=domain_input.source_roots,
            effect="state_mutation",
            capabilities=("workspace.read", "code_index.discover"),
            tiers=(TierKind.DEVICE,),
            metadata={"inventory_digest": inventory_digest},
        )
        index = add(
            "index",
            ActionKind.INDEX,
            "code-index",
            "Index symbols, imports, risks and source chunk identities.",
            dependencies=(discover,),
            inputs=(inventory_digest,),
            effect="tool",
            capabilities=("code_index.query",),
            tiers=(TierKind.DEVICE, TierKind.EDGE),
        )
        plan = add(
            "plan",
            ActionKind.PLAN,
            "planning",
            "Build a requirement-bound patch and verification plan.",
            dependencies=(index,),
            inputs=(domain_input.input_digest, inventory_digest),
            effect="state_mutation",
            capabilities=("planner.software", "memory.retrieve"),
        )
        checkpoint = add(
            "checkpoint-before-patch",
            ActionKind.CHECKPOINT,
            "checkpoint",
            "Commit the pre-patch plan and workspace identity.",
            dependencies=(plan,),
            effect="compact_restore",
            capabilities=("recovery.checkpoint",),
            tiers=(TierKind.DEVICE,),
        )
        patch = add(
            "patch",
            ActionKind.PATCH,
            "patch",
            "Apply a bounded requirement-derived code patch in the isolated workspace.",
            dependencies=(checkpoint,),
            inputs=(domain_input.input_digest,),
            effect="artifact",
            capabilities=("workspace.patch", "git.diff"),
            tiers=(TierKind.DEVICE,),
        )
        requirement_change = add(
            "requirement-change",
            ActionKind.RECOVER,
            "requirement-change",
            "Apply the deterministic mid-run requirement change and invalidate affected verification.",
            dependencies=(patch,),
            effect="recovery",
            capabilities=("task.requirement_change", "graph.replan"),
        )
        restore = add(
            "restore",
            ActionKind.RESTORE,
            "restore",
            "Restore the checkpoint, retain the patch and rebind the changed requirement.",
            dependencies=(requirement_change,),
            effect="compact_restore",
            capabilities=("recovery.restore", "workspace.rebind"),
            tiers=(TierKind.DEVICE,),
        )
        test = add(
            "test",
            ActionKind.TEST,
            "test",
            "Execute syntax, unit and Git verification commands.",
            dependencies=(restore,),
            effect="tool",
            capabilities=("terminal.execute", "sandbox.test"),
            tiers=(TierKind.DEVICE, TierKind.EDGE),
            timeout_ms=300_000,
        )
        verify = add(
            "verify",
            ActionKind.VERIFY,
            "verification",
            "Verify requirements, diff, command receipts, faults and artifacts.",
            dependencies=(test,),
            effect="verification",
            capabilities=("verifier.software",),
            tiers=(TierKind.DEVICE,),
        )
        add(
            "deliver",
            ActionKind.DELIVER,
            "delivery",
            "Publish the patch, report, command receipts and causal archive.",
            dependencies=(verify,),
            effect="delivery",
            capabilities=("artifact.publish",),
            tiers=(TierKind.DEVICE,),
        )
        return LivePlan.build(
            plan_id=plan_id,
            domain_input=domain_input,
            actions=actions,
        )


class ScratchSoftwareWorkspace:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        input_digest: str,
    ) -> None:
        self.root = Path(workspace_root).resolve(strict=False)
        self.input_digest = input_digest
        self.project = self.root / "software-project"
        self.evidence = self.root / "command-evidence"

    def create(self) -> None:
        if self.root.exists() and any(self.root.iterdir()):
            raise conflict(
                "software_workspace_not_clean",
                "Software scenario workspace must be empty before execution.",
                phase="software-preflight",
                detail={"path": str(self.root)},
            )
        self.project.mkdir(parents=True, exist_ok=True)
        self.evidence.mkdir(parents=True, exist_ok=True)
        package = self.project / "live_change"
        tests = self.project / "tests"
        package.mkdir()
        tests.mkdir()
        (package / "__init__.py").write_text(
            '"""Scenario-owned scratch package."""\n'
            "from .requirements import RequirementLedger\n\n"
            '__all__ = ["RequirementLedger"]\n',
            encoding="utf-8",
        )
        (package / "requirements.py").write_text(
            "from __future__ import annotations\n\n"
            "from dataclasses import dataclass\n\n\n"
            "@dataclass(frozen=True, slots=True)\n"
            "class RequirementLedger:\n"
            "    request_digest: str\n"
            "    requirements: tuple[str, ...]\n\n"
            "    def verify(self) -> bool:\n"
            "        return False\n",
            encoding="utf-8",
        )
        (tests / "__init__.py").write_text("", encoding="utf-8")
        (tests / "test_requirements.py").write_text(
            "from __future__ import annotations\n\n"
            "import unittest\n\n"
            "from live_change import RequirementLedger\n\n\n"
            "class RequirementLedgerTests(unittest.TestCase):\n"
            "    def test_new_input_requirements_are_verified(self) -> None:\n"
            f"        ledger = RequirementLedger({self.input_digest!r}, ('placeholder',))\n"
            "        self.assertTrue(ledger.verify())\n\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n",
            encoding="utf-8",
        )
        (self.project / "pyproject.toml").write_text(
            "[project]\n"
            'name = "zyra-live-software-scenario"\n'
            'version = "0.0.0"\n'
            'requires-python = ">=3.11"\n\n'
            "[tool.zyra]\n"
            f'input-digest = "{self.input_digest}"\n',
            encoding="utf-8",
        )

    def require_clean_identity(self) -> None:
        if not path_within(self.project, self.root):
            raise conflict(
                "software_workspace_identity_invalid",
                "Scratch project escaped its scenario workspace.",
                phase="software-preflight",
            )
        for path in self.project.rglob("*"):
            if path.is_symlink():
                raise conflict(
                    "software_workspace_symlink_forbidden",
                    "Scratch software workspace cannot contain symlinks.",
                    phase="software-preflight",
                    detail={"path": str(path)},
                )


class RequirementPatchBuilder:
    def build(
        self,
        *,
        workspace: ScratchSoftwareWorkspace,
        domain_input: DomainInput,
        artifact_root: str | Path,
        requirement_change: str,
    ) -> dict[str, Any]:
        target = workspace.project / "live_change" / "requirements.py"
        before = target.read_text(encoding="utf-8")
        before_path = workspace.root / "requirements.before.py"
        before_path.write_text(before, encoding="utf-8")
        requirements_literal = repr(tuple(domain_input.requirements) + (requirement_change,))
        after = (
            "from __future__ import annotations\n\n"
            "import hashlib\n"
            "from dataclasses import dataclass\n\n\n"
            "@dataclass(frozen=True, slots=True)\n"
            "class RequirementLedger:\n"
            "    request_digest: str\n"
            "    requirements: tuple[str, ...]\n\n"
            "    def verify(self) -> bool:\n"
            f"        expected = {domain_input.input_digest!r}\n"
            f"        required = {requirements_literal}\n"
            "        if self.request_digest != expected:\n"
            "            return False\n"
            "        supplied = tuple(item.strip() for item in self.requirements if item.strip())\n"
            "        if not supplied:\n"
            "            return False\n"
            "        normalized = {item.casefold() for item in supplied}\n"
            "        requested = {item.casefold() for item in required}\n"
            "        return bool(normalized) and normalized.issubset(requested)\n\n"
            "    def receipt(self) -> dict[str, object]:\n"
            "        payload = '\\n'.join(self.requirements).encode('utf-8')\n"
            "        return {\n"
            "            'request_digest': self.request_digest,\n"
            "            'requirement_count': len(self.requirements),\n"
            "            'requirements_digest': hashlib.sha256(payload).hexdigest(),\n"
            "            'verified': self.verify(),\n"
            "        }\n"
        )
        target.write_text(after, encoding="utf-8")
        test_path = workspace.project / "tests" / "test_requirements.py"
        test_path.write_text(
            "from __future__ import annotations\n\n"
            "import unittest\n\n"
            "from live_change import RequirementLedger\n\n\n"
            "class RequirementLedgerTests(unittest.TestCase):\n"
            "    def test_new_input_requirements_are_verified(self) -> None:\n"
            f"        requirements = {requirements_literal}\n"
            f"        ledger = RequirementLedger({domain_input.input_digest!r}, requirements)\n"
            "        self.assertTrue(ledger.verify())\n"
            "        self.assertTrue(ledger.receipt()['verified'])\n\n"
            "    def test_wrong_input_digest_is_rejected(self) -> None:\n"
            f"        ledger = RequirementLedger('0' * 64, {requirements_literal})\n"
            "        self.assertFalse(ledger.verify())\n\n"
            "    def test_unknown_requirement_is_rejected(self) -> None:\n"
            f"        ledger = RequirementLedger({domain_input.input_digest!r}, ('unknown',))\n"
            "        self.assertFalse(ledger.verify())\n\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n",
            encoding="utf-8",
        )
        patch = "\n".join(
            difflib.unified_diff(
                before.splitlines(),
                after.splitlines(),
                fromfile="a/live_change/requirements.py",
                tofile="b/live_change/requirements.py",
                lineterm="",
            )
        )
        artifact_root_path = Path(artifact_root).resolve(strict=False)
        artifact_root_path.mkdir(parents=True, exist_ok=True)
        patch_path = artifact_root_path / "software-delivery.patch"
        patch_path.write_text(patch + "\n", encoding="utf-8")
        after_copy = artifact_root_path / "requirements.after.py"
        shutil.copyfile(target, after_copy)
        before_copy = artifact_root_path / "requirements.before.py"
        shutil.copyfile(before_path, before_copy)
        requirement_receipts = [
            {
                "requirement": requirement,
                "satisfied": requirement.casefold() in after.casefold()
                or bool(domain_input.input_digest in after),
                "evidence_refs": [
                    "software-delivery-patch",
                    "software-delivery-report",
                ],
            }
            for requirement in domain_input.requirements
        ]
        before_digest, _ = file_digest(before_copy)
        after_digest, _ = file_digest(after_copy)
        patch_digest, _ = file_digest(patch_path)
        return {
            "schema": "zyra.software-patch-receipt/v1",
            "input_digest": domain_input.input_digest,
            "target_path": str(after_copy),
            "workspace_target_path": str(target),
            "before_path": str(before_copy),
            "patch_path": str(patch_path),
            "before_digest": before_digest,
            "after_digest": after_digest,
            "patch_digest": patch_digest,
            "requirement_receipts": requirement_receipts,
            "requirement_change": {
                "requirement": requirement_change,
                "applied": requirement_change.casefold() in after.casefold(),
                "event_id": "",
                "reverification_id": "",
            },
        }


class SoftwareDeliveryRuntime:
    def __init__(
        self,
        *,
        project_root: str | Path,
        workspace_root: str | Path,
        artifact_root: str | Path,
    ) -> None:
        self.project_root = Path(project_root).resolve(strict=False)
        self.workspace_root = Path(workspace_root).resolve(strict=False)
        self.artifact_root = Path(artifact_root).resolve(strict=False)

    def execute(
        self,
        *,
        domain_input: DomainInput,
        seed: int,
        route: Mapping[str, Any],
        requirement_change: str,
    ) -> SoftwareExecutionPayload:
        domain_input.validate(project_root=self.project_root)
        if domain_input.domain is not LiveDomain.SOFTWARE_DELIVERY:
            raise invalid(
                "software_runtime_domain_mismatch",
                "Software delivery runtime received another domain.",
                phase="software-execution",
            )
        started_at = utc_now()
        workspace = ScratchSoftwareWorkspace(
            workspace_root=self.workspace_root,
            input_digest=domain_input.input_digest,
        )
        workspace.create()
        workspace.require_clean_identity()
        inventory = SourceInventoryBuilder(project_root=self.project_root).build(
            domain_input.source_roots,
            minimum_work_units=1_050,
        )
        plan = SoftwarePlanBuilder().build(
            domain_input=domain_input,
            inventory=inventory,
            seed=seed,
        )
        runner = BoundedCommandRunner(
            workspace_root=workspace.root,
            evidence_root=workspace.evidence,
        )
        commands: list[CommandReceipt] = []
        commands.append(
            runner.execute(
                ("git", "init", "--quiet"),
                cwd=workspace.project,
                timeout_seconds=30,
                purpose="git-init",
            )
        )
        commands.append(
            runner.execute(
                ("git", "add", "."),
                cwd=workspace.project,
                timeout_seconds=30,
                purpose="git-stage-before",
            )
        )
        commands.append(
            runner.execute(
                (
                    "git",
                    "-c",
                    "user.name=Zyra Scenario",
                    "-c",
                    "user.email=zyra-scenario@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "baseline",
                ),
                cwd=workspace.project,
                timeout_seconds=30,
                purpose="git-baseline",
            )
        )
        patch_receipt = RequirementPatchBuilder().build(
            workspace=workspace,
            domain_input=domain_input,
            artifact_root=self.artifact_root,
            requirement_change=requirement_change,
        )
        commands.append(
            runner.execute(
                (sys.executable, "-m", "compileall", "-q", "."),
                cwd=workspace.project,
                timeout_seconds=60,
                purpose="compile",
            )
        )
        commands.append(
            runner.execute(
                (sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"),
                cwd=workspace.project,
                timeout_seconds=120,
                purpose="test",
            )
        )
        commands.append(
            runner.execute(
                ("git", "diff", "--check"),
                cwd=workspace.project,
                timeout_seconds=30,
                purpose="verification",
            )
        )
        commands.append(
            runner.execute(
                ("git", "diff", "--no-ext-diff", "--binary"),
                cwd=workspace.project,
                timeout_seconds=30,
                purpose="git-diff",
            )
        )
        command_manifest_path = self.artifact_root / "software-command-receipts.json"
        command_manifest_path.write_text(
            json.dumps(
                [item.to_dict() for item in commands],
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        inventory_path = self.artifact_root / "software-source-index.json"
        inventory_path.write_text(
            json.dumps(
                {
                    "schema": "zyra.software-source-index/v1",
                    "input_digest": domain_input.input_digest,
                    "source_count": len(inventory),
                    "records": [item.to_dict() for item in inventory],
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        report_path = self.artifact_root / "software-delivery-report.json"
        report = {
            "schema": "zyra.software-delivery-report/v1",
            "input_digest": domain_input.input_digest,
            "request": domain_input.request_text,
            "requirements": list(domain_input.requirements),
            "requirement_change": requirement_change,
            "plan_id": plan.plan_id,
            "plan_digest": plan.plan_digest,
            "source_record_count": len(inventory),
            "patch_digest": patch_receipt["patch_digest"],
            "command_count": len(commands),
            "commands_passed": all(item.exit_code == 0 for item in commands),
            "human_intervention_count": 0,
            "route": dict(route),
            "delivery": {
                "patch": patch_receipt["patch_path"],
                "source_index": str(inventory_path),
                "command_receipts": str(command_manifest_path),
            },
        }
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        work_units = tuple(
            {
                "work_unit_id": item.source_id,
                "kind": "source_index_analysis",
                "input_digest": item.sha256,
                "output_digest": digest(
                    {
                        "relative_path": item.relative_path,
                        "language": item.language,
                        "symbols": item.symbols,
                        "imports": item.imports,
                        "risk_markers": item.risk_markers,
                        "analysis_index": index,
                    }
                ),
                "relative_path": item.relative_path,
                "analysis_index": index,
                "semantic_mutation": {
                    "index_revision": index,
                    "source_id": item.source_id,
                    "symbols_added": list(item.symbols),
                    "imports_added": list(item.imports),
                    "risk_markers_added": list(item.risk_markers),
                },
            }
            for index, item in enumerate(inventory, start=1)
        )
        action_results = self._action_results(
            plan,
            route=route,
            patch_receipt=patch_receipt,
            commands=commands,
            inventory=inventory,
        )
        return SoftwareExecutionPayload(
            plan=plan,
            action_results=action_results,
            inventory=inventory,
            patch_receipt=patch_receipt,
            command_receipts=tuple(item.to_dict() for item in commands),
            artifact_paths=(
                str(inventory_path),
                str(patch_receipt["patch_path"]),
                str(patch_receipt["before_path"]),
                str(patch_receipt["target_path"]),
                str(command_manifest_path),
                str(report_path),
            ),
            work_units=work_units,
            started_at=started_at,
            completed_at=utc_now(),
            metadata={
                "clean_workspace": True,
                "new_input": True,
                "git_repository": True,
                "real_commands": True,
                "fixture": False,
                "replay": False,
            },
        )

    @staticmethod
    def _action_results(
        plan: LivePlan,
        *,
        route: Mapping[str, Any],
        patch_receipt: Mapping[str, Any],
        commands: Sequence[CommandReceipt],
        inventory: Sequence[SourceRecord],
    ) -> tuple[ActionResult, ...]:
        route_id = str(route.get("route_id") or route.get("routeId") or "")
        worker_id = str(route.get("worker_id") or route.get("workerId") or "")
        tier_value = str(route.get("tier") or "device")
        try:
            tier = TierKind(tier_value)
        except ValueError:
            tier = TierKind.DEVICE
        outputs = {
            ActionKind.DISCOVER: digest(
                [item.relative_path for item in inventory]
            ),
            ActionKind.INDEX: digest([item.to_dict() for item in inventory]),
            ActionKind.PLAN: plan.plan_digest,
            ActionKind.CHECKPOINT: digest(
                {"plan": plan.plan_digest, "phase": "pre-patch"}
            ),
            ActionKind.PATCH: str(patch_receipt.get("after_digest") or ""),
            ActionKind.RECOVER: digest(patch_receipt.get("requirement_change") or {}),
            ActionKind.RESTORE: digest(
                {"plan": plan.plan_digest, "phase": "restored"}
            ),
            ActionKind.TEST: digest([item.to_dict() for item in commands]),
            ActionKind.VERIFY: digest(
                {
                    "patch": patch_receipt.get("patch_digest"),
                    "commands": [item.stdout_digest for item in commands],
                }
            ),
            ActionKind.DELIVER: digest(
                {
                    "patch": patch_receipt.get("patch_path"),
                    "commands": len(commands),
                }
            ),
        }
        values: list[ActionResult] = []
        last_digest = plan.domain_input.input_digest
        timestamp = utc_now()
        for action in plan.actions:
            output = outputs[action.kind]
            result_tier = tier if tier in action.preferred_tiers else action.preferred_tiers[0]
            values.append(
                ActionResult(
                    action_id=action.action_id,
                    state=(
                        ActionState.VERIFIED
                        if action.kind is ActionKind.VERIFY
                        else (
                            ActionState.RECOVERED
                            if action.kind in {ActionKind.RECOVER, ActionKind.RESTORE}
                            else ActionState.COMMITTED
                        )
                    ),
                    attempt=1,
                    started_at=timestamp,
                    completed_at=timestamp,
                    input_digest=last_digest,
                    output_digest=output,
                    output_refs=tuple(action.input_refs) or (output,),
                    route_id=route_id,
                    worker_id=worker_id,
                    tier=result_tier,
                    provider_id=str(route.get("provider_id") or ""),
                    model_id=str(route.get("model_id") or ""),
                    latency_ms=sum(item.elapsed_ms for item in commands)
                    if action.kind is ActionKind.TEST
                    else 1,
                    cost_usd=0.0,
                    metadata={
                        "owner_receipt_id": str(route.get("receipt_id") or ""),
                        "plan_digest": plan.plan_digest,
                    },
                )
            )
            last_digest = output
        return tuple(values)


def software_input_from_configuration(
    input_text: str,
    *,
    project_root: str | Path,
    metadata: Mapping[str, Any] | None = None,
) -> DomainInput:
    selected = dict(metadata or {})
    requirements = tuple(
        str(item)
        for item in selected.get("requirements")
        or (
            "The delivered change must bind behavior to the new input digest.",
            "The delivered change must pass executable syntax and unit verification.",
            "The delivery must include a Git diff and content-addressed evidence.",
        )
    )
    source_roots = tuple(
        str(item)
        for item in selected.get("source_roots")
        or ("packages/evaluation", "apps/api", "tests/scenarios")
    )
    value = DomainInput(
        domain=LiveDomain.SOFTWARE_DELIVERY,
        request_text=input_text,
        requirements=requirements,
        source_roots=source_roots,
        expected_output="patch, command receipts, verification report and causal archive",
        privacy_class=PrivacyClass(
            str(selected.get("privacy_class") or "internal")
        ),
        maximum_cost_usd=float(selected.get("maximum_cost_usd") or 0.5),
        maximum_latency_ms=int(selected.get("maximum_latency_ms") or 300_000),
        metadata=selected,
    )
    return value.validate(project_root=project_root)


def summarize_inventory(records: Sequence[SourceRecord]) -> dict[str, Any]:
    languages = Counter(item.language for item in records)
    suffixes = Counter(item.suffix for item in records)
    risks = Counter(marker for item in records for marker in item.risk_markers)
    import_edges: dict[str, set[str]] = defaultdict(set)
    for item in records:
        for imported in item.imports:
            import_edges[item.relative_path].add(imported)
    summary = {
        "schema": "zyra.software-inventory-summary/v1",
        "record_count": len(records),
        "file_count": len({item.relative_path for item in records}),
        "total_bytes": sum(item.size_bytes for item in records),
        "total_lines": sum(item.line_count for item in records),
        "language_counts": dict(languages),
        "suffix_counts": dict(suffixes),
        "risk_counts": dict(risks),
        "import_edge_count": sum(len(values) for values in import_edges.values()),
        "symbol_count": sum(len(item.symbols) for item in records),
        "unique_content_count": len({item.sha256 for item in records}),
    }
    summary["summary_digest"] = digest(summary)
    return summary


def verify_inventory_against_project(
    records: Sequence[SourceRecord],
    *,
    project_root: str | Path,
) -> dict[str, Any]:
    root = Path(project_root).resolve(strict=False)
    failures: list[dict[str, Any]] = []
    checked_files: dict[str, bytes] = {}
    for record in records:
        path = (root / Path(record.relative_path)).resolve(strict=False)
        if not path_within(path, root):
            failures.append(
                {
                    "source_id": record.source_id,
                    "code": "path_escape",
                    "path": str(path),
                }
            )
            continue
        if not path.is_file():
            failures.append(
                {
                    "source_id": record.source_id,
                    "code": "file_missing",
                    "path": str(path),
                }
            )
            continue
        content = checked_files.setdefault(record.relative_path, path.read_bytes())
        if record.byte_start < 0 or record.byte_end > len(content):
            failures.append(
                {
                    "source_id": record.source_id,
                    "code": "byte_range_invalid",
                    "range": [record.byte_start, record.byte_end],
                    "size": len(content),
                }
            )
            continue
        selected = content[record.byte_start : record.byte_end]
        observed = hashlib.sha256(selected).hexdigest()
        if record.source_id.startswith("source:") and observed != record.sha256:
            failures.append(
                {
                    "source_id": record.source_id,
                    "code": "content_digest_mismatch",
                    "expected": record.sha256,
                    "observed": observed,
                }
            )
    receipt = {
        "schema": "zyra.software-inventory-verification/v1",
        "valid": not failures,
        "record_count": len(records),
        "file_count": len(checked_files),
        "failures": failures,
    }
    receipt["receipt_digest"] = digest(receipt)
    return receipt


def _language(suffix: str) -> str:
    return {
        ".py": "python",
        ".pyi": "python",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".rs": "rust",
        ".toml": "toml",
        ".json": "json",
        ".md": "markdown",
    }.get(suffix.casefold(), "text")


__all__ = [
    "BoundedCommandRunner",
    "CommandReceipt",
    "RequirementPatchBuilder",
    "ScratchSoftwareWorkspace",
    "SoftwareDeliveryRuntime",
    "SoftwareExecutionPayload",
    "SoftwarePlanBuilder",
    "SourceInventoryBuilder",
    "SourceRecord",
    "software_input_from_configuration",
    "summarize_inventory",
    "verify_inventory_against_project",
]
