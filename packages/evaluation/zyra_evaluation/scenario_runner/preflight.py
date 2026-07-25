from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .canonical import digest, file_digest, new_identity, utc_now
from .errors import conflict, invalid, unavailable
from .models import (
    PreflightCheck,
    PreflightPolicy,
    PreflightReceipt,
    PreflightTarget,
    ScenarioConfiguration,
    ScenarioMode,
)


class CleanStateInspector:
    def __init__(
        self,
        *,
        input_seen: Callable[[str], bool],
        maximum_scan_entries: int = 50_000,
    ) -> None:
        self._input_seen = input_seen
        self._maximum_scan_entries = maximum_scan_entries

    def inspect(
        self,
        scenario_run_id: str,
        configuration: ScenarioConfiguration,
    ) -> PreflightReceipt:
        checks = tuple(self.inspect_target(item) for item in configuration.preflight_targets)
        clean = all(item.clean or not target.required for item, target in zip(checks, configuration.preflight_targets))
        previously_seen = self._input_seen(configuration.input_digest)
        new_input = not previously_seen
        if configuration.mode is ScenarioMode.REVIEW_REPLAY:
            new_input = False
        payload = {
            "scenario_run_id": scenario_run_id,
            "clean": clean,
            "new_input": new_input,
            "input_digest": configuration.input_digest,
            "checks": [item.to_dict() for item in checks],
        }
        return PreflightReceipt(
            receipt_id=new_identity("preflight"),
            scenario_run_id=scenario_run_id,
            clean=clean,
            new_input=new_input,
            input_digest=configuration.input_digest,
            checks=checks,
            checked_at=utc_now(),
            receipt_digest=digest(payload),
        )

    def require_formal_admission(
        self,
        scenario_run_id: str,
        configuration: ScenarioConfiguration,
    ) -> PreflightReceipt:
        receipt = self.inspect(scenario_run_id, configuration)
        if configuration.mode is not ScenarioMode.SEALED:
            return receipt
        dirty = [item for item in receipt.checks if not item.clean]
        if dirty:
            raise conflict(
                "scenario_preflight_dirty",
                "Formal scenario start rejected dirty state.",
                phase="preflight",
                detail={
                    "receipt": receipt.to_dict(),
                    "dirty_kinds": [item.kind for item in dirty],
                },
            )
        if not receipt.new_input:
            raise conflict(
                "scenario_input_replayed",
                "Formal scenario input was already admitted; use review replay separately.",
                phase="preflight",
                detail={"input_digest": configuration.input_digest},
            )
        return receipt

    def inspect_target(self, target: PreflightTarget) -> PreflightCheck:
        selected = Path(target.path)
        checked_at = utc_now()
        try:
            if target.policy is PreflightPolicy.ABSENT_FILE:
                return self._absent_file(target, selected, checked_at)
            if target.policy is PreflightPolicy.EMPTY_DIRECTORY:
                return self._empty_directory(target, selected, checked_at, absent_is_clean=False)
            if target.policy is PreflightPolicy.ABSENT_OR_EMPTY:
                return self._empty_directory(target, selected, checked_at, absent_is_clean=True)
            if target.policy is PreflightPolicy.SQLITE_NO_USER_ROWS:
                return self._sqlite_no_rows(target, selected, checked_at)
        except PermissionError as error:
            return self._failed_check(
                target,
                checked_at,
                "permission_denied",
                {"message": str(error)},
            )
        except OSError as error:
            return self._failed_check(
                target,
                checked_at,
                "path_inspection_failed",
                {"message": str(error)},
            )
        raise invalid(
            "scenario_preflight_policy_unsupported",
            "Preflight target uses an unsupported policy.",
            phase="preflight",
        )

    def _absent_file(
        self,
        target: PreflightTarget,
        path: Path,
        checked_at: str,
    ) -> PreflightCheck:
        exists = path.exists()
        is_file = path.is_file() if exists else False
        size = path.stat().st_size if is_file else 0
        clean = not exists
        reason = "path_absent" if clean else "file_exists" if is_file else "path_exists"
        fingerprint = digest(
            {
                "path": str(path),
                "exists": exists,
                "is_file": is_file,
                "size": size,
            }
        )
        return PreflightCheck(
            check_id=new_identity("check"),
            kind=target.kind.value,
            path=str(path),
            policy=target.policy.value,
            clean=clean,
            observed_entries=1 if exists else 0,
            digest=fingerprint,
            reason=reason,
            checked_at=checked_at,
            detail={"exists": exists, "is_file": is_file, "size": size},
        )

    def _empty_directory(
        self,
        target: PreflightTarget,
        path: Path,
        checked_at: str,
        *,
        absent_is_clean: bool,
    ) -> PreflightCheck:
        if not path.exists():
            return PreflightCheck(
                check_id=new_identity("check"),
                kind=target.kind.value,
                path=str(path),
                policy=target.policy.value,
                clean=absent_is_clean,
                observed_entries=0,
                digest=digest({"path": str(path), "exists": False}),
                reason="path_absent" if absent_is_clean else "directory_missing",
                checked_at=checked_at,
                detail={"exists": False},
            )
        if not path.is_dir():
            return self._failed_check(
                target,
                checked_at,
                "not_a_directory",
                {"exists": True, "is_file": path.is_file()},
            )
        ignored = set(target.ignored_names)
        observed: list[dict[str, Any]] = []
        for index, child in enumerate(sorted(path.iterdir(), key=lambda item: item.name)):
            if child.name in ignored:
                continue
            if index >= self._maximum_scan_entries:
                return self._failed_check(
                    target,
                    checked_at,
                    "scan_limit_exceeded",
                    {"maximum": self._maximum_scan_entries},
                )
            stat = child.stat()
            observed.append(
                {
                    "name": child.name,
                    "kind": "directory" if child.is_dir() else "file",
                    "size": stat.st_size,
                    "modified_ns": stat.st_mtime_ns,
                }
            )
        return PreflightCheck(
            check_id=new_identity("check"),
            kind=target.kind.value,
            path=str(path),
            policy=target.policy.value,
            clean=not observed,
            observed_entries=len(observed),
            digest=digest(observed),
            reason="directory_empty" if not observed else "directory_dirty",
            checked_at=checked_at,
            detail={"entries": observed[:100], "truncated": len(observed) > 100},
        )

    def _sqlite_no_rows(
        self,
        target: PreflightTarget,
        path: Path,
        checked_at: str,
    ) -> PreflightCheck:
        if not path.exists():
            return PreflightCheck(
                check_id=new_identity("check"),
                kind=target.kind.value,
                path=str(path),
                policy=target.policy.value,
                clean=True,
                observed_entries=0,
                digest=digest({"path": str(path), "exists": False}),
                reason="database_absent",
                checked_at=checked_at,
                detail={"exists": False},
            )
        if not path.is_file():
            return self._failed_check(target, checked_at, "database_not_file", {})
        uri = path.resolve().as_uri() + "?mode=ro"
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=2)
            tables = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            counts: dict[str, int] = {}
            total = 0
            for table in tables:
                quoted = table.replace('"', '""')
                count = int(connection.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0])
                counts[table] = count
                total += count
            checksum, size = file_digest(path)
            return PreflightCheck(
                check_id=new_identity("check"),
                kind=target.kind.value,
                path=str(path),
                policy=target.policy.value,
                clean=total == 0,
                observed_entries=total,
                digest=checksum,
                reason="database_empty" if total == 0 else "database_has_rows",
                checked_at=checked_at,
                detail={"tables": counts, "size": size},
            )
        except sqlite3.DatabaseError as error:
            return self._failed_check(
                target,
                checked_at,
                "database_unreadable",
                {"message": str(error)},
            )
        finally:
            if connection is not None:
                connection.close()

    def _failed_check(
        self,
        target: PreflightTarget,
        checked_at: str,
        reason: str,
        detail: dict[str, Any],
    ) -> PreflightCheck:
        return PreflightCheck(
            check_id=new_identity("check"),
            kind=target.kind.value,
            path=target.path,
            policy=target.policy.value,
            clean=False,
            observed_entries=0,
            digest=digest({"reason": reason, "detail": detail}),
            reason=reason,
            checked_at=checked_at,
            detail=detail,
        )


def ensure_scratch_roots(targets: Iterable[PreflightTarget]) -> tuple[str, ...]:
    created: list[str] = []
    for target in targets:
        path = Path(target.path)
        if target.kind.value == "database":
            parent = path.parent
        else:
            parent = path
        if parent.exists():
            continue
        parent.mkdir(parents=True, exist_ok=False)
        created.append(str(parent))
    return tuple(created)


def environment_preflight_guards() -> None:
    if os.environ.get("ZYRA_SCENARIO_RUNNER_DISABLED", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise unavailable(
            "scenario_runner_disabled",
            "Scenario runner is disabled; no demo or replay fallback is available.",
            phase="preflight",
        )
