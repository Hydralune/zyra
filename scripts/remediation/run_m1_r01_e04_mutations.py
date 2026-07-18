"""Execution 04 mutation-corpus entry point.

G0 freezes identities, target owners, killer tests and operator purposes.  The
candidate implementation adds the concrete reversible operators after each
migrated owner exists.  Until then this command intentionally supports only
corpus inspection and refuses to claim a mutation result.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ZYRA_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = (
    ZYRA_ROOT.parent
    / "docs"
    / "remediations"
    / "M1-R01-claude-source-custody"
    / "manifests"
    / "execution-04-mutation-manifest.jsonl"
)


def records() -> list[dict[str, object]]:
    return [json.loads(line) for line in MANIFEST.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--id")
    parser.add_argument("--restore")
    args = parser.parse_args()
    corpus = records()
    if args.list:
        print(json.dumps(corpus, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    requested = args.id or args.restore
    if requested and requested not in {row["record_id"] for row in corpus}:
        raise SystemExit(f"unknown E04 mutation: {requested}")
    raise SystemExit(
        "E04 mutation operators are frozen but not executable before their migrated targets exist; "
        "complete the current E04 implementation slice first"
    )


if __name__ == "__main__":
    raise SystemExit(main())
