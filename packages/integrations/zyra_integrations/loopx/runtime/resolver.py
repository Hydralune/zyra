from __future__ import annotations

import importlib.util
from importlib import metadata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import LoopXRuntimeError
from .manifest import (
    LOOPX_SOURCE_COMMIT,
    LOOPX_SOURCE_TREE_COMMIT,
    LOOPX_VERSION,
    LoopXPackageLock,
)


@dataclass(frozen=True, slots=True)
class LoopXRuntimeResolution:
    package_root: Path
    runtime_root: Path
    module_root: Path
    package_init: Path
    manifest_path: Path
    source_digest: str
    mode: str
    custody: str
    distribution_name: str
    distribution_version: str

    def receipt(self, workspace_root: Path) -> dict[str, Any]:
        workspace = workspace_root.resolve()
        state_root = (workspace / ".zyra" / "loopx" / "state").resolve()
        private_runtime_root = (workspace / ".zyra" / "loopx" / "runtime").resolve()
        retired_install_root = (workspace / ".zyra" / "loopx" / "install").resolve()
        return {
            "schema": "zyra.loopx-embedded-runtime-resolution/v1",
            "ready": True,
            "version": LOOPX_VERSION,
            "source_commit": LOOPX_SOURCE_COMMIT,
            "source_tree_commit": LOOPX_SOURCE_TREE_COMMIT,
            "source_digest": self.source_digest,
            "source_kind": "embedded_source",
            "migration_mode": "pinned_embedded_source_integration",
            "profile": "pinned_embedded_source",
            "install_root": str(self.runtime_root),
            "module_root": str(self.module_root.relative_to(self.runtime_root)),
            "package_init": str(self.package_init),
            "manifest_path": str(self.manifest_path),
            "state_root": str(state_root),
            "runtime_root": str(private_runtime_root),
            "retired_install_root": str(retired_install_root),
            "retired_install_root_exists": retired_install_root.exists(),
            "archive_extraction": False,
            "archive_fallback": False,
            "network_access": False,
            "user_home_write": False,
            "system_path_write": False,
            "mode": self.mode,
            "custody": self.custody,
            "distribution_name": self.distribution_name,
            "distribution_version": self.distribution_version,
        }


class LoopXRuntimeResolver:
    """Resolve only the LoopX source embedded in this Zyra checkout/release."""

    def __init__(self, package_root: Path) -> None:
        self.package_root = package_root.resolve()
        self.package_lock = LoopXPackageLock.load(self.package_root)

    def resolve(self, *, verify: bool = True) -> LoopXRuntimeResolution:
        embedded = self.package_lock.embedded_root
        mode = "embedded_source"
        custody = "zyra_source_tree"
        distribution_name = ""
        distribution_version = ""
        if embedded is not None:
            module_root = embedded
            package_init = embedded / "loopx" / "__init__.py"
        else:
            # A built Zyra wheel contains loopx as part of the same distribution.
            spec = importlib.util.find_spec("loopx")
            origin = Path(str(spec.origin or "")).resolve() if spec else Path()
            if not origin.is_file():
                raise LoopXRuntimeError(
                    "The Zyra-bundled LoopX package cannot be resolved.",
                    code="loopx_embedded_source_missing",
                    details={"package_root": str(self.package_root)},
                )
            package_init = origin
            module_root = origin.parent.parent
            embedded = module_root
            mode = "installed_distribution"
            distribution_name, distribution_version = (
                self._verify_installed_distribution_custody(package_init)
            )
            custody = "zyra_distribution_record"
        if not package_init.is_file():
            raise LoopXRuntimeError(
                "The embedded LoopX package initializer is missing.",
                code="loopx_embedded_source_missing",
                details={"path": str(package_init)},
            )
        if verify:
            self.package_lock.verify_embedded_source(deep=True)
        return LoopXRuntimeResolution(
            package_root=self.package_root,
            runtime_root=embedded.resolve(),
            module_root=module_root.resolve(),
            package_init=package_init.resolve(),
            manifest_path=self.package_lock.manifest_path.resolve(),
            source_digest=self.package_lock.source_digest,
            mode=mode,
            custody=custody,
            distribution_name=distribution_name,
            distribution_version=distribution_version,
        )

    def receipt(self, workspace_root: Path, *, verify: bool = True) -> dict[str, Any]:
        return self.resolve(verify=verify).receipt(workspace_root)

    def _verify_installed_distribution_custody(
        self,
        package_init: Path,
    ) -> tuple[str, str]:
        required = {
            package_init.resolve(),
            self.package_lock.path.resolve(),
            self.package_lock.manifest_path.resolve(),
        }
        inspected: list[dict[str, Any]] = []
        for distribution in metadata.distributions():
            name = str(distribution.metadata.get("Name") or "")
            if name.casefold().replace("-", "_") != "zyra":
                continue
            files = distribution.files
            if files is None:
                inspected.append(
                    {
                        "name": name,
                        "version": distribution.version,
                        "record_available": False,
                    }
                )
                continue
            owned = {
                Path(distribution.locate_file(record)).resolve()
                for record in files
            }
            missing = sorted(str(path) for path in required - owned)
            inspected.append(
                {
                    "name": name,
                    "version": distribution.version,
                    "record_available": True,
                    "missing": missing,
                }
            )
            if not missing:
                return name, distribution.version
        raise LoopXRuntimeError(
            "The resolved LoopX package is not owned by the installed Zyra distribution.",
            code="loopx_runtime_origin_mismatch",
            details={
                "package_init": str(package_init),
                "package_lock": str(self.package_lock.path),
                "source_manifest": str(self.package_lock.manifest_path),
                "inspected_distributions": inspected,
            },
        )


__all__ = ["LoopXRuntimeResolution", "LoopXRuntimeResolver"]
