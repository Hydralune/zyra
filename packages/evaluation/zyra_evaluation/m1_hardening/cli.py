from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import HardeningContext
from .integration_service import IntegrationOptions, M1IntegrationService
from .reporting import ReportComparator
from .release_reporting import ReleaseReportBuilder, ReleaseReportStore
from .service import AuditOptions, M1HardeningService
from .store import HardeningStoreError, ReportIntegrityError, ReportNotFound


class CliError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zyra-m1-hardening",
        description="Run and inspect the Zyra M1-S08 hardening foundation gates.",
    )
    parser.add_argument("--project-root", default=".", help="Zyra repository root")
    parser.add_argument("--source-workspace", default="", help="workspace containing source-graphs")
    parser.add_argument("--artifact-root", default="", help="hardening artifact directory inside Zyra")
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit = subparsers.add_parser("audit", help="run the repository hardening audit")
    _add_audit_options(audit, scenario_default=False)
    audit.add_argument("--task-json", default="", help="optional task JSON input")
    audit.add_argument("--events-json", default="", help="optional event-array JSON input")

    scenario = subparsers.add_parser("scenario", help="run the real HTTP foundation scenario and audit it")
    _add_audit_options(scenario, scenario_default=True)
    scenario.add_argument("--base-url", required=True, help="running Zyra API base URL")
    scenario.add_argument("--goal", default="Exercise M1 hardening through the public API.")
    scenario.add_argument("--http-timeout", type=float, default=90.0)

    integration = subparsers.add_parser("integration", help="run the six-scenario M1 integration and exit gates")
    integration.add_argument("--base-url", required=True, help="running Zyra API base URL")
    integration.add_argument("--baseline", default="8065bac109a3bed9ba01e0e92392fec4d05bfca3")
    integration.add_argument("--implementation-commit", default="")
    integration.add_argument("--evidence-commit", default="")
    integration.add_argument("--scenario", action="append", default=[])
    integration.add_argument("--no-scenarios", action="store_true")
    integration.add_argument("--execute-disconnects", action="store_true")
    integration.add_argument("--final-completion", action="store_true")
    integration.add_argument("--run-cleanroom", action="store_true")
    integration.add_argument("--scenario-timeout", type=float, default=120.0)
    integration.add_argument("--benchmark-run-id", default="")
    integration.add_argument("--sealed-policy-json", default="")
    integration.add_argument("--line-evidence-json", default="")
    integration.add_argument("--tier-evidence-json", default="")
    integration.add_argument("--provider-evidence-json", default="")
    integration.add_argument("--evidence-envelopes-json", default="")
    integration.add_argument("--unresolved-requirement", action="append", default=[])
    integration.add_argument("--no-persist", action="store_true")

    status = subparsers.add_parser("status", help="show catalog and report-store status")
    status.add_argument("--compact", action="store_true")

    reports = subparsers.add_parser("reports", help="list persisted reports")
    reports.add_argument("--task-id", default="")
    reports.add_argument("--run-id", default="")
    reports.add_argument("--scenario-id", default="")
    reports.add_argument("--accepted-only", action="store_true")
    reports.add_argument("--limit", type=int, default=100)
    reports.add_argument("--offset", type=int, default=0)

    show = subparsers.add_parser("show", help="load and verify one report")
    show.add_argument("report_id")

    verify = subparsers.add_parser("verify", help="verify the append-only report integrity chain")
    verify.add_argument("--report-id", default="")

    compare = subparsers.add_parser("compare", help="compare two persisted reports")
    compare.add_argument("before_report_id")
    compare.add_argument("after_report_id")

    return parser


def _add_audit_options(parser: argparse.ArgumentParser, *, scenario_default: bool) -> None:
    parser.add_argument("--baseline", default="", help="slice baseline Git commit")
    parser.add_argument("--head", default="HEAD", help="line-audit target Git identity")
    parser.add_argument("--minimum-effective-lines", type=int, default=9000)
    parser.add_argument("--final-completion", action="store_true")
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--no-dynamic-graph-probes", action="store_true")
    parser.add_argument("--run-disable-probes", action="store_true")
    parser.add_argument("--disable-probe", action="append", default=[])
    parser.add_argument("--no-line-audit", action="store_true")
    parser.add_argument("--no-cross-cutting", action="store_true")
    parser.add_argument("--include-scenario", action="store_true", default=scenario_default)
    parser.add_argument("--gate-timeout", type=float, default=180.0)
    parser.add_argument("--audit-lease", type=float, default=1800.0)
    parser.add_argument("--sealed-policy-json", default="")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        project_root = Path(args.project_root).resolve()
        source_workspace = Path(args.source_workspace).resolve() if args.source_workspace else project_root.parent
        artifact_root = Path(args.artifact_root).resolve() if args.artifact_root else project_root / ".tmp" / "m1-hardening"
        service = M1HardeningService(
            project_root,
            source_workspace=source_workspace,
            artifact_root=artifact_root,
        )
        if args.command == "audit":
            return _audit(service, args)
        if args.command == "scenario":
            return _scenario(service, args)
        if args.command == "integration":
            integration_service = M1IntegrationService(
                project_root,
                source_workspace=source_workspace,
                artifact_root=artifact_root,
                foundation_service=service,
            )
            return _integration(integration_service, args)
        if args.command == "status":
            _write_json(service.repository_status(), compact=args.compact)
            return 0
        if args.command == "reports":
            records = service.store.list_records(
                task_id=args.task_id,
                run_id=args.run_id,
                scenario_id=args.scenario_id,
                accepted_only=args.accepted_only,
                limit=args.limit,
                offset=args.offset,
            )
            _write_json(
                {
                    "schema": "zyra.m1-hardening-cli-reports/v1",
                    "count": len(records),
                    "reports": [item.to_dict() for item in records],
                }
            )
            return 0
        if args.command == "show":
            _write_json(service.store.load(args.report_id, verify=True))
            return 0
        if args.command == "verify":
            if args.report_id:
                report = service.store.load(args.report_id, verify=True)
                _write_json(
                    {
                        "ok": True,
                        "report_id": report.get("report_id"),
                        "content_digest": (report.get("storage") or {}).get("content_digest"),
                    }
                )
                return 0
            chain = service.store.verify_chain()
            _write_json(chain)
            return 0 if chain["valid"] else 3
        if args.command == "compare":
            before = service.store.load(args.before_report_id, verify=True)
            after = service.store.load(args.after_report_id, verify=True)
            comparison = ReportComparator().compare(before, after)
            _write_json(comparison)
            return 0 if comparison["compatible"] else 3
        raise CliError(f"unsupported command: {args.command}")
    except (CliError, ValueError, OSError, HardeningStoreError) as error:
        _write_error(type(error).__name__, str(error))
        return 4


def _audit(service: M1HardeningService, args: argparse.Namespace) -> int:
    task = _load_object(args.task_json) if args.task_json else None
    events = _load_array(args.events_json) if args.events_json else []
    if task is None and events:
        raise CliError("--events-json requires --task-json")
    options = _options(service, args)
    if task is not None:
        outcome = service.evaluate_existing_task(task, events, options)
    else:
        outcome = service.audit(
            HardeningContext(
                project_root=service.root,
                workspace_root=service.source_workspace,
                artifact_root=service.artifact_root,
            ),
            options,
        )
    _write_json(outcome.to_dict(include_scenario=False))
    return 0 if outcome.accepted else 2


def _scenario(service: M1HardeningService, args: argparse.Namespace) -> int:
    if args.http_timeout <= 0 or args.http_timeout > 1800:
        raise CliError("--http-timeout must be between 0 and 1800 seconds")
    outcome = service.run_http_foundation(
        args.base_url,
        _options(service, args),
        goal=args.goal,
        timeout_seconds=args.http_timeout,
    )
    _write_json(outcome.to_dict(include_scenario=True))
    return 0 if outcome.accepted else 2


def _integration(service: M1IntegrationService, args: argparse.Namespace) -> int:
    if args.scenario_timeout <= 0 or args.scenario_timeout > 1800:
        raise CliError("--scenario-timeout must be between 0 and 1800 seconds")
    policy = _json_argument(args.sealed_policy_json, expected=dict) if args.sealed_policy_json else {}
    line_evidence = tuple(_load_array(args.line_evidence_json)) if args.line_evidence_json else ()
    tier_evidence = tuple(_load_array(args.tier_evidence_json)) if args.tier_evidence_json else ()
    provider_evidence = tuple(_load_array(args.provider_evidence_json)) if args.provider_evidence_json else ()
    evidence_envelopes = tuple(_load_array(args.evidence_envelopes_json)) if args.evidence_envelopes_json else ()
    implementation_commit = str(args.implementation_commit or "")
    if implementation_commit:
        implementation_commit = _git_identity(service.root, implementation_commit)
    evidence_commit = str(args.evidence_commit or "")
    if evidence_commit:
        evidence_commit = _git_identity(service.root, evidence_commit)
    options = IntegrationOptions(
        baseline_commit=_git_identity(service.root, args.baseline),
        implementation_commit=implementation_commit,
        evidence_commit=evidence_commit,
        scenario_ids=tuple(args.scenario),
        execute_scenarios=not args.no_scenarios,
        execute_disconnects=bool(args.execute_disconnects),
        final_completion=bool(args.final_completion),
        run_cleanroom=bool(args.run_cleanroom),
        scenario_timeout_seconds=float(args.scenario_timeout),
        benchmark_run_id=str(args.benchmark_run_id or ""),
        sealed_policy=policy,
        line_evidence=line_evidence,
        tier_observations=tier_evidence,
        provider_observations=provider_evidence,
        evidence_envelopes=evidence_envelopes,
        unresolved_requirements=tuple(args.unresolved_requirement),
        persist=not args.no_persist,
    )
    outcome = service.execute(args.base_url, options)
    if options.persist:
        report = ReleaseReportBuilder().build(outcome)
        paths = ReleaseReportStore(service.artifact_root / "release").persist(report)
        outcome.artifact_paths.extend((paths["json"], paths["markdown"]))
    _write_json(outcome.to_dict(include_scenario_events=False))
    return 0 if outcome.accepted else 2


def _options(service: M1HardeningService, args: argparse.Namespace) -> AuditOptions:
    baseline = str(args.baseline or _git_identity(service.root, "HEAD~1"))
    policy = _json_argument(args.sealed_policy_json, expected=dict) if args.sealed_policy_json else {}
    options = AuditOptions(
        baseline_commit=baseline,
        final_completion=bool(args.final_completion),
        persist=not args.no_persist,
        run_dynamic_graph_probes=not args.no_dynamic_graph_probes,
        run_disable_probes=bool(args.run_disable_probes),
        selected_disable_probe_ids=tuple(args.disable_probe),
        minimum_effective_lines=args.minimum_effective_lines,
        line_audit_head=args.head,
        include_line_audit=not args.no_line_audit,
        include_cross_cutting=not args.no_cross_cutting,
        include_scenario=bool(args.include_scenario),
        gate_timeout_seconds=args.gate_timeout,
        audit_lease_seconds=args.audit_lease,
        sealed_policy=policy,
    )
    options.validate()
    return options


def _load_object(path_value: str) -> Mapping[str, Any]:
    value = _load_json(path_value)
    if not isinstance(value, Mapping):
        raise CliError(f"expected JSON object: {path_value}")
    return value


def _load_array(path_value: str) -> list[Mapping[str, Any]]:
    value = _load_json(path_value)
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise CliError(f"expected array of JSON objects: {path_value}")
    return value


def _load_json(path_value: str) -> Any:
    path = Path(path_value).resolve()
    if not path.is_file():
        raise CliError(f"JSON input does not exist: {path}")
    if path.stat().st_size > 512 * 1024 * 1024:
        raise CliError(f"JSON input exceeds 512 MiB: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise CliError(f"cannot decode {path}: {error}") from error


def _json_argument(value: str, *, expected: type) -> Any:
    try:
        result = json.loads(value)
    except json.JSONDecodeError as error:
        raise CliError(f"invalid JSON argument: {error}") from error
    if not isinstance(result, expected):
        raise CliError(f"JSON argument must decode to {expected.__name__}")
    return result


def _git_identity(root: Path, expression: str) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", expression],
        cwd=root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if completed.returncode != 0:
        raise CliError(f"cannot resolve Git identity {expression!r}: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _write_json(value: Any, *, compact: bool = False) -> None:
    if compact:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    sys.stdout.write(text + "\n")


def _write_error(code: str, message: str) -> None:
    sys.stderr.write(
        json.dumps(
            {
                "schema": "zyra.m1-hardening-cli-error/v1",
                "ok": False,
                "error": code,
                "message": message,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
    )


if __name__ == "__main__":
    raise SystemExit(main())
