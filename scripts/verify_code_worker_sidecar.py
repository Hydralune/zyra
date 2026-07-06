from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "workers",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_workers import CodeWorkerSidecarClient


def main() -> None:
    client = CodeWorkerSidecarClient(ROOT)
    health = client.health()
    assert health["ok"] is True
    assert health["worker"] == "CodeWorkerRuntime"
    assert health["vendor"]["complete"] is True
    assert health["productizedRuntime"]["complete"] is True
    assert health["productizedRuntime"]["effectiveLineCount"] >= 18_000
    assert health["productizedRuntime"]["referenceCrosswalk"]["ok"] is True
    inventory = client.runtime_inventory()
    assert inventory["source"] == "claude-code-best"
    assert inventory["productizedRuntime"]["complete"] is True
    assert inventory["moduleEntrypoints"]["queryEngine"] is True
    assert inventory["moduleEntrypoints"]["sessionPersistenceRuntime"] is True
    assert inventory["moduleEntrypoints"]["sessionRestoreRuntime"] is True
    assert inventory["moduleEntrypoints"]["apiStreamRuntime"] is True
    assert inventory["toolRuntime"]["baseToolCount"] > 5
    session_contract = client.session_contract()
    assert session_contract["source"] == "claude-code-best"
    assert session_contract["ownerUnit"] == "M1-02B"
    assert session_contract["transcriptPersistence"]["appendOnlyJsonl"] is True
    assert session_contract["transcriptPersistence"]["parentUuidChain"] is True
    assert session_contract["resumeRecovery"]["hasChainTraversal"] is True
    assert session_contract["streamRuntime"]["rawSseStateMachine"] is True
    assert session_contract["streamRuntime"]["hasApiClient"] is True
    assert session_contract["streamRuntime"]["hasFilesApi"] is True
    assert session_contract["streamRuntime"]["hasPromptDumpPipeline"] is True
    assert session_contract["streamRuntime"]["hasQueryProfiler"] is True
    assert session_contract["streamRuntime"]["retryMatrix"]["unattendedRetry"] is True
    assert session_contract["bridgeSessionRuntime"]["hasInboundMessages"] is True
    assert session_contract["sessionCommands"]["hasClearConversation"] is True
    assert session_contract["sessionCommands"]["hasRenameSession"] is True
    print("CodeWorker sidecar verification passed")


if __name__ == "__main__":
    main()
