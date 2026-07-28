from __future__ import annotations

import ast
import io
import json
import tokenize
from pathlib import Path
from typing import Any


MINIMUM_EFFECTIVE_LINES = 4_500
PACKAGE = Path("packages/evaluation/zyra_evaluation/freeze_reporting")

PRODUCTION_LOGIC = {
    "ablation.py",
    "algorithms.py",
    "archive.py",
    "canonical.py",
    "cases.py",
    "compatibility.py",
    "evidence_index.py",
    "inputs.py",
    "langgraph.py",
    "ledger.py",
    "links.py",
    "pipeline.py",
    "replay.py",
    "report.py",
    "scoring.py",
    "value.py",
}

EXCLUDED_BUCKETS = {
    "interface-and-contract": {
        "contracts.py",
        "errors.py",
    },
    "wrapper-and-entrypoint": {
        "__init__.py",
        "__main__.py",
        "cli.py",
    },
    "mapping-data": {
        "sources.py",
    },
}

FORBIDDEN_RUNTIME_MARKERS = (
    "../openclaw",
    "../claude-code-best",
    "../browser-use",
    "../openhands",
    "../opencode",
    "g:\\agent-zoo\\openclaw",
    "g:\\agent-zoo\\claude-code-best",
)


def audit(repository_root: str | Path) -> dict[str, Any]:
    root = Path(repository_root).resolve(strict=True)
    package = root / PACKAGE
    production_rows = []
    excluded_rows = []
    blockers = []
    observed = {path.name for path in package.glob("*.py")}
    missing = sorted(PRODUCTION_LOGIC - observed)
    if missing:
        blockers.append(
            {
                "code": "production-files-missing",
                "paths": missing,
            }
        )
    for filename in sorted(PRODUCTION_LOGIC):
        path = package / filename
        if not path.is_file():
            continue
        count = effective_python_lines(path)
        production_rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "effective_lines": count,
            }
        )
        blockers.extend(_forbidden_markers(path, root))
    for bucket, filenames in sorted(EXCLUDED_BUCKETS.items()):
        for filename in sorted(filenames):
            path = package / filename
            if not path.is_file():
                continue
            excluded_rows.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "bucket": bucket,
                    "physical_lines": len(
                        path.read_text(encoding="utf-8").splitlines()
                    ),
                    "effective_lines": 0,
                }
            )
            blockers.extend(_forbidden_markers(path, root))
    production_total = sum(
        int(item["effective_lines"]) for item in production_rows
    )
    if production_total < MINIMUM_EFFECTIVE_LINES:
        blockers.append(
            {
                "code": "minimum-effective-lines-not-met",
                "minimum": MINIMUM_EFFECTIVE_LINES,
                "observed": production_total,
            }
        )
    receipt = {
        "schema": "zyra.m3-s03-01-effective-line-audit/v1",
        "valid": not blockers,
        "minimum_effective_lines": MINIMUM_EFFECTIVE_LINES,
        "production_effective_lines": production_total,
        "production": production_rows,
        "excluded": excluded_rows,
        "excluded_categories": [
            "tests",
            "generated report/JSON/Markdown/archive",
            "documentation and review evidence",
            "DTO/type/interface-only code",
            "thin CLI and entrypoint wrappers",
            "mapping/data tables",
            "mock/fixture-only code",
            "vendor/source-pool code",
        ],
        "blockers": blockers,
    }
    return receipt


def effective_python_lines(path: Path) -> int:
    """Count physical code lines, excluding comments, blanks, and docstrings."""

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    docstring_lines: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstring_lines.update(
                range(body[0].lineno, body[0].end_lineno + 1)
            )
    code_lines: set[int] = set()
    ignored_tokens = {
        tokenize.ENCODING,
        tokenize.ENDMARKER,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.COMMENT,
    }
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in ignored_tokens:
            continue
        if token.start[0] in docstring_lines:
            continue
        code_lines.add(token.start[0])
    return len(code_lines)


def _forbidden_markers(path: Path, root: Path) -> list[dict[str, Any]]:
    lowered = path.read_text(encoding="utf-8").lower()
    return [
        {
            "code": "forbidden-source-runtime-dependency",
            "path": path.relative_to(root).as_posix(),
            "marker": marker,
        }
        for marker in FORBIDDEN_RUNTIME_MARKERS
        if marker in lowered
    ]


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    receipt = audit(root)
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if receipt["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
