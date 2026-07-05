from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "integrations",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_integrations import (
    build_contract_report,
    contract_summary,
    ledger_api_contracts,
    ledger_cli_contracts,
    ledger_event_contracts,
    ledger_script_contracts,
)


class LedgerContractTests(unittest.TestCase):
    def test_api_contracts_include_required_runtime_surfaces(self) -> None:
        contracts = ledger_api_contracts()
        paths = {f"{contract.method} {contract.path}" for contract in contracts}

        self.assertIn("GET /ledger", paths)
        self.assertIn("GET /ledger/audit", paths)
        self.assertIn("GET /ledger/readiness", paths)
        self.assertIn("GET /ledger/report", paths)
        self.assertIn("GET /ledger/accounting", paths)
        self.assertIn("GET /ledger/linecount", paths)
        self.assertIn("POST /ledger/{ledger_id}/advance", paths)

    def test_cli_contracts_include_gate_linecount_and_advance(self) -> None:
        commands = {contract.command: contract for contract in ledger_cli_contracts()}

        self.assertIn("gate", commands)
        self.assertIn("linecount", commands)
        self.assertIn("accounting", commands)
        self.assertIn("advance", commands)
        self.assertTrue(commands["advance"].mutates_ledger)
        self.assertTrue(commands["audit"].writes_event)

    def test_event_contracts_define_audit_and_update_payloads(self) -> None:
        payloads = {contract.payload_key: contract for contract in ledger_event_contracts()}

        self.assertIn("integration_ledger_audit", payloads)
        self.assertIn("integration_ledger_update", payloads)
        self.assertIn("finding_count", payloads["integration_ledger_audit"].required_payload_fields)
        self.assertIn("before", payloads["integration_ledger_update"].required_payload_fields)

    def test_script_contracts_define_verification_failure_conditions(self) -> None:
        scripts = {contract.script: contract for contract in ledger_script_contracts()}

        self.assertIn("scripts/verify_internalization_ledger.py", scripts)
        verify = scripts["scripts/verify_internalization_ledger.py"]
        self.assertTrue(any("shortfall" in item for item in verify.failure_conditions))

    def test_contract_report_detects_missing_required_cli_command(self) -> None:
        implemented = {contract.command for contract in ledger_cli_contracts()} - {"advance"}
        report = build_contract_report(implemented_cli_commands=implemented)

        self.assertFalse(report.ok)
        self.assertTrue(any(finding.name == "advance" for finding in report.findings))

    def test_contract_report_accepts_complete_declared_contracts(self) -> None:
        implemented_cli = {contract.command for contract in ledger_cli_contracts()}
        implemented_api = {f"{contract.method} {contract.path}" for contract in ledger_api_contracts()}
        report = build_contract_report(
            implemented_cli_commands=implemented_cli,
            implemented_api_paths=implemented_api,
        )
        summary = contract_summary(report)

        self.assertTrue(report.ok)
        self.assertEqual(summary["finding_count"], 0)
        self.assertGreater(summary["required_surface_count"], 0)


if __name__ == "__main__":
    unittest.main()
