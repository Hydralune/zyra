from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class FileKind(StrEnum):
    SOURCE = "source"
    MANIFEST = "manifest"
    LOCK = "lock"
    RUNTIME_ASSET = "runtime_asset"
    NATIVE = "native"
    BUILD = "build"
    DOCUMENTATION = "documentation"


class InstallState(StrEnum):
    NEW = "new"
    VERIFYING = "verifying"
    STAGING = "staging"
    MIGRATING = "migrating"
    DOCTORING = "doctoring"
    ACTIVATING = "activating"
    COMMITTED = "committed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"
    UNINSTALLED = "uninstalled"


class GateState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class FileRecord:
    path: str
    size: int
    sha256: str
    mode: int
    kind: FileKind
    executable: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["kind"] = self.kind.value
        return value


@dataclass(frozen=True, slots=True)
class BoundaryFinding:
    code: str
    path: str
    message: str
    blocker: bool = True
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RequirementRecord:
    name: str
    version: str
    hashes: tuple[str, ...]
    marker: str = ""
    extras: tuple[str, ...] = ()
    source_line: int = 0

    @property
    def canonical_name(self) -> str:
        return self.name.lower().replace("_", "-").replace(".", "-")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["hashes"] = list(self.hashes)
        value["extras"] = list(self.extras)
        return value


@dataclass(frozen=True, slots=True)
class ChecksumManifest:
    algorithm: str
    root_digest: str
    files: tuple[FileRecord, ...]
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.checksum-manifest/v1",
            "algorithm": self.algorithm,
            "root_digest": self.root_digest,
            "generated_at": self.generated_at,
            "files": [item.to_dict() for item in self.files],
        }


@dataclass(frozen=True, slots=True)
class OperationRecord:
    sequence: int
    kind: str
    target: str
    before_digest: str
    after_digest: str
    reversible: bool
    status: str
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class InstallReceipt:
    transaction_id: str
    idempotency_key: str
    release_id: str
    state: InstallState
    revision: int
    install_root: str
    active_path: str
    manifest_digest: str
    migration_version: int
    owned_paths: tuple[str, ...]
    operations: tuple[OperationRecord, ...]
    created_at: str
    updated_at: str
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.install-receipt/v1",
            "transaction_id": self.transaction_id,
            "idempotency_key": self.idempotency_key,
            "release_id": self.release_id,
            "state": self.state.value,
            "revision": self.revision,
            "install_root": self.install_root,
            "active_path": self.active_path,
            "manifest_digest": self.manifest_digest,
            "migration_version": self.migration_version,
            "owned_paths": list(self.owned_paths),
            "operations": [item.to_dict() for item in self.operations],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class GateReceipt:
    gate_id: str
    state: GateState
    required: bool
    started_at: str
    finished_at: str
    duration_ms: float
    command: tuple[str, ...]
    exit_code: int | None
    stdout_digest: str
    stderr_digest: str
    artifacts: tuple[str, ...]
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["state"] = self.state.value
        value["command"] = list(self.command)
        value["artifacts"] = list(self.artifacts)
        return value


@dataclass(frozen=True, slots=True)
class PlatformPlan:
    platform: str
    architecture: str
    python: str
    install_commands: tuple[tuple[str, ...], ...]
    lifecycle_commands: dict[str, tuple[str, ...]]
    environment: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "architecture": self.architecture,
            "python": self.python,
            "install_commands": [list(item) for item in self.install_commands],
            "lifecycle_commands": {
                key: list(value) for key, value in self.lifecycle_commands.items()
            },
            "environment": dict(self.environment),
        }


@dataclass(frozen=True, slots=True)
class ReleaseLayout:
    project_root: Path
    work_root: Path
    output_root: Path
    state_root: Path

    def ensure(self) -> None:
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.state_root.mkdir(parents=True, exist_ok=True)


__all__ = [
    "BoundaryFinding",
    "ChecksumManifest",
    "FileKind",
    "FileRecord",
    "GateReceipt",
    "GateState",
    "InstallReceipt",
    "InstallState",
    "OperationRecord",
    "PlatformPlan",
    "ReleaseLayout",
    "RequirementRecord",
]
