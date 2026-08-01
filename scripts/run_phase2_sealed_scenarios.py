from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PRODUCTIZATION_ROOT = ROOT / "packages" / "productization"
if str(PRODUCTIZATION_ROOT) not in sys.path:
    sys.path.insert(0, str(PRODUCTIZATION_ROOT))

from zyra_evaluation.policy_benchmark.sealed_long_run import (  # noqa: E402
    SealedLongRunError,
    SealedLongRunRunner,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the two P2-S06-02 sealed adversarial long scenarios."
    )
    parser.add_argument(
        "--manifest",
        required=True,
        help="frozen sealed manifest JSON",
    )
    parser.add_argument(
        "--evidence-root",
        help="clean evidence output root; defaults to the frozen manifest",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        index, index_path = SealedLongRunRunner(
            project_root=ROOT,
            manifest_path=arguments.manifest,
            evidence_root=arguments.evidence_root,
        ).run()
    except (OSError, ValueError, SealedLongRunError) as error:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": type(error).__name__,
                    "message": str(error),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "evidence_index": str(index_path),
                "index_digest": index["index_digest"],
                "run_count": len(index["runs"]),
                "failed_run_count": len(index["failed_runs"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
