from __future__ import annotations

import json
import shutil
from pathlib import Path

from zyra_integrations.source_custody.engine import SourceCustodyEngine
from zyra_integrations.source_custody.policy import AuditMode


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = (
    REPO_ROOT
    / "packages"
    / "integrations"
    / "zyra_integrations"
    / "data"
)


def _copy_fixture_input(root: Path, source: Path, relative: str) -> None:
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def test_inventory_pipeline_emits_checksum_bound_receipt_and_queue(
    tmp_path: Path,
) -> None:
    _copy_fixture_input(
        tmp_path,
        DATA_ROOT / "source_custody_catalog.json",
        "packages/integrations/zyra_integrations/data/source_custody_catalog.json",
    )
    _copy_fixture_input(
        tmp_path,
        DATA_ROOT / "source_custody_processes.json",
        "packages/integrations/zyra_integrations/data/source_custody_processes.json",
    )
    _copy_fixture_input(
        tmp_path,
        DATA_ROOT / "internalization_ledger_seed.json",
        "packages/integrations/zyra_integrations/data/internalization_ledger_seed.json",
    )
    _copy_fixture_input(tmp_path, REPO_ROOT / "docs/vendor-map.md", "docs/vendor-map.md")
    _copy_fixture_input(
        tmp_path, REPO_ROOT / "third_party/NOTICE.md", "third_party/NOTICE.md"
    )
    source = tmp_path / "packages" / "demo" / "runtime.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from langgraph.graph import StateGraph\n"
        "import subprocess\n"
        "subprocess.run(['python', '../opencode/main.py'])\n",
        encoding="utf-8",
    )
    javascript = tmp_path / "packages" / "demo" / "runner.ts"
    javascript.write_text(
        "import { exec } from 'node:child_process';\n"
        "exec('git status');\n",
        encoding="utf-8",
    )

    result = SourceCustodyEngine(
        tmp_path,
        mode=AuditMode.INVENTORY,
        include_vendor=False,
        revision="1d19ea39a8313091dcfbc00f78c79fdfdeab7cf4",
        baseline_revision="1d19ea39a8313091dcfbc00f78c79fdfdeab7cf4",
    ).run()
    receipt_path = result.write_receipt(tmp_path / "out" / "receipt.json")
    queue_path = result.write_work_queue(tmp_path / "out" / "queue.json")

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    codes = {
        finding["code"]
        for audit_section in receipt["sections"]
        for finding in audit_section["findings"]
    }
    assert result.valid
    assert not result.release_ready
    assert receipt["receipt_digest"].startswith("sha256:")
    assert queue["source_receipt_digest"] == receipt["receipt_digest"]
    assert queue["digest"].startswith("sha256:")
    assert "langgraph_broad_runtime_import" in codes
    assert "python_parent_source_path" in codes
    assert "javascript_shell_process_call" in codes
    assert "source_notice_status_invalid" not in codes
    assert receipt["openclaw_boundary"][0]["status"] == "excluded_forward_only"
    assert queue["items"]
