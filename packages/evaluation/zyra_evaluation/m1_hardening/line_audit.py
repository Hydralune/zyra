from __future__ import annotations

import ast
import re
import subprocess
import tokenize
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .contracts import Finding, GateResult, GateStatus, Severity


class LineBucket:
    PRODUCTION = "production"
    TEST = "test"
    GENERATED = "generated"
    DATA = "data"
    DOCS = "docs"
    VENDOR = "vendor_like_source_pool"
    ADAPTER = "adapter_only"
    MOCK = "mock_fixture"
    SCRIPT = "productized_script"
    OTHER = "other"


@dataclass(slots=True)
class FileLineAudit:
    path: str
    bucket: str
    language: str
    added_lines: int
    deleted_lines: int
    effective_production_lines: int
    excluded_lines: int
    exclusions: Counter[str] = field(default_factory=Counter)
    added_line_numbers: tuple[int, ...] = ()
    source_role: str = "zyra_owned_hardening"
    migration_mode: str = "audit_and_hardening_only"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "bucket": self.bucket,
            "language": self.language,
            "added_lines": self.added_lines,
            "deleted_lines": self.deleted_lines,
            "effective_production_lines": self.effective_production_lines,
            "excluded_lines": self.excluded_lines,
            "exclusions": dict(sorted(self.exclusions.items())),
            "added_line_numbers": list(self.added_line_numbers),
            "source_role": self.source_role,
            "migration_mode": self.migration_mode,
        }


class GitDiffReader:
    def __init__(self, project_root: Path) -> None:
        self.root = project_root.resolve()

    def numstat(self, baseline: str, head: str = "HEAD") -> dict[str, tuple[int, int]]:
        output = self._git("diff", "--numstat", baseline, head, "--")
        result: dict[str, tuple[int, int]] = {}
        for line in output.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added_raw, deleted_raw, path = parts
            try:
                added = int(added_raw) if added_raw != "-" else 0
                deleted = int(deleted_raw) if deleted_raw != "-" else 0
            except ValueError:
                continue
            result[self._normalize_rename(path)] = (added, deleted)
        return result

    def added_lines(self, baseline: str, head: str = "HEAD") -> dict[str, set[int]]:
        output = self._git("diff", "--unified=0", "--no-color", baseline, head, "--")
        current = ""
        result: dict[str, set[int]] = defaultdict(set)
        next_line = 0
        for line in output.splitlines():
            if line.startswith("+++ b/"):
                current = line[6:]
                continue
            if line.startswith("+++ /dev/null"):
                current = ""
                continue
            hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if hunk:
                next_line = int(hunk.group(1))
                continue
            if not current or line.startswith("\\ No newline"):
                continue
            if line.startswith("+") and not line.startswith("+++"):
                result[current].add(next_line)
                next_line += 1
            elif line.startswith("-") and not line.startswith("---"):
                continue
            else:
                next_line += 1
        return dict(result)

    def status(self, baseline: str, head: str = "HEAD") -> dict[str, str]:
        output = self._git("diff", "--name-status", baseline, head, "--")
        result: dict[str, str] = {}
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                result[parts[-1]] = parts[0]
        return result

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        completed = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=self.root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        return completed.returncode == 0

    def _git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=self.root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
        return completed.stdout

    @staticmethod
    def _normalize_rename(value: str) -> str:
        if " => " not in value:
            return value
        if "{" in value and "}" in value:
            prefix, remainder = value.split("{", 1)
            middle, suffix = remainder.split("}", 1)
            _old, new = middle.split(" => ", 1)
            return f"{prefix}{new}{suffix}"
        return value.rsplit(" => ", 1)[-1]


class PythonEffectiveLineClassifier:
    def classify(self, text: str, added: set[int]) -> tuple[set[int], Counter[str]]:
        effective = set(added)
        exclusions: Counter[str] = Counter()
        self._exclude_blank_comments(text, effective, exclusions)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            exclusions["syntax_unclassified"] += len(effective)
            return set(), exclusions
        parents = self._parents(tree)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                self._exclude_node(node, effective, exclusions, "import")
            elif isinstance(node, ast.AnnAssign) and node.value is None:
                self._exclude_node(node, effective, exclusions, "annotation_only")
            elif isinstance(node, ast.Pass):
                self._exclude_node(node, effective, exclusions, "pass_only")
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                self._exclude_node(node, effective, exclusions, "docstring_or_literal")
            elif isinstance(node, ast.ClassDef):
                if self._is_protocol(node):
                    self._exclude_node(node, effective, exclusions, "protocol")
                elif self._is_schema_class(node):
                    self._exclude_schema_members(node, effective, exclusions)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if self._overload_only(node) or self._inside_protocol(node, parents):
                    self._exclude_node(node, effective, exclusions, "interface_signature")
                else:
                    self._exclude_signature(node, effective, exclusions)
            elif isinstance(node, (ast.arguments, ast.arg)):
                continue
        return effective, exclusions

    @staticmethod
    def _exclude_blank_comments(text: str, effective: set[int], exclusions: Counter[str]) -> None:
        lines = text.splitlines()
        comment_lines: set[int] = set()
        try:
            tokens = tokenize.generate_tokens(StringIO(text).readline)
            for token in tokens:
                if token.type == tokenize.COMMENT:
                    comment_lines.update(range(token.start[0], token.end[0] + 1))
        except tokenize.TokenError:
            pass
        for number in list(effective):
            line = lines[number - 1] if 0 < number <= len(lines) else ""
            if not line.strip():
                effective.remove(number)
                exclusions["blank"] += 1
            elif number in comment_lines and line.lstrip().startswith("#"):
                effective.remove(number)
                exclusions["comment"] += 1

    @staticmethod
    def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
        return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

    @staticmethod
    def _is_protocol(node: ast.ClassDef) -> bool:
        return any(
            isinstance(base, ast.Name) and base.id == "Protocol"
            or isinstance(base, ast.Attribute) and base.attr == "Protocol"
            for base in node.bases
        )

    @staticmethod
    def _is_schema_class(node: ast.ClassDef) -> bool:
        decorators = {
            decorator.id if isinstance(decorator, ast.Name) else decorator.func.id
            if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Name)
            else ""
            for decorator in node.decorator_list
        }
        dataclass_like = bool(decorators & {"dataclass", "define", "frozen"})
        pydantic_like = any(
            isinstance(base, ast.Name) and base.id in {"BaseModel", "TypedDict"}
            or isinstance(base, ast.Attribute) and base.attr in {"BaseModel", "TypedDict"}
            for base in node.bases
        )
        return dataclass_like or pydantic_like

    def _exclude_schema_members(
        self,
        node: ast.ClassDef,
        effective: set[int],
        exclusions: Counter[str],
    ) -> None:
        for item in node.body:
            if isinstance(item, ast.AnnAssign):
                self._exclude_node(item, effective, exclusions, "schema_dto_field")
            elif isinstance(item, ast.Assign) and all(isinstance(target, ast.Name) for target in item.targets):
                self._exclude_node(item, effective, exclusions, "schema_dto_field")
        header_end = node.body[0].lineno - 1 if node.body else node.lineno
        self._exclude_range(node.lineno, header_end, effective, exclusions, "schema_dto_header")

    @staticmethod
    def _overload_only(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        decorated = any(
            isinstance(decorator, ast.Name) and decorator.id in {"overload", "abstractmethod"}
            or isinstance(decorator, ast.Attribute) and decorator.attr in {"overload", "abstractmethod"}
            for decorator in node.decorator_list
        )
        body_only = all(
            isinstance(item, (ast.Pass, ast.Expr))
            and (not isinstance(item, ast.Expr) or isinstance(item.value, ast.Constant))
            for item in node.body
        )
        return decorated and body_only

    @staticmethod
    def _inside_protocol(node: ast.AST, parents: Mapping[ast.AST, ast.AST]) -> bool:
        parent = parents.get(node)
        return isinstance(parent, ast.ClassDef) and PythonEffectiveLineClassifier._is_protocol(parent)

    def _exclude_signature(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        effective: set[int],
        exclusions: Counter[str],
    ) -> None:
        first_body = node.body[0].lineno if node.body else node.end_lineno or node.lineno
        signature_end = max(node.lineno, first_body - 1)
        # Keep the def line when it also contains executable one-line body.
        if first_body == node.lineno:
            return
        self._exclude_range(node.lineno + 1, signature_end, effective, exclusions, "signature_continuation")

    def _exclude_node(
        self,
        node: ast.AST,
        effective: set[int],
        exclusions: Counter[str],
        reason: str,
    ) -> None:
        self._exclude_range(
            int(getattr(node, "lineno", 0)),
            int(getattr(node, "end_lineno", getattr(node, "lineno", 0))),
            effective,
            exclusions,
            reason,
        )

    @staticmethod
    def _exclude_range(
        start: int,
        end: int,
        effective: set[int],
        exclusions: Counter[str],
        reason: str,
    ) -> None:
        for number in range(max(1, start), max(start, end) + 1):
            if number in effective:
                effective.remove(number)
                exclusions[reason] += 1


class ScriptEffectiveLineClassifier:
    _BLOCK_START = re.compile(r"^\s*(?:export\s+)?(?:declare\s+)?(?:interface|type)\b")
    _DECLARE = re.compile(r"^\s*(?:export\s+)?declare\b")
    _IMPORT = re.compile(r"^\s*(?:import|export\s+\{[^}]*\}\s+from)\b")
    _COMMENT = re.compile(r"^\s*(?://|/\*|\*|\*/)")

    def classify(self, text: str, added: set[int]) -> tuple[set[int], Counter[str]]:
        effective = set(added)
        exclusions: Counter[str] = Counter()
        lines = text.splitlines()
        interface_depth = 0
        interface_mode = False
        for number, line in enumerate(lines, start=1):
            if number not in effective:
                if interface_mode:
                    interface_depth += line.count("{") - line.count("}")
                    if interface_depth <= 0 and (";" in line or "}" in line):
                        interface_mode = False
                continue
            stripped = line.strip()
            reason = ""
            if not stripped:
                reason = "blank"
            elif self._COMMENT.match(line):
                reason = "comment"
            elif self._IMPORT.match(line):
                reason = "import"
            elif self._DECLARE.match(line):
                reason = "declare"
            elif interface_mode:
                reason = "interface_or_type"
            elif self._BLOCK_START.match(line):
                reason = "interface_or_type"
                if "{" in line and ("}" not in line or line.index("{") > line.index("}")):
                    interface_mode = True
                    interface_depth = line.count("{") - line.count("}")
                elif "=" in line and ";" not in line:
                    interface_mode = True
                    interface_depth = line.count("{") - line.count("}")
            elif re.fullmatch(r"\s*[{}();,]+\s*", line):
                reason = "punctuation_only"
            if reason:
                effective.remove(number)
                exclusions[reason] += 1
            if interface_mode:
                interface_depth += line.count("{") - line.count("}") if reason != "interface_or_type" else 0
                if interface_depth <= 0 and (";" in line or "}" in line):
                    interface_mode = False
        return effective, exclusions


class EffectiveLineAuditor:
    def __init__(self, project_root: str | Path) -> None:
        self.root = Path(project_root).resolve()
        self.git = GitDiffReader(self.root)
        self.python = PythonEffectiveLineClassifier()
        self.script = ScriptEffectiveLineClassifier()

    def evaluate(
        self,
        baseline: str,
        *,
        head: str = "HEAD",
        minimum_effective_production: int = 0,
        protected_source_pool_commit: str = "",
    ) -> GateResult:
        result = GateResult(
            gate_id="effective-line-audit",
            status=GateStatus.NOT_RUN,
            summary="Per-file effective production line audit with explicit exclusions and buckets.",
        )
        numstat = self.git.numstat(baseline, head)
        added_lines = self.git.added_lines(baseline, head)
        audits: list[FileLineAudit] = []
        for path, (added, deleted) in sorted(numstat.items()):
            audits.append(self._audit_file(path, added, deleted, added_lines.get(path, set())))
        effective = sum(item.effective_production_lines for item in audits)
        bucket_raw: Counter[str] = Counter()
        bucket_effective: Counter[str] = Counter()
        exclusion_counts: Counter[str] = Counter()
        languages: Counter[str] = Counter()
        for item in audits:
            bucket_raw[item.bucket] += item.added_lines
            bucket_effective[item.bucket] += item.effective_production_lines
            exclusion_counts.update(item.exclusions)
            if item.effective_production_lines:
                languages[item.language] += item.effective_production_lines
        if minimum_effective_production and effective < minimum_effective_production:
            result.add(
                Finding(
                    code="line-audit.minimum_not_met",
                    severity=Severity.BLOCKER,
                    summary="Effective production line minimum is not met.",
                    detail=f"effective={effective}; required={minimum_effective_production}",
                )
            )
        protected_vendor_raw = 0
        unprotected_vendor_raw = bucket_raw[LineBucket.VENDOR]
        protection_valid = False
        if protected_source_pool_commit:
            protection_valid = self.git.is_ancestor(
                baseline,
                protected_source_pool_commit,
            ) and self.git.is_ancestor(protected_source_pool_commit, head)
            if protection_valid:
                protected_numstat = self.git.numstat(
                    baseline,
                    protected_source_pool_commit,
                )
                current_numstat = self.git.numstat(
                    protected_source_pool_commit,
                    head,
                )
                protected_vendor_raw = sum(
                    added
                    for path, (added, _deleted) in protected_numstat.items()
                    if self._bucket(path) == LineBucket.VENDOR
                )
                unprotected_vendor_raw = sum(
                    added
                    for path, (added, _deleted) in current_numstat.items()
                    if self._bucket(path) == LineBucket.VENDOR
                )
            else:
                result.add(
                    Finding(
                        code="line-audit.source_pool_protection_invalid",
                        severity=Severity.BLOCKER,
                        summary="Protected source-pool boundary is not on the audited commit ancestry.",
                        detail=(
                            f"baseline={baseline}; protected={protected_source_pool_commit}; "
                            f"head={head}"
                        ),
                    )
                )
        if unprotected_vendor_raw:
            result.add(
                Finding(
                    code="line-audit.vendor_source_added",
                    severity=Severity.BLOCKER,
                    summary="Vendor/source-pool content was added within the slice diff.",
                    detail=f"unprotected_raw={unprotected_vendor_raw}",
                )
            )
        elif protected_vendor_raw:
            result.add(
                Finding(
                    code="line-audit.protected_source_pool_excluded",
                    severity=Severity.WARNING,
                    summary="Protected historical source-pool content receives zero line credit.",
                    detail=(
                        f"protected_raw={protected_vendor_raw}; "
                        f"protected_commit={protected_source_pool_commit}"
                    ),
                )
            )
        if bucket_effective[LineBucket.ADAPTER] > 0:
            result.add(
                Finding(
                    code="line-audit.adapter_counted",
                    severity=Severity.BLOCKER,
                    summary="Adapter-only lines were counted as effective production.",
                )
            )
        result.metrics.update(
            {
                "baseline": baseline,
                "head": head,
                "minimum_effective_production": minimum_effective_production,
                "protected_source_pool_commit": protected_source_pool_commit,
                "source_pool_protection_valid": protection_valid,
                "protected_vendor_raw_added": protected_vendor_raw,
                "unprotected_vendor_raw_added": unprotected_vendor_raw,
                "effective_production_lines": effective,
                "raw_added_lines": sum(item.added_lines for item in audits),
                "raw_deleted_lines": sum(item.deleted_lines for item in audits),
                "bucket_raw_added": dict(sorted(bucket_raw.items())),
                "bucket_effective": dict(sorted(bucket_effective.items())),
                "exclusions": dict(sorted(exclusion_counts.items())),
                "effective_by_language": dict(sorted(languages.items())),
                "files": [item.to_dict() for item in audits],
            }
        )
        return result.finish()

    def _audit_file(
        self,
        path_value: str,
        added: int,
        deleted: int,
        line_numbers: set[int],
    ) -> FileLineAudit:
        path = self.root / path_value
        bucket = self._bucket(path_value)
        language = self._language(path.suffix.lower())
        countable = bucket in {LineBucket.PRODUCTION, LineBucket.SCRIPT}
        exclusions: Counter[str] = Counter()
        effective_lines: set[int] = set()
        if countable and path.is_file() and language in {"python", "typescript", "javascript", "rust"}:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                text = ""
            if language == "python":
                effective_lines, exclusions = self.python.classify(text, set(line_numbers))
            else:
                effective_lines, exclusions = self.script.classify(text, set(line_numbers))
            non_code = max(0, added - len(line_numbers))
            if non_code:
                exclusions["binary_or_unmapped"] += non_code
        else:
            reason = {
                LineBucket.TEST: "test",
                LineBucket.GENERATED: "generated",
                LineBucket.DATA: "data",
                LineBucket.DOCS: "docs",
                LineBucket.VENDOR: "vendor_like",
                LineBucket.ADAPTER: "adapter_only",
                LineBucket.MOCK: "mock_fixture",
                LineBucket.OTHER: "other_nonproduction",
            }.get(bucket, "uncountable")
            exclusions[reason] += added
        return FileLineAudit(
            path=path_value,
            bucket=bucket,
            language=language,
            added_lines=added,
            deleted_lines=deleted,
            effective_production_lines=len(effective_lines),
            excluded_lines=max(0, added - len(effective_lines)),
            exclusions=exclusions,
            added_line_numbers=tuple(sorted(line_numbers)),
        )

    @staticmethod
    def _bucket(path: str) -> str:
        normalized = path.replace("\\", "/").lower()
        parts = normalized.split("/")
        suffix = Path(normalized).suffix
        name = Path(normalized).name
        if "vendor" in parts or "vendor-runtimes" in parts or any(
            segment in parts for segment in ("source-pool", "runtime-sources", "third_party")
        ):
            return LineBucket.VENDOR
        if "tests" in parts or "test" in parts or name.startswith("test_") or ".test." in name or ".spec." in name:
            return LineBucket.TEST
        if "fixtures" in parts or "mocks" in parts or "__snapshots__" in parts:
            return LineBucket.MOCK
        if "docs" in parts or suffix in {".md", ".rst", ".adoc"}:
            return LineBucket.DOCS
        if any(token in name for token in ("generated", ".gen.", "_pb2.py")) or "generated" in parts:
            return LineBucket.GENERATED
        if suffix in {".json", ".yaml", ".yml", ".csv", ".tsv", ".toml", ".lock", ".sql"}:
            return LineBucket.DATA
        if any(token in name for token in ("adapter", "bridge", "port")) and (
            "adapters" in parts or "ports" in parts or "bridges" in parts
        ):
            return LineBucket.ADAPTER
        if parts and parts[0] == "scripts":
            return LineBucket.SCRIPT
        if parts and parts[0] in {"apps", "packages", "skills"}:
            return LineBucket.PRODUCTION
        return LineBucket.OTHER

    @staticmethod
    def _language(suffix: str) -> str:
        return {
            ".py": "python",
            ".pyi": "python",
            ".ts": "typescript",
            ".tsx": "typescript",
            ".js": "javascript",
            ".jsx": "javascript",
            ".rs": "rust",
        }.get(suffix, "other")
