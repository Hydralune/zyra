from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "zyra.m3-01.source-custody-closure/v1"
PROTECTED_QUEUE = (
    "packages/integrations/zyra_integrations/data/"
    "m3_01b_source_custody_work_queue.json"
)
PROTECTED_QUEUE_DIGEST = (
    "sha256:8f2a7958f5837cf63080cf84c37c580bc823fd01c16b5c9b033a05480dfaf8be"
)
PROTECTED_SOURCE_RECEIPT_DIGEST = (
    "sha256:ab935910aa18f3e8ce4bf0d9292b1dae551bc70ab48a72e5d54b6845831048bb"
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify that M3-01B consumed the protected M3-S01A-01 source "
            "custody queue and has no release-blocking current findings."
        )
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--revision", required=True)
    parser.add_argument("--source-receipt", type=Path, required=True)
    parser.add_argument("--source-work-queue", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"expected JSON object: {path}")
    return payload


def _resolved(root: Path, path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _stable_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def verify_closure(
    *,
    project_root: Path,
    revision: str,
    source_receipt: Mapping[str, Any],
    source_queue: Mapping[str, Any],
) -> dict[str, Any]:
    protected = _load(project_root / PROTECTED_QUEUE)
    protected_items = list(protected.get("items") or [])
    current_items = list(source_queue.get("items") or [])
    protected_m3_01b = [
        item for item in protected_items if item.get("owner_unit") == "M3-01B"
    ]
    protected_blocking = [
        item for item in protected_items if item.get("release_blocking") is True
    ]
    protected_m3_01b_blocking = [
        item
        for item in protected_blocking
        if item.get("owner_unit") == "M3-01B"
    ]
    current_m3_01b_blocking = [
        item
        for item in current_items
        if item.get("owner_unit") == "M3-01B"
        and item.get("release_blocking") is True
    ]
    current_deferred_blocking = [
        item
        for item in current_items
        if item.get("owner_unit") != "M3-01B"
        and item.get("release_blocking") is True
    ]
    protected_fingerprints = {
        fingerprint
        for item in protected_m3_01b_blocking
        for fingerprint in item.get("finding_fingerprints") or []
    }
    current_blocking_fingerprints = {
        fingerprint
        for item in current_items
        if item.get("release_blocking") is True
        for fingerprint in item.get("finding_fingerprints") or []
    }
    unresolved_protected = sorted(
        protected_fingerprints & current_blocking_fingerprints
    )
    deferred_codes = sorted(
        {
            code
            for item in current_deferred_blocking
            for code in item.get("finding_codes") or []
        }
    )
    protected_valid = (
        protected.get("digest") == PROTECTED_QUEUE_DIGEST
        and protected.get("source_receipt_digest")
        == PROTECTED_SOURCE_RECEIPT_DIGEST
        and len(protected_items) == 251
        and len(protected_m3_01b) == 246
        and len(protected_blocking) == 143
    )
    current_valid = (
        source_receipt.get("revision") == revision
        and source_queue.get("revision") == revision
        and source_queue.get("source_receipt_digest")
        == source_receipt.get("receipt_digest")
        and not current_m3_01b_blocking
        and not unresolved_protected
        and deferred_codes == ["python_lockfile_missing"]
        and all(
            item.get("owner_unit") == "M3-02B"
            for item in current_deferred_blocking
        )
    )
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "revision": revision,
        "protected_input": {
            "path": PROTECTED_QUEUE,
            "digest": protected.get("digest"),
            "source_receipt_digest": protected.get("source_receipt_digest"),
            "items": len(protected_items),
            "m3_01b_items": len(protected_m3_01b),
            "blocking_items": len(protected_blocking),
            "m3_01b_blocking_items": len(protected_m3_01b_blocking),
            "blocking_fingerprints": len(protected_fingerprints),
            "valid": protected_valid,
        },
        "current_scan": {
            "receipt_digest": source_receipt.get("receipt_digest"),
            "work_queue_digest": source_queue.get("digest"),
            "items": len(current_items),
            "m3_01b_blocking_items": len(current_m3_01b_blocking),
            "unresolved_protected_fingerprints": unresolved_protected,
            "deferred_blocking_items": len(current_deferred_blocking),
            "deferred_blocking_codes": deferred_codes,
            "deferred_owner": "M3-02B",
            "valid": current_valid,
        },
        "release_ready_for_m3_01": protected_valid and current_valid,
    }
    result["receipt_digest"] = _stable_digest(result)
    return result


def main() -> int:
    arguments = _arguments()
    project_root = arguments.project_root.resolve()
    source_receipt_path = _resolved(project_root, arguments.source_receipt)
    source_queue_path = _resolved(project_root, arguments.source_work_queue)
    output_path = _resolved(project_root, arguments.output)
    result = verify_closure(
        project_root=project_root,
        revision=arguments.revision,
        source_receipt=_load(source_receipt_path),
        source_queue=_load(source_queue_path),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "release_ready_for_m3_01": result[
                    "release_ready_for_m3_01"
                ],
                "receipt_digest": result["receipt_digest"],
                "revision": arguments.revision,
                "m3_01b_blocking_items": result["current_scan"][
                    "m3_01b_blocking_items"
                ],
                "deferred_blocking_codes": result["current_scan"][
                    "deferred_blocking_codes"
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0 if result["release_ready_for_m3_01"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
