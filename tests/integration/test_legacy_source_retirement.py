from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_root in (
    ROOT / "packages" / "core",
    ROOT / "packages" / "integrations",
):
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))

from zyra_integrations import (  # noqa: E402
    browser_use_source_identity,
    claude_code_source_identity,
)
from zyra_integrations.legacy_source_retirement import (  # noqa: E402
    GitFile,
    audit_candidate_files,
    freeze_retirement_manifest,
    load_retirement_manifest,
    verify_retirement_manifest,
)
from zyra_integrations.ledger_migrations import (  # noqa: E402
    current_legacy_target_paths,
)
from zyra_integrations.ledger_store import load_seed_ledger  # noqa: E402
from zyra_integrations.source_extraction import (  # noqa: E402
    LegacySourcePoolRetiredError,
    claude_code_m1_01b_plan,
    write_runtime_scaffold_files,
)


MANIFEST_PATH = (
    ROOT
    / "docs"
    / "reviews"
    / "evidence"
    / "P2-S02A-01"
    / "legacy-source-pool-retirement-manifest.json"
)


class LegacySourceRetirementTests(unittest.TestCase):
    def test_frozen_manifest_rebuilds_from_base_git_objects(self) -> None:
        manifest = load_retirement_manifest(MANIFEST_PATH)
        rebuilt = freeze_retirement_manifest(
            ROOT,
            base_revision=manifest["frozen_source"]["commit"],
        )

        self.assertEqual(rebuilt, manifest)
        self.assertEqual(
            sum(item["file_count"] for item in manifest["retired_roots"]),
            3684,
        )
        self.assertEqual(
            sum(item["total_bytes"] for item in manifest["retired_roots"]),
            30_656_839,
        )
        self.assertEqual(
            manifest["custody_transition"]["historical_legacy_target_count"],
            273,
        )
        self.assertEqual(
            manifest["custody_transition"]["current_legacy_target_count_required"],
            0,
        )

    def test_manifest_tamper_fails_independent_verifier(self) -> None:
        manifest = load_retirement_manifest(MANIFEST_PATH)
        tampered = copy.deepcopy(manifest)
        tampered["retired_roots"][0]["files"][0]["sha256"] = "0" * 64

        report = verify_retirement_manifest(
            ROOT,
            tampered,
            target_revision="HEAD",
            check_worktree=False,
        )
        codes = {finding.code for finding in report.findings}

        self.assertFalse(report.valid)
        self.assertIn("manifest_digest_mismatch", codes)
        self.assertIn("frozen_file_mismatch", codes)

    def test_candidate_gate_rejects_reintroduced_and_renamed_pools(self) -> None:
        legacy_blob = "a" * 40
        retained_notice_blob = "c" * 40
        candidates = (
            GitFile(
                path="vendor/reintroduced.py",
                mode="100644",
                blob_id="b" * 40,
                size=1,
            ),
            GitFile(
                path="third_party/renamed.py",
                mode="100644",
                blob_id=legacy_blob,
                size=1,
            ),
            GitFile(
                path="packages/runtime/formal.py",
                mode="100644",
                blob_id=legacy_blob,
                size=1,
            ),
            GitFile(
                path="third_party/NOTICE.md",
                mode="100644",
                blob_id=retained_notice_blob,
                size=1,
            ),
        )

        findings = audit_candidate_files(
            candidates,
            legacy_blob_ids={legacy_blob},
            base_nonlegacy_pairs={
                ("third_party/NOTICE.md", retained_notice_blob),
            },
        )
        codes = {finding.code for finding in findings}

        self.assertIn("forbidden_source_pool_path", codes)
        self.assertIn("renamed_source_pool_blob", codes)
        self.assertFalse(
            any(
                finding.path == "third_party/NOTICE.md"
                for finding in findings
            )
        )

    def test_source_identities_are_metadata_only_and_roots_are_absent(self) -> None:
        for source in (
            claude_code_source_identity(),
            browser_use_source_identity(),
        ):
            payload = source.to_dict()
            self.assertEqual(payload["status"], "retired")
            self.assertEqual(payload["availability"], "not_applicable")
            self.assertFalse(payload["filesystem_required"])
            self.assertFalse(payload["fallback_available"])
            self.assertNotIn("root", payload)

        self.assertFalse((ROOT / "vendor").exists())
        self.assertFalse((ROOT / "vendor-runtimes").exists())

    def test_historical_extractor_and_writer_fail_without_recreating_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            with self.assertRaises(LegacySourcePoolRetiredError):
                claude_code_m1_01b_plan(
                    project_root=project_root,
                    source_workspace_root=project_root.parent,
                    dry_run=True,
                )
            with self.assertRaises(LegacySourcePoolRetiredError):
                write_runtime_scaffold_files(project_root)
            self.assertEqual(list(project_root.iterdir()), [])

    def test_current_ledger_has_zero_legacy_targets(self) -> None:
        ledger = load_seed_ledger()

        self.assertEqual(current_legacy_target_paths(ledger.entries()), [])
        historical_rows = [
            entry
            for entry in ledger.entries()
            if entry.metadata.get("historical_legacy_target_count")
        ]
        self.assertTrue(historical_rows)
        self.assertTrue(
            all(
                entry.metadata.get("historical_target_provenance")
                == (
                    "docs/reviews/evidence/P2-S02A-01/"
                    "legacy-source-pool-retirement-manifest.json"
                )
                for entry in historical_rows
            )
        )

    def test_manifest_is_plain_json_without_runtime_loading(self) -> None:
        payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["frozen_source"]["read_mode"],
            "git_objects_only",
        )
        self.assertFalse(
            payload["target_policy"]["runtime_filesystem_fallback_allowed"]
        )


if __name__ == "__main__":
    unittest.main()
