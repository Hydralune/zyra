from __future__ import annotations

import json
from pathlib import Path

import pytest

from zyra_integrations.source_custody.catalog import CatalogLoader
from zyra_integrations.source_custody.dependencies import DependencyAuditor
from zyra_integrations.source_custody.javascript_analyzer import JavaScriptAnalyzer
from zyra_integrations.source_custody.model import (
    RuleSwitches,
    SourceEntry,
    finding,
    section,
)
from zyra_integrations.source_custody.policy import AuditMode, FindingPolicy
from zyra_integrations.source_custody.processes import ProcessProfile, ProcessUse
from zyra_integrations.source_custody.python_analyzer import PythonAnalyzer
from zyra_integrations.source_custody.repository import RepositoryScanner


REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = (
    REPO_ROOT
    / "packages"
    / "integrations"
    / "zyra_integrations"
    / "data"
    / "source_custody_catalog.json"
)


def _codes(audit_section) -> set[str]:
    return {item.code for item in audit_section.findings}


def _catalog_payload() -> dict[str, object]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _scan(root: Path):
    return RepositoryScanner(root, include_vendor=False).scan()[0]


def _analyze_source(root: Path):
    inventory = _scan(root)
    python, python_section = PythonAnalyzer(root).analyze(inventory)
    javascript, javascript_section = JavaScriptAnalyzer(root).analyze(inventory)
    return inventory, python, python_section, javascript, javascript_section


def test_checked_in_catalog_is_complete_and_role_bounded() -> None:
    result = CatalogLoader(REPO_ROOT).load(CATALOG_PATH)

    assert result.catalog is not None
    assert len(result.catalog.entries) == 28
    assert result.catalog.historical_boundaries[0].status == "excluded_forward_only"
    assert _codes(result.section) == {"source_license_unresolved"}
    assert sum(
        item.code == "source_license_unresolved"
        for item in result.section.findings
    ) == 5
    for entries in result.catalog.by_capability().values():
        active = [entry for entry in entries if entry.active]
        if active:
            assert sum(entry.primary for entry in active) == 1
            assert sum(entry.supplementary for entry in active) <= 2


def test_catalog_duplicate_entry_is_release_blocking(tmp_path: Path) -> None:
    payload = _catalog_payload()
    payload["entries"][1]["entry_id"] = payload["entries"][0]["entry_id"]
    path = tmp_path / "catalog.json"
    _write_json(path, payload)

    result = CatalogLoader(tmp_path).load(path)

    assert "catalog_entry_duplicate" in _codes(result.section)
    assert not result.section.valid


def test_unresolved_active_license_routes_to_release_queue() -> None:
    result = CatalogLoader(REPO_ROOT).load(CATALOG_PATH)

    policy = FindingPolicy().apply([result.section], mode=AuditMode.CANDIDATE)

    license_items = [
        item
        for item in policy.work_queue
        if "source_license_unresolved" in item.finding_codes
    ]
    assert license_items
    assert sum(len(item.finding_fingerprints) for item in license_items) == 5
    assert all(item.owner_unit == "M3-01B" for item in license_items)
    assert all(item.priority.value == "P0" for item in license_items)


def test_catalog_missing_repository_is_release_blocking(tmp_path: Path) -> None:
    payload = _catalog_payload()
    payload["entries"] = [
        row for row in payload["entries"] if row["source_repo"] != "agentscope"
    ]
    path = tmp_path / "catalog.json"
    _write_json(path, payload)

    result = CatalogLoader(tmp_path).load(path)

    assert "required_source_repository_missing" in _codes(result.section)


def test_catalog_duplicate_primary_is_release_blocking(tmp_path: Path) -> None:
    payload = _catalog_payload()
    duplicate = dict(payload["entries"][0])
    duplicate["entry_id"] = "src_second_code_worker_primary"
    duplicate["source_repo"] = "hermes-agent"
    payload["entries"].append(duplicate)
    path = tmp_path / "catalog.json"
    _write_json(path, payload)

    result = CatalogLoader(tmp_path).load(path)

    assert "active_capability_primary_count" in _codes(result.section)


def test_role_rule_disable_creates_a_detectable_mutation_survivor(
    tmp_path: Path,
) -> None:
    payload = _catalog_payload()
    duplicate = dict(payload["entries"][0])
    duplicate["entry_id"] = "src_mutant_second_primary"
    duplicate["source_repo"] = "opencode"
    payload["entries"].append(duplicate)
    path = tmp_path / "catalog.json"
    _write_json(path, payload)

    enabled = CatalogLoader(tmp_path).load(path)
    disabled = CatalogLoader(
        tmp_path, switches=RuleSwitches().disabled("roles")
    ).load(path)

    assert "active_capability_primary_count" in _codes(enabled.section)
    assert "active_capability_primary_count" not in _codes(disabled.section)


def test_source_entry_rejects_openclaw_forward_role() -> None:
    raw = dict(_catalog_payload()["entries"][0])
    raw["entry_id"] = "src_openclaw_forbidden"
    raw["source_repo"] = "openclaw"

    with pytest.raises(ValueError, match="OpenClaw"):
        SourceEntry.parse(raw)


def test_source_entry_rejects_reference_as_active_runtime() -> None:
    raw = dict(_catalog_payload()["entries"][-1])
    raw.update(
        {
            "status": "active",
            "role": "reference_only",
            "landing_status": "internalized",
            "target_languages": ["python"],
            "target_paths": ["packages/example"],
            "test_paths": ["tests/test_example.py"],
            "runtime_entry": {"module": "example", "symbol": "", "command": "", "protocol": ""},
        }
    )

    with pytest.raises(ValueError, match="inactive-only role"):
        SourceEntry.parse(raw)


def test_python_analyzer_detects_parent_source_and_langgraph_runtime(
    tmp_path: Path,
) -> None:
    source = tmp_path / "packages" / "demo" / "runtime.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from langgraph.graph import StateGraph\n"
        "import subprocess\n"
        "subprocess.run(['python', '../opencode/runtime.py'], check=True)\n",
        encoding="utf-8",
    )
    inventory = _scan(tmp_path)

    _, audit_section = PythonAnalyzer(tmp_path).analyze(inventory)

    assert "langgraph_broad_runtime_import" in _codes(audit_section)
    assert "python_parent_source_path" in _codes(audit_section)


def test_langgraph_rule_disable_is_observable(tmp_path: Path) -> None:
    source = tmp_path / "packages" / "demo" / "runtime.py"
    source.parent.mkdir(parents=True)
    source.write_text("from langgraph.graph import StateGraph\n", encoding="utf-8")
    inventory = _scan(tmp_path)

    _, enabled = PythonAnalyzer(tmp_path).analyze(inventory)
    _, disabled = PythonAnalyzer(
        tmp_path, switches=RuleSwitches().disabled("langgraph")
    ).analyze(inventory)

    assert "langgraph_broad_runtime_import" in _codes(enabled)
    assert "langgraph_broad_runtime_import" not in _codes(disabled)


def test_javascript_analyzer_ignores_comments_but_detects_runtime_import(
    tmp_path: Path,
) -> None:
    source = tmp_path / "apps" / "web" / "src" / "runtime.ts"
    source.parent.mkdir(parents=True)
    source.write_text(
        "// import ignored from '../../../opencode/runtime'\n"
        "import { StateGraph } from 'langgraph';\n"
        "const marker = '../OpenHands/server';\n",
        encoding="utf-8",
    )
    inventory = _scan(tmp_path)

    analyses, audit_section = JavaScriptAnalyzer(tmp_path).analyze(inventory)

    assert analyses[0].parse_error == ""
    assert "langgraph_javascript_runtime_import" in _codes(audit_section)
    assert "javascript_source_path_literal" in _codes(audit_section)
    assert len(
        [
            item
            for item in audit_section.findings
            if item.code == "javascript_source_path_literal"
        ]
    ) == 1


def test_javascript_regex_exec_is_not_a_child_process(tmp_path: Path) -> None:
    source = tmp_path / "apps" / "web" / "src" / "matcher.ts"
    source.parent.mkdir(parents=True)
    source.write_text(
        "const pattern = /(?:token|secret)([A-Z'\"/]+)/gi;\n"
        "export const match = (value: string) => pattern.exec(value);\n",
        encoding="utf-8",
    )
    inventory = _scan(tmp_path)

    analyses, audit_section = JavaScriptAnalyzer(tmp_path).analyze(inventory)

    assert analyses[0].parse_error == ""
    assert "javascript_shell_process_call" not in _codes(audit_section)


def test_javascript_lexer_handles_division_and_nested_templates(
    tmp_path: Path,
) -> None:
    source = tmp_path / "packages" / "runtime" / "syntax.ts"
    source.parent.mkdir(parents=True)
    source.write_text(
        "const ratio = weights[0]! / total;\n"
        "const value = `aliases: ${JSON.stringify([`${name}-alias`])}`;\n"
        "const quoted = `value=${name.replace(/\"/g, '\\\\\"')}`;\n"
        "if (!/^[!#$%&'*+\\\\-.^_`|~0-9a-z]+$/.test(name)) throw Error();\n",
        encoding="utf-8",
    )
    inventory = _scan(tmp_path)

    analyses, _ = JavaScriptAnalyzer(tmp_path).analyze(inventory)

    assert analyses[0].parse_error == ""


def test_javascript_imported_exec_is_audited_as_process(tmp_path: Path) -> None:
    source = tmp_path / "packages" / "runtime" / "runner.ts"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import { exec } from 'node:child_process';\n"
        "exec('git status');\n",
        encoding="utf-8",
    )
    inventory = _scan(tmp_path)

    _, audit_section = JavaScriptAnalyzer(tmp_path).analyze(inventory)

    assert "javascript_shell_process_call" in _codes(audit_section)


def test_dependency_auditor_detects_undeclared_python_and_javascript(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='fixture'\nversion='1.0.0'\ndependencies=[]\n",
        encoding="utf-8",
    )
    (tmp_path / "package.json").write_text(
        '{"name":"fixture","version":"1.0.0","dependencies":{}}\n',
        encoding="utf-8",
    )
    (tmp_path / "bun.lock").write_text('{"lockfileVersion":1}\n', encoding="utf-8")
    python_path = tmp_path / "packages" / "demo" / "runtime.py"
    python_path.parent.mkdir(parents=True)
    python_path.write_text("import requests\n", encoding="utf-8")
    js_path = tmp_path / "apps" / "web" / "runtime.ts"
    js_path.parent.mkdir(parents=True)
    js_path.write_text("import leftPad from 'left-pad';\n", encoding="utf-8")
    inventory, python, _, javascript, _ = _analyze_source(tmp_path)

    _, audit_section = DependencyAuditor(tmp_path).audit(
        inventory, python, javascript
    )

    assert "python_dependency_undeclared" in _codes(audit_section)
    assert "javascript_dependency_undeclared" in _codes(audit_section)


def test_dependency_rule_disable_is_observable(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='fixture'\nversion='1.0.0'\ndependencies=[]\n",
        encoding="utf-8",
    )
    source = tmp_path / "packages" / "demo" / "runtime.py"
    source.parent.mkdir(parents=True)
    source.write_text("import requests\n", encoding="utf-8")
    inventory, python, _, javascript, _ = _analyze_source(tmp_path)

    _, enabled = DependencyAuditor(tmp_path).audit(
        inventory, python, javascript
    )
    _, disabled = DependencyAuditor(
        tmp_path, switches=RuleSwitches().disabled("dependencies")
    ).audit(inventory, python, javascript)

    assert "python_dependency_undeclared" in _codes(enabled)
    assert "python_dependency_undeclared" not in _codes(disabled)


def test_repository_scanner_excludes_ephemeral_cache(tmp_path: Path) -> None:
    tracked = tmp_path / "packages" / "demo.py"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("VALUE = 1\n", encoding="utf-8")
    cache = tmp_path / ".tmp" / "large.py"
    cache.parent.mkdir()
    cache.write_text("VALUE = 2\n", encoding="utf-8")

    inventory = _scan(tmp_path)

    assert [item.path for item in inventory.files] == ["packages/demo.py"]
    assert ".tmp" in inventory.skipped_directories


def test_machine_readable_data_is_not_misclassified_as_minified_source(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "packages" / "demo" / "data" / "queue.json"
    payload.parent.mkdir(parents=True)
    payload.write_text(
        json.dumps({"items": ["x" * 400 for _ in range(20)]}),
        encoding="utf-8",
    )

    _, audit_section = RepositoryScanner(tmp_path).scan()

    assert "minified_source_in_production_tree" not in _codes(audit_section)


def test_process_profile_matches_wildcard_executable() -> None:
    profile = ProcessProfile.parse(
        {
            "profile_id": "test_harness",
            "kind": "test",
            "owner": "quality",
            "executable": "*",
            "command_prefixes": [],
            "callsite_patterns": ["tests/**"],
            "modes": ["test"],
            "transport": "none",
            "ports": [],
            "environment": [],
            "package": "",
            "entrypoint": "",
            "healthcheck": "pytest assertion",
            "source_role_entry_ids": [],
            "default_reachable": False,
            "external": False,
            "reason": "The test harness owns bounded child commands in behavior tests.",
        }
    )
    use = ProcessUse(
        path="tests/unit/test_runtime.py",
        line=4,
        language="python",
        callee="subprocess.run",
        executable="git",
        argv=("git", "status"),
        shell=False,
        literal=True,
        port=0,
        source="python_call",
    )

    assert profile.matches(use)


def test_policy_emits_deterministic_actionable_queue() -> None:
    audit_section = section(
        "fixture",
        findings=[
            finding(
                "langgraph_python_runtime_import",
                "Broad LangGraph runtime import.",
                "langgraph",
                path="packages/runtime.py",
            ),
            finding(
                "process_use_undeclared",
                "Undeclared child process.",
                "process",
                path="packages/runtime.py",
            ),
        ],
    )

    first = FindingPolicy().apply([audit_section], mode=AuditMode.CANDIDATE)
    second = FindingPolicy().apply([audit_section], mode=AuditMode.CANDIDATE)

    assert not first.valid
    assert first.digest == second.digest
    assert [item.action.value for item in first.work_queue] == ["remove", "declare"]
    assert all(item.owner_unit == "M3-01B" for item in first.work_queue)


def test_policy_queue_is_stable_for_case_variant_source_names() -> None:
    findings = [
        finding(
            "python_parent_source_path",
            "Parent source path uses canonical spelling.",
            "dependencies",
            source_repo="OpenHands",
            path="packages/a.py",
        ),
        finding(
            "python_parent_source_path",
            "Parent source path uses normalized spelling.",
            "dependencies",
            source_repo="openhands",
            path="packages/b.py",
        ),
    ]

    first = FindingPolicy().apply(
        [section("fixture", findings=findings)],
        mode=AuditMode.INVENTORY,
    )
    second = FindingPolicy().apply(
        [section("fixture", findings=reversed(findings))],
        mode=AuditMode.INVENTORY,
    )

    assert first.digest == second.digest
    assert first.work_queue == second.work_queue
    assert first.work_queue[0].source_repositories == ("OpenHands", "openhands")
