from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVALUATION_ROOT = PROJECT_ROOT / "packages" / "evaluation"
if str(EVALUATION_ROOT) not in sys.path:
    sys.path.insert(0, str(EVALUATION_ROOT))

from zyra_evaluation.policy_benchmark.mechanism_readiness import (
    build_mechanism_readiness_report,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Audit frozen Phase 1 evidence for deterministic Phase 2 mechanism "
            "input and causal-evidence readiness."
        )
    )
    result.add_argument(
        "--baseline-manifest",
        default="docs/release/phase2-baseline-manifest.json",
    )
    result.add_argument(
        "--config",
        default="config/phase2/mechanism-readiness.json",
    )
    result.add_argument(
        "--output",
        default=(
            "docs/reviews/phase2/MechanismEvidenceReadinessReport.json"
        ),
    )
    result.add_argument(
        "--implementation-commit",
        default="",
        help="Implementation commit to bind; defaults to the current Git HEAD.",
    )
    return result


def _repository_path(value: str, *, must_exist: bool) -> Path:
    selected = Path(value)
    target = selected.resolve() if selected.is_absolute() else (PROJECT_ROOT / selected).resolve()
    try:
        target.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise ValueError(f"path must remain inside the Zyra repository: {value}") from exc
    if must_exist and not target.is_file():
        raise ValueError(f"required file is missing: {target}")
    return target


def _head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_report(path: Path, report: dict[str, object]) -> None:
    encoded = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(path)


def run(arguments: list[str] | None = None) -> int:
    options = parser().parse_args(arguments)
    baseline = _repository_path(options.baseline_manifest, must_exist=True)
    config = _repository_path(options.config, must_exist=True)
    output = _repository_path(options.output, must_exist=False)
    implementation_commit = options.implementation_commit or _head()
    report = build_mechanism_readiness_report(
        PROJECT_ROOT,
        baseline_manifest=baseline,
        config_path=config,
        implementation_commit=implementation_commit,
    )
    _write_report(output, report)
    summary = {
        "schema": report["schema"],
        "valid": report["valid"],
        "readiness_stage": report["readiness_stage"],
        "implementation_commit": report["implementation_commit"],
        "report_digest": report["report_digest"],
        "evidence_index_digest": report["evidence_index"]["index_digest"],
        "mechanism_statuses": report["mechanism_statuses"],
        "training_sample_count": report["training_sample_count"],
        "output": output.relative_to(PROJECT_ROOT).as_posix(),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["valid"] is True else 2


def main() -> None:
    try:
        raise SystemExit(run())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(
            json.dumps(
                {
                    "schema": "zyra.mechanism-readiness-audit-error/v1",
                    "valid": False,
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
