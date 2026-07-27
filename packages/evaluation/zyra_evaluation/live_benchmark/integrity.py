from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .canonical import (
    atomic_json,
    content_digest,
    digest,
    file_digest,
    identity,
    invalid,
    mapping,
    require_digest,
    resolve_within,
    scan_secret_canaries,
    sequence,
    utc_now,
)


class EvidenceIntegrityBuilder:
    def __init__(
        self,
        output_root: str | Path,
        *,
        secret_canaries: Iterable[str] = (),
    ) -> None:
        self.output_root = Path(output_root).resolve(strict=False)
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.secret_canaries = tuple(str(item) for item in secret_canaries if item)

    def write_json_member(
        self,
        path: str,
        value: Any,
        *,
        kind: str,
        required: bool = True,
    ) -> dict[str, Any]:
        target = resolve_within(path, self.output_root, "evidence member")
        payload_digest, byte_count = atomic_json(target, value)
        payload = target.read_bytes()
        scan_secret_canaries(payload, self.secret_canaries)
        return {
            "path": str(target.relative_to(self.output_root)).replace("\\", "/"),
            "kind": kind,
            "required": required,
            "sha256": payload_digest,
            "bytes": byte_count,
        }

    def register_file(
        self,
        path: str | Path,
        *,
        kind: str,
        required: bool = True,
    ) -> dict[str, Any]:
        target = resolve_within(path, self.output_root, "evidence member", must_exist=True)
        payload_digest, byte_count = file_digest(target)
        scan_secret_canaries(target.read_bytes(), self.secret_canaries)
        return {
            "path": str(target.relative_to(self.output_root)).replace("\\", "/"),
            "kind": kind,
            "required": required,
            "sha256": payload_digest,
            "bytes": byte_count,
        }

    def build_manifest(
        self,
        *,
        campaign_id: str,
        commit_sha: str,
        members: Sequence[Mapping[str, Any]],
        roots: Mapping[str, str],
    ) -> dict[str, Any]:
        selected_campaign = identity(campaign_id, "campaign id")
        verified_members = verify_members(members, self.output_root)
        root_values = {
            str(key): require_digest(value, f"{key} root digest")
            for key, value in roots.items()
        }
        manifest = {
            "schema": "zyra.live-benchmark-evidence-manifest/v1",
            "campaign_id": selected_campaign,
            "commit_sha": commit_sha,
            "member_count": len(verified_members),
            "members": verified_members,
            "roots": dict(sorted(root_values.items())),
            "merkle_root": merkle_root(verified_members),
            "created_at": utc_now(),
        }
        manifest["manifest_digest"] = digest(manifest)
        return manifest


class EvidenceIntegrityVerifier:
    def verify(
        self,
        manifest: Mapping[str, Any],
        *,
        evidence_root: str | Path,
        expected_campaign_id: str,
        expected_commit: str,
    ) -> dict[str, Any]:
        root = Path(evidence_root).resolve(strict=True)
        campaign_id = identity(
            manifest.get("campaign_id"),
            "manifest campaign id",
        )
        if campaign_id != identity(expected_campaign_id, "expected campaign id"):
            raise invalid(
                "benchmark_manifest_campaign_mismatch",
                "Evidence manifest belongs to another campaign.",
                phase="integrity",
            )
        if manifest.get("commit_sha") != expected_commit:
            raise invalid(
                "benchmark_manifest_commit_mismatch",
                "Evidence manifest belongs to another commit.",
                phase="integrity",
            )
        members = tuple(
            mapping(item, "manifest member")
            for item in sequence(manifest.get("members"), "manifest members")
        )
        verified = verify_members(members, root)
        declared_count = int(manifest.get("member_count", -1))
        if declared_count != len(verified):
            raise invalid(
                "benchmark_manifest_member_count_mismatch",
                "Evidence manifest member count is invalid.",
                phase="integrity",
            )
        observed_root = merkle_root(verified)
        if manifest.get("merkle_root") != observed_root:
            raise invalid(
                "benchmark_manifest_merkle_mismatch",
                "Evidence manifest Merkle root is invalid.",
                phase="integrity",
            )
        projection = dict(manifest)
        declared_digest = projection.pop("manifest_digest", "")
        observed_digest = digest(projection)
        if declared_digest != observed_digest:
            raise invalid(
                "benchmark_manifest_digest_mismatch",
                "Evidence manifest digest is invalid.",
                phase="integrity",
            )
        required_missing = [
            item["path"]
            for item in verified
            if item.get("required") is True
            and not resolve_within(
                item["path"],
                root,
                "required evidence member",
                must_exist=True,
            ).is_file()
        ]
        if required_missing:
            raise invalid(
                "benchmark_manifest_required_member_missing",
                "Required evidence member is missing.",
                phase="integrity",
                detail={"paths": required_missing},
            )
        receipt = {
            "schema": "zyra.live-benchmark-evidence-verification/v1",
            "valid": True,
            "campaign_id": campaign_id,
            "commit_sha": expected_commit,
            "member_count": len(verified),
            "merkle_root": observed_root,
            "manifest_digest": observed_digest,
            "verified_at": utc_now(),
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt


def verify_members(
    members: Sequence[Mapping[str, Any]],
    root: Path,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    paths: list[str] = []
    output: list[dict[str, Any]] = []
    for index, member in enumerate(members):
        path = str(member.get("path") or "").replace("\\", "/")
        if not path:
            findings.append({"code": "member-path-missing", "index": index})
            continue
        try:
            target = resolve_within(path, root, "evidence member", must_exist=True)
        except ValueError:
            findings.append({"code": "member-path-invalid", "path": path})
            continue
        if not target.is_file():
            findings.append({"code": "member-not-file", "path": path})
            continue
        observed_digest, observed_bytes = file_digest(target)
        try:
            expected_digest = require_digest(
                member.get("sha256"),
                f"member {path} digest",
            )
        except ValueError:
            expected_digest = ""
        if expected_digest != observed_digest:
            findings.append(
                {
                    "code": "member-digest-mismatch",
                    "path": path,
                    "expected": expected_digest,
                    "observed": observed_digest,
                }
            )
        if int(member.get("bytes", -1)) != observed_bytes:
            findings.append(
                {
                    "code": "member-size-mismatch",
                    "path": path,
                    "expected": member.get("bytes"),
                    "observed": observed_bytes,
                }
            )
        paths.append(path)
        output.append(
            {
                "path": path,
                "kind": str(member.get("kind") or "unknown"),
                "required": member.get("required") is True,
                "sha256": observed_digest,
                "bytes": observed_bytes,
            }
        )
    duplicates = sorted(
        item for item, count in Counter(paths).items() if count > 1
    )
    if duplicates:
        findings.append({"code": "member-path-duplicate", "paths": duplicates})
    if findings:
        raise invalid(
            "benchmark_evidence_members_invalid",
            "Benchmark evidence members failed integrity verification.",
            phase="integrity",
            detail={"findings": findings},
        )
    return sorted(output, key=lambda item: item["path"])


def merkle_root(members: Sequence[Mapping[str, Any]]) -> str:
    leaves = [
        digest(
            {
                "path": item["path"],
                "sha256": item["sha256"],
                "bytes": item["bytes"],
                "kind": item.get("kind"),
                "required": item.get("required") is True,
            }
        )
        for item in sorted(members, key=lambda value: str(value["path"]))
    ]
    if not leaves:
        return content_digest(b"")
    level = leaves
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [
            digest({"left": level[index], "right": level[index + 1]})
            for index in range(0, len(level), 2)
        ]
    return level[0]
