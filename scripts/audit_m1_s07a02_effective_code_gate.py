from __future__ import annotations

import argparse
import ast
import io
import json
import re
import subprocess
import tokenize
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SLICE_BASELINE = "8981599da7312f2bfba7b3303498efebb8339e39"
PARENT_BASELINE = "f2db58c4ab4726f5d3ec2878c12114a3a2dc0e9b"
IMPLEMENTATION = "16f24689c1817a1f74fec9afb451d24b97983670"
PARENT_IMPLEMENTATION_COMMITS = {
    "83251b198abe88e6b848f1095b6faed1005cd39d",
    "150389567046f0cf43205b2e649e74f8543421ae",
    IMPLEMENTATION,
}

BUCKETS = (
    "production_runtime",
    "ui_behavior",
    "ui_presentation",
    "type_declaration",
    "schema_dto_data",
    "adapter_only",
    "generated",
    "test_mock_fixture",
    "nonproduction_tooling",
    "docs_comments_blank",
    "unrelated_scope",
)
EFFECTIVE_BUCKETS = {"production_runtime", "ui_behavior"}


def git(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode:
        raise RuntimeError(
            f"git {' '.join(arguments)} failed: {completed.stderr.strip()}"
        )
    return completed.stdout


def target_text(target: str, path: str) -> str:
    return git("show", f"{target}:{path}")


def added_target_lines(base: str, target: str) -> dict[str, set[int]]:
    output = git("diff", "--unified=0", "--no-ext-diff", base, target)
    result: dict[str, set[int]] = {}
    current_path = ""
    target_line = 0
    for raw in output.splitlines():
        if raw.startswith("+++ b/"):
            current_path = raw[6:]
            result.setdefault(current_path, set())
            continue
        if raw.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", raw)
            if not match:
                raise RuntimeError(f"cannot parse hunk header: {raw}")
            target_line = int(match.group(1))
            continue
        if not current_path or raw.startswith("diff --git"):
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            result[current_path].add(target_line)
            target_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue
        elif raw.startswith("\\"):
            continue
        else:
            target_line += 1
    return {path: lines for path, lines in result.items() if lines}


def blame_commits(target: str, path: str) -> dict[int, str]:
    output = git("blame", "--line-porcelain", target, "--", path)
    commits: dict[int, str] = {}
    current_commit = ""
    current_line = 0
    for raw in output.splitlines():
        header = re.match(r"^([0-9a-f^]{40}) \d+ (\d+)(?: \d+)?$", raw)
        if header:
            current_commit = header.group(1).lstrip("^")
            current_line = int(header.group(2))
        elif raw.startswith("\t"):
            commits[current_line] = current_commit
            current_line += 1
    return commits


def language(path: str) -> str:
    suffix = PurePosixPath(path).suffix.casefold()
    return {
        ".py": "python",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".json": "json",
        ".md": "markdown",
    }.get(suffix, suffix.lstrip(".") or "unknown")


def source_role(path: str) -> str:
    normalized = path.casefold()
    if "/data/" in normalized or normalized.startswith("docs/"):
        return "evidence_or_data"
    if normalized.startswith("scripts/"):
        return "remediation_tooling"
    if "/test/" in normalized or normalized.startswith("tests/"):
        return "mixed_behavior_verification"
    if "omp-worker-control" in normalized and normalized.endswith((".ts", ".tsx")):
        return "oh-my-pi_supplementary_same_language"
    if normalized == "packages/workers/zyra_workers/edge_pool/integration.py":
        return "oh-my-pi_supplementary_protocol_adapter"
    if normalized.startswith("packages/scheduler/"):
        return "agentscope_primary_same_language"
    if normalized.startswith("apps/api/"):
        return "zyra_integration_of_agentscope_primary_and_omp_projection"
    return "zyra_supporting"


def python_metadata(text: str) -> tuple[set[int], set[int], set[int]]:
    type_lines: set[int] = set()
    schema_lines: set[int] = set()
    docs_lines: set[int] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError as error:
        raise RuntimeError(f"cannot parse Python target: {error}") from error

    def mark(node: ast.AST, destination: set[int]) -> None:
        start = getattr(node, "lineno", 0)
        end = getattr(node, "end_lineno", start)
        destination.update(range(start, end + 1))

    def mark_docstring(body: Sequence[ast.stmt]) -> None:
        if body and isinstance(body[0], ast.Expr):
            value = body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                mark(body[0], docs_lines)

    mark_docstring(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            mark_docstring(node.body)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = node.module if isinstance(node, ast.ImportFrom) else ""
            names = {alias.name for alias in node.names}
            if module == "typing" or "typing" in names:
                mark(node, type_lines)
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
            mark(node, type_lines)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            normalized = node.value.upper()
            if "CREATE TABLE" in normalized or "CREATE INDEX" in normalized:
                mark(node, schema_lines)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets: list[ast.expr] = (
                list(node.targets) if isinstance(node, ast.Assign) else [node.target]
            )
            names = {
                target.id
                for target in targets
                if isinstance(target, ast.Name)
            }
            if any("SCHEMA" in name or name.endswith("_VERSION") for name in names):
                mark(node, schema_lines)

    for node in (item for item in ast.walk(tree) if isinstance(item, ast.ClassDef)):
        bases = {
            (base.id if isinstance(base, ast.Name) else base.attr if isinstance(base, ast.Attribute) else "")
            for base in node.bases
        }
        decorators = {
            (item.id if isinstance(item, ast.Name) else item.func.id if isinstance(item, ast.Call) and isinstance(item.func, ast.Name) else "")
            for item in node.decorator_list
        }
        if "Protocol" in bases:
            mark(node, type_lines)
            for decorator in node.decorator_list:
                mark(decorator, type_lines)
            continue
        schema_class = bool(bases & {"Enum", "IntEnum", "StrEnum"}) or "dataclass" in decorators
        if not schema_class:
            continue
        schema_lines.add(node.lineno)
        for decorator in node.decorator_list:
            mark(decorator, schema_lines)
        for child in node.body:
            if isinstance(child, (ast.Assign, ast.AnnAssign)):
                mark(child, schema_lines)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name in {
                "to_dict",
                "from_dict",
                "model_dump",
                "model_validate",
            }:
                mark(child, schema_lines)
                for decorator in child.decorator_list:
                    mark(decorator, schema_lines)
            elif isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant) and isinstance(child.value.value, str):
                mark(child, docs_lines)

    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT:
                docs_lines.add(token.start[0])
    except tokenize.TokenError:
        pass
    return type_lines, schema_lines, docs_lines


def typescript_metadata(lines: Sequence[str]) -> tuple[set[int], set[int]]:
    type_lines: set[int] = set()
    docs_lines: set[int] = set()
    in_block_comment = False
    in_type = False
    brace_depth = 0
    type_kind = ""
    for number, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if in_block_comment:
            docs_lines.add(number)
            if "*/" in stripped:
                in_block_comment = False
            continue
        if stripped.startswith("/*"):
            docs_lines.add(number)
            if "*/" not in stripped:
                in_block_comment = True
            continue
        if stripped.startswith("//"):
            docs_lines.add(number)
            continue
        if in_type:
            type_lines.add(number)
            if type_kind == "interface":
                brace_depth += raw.count("{") - raw.count("}")
                if brace_depth <= 0 and "}" in raw:
                    in_type = False
            elif ";" in raw:
                in_type = False
            continue
        if re.match(r"^(export\s+)?interface\s+", stripped):
            type_lines.add(number)
            in_type = True
            type_kind = "interface"
            brace_depth = raw.count("{") - raw.count("}")
            if brace_depth <= 0 and "}" in raw:
                in_type = False
            continue
        if re.match(r"^(export\s+)?type\s+", stripped):
            type_lines.add(number)
            in_type = ";" not in raw
            type_kind = "type"
            continue
        if stripped.startswith("import type ") or stripped.startswith("declare "):
            type_lines.add(number)
        elif re.match(r"^type\s+[A-Za-z_]", stripped):
            type_lines.add(number)
        elif re.match(r"^\s*type\s+[A-Za-z_][A-Za-z0-9_]*[,]?$", raw):
            type_lines.add(number)
    return type_lines, docs_lines


def file_buckets(
    *,
    target: str,
    path: str,
    additions: set[int],
    allowed_commits: set[str] | None,
) -> dict[str, Any]:
    text = target_text(target, path)
    lines = text.splitlines()
    suffix = PurePosixPath(path).suffix.casefold()
    normalized = path.casefold()
    counters: Counter[str] = Counter({bucket: 0 for bucket in BUCKETS})
    blame = blame_commits(target, path) if allowed_commits is not None else {}
    py_type: set[int] = set()
    py_schema: set[int] = set()
    docs: set[int] = set()
    ts_type: set[int] = set()
    if suffix == ".py":
        py_type, py_schema, docs = python_metadata(text)
    elif suffix in {".ts", ".tsx"}:
        ts_type, docs = typescript_metadata(lines)

    for number in sorted(additions):
        raw = lines[number - 1] if 0 < number <= len(lines) else ""
        stripped = raw.strip()
        if allowed_commits is not None and blame.get(number) not in allowed_commits:
            category = "unrelated_scope"
        elif normalized.startswith("tests/") or "/test/" in normalized or "/tests/" in normalized:
            category = "test_mock_fixture"
        elif normalized.startswith("scripts/"):
            category = "nonproduction_tooling"
        elif normalized.startswith("docs/") or "/data/" in normalized or suffix in {".json", ".yaml", ".yml", ".csv", ".md"}:
            category = "schema_dto_data"
        elif not stripped or number in docs:
            category = "docs_comments_blank"
        elif suffix == ".py" and PurePosixPath(path).name == "__init__.py":
            category = "type_declaration"
        elif suffix in {".ts", ".tsx"} and PurePosixPath(path).name == "index.ts":
            category = "type_declaration"
        elif number in py_type or number in ts_type or suffix == ".d.ts":
            category = "type_declaration"
        elif number in py_schema:
            category = "schema_dto_data"
        elif normalized == "packages/workers/zyra_workers/edge_pool/integration.py":
            category = "adapter_only"
        else:
            category = "production_runtime"
        counters[category] += 1

    raw_additions = len(additions)
    if sum(counters.values()) != raw_additions:
        raise AssertionError(f"bucket mismatch for {path}")
    return {
        "path": path,
        "language": language(path),
        "source_role": source_role(path),
        "raw_additions": raw_additions,
        **{bucket: counters[bucket] for bucket in BUCKETS},
        "effective_production": sum(counters[item] for item in EFFECTIVE_BUCKETS),
    }


def audit_range(
    *,
    base: str,
    target: str,
    allowed_commits: set[str] | None = None,
) -> dict[str, Any]:
    changes = added_target_lines(base, target)
    files = [
        file_buckets(
            target=target,
            path=path,
            additions=lines,
            allowed_commits=allowed_commits,
        )
        for path, lines in sorted(changes.items())
    ]
    totals: Counter[str] = Counter()
    for item in files:
        totals["raw_additions"] += int(item["raw_additions"])
        totals["effective_production"] += int(item["effective_production"])
        for bucket in BUCKETS:
            totals[bucket] += int(item[bucket])
    if totals["raw_additions"] != sum(totals[bucket] for bucket in BUCKETS):
        raise AssertionError("range bucket total mismatch")
    by_role_language: dict[tuple[str, str], Counter[str]] = {}
    for item in files:
        key = (str(item["source_role"]), str(item["language"]))
        counter = by_role_language.setdefault(key, Counter())
        counter["raw_additions"] += int(item["raw_additions"])
        counter["production"] += int(item["effective_production"])
        counter["test"] += int(item["test_mock_fixture"])
        counter["adapter"] += int(item["adapter_only"])
        counter["generated_data_docs"] += (
            int(item["generated"])
            + int(item["schema_dto_data"])
            + int(item["type_declaration"])
            + int(item["docs_comments_blank"])
            + int(item["nonproduction_tooling"])
            + int(item["unrelated_scope"])
        )
    role_rows = [
        {"source_role": role, "language": lang, **dict(counter)}
        for (role, lang), counter in sorted(by_role_language.items())
    ]
    return {
        "base": base,
        "target": target,
        "allowed_implementation_commits": sorted(allowed_commits or []),
        "files": files,
        "totals": dict(totals),
        "by_source_role_and_language": role_rows,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scope", choices=("slice", "parent", "both"), default="both"
    )
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--fail-on-gate", action="store_true")
    arguments = parser.parse_args(argv)
    result: dict[str, Any] = {
        "schema": "zyra.effective-code-language-gate-audit/v1",
        "audit_tool_is_nonproduction": True,
        "method": {
            "raw": "target-side added lines from git diff --unified=0",
            "ownership": "git blame at frozen implementation target",
            "python": "AST/token classification of Protocol, dataclass/enum fields, DTO mapping, comments and runtime statements",
            "typescript": "type/interface/import-type/declaration and comment range classification",
            "effective": "production_runtime plus UI_behavior only",
        },
    }
    if arguments.scope in {"slice", "both"}:
        result["slice"] = audit_range(
            base=SLICE_BASELINE,
            target=IMPLEMENTATION,
        )
    if arguments.scope in {"parent", "both"}:
        result["parent"] = audit_range(
            base=PARENT_BASELINE,
            target=IMPLEMENTATION,
            allowed_commits=PARENT_IMPLEMENTATION_COMMITS,
        )
    if arguments.summary_only:
        for scope in ("slice", "parent"):
            report = result.get(scope)
            if isinstance(report, dict):
                report.pop("files", None)
    blockers: list[str] = []
    slice_report = result.get("slice")
    if isinstance(slice_report, Mapping):
        effective = int(slice_report["totals"]["effective_production"])
        if effective < 6000:
            blockers.append(f"slice effective production {effective} < 6000")
        roles = {
            (str(item["source_role"]), str(item["language"])): int(item["production"])
            for item in slice_report["by_source_role_and_language"]
        }
        if roles.get(("agentscope_primary_same_language", "python"), 0) <= 0:
            blockers.append("AgentScope primary Python effective production is zero")
        if roles.get(("oh-my-pi_supplementary_same_language", "typescript"), 0) <= 0:
            blockers.append("OMP supplementary TypeScript effective production is zero")
    parent_report = result.get("parent")
    if isinstance(parent_report, Mapping):
        effective = int(parent_report["totals"]["effective_production"])
        if effective < 15000:
            blockers.append(f"parent effective production {effective} < 15000")
    result["gate"] = {
        "ok": not blockers,
        "blockers": blockers,
        "slice_minimum": 6000,
        "parent_minimum": 15000,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if arguments.fail_on_gate and blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
