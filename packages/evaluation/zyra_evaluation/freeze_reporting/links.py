from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .canonical import (
    file_digest,
    require_commit,
    require_digest,
    require_identity,
    require_mapping,
    require_sequence,
    require_text,
    resolve_inside,
    safe_relative_path,
)
from .errors import blocker, fail, require_no_blockers


class EvidenceLinkResolver:
    """Resolves evidence links inside one admitted repository boundary."""

    def __init__(self, repository_root: str | Path) -> None:
        self.repository_root = Path(repository_root).resolve(strict=True)

    def resolve(self, reference: Mapping[str, Any]) -> dict[str, Any]:
        selected = require_mapping(reference, "evidence reference")
        reference_id = require_identity(
            selected.get("reference_id"),
            "reference id",
        )
        kind = require_text(selected.get("kind"), "reference kind", maximum=64)
        relative = safe_relative_path(selected.get("path"), "reference path")
        target = resolve_inside(self.repository_root, relative, must_exist=True)
        expected = require_digest(selected.get("sha256"), "reference sha256")
        observed = self._target_digest(target)
        if expected != observed:
            raise fail(
                "evidence-link-digest-mismatch",
                "Evidence link target digest does not match.",
                phase="link",
                detail={
                    "reference_id": reference_id,
                    "path": relative,
                    "expected": expected,
                    "observed": observed,
                },
            )
        receipt = {
            "reference_id": reference_id,
            "kind": kind,
            "path": relative,
            "sha256": observed,
            "target_type": "directory" if target.is_dir() else "file",
        }
        if selected.get("selector"):
            selector = require_text(
                selected.get("selector"),
                "reference selector",
                maximum=2048,
            )
            self._verify_selector(target, selector)
            receipt["selector"] = selector
        if selected.get("commit"):
            receipt["commit"] = require_commit(
                selected.get("commit"),
                "reference commit",
            )
        return receipt

    def resolve_all(
        self,
        references: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        receipts: list[dict[str, Any]] = []
        findings: list[dict[str, Any]] = []
        identities: set[str] = set()
        for reference in references:
            try:
                receipt = self.resolve(reference)
            except Exception as error:
                if hasattr(error, "to_dict"):
                    findings.append(error.to_dict())
                else:
                    findings.append(
                        blocker(
                            "evidence-link-resolution-failed",
                            str(error),
                        )
                    )
                continue
            reference_id = receipt["reference_id"]
            if reference_id in identities:
                findings.append(
                    blocker(
                        "evidence-reference-id-duplicate",
                        "Evidence reference identifier is duplicated.",
                        reference_id=reference_id,
                    )
                )
            identities.add(reference_id)
            receipts.append(receipt)
        require_no_blockers(
            findings,
            code="evidence-link-set-invalid",
            message="Evidence reference set contains broken links.",
            phase="link",
        )
        kinds = defaultdict(int)
        for receipt in receipts:
            kinds[receipt["kind"]] += 1
        return {
            "valid": True,
            "reference_count": len(receipts),
            "kind_counts": dict(sorted(kinds.items())),
            "references": sorted(receipts, key=lambda item: item["reference_id"]),
        }

    def verify_frozen_resolution(
        self,
        reference: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Verify the resolution receipt sealed into a historical index.

        External targets are resolved while an index is built.  A materialized
        freeze output must instead verify the resolution receipt protected by
        the index digest and evidence archive; consulting the mutable current
        checkout would make historical evidence depend on later source edits.
        """

        selected = require_mapping(reference, "frozen evidence reference")
        reference_id = require_identity(
            selected.get("reference_id"),
            "reference id",
        )
        kind = require_text(selected.get("kind"), "reference kind", maximum=64)
        relative = safe_relative_path(selected.get("path"), "reference path")
        expected = require_digest(selected.get("sha256"), "reference sha256")
        if selected.get("resolved") is not True:
            raise fail(
                "evidence-frozen-resolution-not-attested",
                "Frozen evidence reference is not marked resolved.",
                phase="link",
                detail={"reference_id": reference_id, "path": relative},
            )
        receipt = require_mapping(
            selected.get("resolution"),
            "frozen evidence resolution",
        )
        expected_fields: dict[str, Any] = {
            "reference_id": reference_id,
            "kind": kind,
            "path": relative,
            "sha256": expected,
        }
        if selected.get("selector"):
            expected_fields["selector"] = require_text(
                selected.get("selector"),
                "reference selector",
                maximum=2048,
            )
        if selected.get("commit"):
            expected_fields["commit"] = require_commit(
                selected.get("commit"),
                "reference commit",
            )
        mismatches = {
            field: {
                "reference": value,
                "resolution": receipt.get(field),
            }
            for field, value in expected_fields.items()
            if receipt.get(field) != value
        }
        target_type = require_text(
            receipt.get("target_type"),
            "frozen resolution target type",
            maximum=32,
        )
        if target_type not in {"file", "directory"}:
            mismatches["target_type"] = {
                "reference": "file-or-directory",
                "resolution": target_type,
            }
        if mismatches:
            raise fail(
                "evidence-frozen-resolution-mismatch",
                "Frozen evidence resolution disagrees with its reference.",
                phase="link",
                detail={
                    "reference_id": reference_id,
                    "path": relative,
                    "mismatches": mismatches,
                },
            )
        return dict(receipt)

    def graph(
        self,
        score_entries: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        findings: list[dict[str, Any]] = []
        for requirement_id, entry in sorted(score_entries.items()):
            score_node = f"score:{requirement_id}"
            nodes[score_node] = {
                "node_id": score_node,
                "kind": "score-item",
                "label": requirement_id,
            }
            references = require_sequence(
                entry.get("references"),
                f"{requirement_id} references",
            )
            for reference in references:
                selected = require_mapping(reference, "score reference")
                reference_id = require_identity(
                    selected.get("reference_id"),
                    "reference id",
                )
                reference_node = f"reference:{reference_id}"
                target_node = f"target:{safe_relative_path(selected.get('path'))}"
                nodes[reference_node] = {
                    "node_id": reference_node,
                    "kind": str(selected.get("kind") or ""),
                    "label": reference_id,
                }
                nodes[target_node] = {
                    "node_id": target_node,
                    "kind": "target",
                    "label": safe_relative_path(selected.get("path")),
                }
                edges.append(
                    {
                        "source": score_node,
                        "target": reference_node,
                        "relation": "supported-by",
                    }
                )
                edges.append(
                    {
                        "source": reference_node,
                        "target": target_node,
                        "relation": "resolves-to",
                    }
                )
        for edge in edges:
            if edge["source"] not in nodes or edge["target"] not in nodes:
                findings.append(
                    blocker(
                        "evidence-link-graph-dangling-edge",
                        "Evidence graph contains a dangling edge.",
                        edge=edge,
                    )
                )
        require_no_blockers(
            findings,
            code="evidence-link-graph-invalid",
            message="Evidence link graph is inconsistent.",
            phase="link",
        )
        return {
            "nodes": sorted(nodes.values(), key=lambda item: item["node_id"]),
            "edges": sorted(
                edges,
                key=lambda item: (
                    item["source"],
                    item["relation"],
                    item["target"],
                ),
            ),
        }

    @staticmethod
    def _verify_selector(target: Path, selector: str) -> None:
        if not target.is_file():
            raise fail(
                "evidence-selector-target-not-file",
                "Selector requires a file evidence target.",
                phase="link",
                detail={"path": str(target), "selector": selector},
            )
        if target.stat().st_size > 128 * 1024 * 1024:
            raise fail(
                "evidence-selector-target-too-large",
                "Selector target exceeds the safe inspection limit.",
                phase="link",
                detail={"path": str(target)},
            )
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise fail(
                "evidence-selector-target-unreadable",
                "Selector target is not readable UTF-8 text.",
                phase="link",
                detail={"path": str(target)},
            ) from error
        if selector not in text:
            raise fail(
                "evidence-selector-missing",
                "Evidence selector is not present in its target.",
                phase="link",
                detail={"path": str(target), "selector": selector},
            )

    @staticmethod
    def _target_digest(target: Path) -> str:
        if target.is_file():
            return file_digest(target)
        members: list[dict[str, Any]] = []
        for path in sorted(item for item in target.rglob("*") if item.is_file()):
            members.append(
                {
                    "path": path.relative_to(target).as_posix(),
                    "sha256": file_digest(path),
                    "bytes": path.stat().st_size,
                }
            )
        from .canonical import digest

        return digest(members)


def collect_references(
    entries: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for requirement_id, entry in sorted(entries.items()):
        for item in require_sequence(
            entry.get("references"),
            f"{requirement_id} references",
        ):
            references.append(require_mapping(item, "evidence reference"))
    return references
