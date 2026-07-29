from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for package_path in (
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "workers",
):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_workers import CodeWorkerSidecarClient  # noqa: E402


def main() -> None:
    client = CodeWorkerSidecarClient(ROOT)
    health = client.health()
    inventory = client.runtime_inventory()
    query = client.query_contract()
    session = client.session_contract()
    tools = client.tool_loop_contract()

    assert health["ok"] is True
    assert health["runtime"] == "zyra-typescript-claude-runtime"
    assert health["source"] == "zyra-typescript-runtime"
    assert health["upstreamSource"] == "claude-code-best"
    assert health["canonicalOwner"] == "typescript"
    assert health["requiresRootSourceRepo"] is False
    assert health["requiresVendorRuntime"] is False
    assert health["requiresLegacyInspectionSidecar"] is False
    assert health["vendor"]["complete"] is False
    assert health["vendor"]["requiredForMainPath"] is False
    assert health["vendor"]["vendorRoot"] == ""
    assert health["productizedRuntime"]["complete"] is True
    assert health["productizedRuntime"]["effectiveLineCount"] >= 18_000
    assert health["productizedRuntime"]["referenceCrosswalk"]["ok"] is True

    assert inventory["source"] == "zyra-typescript-runtime"
    assert inventory["upstreamSource"] == "claude-code-best"
    assert inventory["productizedRuntime"]["complete"] is True
    assert inventory["productizedRuntime"]["moduleChecks"]["queryEngine"] is True
    assert inventory["productizedRuntime"]["moduleChecks"]["toolOrchestration"] is True
    assert inventory["moduleEntrypoints"]["queryEngine"] == "ClaudeRuntimeCore"

    assert query["canonicalOwner"] == "typescript"
    assert query["requiresRootSourceRepo"] is False
    assert query["requiresVendorRuntime"] is False
    assert query["toolOrchestration"]["readOnlyConcurrent"] is True
    assert query["toolOrchestration"]["writeSerial"] is True
    assert session["canonicalOwner"] == "typescript"
    assert session["pythonProjectionIsCanonical"] is False
    assert tools["resultBudgetOwner"] == "typescript"
    print("CodeWorker formal TypeScript runtime verification passed")


if __name__ == "__main__":
    main()
