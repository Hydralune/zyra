from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
for package_path in (
    PROJECT_ROOT,
    PROJECT_ROOT / "packages" / "core",
    PROJECT_ROOT / "packages" / "integrations",
    PROJECT_ROOT / "packages" / "runtime",
):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))


from zyra_integrations import LedgerLifecycle, MainPathStatus
from zyra_integrations.mcp.source_audit import (
    MCP_SOURCE_DECISIONS,
    REQUIRED_SOURCE_PATHS,
    SourceDisposition,
    assert_mcp_source_coverage,
    audit_mcp_sources,
    source_decision,
)
from zyra_integrations.source_provenance import BundledSourceProvenance
from scripts.sync_mcp_source_ledger import (
    DEFAULT_LEDGER_PATH,
    OWNER_UNIT,
    canonical_json,
    expected_entries,
    rewrite_payload,
)


class McpSourceAuditTests(unittest.TestCase):
    def test_required_parent_sources_have_explicit_complete_decisions(self) -> None:
        report = assert_mcp_source_coverage()

        self.assertTrue(report.complete)
        self.assertGreaterEqual(len(report.decisions), 60)
        self.assertEqual(
            set(REQUIRED_SOURCE_PATHS),
            {"claude-code-best", "agent-framework", "opencode", "agentscope", "hermes-agent"},
        )
        self.assertEqual(
            set(report.counts),
            {
                SourceDisposition.ACTIVE,
                SourceDisposition.ADAPTER,
                SourceDisposition.CONTRACT_ONLY,
                SourceDisposition.REFERENCE_ONLY,
                SourceDisposition.DEFERRED,
            },
        )

    def test_runtime_claims_have_real_targets_tests_and_main_path_evidence(self) -> None:
        for decision in MCP_SOURCE_DECISIONS:
            with self.subTest(source=decision.key):
                if not decision.claims_runtime_ownership:
                    self.assertTrue(decision.replacement or decision.next_owner or decision.rationale)
                    continue
                self.assertTrue(decision.runtime_entry)
                self.assertTrue(decision.test_target)
                self.assertTrue(decision.main_path_evidence)
                # A behavior test that no longer exists is not evidence.  The
                # ledger silently pointed at deleted pre-cutover test modules
                # because only membership in the allow-list was checked.
                self.assertTrue(
                    (PROJECT_ROOT / decision.test_target).is_file(),
                    decision.test_target,
                )
                for target in decision.target_paths:
                    self.assertTrue((PROJECT_ROOT / target).is_file(), target)
                    self.assertNotIn("vendor", Path(target).parts)
                    self.assertNotIn("..", Path(target).parts)

    def test_every_recorded_source_path_has_verified_bundled_provenance(self) -> None:
        provenance = BundledSourceProvenance(PROJECT_ROOT)
        receipt = provenance.verify()

        self.assertTrue(receipt["ready"])
        self.assertGreaterEqual(receipt["source_file_count"], len(MCP_SOURCE_DECISIONS))
        for decision in MCP_SOURCE_DECISIONS:
            with self.subTest(source=decision.key):
                path = provenance.source_file(decision.repository, decision.source_path)
                self.assertTrue(path.is_file(), decision.key)
                self.assertTrue(path.is_relative_to(PROJECT_ROOT / "provenance"))

    def test_high_risk_source_boundaries_are_not_overclaimed(self) -> None:
        generated_skill = source_decision("claude-code-best", "src/skills/mcpSkills.ts")
        agent_framework_skill = source_decision(
            "agent-framework", "python/packages/core/agent_framework/_skills.py"
        )
        agentscope_gateway = source_decision(
            "agentscope", "src/agentscope/workspace/_mcp_gateway/_mcp_gateway_app.py"
        )
        hermes_server = source_decision("hermes-agent", "mcp_serve.py")

        self.assertIs(generated_skill.disposition, SourceDisposition.REFERENCE_ONLY)
        self.assertEqual(generated_skill.next_owner, "M1-03C")
        self.assertIs(agent_framework_skill.disposition, SourceDisposition.DEFERRED)
        self.assertEqual(agent_framework_skill.next_owner, "M1-03C")
        self.assertIs(agentscope_gateway.disposition, SourceDisposition.DEFERRED)
        self.assertEqual(agentscope_gateway.next_owner, "M1-05B")
        self.assertIs(hermes_server.disposition, SourceDisposition.DEFERRED)

    def test_selected_supplemental_sources_map_to_zyra_owned_runtime(self) -> None:
        expected = {
            ("agent-framework", "python/packages/core/agent_framework/_mcp.py"),
            ("opencode", "packages/opencode/src/mcp/index.ts"),
            ("opencode", "packages/opencode/src/mcp/catalog.ts"),
            ("agentscope", "src/agentscope/tool/_adapters.py"),
            ("hermes-agent", "tools/mcp_tool.py"),
            ("hermes-agent", "tools/mcp_oauth.py"),
        }
        by_key = {(decision.repository, decision.source_path): decision for decision in MCP_SOURCE_DECISIONS}

        for key in expected:
            with self.subTest(source=key):
                decision = by_key[key]
                self.assertTrue(decision.claims_runtime_ownership)
                self.assertTrue(all(path.startswith("packages/") for path in decision.target_paths))
                self.assertFalse(any("../" in value for value in (*decision.target_paths, decision.main_path_evidence)))

    def test_audit_rejects_duplicate_missing_required_and_incomplete_active_rows(self) -> None:
        first = MCP_SOURCE_DECISIONS[0]
        duplicate = audit_mcp_sources((*MCP_SOURCE_DECISIONS, first))
        self.assertIn("duplicate", {issue.code for issue in duplicate.blocking_issues})

        omitted = tuple(decision for decision in MCP_SOURCE_DECISIONS if decision is not first)
        missing = audit_mcp_sources(omitted)
        self.assertIn("required-source-missing", {issue.code for issue in missing.blocking_issues})

        active = next(decision for decision in MCP_SOURCE_DECISIONS if decision.claims_runtime_ownership)
        incomplete = replace(active, runtime_entry=None, test_target=None, main_path_evidence="")
        altered = tuple(incomplete if decision is active else decision for decision in MCP_SOURCE_DECISIONS)
        report = audit_mcp_sources(altered)
        codes = {issue.code for issue in report.blocking_issues}
        self.assertTrue({"missing-entry", "missing-test", "missing-main-path"}.issubset(codes))

    def test_ledger_projection_only_replaces_m1_03b_rows(self) -> None:
        original = json.loads(DEFAULT_LEDGER_PATH.read_text(encoding="utf-8"))
        rewritten = rewrite_payload(original)
        original_other = [row for row in original["entries"] if row.get("owner_unit") != OWNER_UNIT]
        rewritten_other = [row for row in rewritten["entries"] if row.get("owner_unit") != OWNER_UNIT]
        projected = [row for row in rewritten["entries"] if row.get("owner_unit") == OWNER_UNIT]

        self.assertEqual(original_other, rewritten_other)
        self.assertEqual(len(projected), len(MCP_SOURCE_DECISIONS))
        self.assertEqual(
            {(row["source_repo"], row["source_path"]) for row in projected},
            {(decision.repository, decision.source_path) for decision in MCP_SOURCE_DECISIONS},
        )

    def test_runtime_ledger_rows_claim_tested_productized_behavior_only_for_selected_sources(self) -> None:
        entries = expected_entries()
        by_key = {(entry.source_repo, entry.source_path): entry for entry in entries}
        for decision in MCP_SOURCE_DECISIONS:
            with self.subTest(source=decision.key):
                entry = by_key[(decision.repository, decision.source_path)]
                self.assertEqual(entry.owner_unit, OWNER_UNIT)
                if decision.claims_runtime_ownership:
                    self.assertIs(entry.lifecycle, LedgerLifecycle.PRODUCTIZED)
                    self.assertIs(entry.main_path_status, MainPathStatus.TESTED_MAIN_PATH)
                    self.assertTrue(entry.main_path.worker_runtime)
                    self.assertTrue(all(binding.required_for_main_path for binding in entry.target_bindings[:-1]))
                else:
                    self.assertIn(entry.lifecycle, {LedgerLifecycle.CANDIDATE, LedgerLifecycle.DEFERRED})
                    self.assertFalse(any(binding.required_for_main_path for binding in entry.target_bindings))

    def test_check_mode_accepts_an_aligned_temporary_ledger_without_writing(self) -> None:
        original = json.loads(DEFAULT_LEDGER_PATH.read_text(encoding="utf-8"))
        aligned = canonical_json(rewrite_payload(original))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            path.write_text(aligned, encoding="utf-8")
            before = path.read_bytes()
            completed = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "sync_mcp_source_ledger.py"),
                    "--ledger-path",
                    str(path),
                    "--check",
                ],
                cwd=PROJECT_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("mcp_source_ledger_aligned=true", completed.stdout)
            self.assertEqual(before, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
