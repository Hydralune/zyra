from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVALUATION_PACKAGE = ROOT / "packages" / "evaluation"
if str(EVALUATION_PACKAGE) not in sys.path:
    sys.path.insert(0, str(EVALUATION_PACKAGE))
PRODUCTIZATION_PACKAGE = ROOT / "packages" / "productization"
if str(PRODUCTIZATION_PACKAGE) not in sys.path:
    sys.path.insert(0, str(PRODUCTIZATION_PACKAGE))

from zyra_evaluation.policy_benchmark import (  # noqa: E402
    ContractViolation,
    Phase2PolicyContractBundle,
)


def _git_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _write_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate frozen Phase 2 source, owner, metric, and activation contracts."
    )
    parser.add_argument(
        "--config-root",
        type=Path,
        default=None,
        help="Optional directory containing the four Phase 2 contract files.",
    )
    parser.add_argument(
        "--target-commit",
        default="",
        help="Implementation commit recorded in an emitted validation receipt.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional repository-relative validation receipt path.",
    )
    parser.add_argument(
        "--require-strongest-active",
        action="store_true",
        help="Fail unless every strongest-profile mechanism is deterministic_ready.",
    )
    args = parser.parse_args()

    try:
        bundle = Phase2PolicyContractBundle.load(
            ROOT,
            config_root=args.config_root,
        )
        report = (
            bundle.require_strongest_activation()
            if args.require_strongest_active
            else bundle.validate()
        )
        payload = report.to_dict()
        payload["target_commit"] = args.target_commit or _git_head()
        payload["strongest_profile_requested"] = args.require_strongest_active
    except ContractViolation as exc:
        payload = {
            "schema": "zyra.phase2-policy-contract-validation/v1",
            "valid": False,
            "target_commit": args.target_commit or _git_head(),
            "finding": exc.to_dict(),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        raise SystemExit(1) from exc

    if args.output is not None:
        target = args.output
        if not target.is_absolute():
            target = ROOT / target
        try:
            target.resolve().relative_to(ROOT.resolve())
        except ValueError as exc:
            raise SystemExit("--output must remain inside the Zyra repository") from exc
        _write_atomic(target, payload)

    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
