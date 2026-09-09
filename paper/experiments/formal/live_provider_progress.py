"""Print one compact, read-only progress snapshot for an active ZYRA run."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    if not args.database.exists():
        print(json.dumps({"ready": False}, separators=(",", ":")))
        return 0

    connection = sqlite3.connect(
        f"file:{args.database.as_posix()}?mode=ro", uri=True, timeout=1
    )
    try:
        row = connection.execute(
            """
            SELECT
              COUNT(*),
              SUM(CASE WHEN outcome = 'succeeded' THEN 1 ELSE 0 END),
              SUM(CASE WHEN outcome IS NOT NULL AND outcome NOT IN ('succeeded', 'running') THEN 1 ELSE 0 END),
              SUM(CASE WHEN outcome IS NULL OR outcome = 'running' THEN 1 ELSE 0 END),
              MAX(completed_at),
              COALESCE(SUM(json_extract(json, '$.requestBytes')), 0),
              COALESCE(SUM(json_extract(json, '$.responseBytes')), 0)
            FROM provider_dispatch_attempts
            """
        ).fetchone()
    finally:
        connection.close()

    total, succeeded, failed, active, last_activity_ms, request_bytes, response_bytes = row
    print(
        json.dumps(
            {
                "ready": True,
                "dispatches": int(total or 0),
                "succeeded": int(succeeded or 0),
                "failed": int(failed or 0),
                "active": int(active or 0),
                "last_activity_ms": last_activity_ms,
                "request_bytes": int(request_bytes or 0),
                "response_bytes": int(response_bytes or 0),
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
