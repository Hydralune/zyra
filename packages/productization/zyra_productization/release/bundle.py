from __future__ import annotations

import gzip
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import tomllib
import uuid
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from zyra_integrations.loopx.install import LoopXDoctor, LoopXPackageLock

from .errors import IntegrityViolation, InventoryViolation, ReleaseError
from .integrity import (
    ArchiveInspector,
    BoundaryScanner,
    BunLock,
    CanonicalTreeWalker,
    ChecksumBuilder,
    ChecksumVerifier,
    PythonLock,
    normalize_relative_path,
    sha256_file,
    stable_digest,
    stable_json,
)
from .inventory import (
    Component,
    ConfigurationProvisioner,
    JavaScriptComponentInventory,
    NoticeBuilder,
    PythonComponentInventory,
    RuntimeInventory,
    SbomBuilder,
    default_release_configuration,
)
from .models import ChecksumManifest, FileRecord
from .policy import DEFAULT_RELEASE_POLICY, ReleasePolicy
from .wheel import DeterministicWheelBuilder


RELEASE_MANIFEST_SCHEMA = "zyra.release-manifest/v1"


def source_revision(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise ReleaseError(
            "Release source revision cannot be resolved.",
            code="release_revision_unavailable",
            details={"stderr": result.stderr.strip()},
        )
    revision = result.stdout.strip()
    if len(revision) != 40:
        raise ReleaseError(
            "Release source revision is not a full commit.",
            code="release_revision_invalid",
            details={"revision": revision},
        )
    return revision


def assert_clean_git(root: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise ReleaseError(
            "Release Git status cannot be resolved.",
            code="release_git_status_failed",
            details={"stderr": result.stderr.strip()},
        )
    dirty = [line for line in result.stdout.splitlines() if line.strip()]
    if dirty:
        raise ReleaseError(
            "Release source worktree is not clean.",
            code="release_worktree_dirty",
            details={"entries": dirty[:100], "entry_count": len(dirty)},
        )
    return {
        "schema": "zyra.release-git-receipt/v1",
        "ready": True,
        "revision": source_revision(root),
        "dirty_entry_count": 0,
    }


class ReleaseManifest:
    def __init__(self, value: Mapping[str, Any]) -> None:
        self.value = dict(value)
        self.validate()

    @property
    def release_id(self) -> str:
        return str(self.value["release_id"])

    @property
    def source_commit(self) -> str:
        return str(self.value["source_commit"])

    @property
    def payload_digest(self) -> str:
        return str(self.value["payload"]["root_digest"])

    @property
    def digest(self) -> str:
        return stable_digest(self.value)

    def validate(self) -> None:
        if self.value.get("schema") != RELEASE_MANIFEST_SCHEMA:
            raise IntegrityViolation(
                "Release manifest schema is unsupported.",
                code="release_manifest_schema",
                details={"schema": self.value.get("schema")},
            )
        release_id = str(self.value.get("release_id") or "")
        if not release_id or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
            for character in release_id
        ):
            raise IntegrityViolation(
                "Release id is invalid.",
                code="release_manifest_id",
                details={"release_id": release_id},
            )
        commit = str(self.value.get("source_commit") or "")
        if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
            raise IntegrityViolation(
                "Release source commit is invalid.",
                code="release_manifest_commit",
                details={"source_commit": commit},
            )
        payload = self.value.get("payload")
        if not isinstance(payload, Mapping):
            raise IntegrityViolation(
                "Release payload description is invalid.",
                code="release_manifest_payload",
            )
        root_digest = str(payload.get("root_digest") or "")
        if len(root_digest) != 64:
            raise IntegrityViolation(
                "Release payload digest is invalid.",
                code="release_manifest_payload_digest",
            )
        locks = self.value.get("locks")
        if not isinstance(locks, Mapping):
            raise IntegrityViolation(
                "Release lock description is invalid.",
                code="release_manifest_locks",
            )
        for required in ("python", "javascript"):
            item = locks.get(required)
            if not isinstance(item, Mapping) or not item.get("digest"):
                raise IntegrityViolation(
                    "Release manifest does not bind a required lock.",
                    code="release_manifest_lock_missing",
                    details={"lock": required},
                )
        artifacts = self.value.get("artifacts")
        if not isinstance(artifacts, Mapping):
            raise IntegrityViolation(
                "Release artifacts description is invalid.",
                code="release_manifest_artifacts",
            )
        for name in (
            "checksums",
            "sbom",
            "notice",
            "runtime_inventory",
            "configuration",
            "benchmark",
            "python_wheel",
        ):
            item = artifacts.get(name)
            if not isinstance(item, Mapping):
                raise IntegrityViolation(
                    "Release manifest is missing a mandatory artifact.",
                    code="release_manifest_artifact_missing",
                    details={"artifact": name},
                )
            digest = str(item.get("sha256") or "")
            if len(digest) != 64:
                raise IntegrityViolation(
                    "Release artifact digest is invalid.",
                    code="release_manifest_artifact_digest",
                    details={"artifact": name},
                )
        loopx_lock = locks.get("loopx")
        if loopx_lock is not None:
            if not isinstance(loopx_lock, Mapping):
                raise IntegrityViolation(
                    "Release LoopX lock description is invalid.",
                    code="release_manifest_loopx_lock",
                )
            for field in (
                "digest",
                "version",
                "source_commit",
                "source_digest",
            ):
                if not loopx_lock.get(field):
                    raise IntegrityViolation(
                        "Release manifest LoopX lock is incomplete.",
                        code="release_manifest_loopx_lock",
                        details={"field": field},
                    )
            for name in (
                "loopx_package_lock",
                "loopx_wheel",
                "loopx_source_bundle",
                "loopx_doctor_metadata",
            ):
                item = artifacts.get(name)
                if not isinstance(item, Mapping):
                    raise IntegrityViolation(
                        "Release manifest is missing a LoopX artifact.",
                        code="release_manifest_artifact_missing",
                        details={"artifact": name},
                    )
                digest = str(item.get("sha256") or "")
                if len(digest) != 64:
                    raise IntegrityViolation(
                        "Release LoopX artifact digest is invalid.",
                        code="release_manifest_artifact_digest",
                        details={"artifact": name},
                    )
        platforms = self.value.get("platforms")
        if not isinstance(platforms, Sequence) or isinstance(platforms, (str, bytes)):
            raise IntegrityViolation(
                "Release platform list is invalid.",
                code="release_manifest_platforms",
            )
        names = {str(item.get("platform")) for item in platforms if isinstance(item, Mapping)}
        if not {"windows", "linux"}.issubset(names):
            raise IntegrityViolation(
                "Release must declare Windows and Linux plans.",
                code="release_manifest_platform_incomplete",
                details={"platforms": sorted(names)},
            )

    def to_dict(self) -> dict[str, Any]:
        return dict(self.value)

    @classmethod
    def load(cls, path: Path) -> "ReleaseManifest":
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise IntegrityViolation(
                "Release manifest cannot be parsed.",
                code="release_manifest_unreadable",
                details={"path": str(path), "error": str(error)},
            ) from error
        if not isinstance(value, Mapping):
            raise IntegrityViolation(
                "Release manifest root must be an object.",
                code="release_manifest_root",
            )
        return cls(value)


class BenchmarkEvidenceLinker:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()

    def link(
        self,
        *,
        expected_commit: str | None,
        report_path: Path | None = None,
        deployment_path: Path | None = None,
    ) -> dict[str, Any]:
        report_path = report_path or self._current_report_path()
        if not report_path.is_file():
            candidates = sorted(
                (
                    self.project_root
                    / "docs/reviews/evidence/M3-S02A-02"
                ).glob("formal-live-*/benchmark-report.json")
            )
            if not candidates:
                raise InventoryViolation(
                    "Formal benchmark report is missing.",
                    code="release_benchmark_missing",
                    details={"path": str(report_path)},
                )
            report_path = candidates[-1]
        report = self._load_mapping(report_path)
        deployment_path = deployment_path or report_path.with_name(
            "protected-deployment-evidence.json"
        )
        if not deployment_path.is_file():
            raise InventoryViolation(
                "Protected deployment evidence is missing.",
                code="release_deployment_evidence_missing",
                details={"path": str(deployment_path)},
            )
        deployment = self._load_mapping(deployment_path)
        verification_path = report_path.with_name("verification-summary.json")
        statistical_path = report_path.with_name("statistical-evaluation.json")
        current_evidence_path = report_path.with_name(
            "current-campaign-evidence.json"
        )
        verification = (
            self._load_mapping(verification_path)
            if verification_path.is_file()
            else {}
        )
        statistical = (
            self._load_mapping(statistical_path)
            if statistical_path.is_file()
            else {}
        )
        current_evidence = (
            self._load_mapping(current_evidence_path)
            if current_evidence_path.is_file()
            else {}
        )
        revision = self._extract_revision(report)
        deployment_revision = self._extract_revision(deployment)
        if expected_commit is not None and revision != expected_commit:
            raise InventoryViolation(
                "Formal benchmark evidence is not bound to the release revision.",
                code="release_benchmark_revision_mismatch",
                details={
                "expected": expected_commit,
                    "actual": revision,
                    "report": str(report_path),
                },
            )
        summary = self._extract_summary(
            {"report": report, "verification": verification}
        )
        latency_comparisons = self._latency_comparisons(statistical)
        if latency_comparisons:
            medians = sorted(
                abs(float(item["p50_delta"]))
                for item in latency_comparisons
            )
            p95_values = [
                abs(float(item["p95_absolute_delta"]))
                for item in latency_comparisons
            ]
            summary["p50_ms"] = medians[len(medians) // 2]
            summary["p95_ms"] = max(p95_values)
        if int(summary["human_intervention_count"]) != 0:
            raise InventoryViolation(
                "Formal benchmark requires human intervention.",
                code="release_benchmark_human_intervention",
                details={"count": summary["human_intervention_count"]},
            )
        if int(summary["minimum_effective_transitions"]) < 2000:
            raise InventoryViolation(
                "Formal benchmark does not meet the effective-transition gate.",
                code="release_benchmark_transition_floor",
                details={"minimum": summary["minimum_effective_transitions"]},
            )
        profiles = self._extract_profiles(deployment)
        missing_profiles = sorted({"device", "edge", "cloud"} - set(profiles))
        if missing_profiles:
            raise InventoryViolation(
                "Protected evidence does not cover all deployment profiles.",
                code="release_benchmark_profiles_incomplete",
                details={"missing": missing_profiles},
            )
        link = {
            "schema": "zyra.release-benchmark-link/v1",
            "ready": True,
            "source_commit": revision,
            "deployment_source_commit": deployment_revision,
            "report": self._relative(report_path),
            "report_sha256": sha256_file(report_path),
            "deployment_evidence": self._relative(deployment_path),
            "deployment_sha256": sha256_file(deployment_path),
            "verification_summary": (
                self._relative(verification_path)
                if verification_path.is_file()
                else ""
            ),
            "verification_sha256": (
                sha256_file(verification_path)
                if verification_path.is_file()
                else ""
            ),
            "statistical_evaluation": (
                self._relative(statistical_path)
                if statistical_path.is_file()
                else ""
            ),
            "statistical_sha256": (
                sha256_file(statistical_path)
                if statistical_path.is_file()
                else ""
            ),
            "current_campaign_evidence": (
                self._relative(current_evidence_path)
                if current_evidence_path.is_file()
                else ""
            ),
            "current_campaign_evidence_sha256": (
                sha256_file(current_evidence_path)
                if current_evidence_path.is_file()
                else ""
            ),
            "summary": summary,
            "latency_comparisons": latency_comparisons,
            "profiles": profiles,
            "providers": self._extract_providers(
                report,
                deployment,
                current_evidence,
            ),
        }
        link["digest"] = stable_digest(link)
        return link

    def _current_report_path(self) -> Path:
        evidence_root = (
            self.project_root
            / "docs/reviews/evidence/M3-S02A-02"
        )
        pointer_path = evidence_root / "formal-current.json"
        legacy_path = evidence_root / "formal-current" / "benchmark-report.json"
        if not pointer_path.is_file():
            return legacy_path
        pointer = self._load_mapping(pointer_path)
        relative_root = str(pointer.get("relative_evidence_root") or "")
        if not relative_root:
            raise InventoryViolation(
                "Formal benchmark pointer has no evidence root.",
                code="release_benchmark_pointer_root_missing",
                details={"pointer": str(pointer_path)},
            )
        normalized = normalize_relative_path(relative_root)
        resolved_root = (self.project_root / normalized).resolve()
        if not resolved_root.is_relative_to(self.project_root):
            raise InventoryViolation(
                "Formal benchmark pointer escapes the project root.",
                code="release_benchmark_pointer_escape",
                details={"pointer": str(pointer_path), "root": relative_root},
            )
        report_path = resolved_root / "benchmark-report.json"
        if not report_path.is_file():
            raise InventoryViolation(
                "Formal benchmark pointer targets a missing report.",
                code="release_benchmark_pointer_target_missing",
                details={
                    "pointer": str(pointer_path),
                    "report": str(report_path),
                },
            )
        report = self._load_mapping(report_path)
        pointer_commit = str(pointer.get("implementation_commit") or "")
        report_commit = self._extract_revision(report)
        if pointer_commit != report_commit:
            raise InventoryViolation(
                "Formal benchmark pointer and report target different commits.",
                code="release_benchmark_pointer_revision_mismatch",
                details={
                    "pointer_commit": pointer_commit,
                    "report_commit": report_commit,
                },
            )
        return report_path

    @staticmethod
    def _load_mapping(path: Path) -> Mapping[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise InventoryViolation(
                "Benchmark evidence cannot be parsed.",
                code="release_benchmark_invalid",
                details={"path": str(path), "error": str(error)},
            ) from error
        if not isinstance(value, Mapping):
            raise InventoryViolation(
                "Benchmark evidence root must be an object.",
                code="release_benchmark_root_invalid",
                details={"path": str(path)},
            )
        return value

    @staticmethod
    def _extract_revision(
        report: Mapping[str, Any],
        deployment: Mapping[str, Any] | None = None,
    ) -> str:
        candidates: list[str] = []
        for value in (report, *((deployment,) if deployment is not None else ())):
            for key in (
                "source_commit",
                "commit",
                "commit_sha",
                "target_commit",
                "zyra_commit",
                "implementation_commit",
            ):
                raw = value.get(key)
                if raw:
                    candidates.append(str(raw))
            metadata = value.get("metadata")
            if isinstance(metadata, Mapping):
                for key in ("source_commit", "commit", "target_commit"):
                    raw = metadata.get(key)
                    if raw:
                        candidates.append(str(raw))
        full = [item for item in candidates if len(item) == 40]
        if not full:
            raise InventoryViolation(
                "Benchmark evidence has no full source commit.",
                code="release_benchmark_commit_missing",
                details={"candidates": candidates},
            )
        if len(set(full)) != 1:
            raise InventoryViolation(
                "Benchmark evidence contains conflicting revisions.",
                code="release_benchmark_commit_conflict",
                details={"commits": sorted(set(full))},
            )
        return full[0]

    @staticmethod
    def _extract_summary(report: Mapping[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(report, ensure_ascii=False)
        def find_number(keys: Sequence[str], default: float = 0) -> float:
            queue: list[Any] = [report]
            while queue:
                current = queue.pop()
                if isinstance(current, Mapping):
                    for key, value in current.items():
                        if str(key) in keys and isinstance(value, (int, float)):
                            return float(value)
                        queue.append(value)
                elif isinstance(current, Sequence) and not isinstance(
                    current, (str, bytes)
                ):
                    queue.extend(current)
            return default

        minimum = find_number(
            (
                "minimum_effective_transitions",
                "min_effective_transitions",
                "minimum_canonical_state_transitions",
                "long_run_min_effective_steps",
            ),
            0,
        )
        if minimum == 0:
            matches = [
                int(item)
                for item in re.findall(
                    r'"(?:effective_transition_count|effective_transitions)"\s*:\s*(\d+)',
                    encoded,
                )
            ]
            minimum = min(matches) if matches else 0
        return {
            "minimum_effective_transitions": int(minimum),
            "maximum_effective_transitions": int(
                find_number(
                    (
                        "maximum_effective_transitions",
                        "max_effective_transitions",
                        "long_run_max_effective_steps",
                    ),
                    minimum,
                )
            ),
            "human_intervention_count": int(
                find_number(("human_intervention_count", "human_interventions"), 0)
            ),
            "p50_ms": find_number(("p50_ms", "latency_p50_ms", "p50"), 0),
            "p95_ms": find_number(("p95_ms", "latency_p95_ms", "p95"), 0),
            "score": find_number(
                (
                    "competition_score",
                    "total_score",
                    "verified_score",
                    "verified",
                    "score",
                ),
                0,
            ),
        }

    @staticmethod
    def _extract_profiles(value: Mapping[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(value, ensure_ascii=False).casefold()
        presence = {
            "device": "device" in encoded or '"tier": "local"' in encoded,
            "edge": "edge" in encoded,
            "cloud": "cloud" in encoded,
        }
        return {
            profile: {
                "present": present,
                "evidence_digest": stable_digest(
                    {"profile": profile, "document": stable_digest(value)}
                ),
            }
            for profile, present in presence.items()
            if present
        }

    @staticmethod
    def _extract_providers(
        report: Mapping[str, Any],
        deployment: Mapping[str, Any],
        current_evidence: Mapping[str, Any] | None = None,
    ) -> list[str]:
        current_ids = (
            current_evidence.get("current_provider_ids")
            if isinstance(current_evidence, Mapping)
            else None
        )
        if isinstance(current_ids, Sequence) and not isinstance(
            current_ids, (str, bytes)
        ):
            normalized = sorted(
                {
                    str(provider).strip()
                    for provider in current_ids
                    if str(provider).strip()
                }
            )
            if normalized:
                return normalized
        encoded = json.dumps(
            {"report": report, "deployment": deployment},
            ensure_ascii=False,
        ).casefold()
        candidates = (
            "openai",
            "deepseek",
            "anthropic",
            "google",
            "azure",
            "ollama",
            "local",
        )
        return [provider for provider in candidates if provider in encoded]

    @staticmethod
    def _latency_comparisons(
        value: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        raw = value.get("comparisons")
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return []
        output: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            metric_id = str(item.get("metric_id") or "")
            unit = str(item.get("unit") or "")
            if (
                not any(
                    fragment in metric_id.casefold()
                    for fragment in ("latency", "duration", "wall_clock")
                )
                and unit.casefold() not in {"ms", "milliseconds"}
            ):
                continue
            output.append(
                {
                    "domain": str(item.get("domain") or ""),
                    "baseline_variant": str(
                        item.get("baseline_variant") or ""
                    ),
                    "candidate_variant": str(
                        item.get("candidate_variant") or ""
                    ),
                    "metric_id": metric_id,
                    "unit": unit,
                    "p50_delta": float(item.get("median_delta") or 0),
                    "p95_absolute_delta": float(
                        item.get("p95_absolute_delta") or 0
                    ),
                    "pair_count": int(item.get("pair_count") or 0),
                    "verdict": str(item.get("verdict") or ""),
                }
            )
        return output

    def _relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.project_root).as_posix()
        except ValueError:
            return str(path.resolve())


class DeterministicArchiveWriter:
    def __init__(
        self,
        source_root: Path,
        *,
        policy: ReleasePolicy = DEFAULT_RELEASE_POLICY,
    ) -> None:
        self.source_root = source_root.resolve()
        self.policy = policy

    def write_tar_gz(
        self,
        output: Path,
        *,
        archive_root: str,
        include: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        root_name = normalize_relative_path(archive_root)
        if "/" in root_name or root_name == ".":
            raise IntegrityViolation(
                "Archive root must be one portable path component.",
                code="archive_root_invalid",
                details={"archive_root": archive_root},
            )
        entries = self._entries(include)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.tmp-{os.getpid()}")
        try:
            with temporary.open("wb") as raw:
                with gzip.GzipFile(
                    filename="",
                    mode="wb",
                    fileobj=raw,
                    mtime=self.policy.source_date_epoch,
                ) as compressed:
                    with tarfile.open(
                        fileobj=compressed,
                        mode="w",
                        format=tarfile.PAX_FORMAT,
                    ) as archive:
                        root_info = tarfile.TarInfo(root_name)
                        root_info.type = tarfile.DIRTYPE
                        root_info.mode = 0o755
                        self._normalize_tar_info(root_info)
                        archive.addfile(root_info)
                        directories = self._directories(entries)
                        for directory in directories:
                            info = tarfile.TarInfo(f"{root_name}/{directory}")
                            info.type = tarfile.DIRTYPE
                            info.mode = 0o755
                            self._normalize_tar_info(info)
                            archive.addfile(info)
                        for relative, path, mode in entries:
                            data = path.read_bytes()
                            info = tarfile.TarInfo(f"{root_name}/{relative}")
                            info.size = len(data)
                            info.mode = mode
                            self._normalize_tar_info(info)
                            archive.addfile(info, io.BytesIO(data))
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        return ArchiveInspector(output, policy=self.policy).inspect()

    def write_zip(
        self,
        output: Path,
        *,
        archive_root: str,
        include: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        root_name = normalize_relative_path(archive_root)
        if "/" in root_name or root_name == ".":
            raise IntegrityViolation(
                "Archive root must be one portable path component.",
                code="archive_root_invalid",
                details={"archive_root": archive_root},
            )
        entries = self._entries(include)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.tmp-{os.getpid()}")
        timestamp = datetime.fromtimestamp(
            max(self.policy.source_date_epoch, 315532800),
            UTC,
        )
        zip_time = (
            timestamp.year,
            timestamp.month,
            timestamp.day,
            timestamp.hour,
            timestamp.minute,
            timestamp.second,
        )
        try:
            with zipfile.ZipFile(
                temporary,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            ) as archive:
                for directory in (root_name, *(
                    f"{root_name}/{item}" for item in self._directories(entries)
                )):
                    info = zipfile.ZipInfo(directory.rstrip("/") + "/", zip_time)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFDIR | 0o755) << 16
                    info.compress_type = zipfile.ZIP_STORED
                    archive.writestr(info, b"")
                for relative, path, mode in entries:
                    info = zipfile.ZipInfo(f"{root_name}/{relative}", zip_time)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | mode) << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(info, path.read_bytes())
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        return ArchiveInspector(output, policy=self.policy).inspect()

    def _entries(
        self,
        include: Iterable[str] | None,
    ) -> list[tuple[str, Path, int]]:
        include_set = (
            {normalize_relative_path(item) for item in include}
            if include is not None
            else None
        )
        output: list[tuple[str, Path, int]] = []
        for entry in CanonicalTreeWalker(
            self.source_root,
            policy=self.policy,
        ).walk():
            if entry.is_symlink:
                raise IntegrityViolation(
                    "Deterministic archive source contains a symlink.",
                    code="archive_source_symlink",
                    details={"path": entry.relative_path},
                )
            if include_set is not None and entry.relative_path not in include_set:
                continue
            mode = 0o755 if entry.executable else 0o644
            output.append((entry.relative_path, entry.path, mode))
        output.sort(key=lambda item: item[0].encode("utf-8"))
        if include_set is not None:
            missing = sorted(include_set - {item[0] for item in output})
            if missing:
                raise IntegrityViolation(
                    "Deterministic archive input is incomplete.",
                    code="archive_input_missing",
                    details={"missing": missing},
                )
        return output

    @staticmethod
    def _directories(entries: Sequence[tuple[str, Path, int]]) -> tuple[str, ...]:
        directories: set[str] = set()
        for relative, _, _ in entries:
            parent = PurePosixPath(relative).parent
            while parent.as_posix() != ".":
                directories.add(parent.as_posix())
                parent = parent.parent
        return tuple(sorted(directories, key=lambda item: item.encode("utf-8")))

    def _normalize_tar_info(self, info: tarfile.TarInfo) -> None:
        info.mtime = self.policy.source_date_epoch
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.pax_headers = {}


class ReleaseBundleBuilder:
    def __init__(
        self,
        project_root: Path,
        output_root: Path,
        *,
        policy: ReleasePolicy = DEFAULT_RELEASE_POLICY,
    ) -> None:
        self.project_root = project_root.resolve()
        self.output_root = output_root.resolve()
        self.policy = policy

    def build(
        self,
        *,
        release_id: str,
        expected_commit: str | None = None,
        benchmark_expected_commit: str | None = None,
        archive_format: str = "tar.gz",
        require_python_hashes: bool = True,
        benchmark_report: Path | None = None,
        deployment_evidence: Path | None = None,
    ) -> dict[str, Any]:
        commit = source_revision(self.project_root)
        if expected_commit and commit != expected_commit:
            raise ReleaseError(
                "Release source commit does not match the frozen target.",
                code="release_target_commit_mismatch",
                details={"expected": expected_commit, "actual": commit},
            )
        boundary = BoundaryScanner(
            self.project_root,
            policy=self.policy,
        ).enforce()
        python_lock = PythonLock.load(
            self.project_root / "requirements.txt",
            require_hashes=require_python_hashes,
        )
        with (self.project_root / "pyproject.toml").open("rb") as stream:
            pyproject = tomllib.load(stream)
        python_receipt = python_lock.verify_project_requirements(pyproject)
        bun_lock = BunLock.load(self.project_root / "bun.lock")
        bun_receipt = bun_lock.verify_workspace(self.project_root)
        runtime_inventory = RuntimeInventory(
            self.project_root,
            policy=self.policy,
        ).enforce()
        loopx_lock = LoopXPackageLock.discover(self.project_root)
        loopx_verification: Mapping[str, Any] | None = None
        loopx_component: tuple[Component, ...] = ()
        if loopx_lock is not None:
            loopx_verification = loopx_lock.verify_artifacts(deep=True)
            component = loopx_lock.component()
            loopx_component = (
                Component(
                    component_type="library",
                    name=str(component["name"]),
                    version=str(component["version"]),
                    purl=str(component["purl"]),
                    licenses=tuple(component["licenses"]),
                    hashes=tuple(component["hashes"]),
                    properties=tuple(component["properties"]),
                    dependencies=(),
                ),
            )
        components = (
            *PythonComponentInventory(python_lock).build(),
            *JavaScriptComponentInventory(self.project_root, bun_lock).build(),
            *loopx_component,
        )
        version = str(
            (pyproject.get("project") or {}).get("version") or "0.0.0"
        )
        benchmark = BenchmarkEvidenceLinker(self.project_root).link(
            expected_commit=benchmark_expected_commit,
            report_path=benchmark_report,
            deployment_path=deployment_evidence,
        )
        stage_parent = self.output_root / ".stage"
        stage = stage_parent / f"{release_id}-{uuid.uuid4().hex}"
        payload_root = stage / release_id
        stage.mkdir(parents=True, exist_ok=False)
        try:
            self._copy_release_inputs(payload_root)
            generated_at = datetime.fromtimestamp(
                self.policy.source_date_epoch,
                UTC,
            ).isoformat()
            configuration = default_release_configuration()
            ConfigurationProvisioner(policy=self.policy).validate_template(
                configuration
            )
            artifacts_root = payload_root / "release"
            artifacts_root.mkdir(parents=True, exist_ok=True)
            configuration_path = artifacts_root / "configuration.example.json"
            self._write_json(configuration_path, configuration)
            runtime_path = artifacts_root / "runtime-inventory.json"
            self._write_json(runtime_path, runtime_inventory)
            benchmark_path = artifacts_root / "benchmark-link.json"
            self._write_json(benchmark_path, benchmark)
            loopx_doctor_metadata_path: Path | None = None
            payload_loopx_lock: LoopXPackageLock | None = None
            if loopx_lock is not None:
                payload_loopx_lock = LoopXPackageLock.load(payload_root)
                loopx_doctor_metadata_path = (
                    artifacts_root / "loopx-doctor-metadata.json"
                )
                self._write_json(
                    loopx_doctor_metadata_path,
                    {
                        **dict(payload_loopx_lock.value["doctor"]),
                        "package_lock_digest": payload_loopx_lock.lock_digest,
                        "artifact_verification": dict(loopx_verification or {}),
                    },
                )
            sbom = SbomBuilder(
                project_name="zyra",
                project_version=version,
                commit=commit,
            ).build(
                components,
                runtime_inventory=runtime_inventory,
                serial=f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, commit)}",
                timestamp=generated_at,
            )
            sbom_path = artifacts_root / "sbom.cdx.json"
            self._write_json(sbom_path, sbom)
            notice = NoticeBuilder().build(
                components,
                project_name="Zyra",
                project_version=version,
                source_revision=commit,
            )
            notice_path = artifacts_root / "NOTICE"
            notice_path.write_text(notice, encoding="utf-8", newline="\n")
            wheel_receipt = DeterministicWheelBuilder(
                payload_root,
                policy=self.policy,
            ).build(artifacts_root / "wheels")
            wheel_path = (
                artifacts_root
                / "wheels"
                / str(wheel_receipt["path"])
            )
            payload_checksum = ChecksumBuilder(
                payload_root,
                policy=self.policy,
            ).build(generated_at=generated_at)
            checksum_path = artifacts_root / "checksums.json"
            self._write_json(checksum_path, payload_checksum.to_dict())
            artifacts = {
                "checksums": self._artifact_reference(
                    payload_root,
                    checksum_path,
                ),
                "sbom": self._artifact_reference(payload_root, sbom_path),
                "notice": self._artifact_reference(payload_root, notice_path),
                "runtime_inventory": self._artifact_reference(
                    payload_root,
                    runtime_path,
                ),
                "configuration": self._artifact_reference(
                    payload_root,
                    configuration_path,
                ),
                "benchmark": self._artifact_reference(
                    payload_root,
                    benchmark_path,
                ),
                "python_wheel": self._artifact_reference(
                    payload_root,
                    wheel_path,
                ),
            }
            if (
                payload_loopx_lock is not None
                and loopx_doctor_metadata_path is not None
            ):
                artifacts.update(
                    {
                        "loopx_package_lock": self._artifact_reference(
                            payload_root,
                            payload_loopx_lock.path,
                        ),
                        "loopx_wheel": self._artifact_reference(
                            payload_root,
                            payload_loopx_lock.resolve_artifact(
                                payload_loopx_lock.artifacts["wheel"]
                            ),
                        ),
                        "loopx_source_bundle": self._artifact_reference(
                            payload_root,
                            payload_loopx_lock.resolve_artifact(
                                payload_loopx_lock.artifacts["source_bundle"]
                            ),
                        ),
                        "loopx_doctor_metadata": self._artifact_reference(
                            payload_root,
                            loopx_doctor_metadata_path,
                        ),
                    }
                )
            locks: dict[str, Any] = {
                "python": {
                    "path": "requirements.txt",
                    "digest": python_receipt["digest"],
                    "requirement_count": python_receipt["requirement_count"],
                    "hashes_required": require_python_hashes,
                },
                "javascript": {
                    "path": "bun.lock",
                    "digest": bun_receipt["lock_digest"],
                    "workspace_count": bun_receipt["workspace_count"],
                },
            }
            if payload_loopx_lock is not None:
                locks["loopx"] = {
                    "path": payload_loopx_lock.path.relative_to(
                        payload_root
                    ).as_posix(),
                    "digest": payload_loopx_lock.lock_digest,
                    "version": str(payload_loopx_lock.package["version"]),
                    "source_commit": str(
                        payload_loopx_lock.package["source_commit"]
                    ),
                    "source_digest": payload_loopx_lock.source_digest,
                    "profiles": sorted(payload_loopx_lock.profiles),
                }
            manifest_value = {
                "schema": RELEASE_MANIFEST_SCHEMA,
                "release_id": release_id,
                "project": "zyra",
                "version": version,
                "source_commit": commit,
                "source_date_epoch": self.policy.source_date_epoch,
                "created_at": generated_at,
                "payload": {
                    "root_digest": payload_checksum.root_digest,
                    "file_count": len(payload_checksum.files),
                },
                "locks": locks,
                "artifacts": artifacts,
                "platforms": [
                    {
                        "platform": "windows",
                        "architectures": ["amd64", "arm64"],
                        "launcher": "zyra-release.exe-or-python",
                        "loopx_profile": (
                            "windows_release_offline_wheel"
                            if payload_loopx_lock is not None
                            else ""
                        ),
                    },
                    {
                        "platform": "linux",
                        "architectures": ["x86_64", "aarch64"],
                        "launcher": "zyra-release",
                        "loopx_profile": (
                            "linux_wsl_upstream_semantics"
                            if payload_loopx_lock is not None
                            else ""
                        ),
                    },
                ],
                "migration": {
                    "current_version": 1,
                    "minimum_rollback_version": 0,
                    "transactional": True,
                },
                "boundary": {
                    "included_count": boundary["included_count"],
                    "excluded_count": boundary["excluded_count"],
                    "included_digest": boundary["included_digest"],
                    "excluded_digest": boundary["excluded_digest"],
                },
            }
            manifest = ReleaseManifest(manifest_value)
            manifest_path = artifacts_root / "manifest.json"
            self._write_json(manifest_path, manifest.to_dict())
            archive_name = (
                f"{release_id}.zip"
                if archive_format == "zip"
                else f"{release_id}.tar.gz"
            )
            output = self.output_root / archive_name
            writer = DeterministicArchiveWriter(
                stage,
                policy=self.policy,
            )
            if archive_format == "zip":
                archive_receipt = writer.write_zip(
                    output,
                    archive_root="zyra-release",
                )
            elif archive_format == "tar.gz":
                archive_receipt = writer.write_tar_gz(
                    output,
                    archive_root="zyra-release",
                )
            else:
                raise IntegrityViolation(
                    "Requested release archive format is unsupported.",
                    code="release_archive_format",
                    details={"archive_format": archive_format},
                )
            result = {
                "schema": "zyra.release-bundle-receipt/v1",
                "ready": True,
                "release_id": release_id,
                "source_commit": commit,
                "manifest_digest": manifest.digest,
                "payload_digest": payload_checksum.root_digest,
                "archive": str(output),
                "archive_sha256": archive_receipt["archive_sha256"],
                "archive_size": archive_receipt["archive_size"],
                "archive_type": archive_receipt["archive_type"],
                "file_count": len(payload_checksum.files),
                "python_lock": python_receipt,
                "javascript_lock": bun_receipt,
                "boundary": boundary,
                "benchmark": benchmark,
                "python_wheel": wheel_receipt,
                "loopx": (
                    {
                        "version": str(loopx_lock.package["version"]),
                        "source_commit": str(
                            loopx_lock.package["source_commit"]
                        ),
                        "source_digest": loopx_lock.source_digest,
                        "package_lock_digest": loopx_lock.lock_digest,
                        "profiles": sorted(loopx_lock.profiles),
                        "artifact_verification": dict(
                            loopx_verification or {}
                        ),
                    }
                    if loopx_lock is not None
                    else None
                ),
            }
            return result
        finally:
            if stage.exists():
                shutil.rmtree(stage)
            try:
                stage_parent.rmdir()
            except OSError:
                pass

    def verify(self, archive: Path) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="zyra-release-verify-") as raw:
            destination = Path(raw)
            inspection = ArchiveInspector(
                archive,
                policy=self.policy,
            ).safe_extract(destination)
            archive_root = destination / str(inspection["root"])
            payload_candidates = [
                item for item in archive_root.iterdir() if item.is_dir()
            ]
            if len(payload_candidates) != 1:
                raise IntegrityViolation(
                    "Release archive payload root is ambiguous.",
                    code="release_payload_root_ambiguous",
                    details={"entries": [item.name for item in payload_candidates]},
                )
            payload_root = payload_candidates[0]
            manifest_path = payload_root / "release" / "manifest.json"
            manifest = ReleaseManifest.load(manifest_path)
            artifacts = manifest.value["artifacts"]
            verified_artifacts: dict[str, Any] = {}
            for name, reference in artifacts.items():
                path = payload_root / normalize_relative_path(
                    str(reference["path"])
                )
                if not path.is_file():
                    raise IntegrityViolation(
                        "Release manifest artifact is missing.",
                        code="release_artifact_missing",
                        details={"artifact": name, "path": str(path)},
                    )
                actual = sha256_file(path)
                expected = str(reference["sha256"])
                if actual != expected:
                    raise IntegrityViolation(
                        "Release manifest artifact checksum does not match.",
                        code="release_artifact_checksum",
                        details={
                            "artifact": name,
                            "expected": expected,
                            "actual": actual,
                        },
                    )
                verified_artifacts[name] = {
                    "path": str(reference["path"]),
                    "sha256": actual,
                }
            checksum_value = json.loads(
                (
                    payload_root
                    / normalize_relative_path(
                        str(artifacts["checksums"]["path"])
                    )
                ).read_text(encoding="utf-8")
            )
            try:
                ChecksumVerifier(
                    payload_root,
                    policy=self.policy,
                ).verify(checksum_value, reject_extra=False)
            except IntegrityViolation as error:
                if not self._checksum_self_reference_only(error):
                    raise
            wheel_reference = artifacts["python_wheel"]
            wheel_verification = DeterministicWheelBuilder(
                payload_root,
                policy=self.policy,
            ).verify(
                payload_root
                / normalize_relative_path(str(wheel_reference["path"]))
            )
            loopx_verification: Mapping[str, Any] | None = None
            if "loopx" in manifest.value["locks"]:
                loopx_verification = LoopXDoctor(payload_root).run(deep=True)
                if loopx_verification.get("ready") is not True:
                    raise IntegrityViolation(
                        "Release LoopX deep doctor failed.",
                        code="release_loopx_doctor_failed",
                        details={"doctor": dict(loopx_verification)},
                    )
            return {
                "schema": "zyra.release-bundle-verification/v1",
                "ready": True,
                "archive": str(archive.resolve()),
                "archive_sha256": inspection["archive_sha256"],
                "release_id": manifest.release_id,
                "source_commit": manifest.source_commit,
                "manifest_digest": manifest.digest,
                "payload_digest": manifest.payload_digest,
                "verified_artifacts": verified_artifacts,
                "python_wheel": wheel_verification,
                "loopx": (
                    dict(loopx_verification)
                    if loopx_verification is not None
                    else None
                ),
            }

    def _copy_release_inputs(self, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=False)
        for entry in CanonicalTreeWalker(
            self.project_root,
            policy=self.policy,
        ).walk():
            if entry.is_symlink:
                raise IntegrityViolation(
                    "Release input contains a symlink.",
                    code="release_input_symlink",
                    details={"path": entry.relative_path},
                )
            target = destination.joinpath(*PurePosixPath(entry.relative_path).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(entry.path, target)
            target.chmod(0o755 if entry.executable else 0o644)

    @staticmethod
    def _artifact_reference(root: Path, path: Path) -> dict[str, Any]:
        return {
            "path": path.relative_to(root).as_posix(),
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    @staticmethod
    def _checksum_self_reference_only(error: IntegrityViolation) -> bool:
        details = error.details
        changed = details.get("changed", [])
        extra = details.get("extra", [])
        missing = details.get("missing", [])
        if missing or extra:
            return False
        return bool(changed) and all(
            str(item.get("path")) == "release/checksums.json"
            for item in changed
            if isinstance(item, Mapping)
        )


__all__ = [
    "BenchmarkEvidenceLinker",
    "DeterministicArchiveWriter",
    "RELEASE_MANIFEST_SCHEMA",
    "ReleaseBundleBuilder",
    "ReleaseManifest",
    "assert_clean_git",
    "source_revision",
]
