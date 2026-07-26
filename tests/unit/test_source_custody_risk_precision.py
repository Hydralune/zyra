from __future__ import annotations

from zyra_integrations.source_custody.risks import source_audited_interpreter


def test_virtual_environment_interpreter_survives_package_script_normalization() -> None:
    assert source_audited_interpreter(r".\.venv\Scripts\python.exe")
    assert source_audited_interpreter(".venvscriptspython.exe")
    assert source_audited_interpreter("node.exe")


def test_unrelated_native_executable_remains_opaque() -> None:
    assert not source_audited_interpreter("productpython.exe")
    assert not source_audited_interpreter("packages/runtime/opaque-owner.exe")
