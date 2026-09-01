from __future__ import annotations

from pathlib import Path

from scripts.verify_product_entry_release import (
    EXPECTED_COMMAND_LINES,
    PROJECT_ROOT,
    audit_cli_dependency_closure,
    audit_node_artifact,
)


def test_release_command_surface_includes_product_diagnostics() -> None:
    assert len(EXPECTED_COMMAND_LINES) == 11
    assert "zyra doctor [--bundle <file>]     read-only product diagnostics" in EXPECTED_COMMAND_LINES


def test_cli_dependency_closure_excludes_web_react_tui_and_external_paths() -> None:
    receipt = audit_cli_dependency_closure(PROJECT_ROOT)

    assert receipt["ready"] is True
    assert receipt["workspace_packages"] == [
        "@zyra/cli",
        "@zyra/commands",
        "@zyra/typed-api-client",
    ]
    assert receipt["external_packages"] == []
    assert receipt["forbidden_dependencies"] == []
    assert receipt["external_path_dependencies"] == []
    assert receipt["web_runtime_in_closure"] is False


def test_node_artifact_audit_rejects_web_react_and_reference_runtime(
    tmp_path: Path,
) -> None:
    clean = tmp_path / "clean.js"
    clean.write_text("#!/usr/bin/env node\nconsole.log('zyra')\n", encoding="utf-8")
    assert audit_node_artifact(clean)["ready"] is True

    contaminated = tmp_path / "contaminated.js"
    contaminated.write_text(
        "#!/usr/bin/env node\nimport 'react-dom'; // claude-code-best\n",
        encoding="utf-8",
    )
    receipt = audit_node_artifact(contaminated)
    assert receipt["ready"] is False
    assert receipt["dom_react_tui_runtime_present"] is True
    assert receipt["forbidden_runtime_markers"] == [
        "claude-code-best",
        "react-dom",
    ]
