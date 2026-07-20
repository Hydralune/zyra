from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from zyra_code_index import (
    BoundWorkspaceSource,
    CodeIndexRuntime,
    ContentSearchMode,
    ContentSearchQuery,
    InjectedLspAdapter,
    PathPolicyError,
    SearchBudget,
    StaleWorkspaceError,
    SymbolCapability,
    SymbolProvenance,
    SymbolQuery,
    WorkspacePatchIndexBridge,
    WorkspacePathPolicy,
)


class CodeIndexFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "vendor").mkdir()
        (self.root / ".git").mkdir()
        (self.root / "src" / "app.py").write_text(
            """\
class RecoveryService:
    \"\"\"Coordinates a fenced recovery generation.\"\"\"

    def recover(self, value: str) -> str:
        return helper(value)


def helper(value: str) -> str:
    category = value
    cat = \"exact cat token\"
    return f\"recovery {category} {cat}\"
""",
            encoding="utf-8",
        )
        (self.root / "src" / "service.ts").write_text(
            """\
export interface LeaseState { generation: number }
export function publishGeneration(state: LeaseState): number {
  return state.generation
}
""",
            encoding="utf-8",
        )
        (self.root / "tests" / "test_app.py").write_text(
            """\
from src.app import RecoveryService, helper

def test_recovery_service():
    assert \"recovery\" in RecoveryService().recover(\"value\")
    assert helper(\"value\")
""",
            encoding="utf-8",
        )
        (self.root / "vendor" / "secret.py").write_text("SECRET = 'must not index'\n", encoding="utf-8")
        (self.root / ".git" / "config").write_text("recovery secret\n", encoding="utf-8")
        (self.root / "src" / "bundle.min.js").write_text("function hiddenRecovery(){}\n", encoding="utf-8")
        self.index_path = self.root / "state" / "code-index.sqlite3"
        source = BoundWorkspaceSource.for_test(self.root, binding_revision=1)
        self.runtime = CodeIndexRuntime(source, index_path=self.index_path)
        self.build = self.runtime.rebuild()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_discovery_content_budget_and_ignore_policy(self) -> None:
        indexed_paths = [item.logical_path for item in self.runtime.store.files("test-workspace")]
        self.assertEqual(indexed_paths, ["src/app.py", "src/service.ts", "tests/test_app.py"])
        self.assertNotIn("vendor/secret.py", indexed_paths)
        self.assertNotIn("src/bundle.min.js", indexed_paths)
        self.assertGreaterEqual(self.build.ignored_count, 3)

        whole_word = self.runtime.search(
            ContentSearchQuery(pattern="cat", whole_word=True)
        )
        self.assertTrue(whole_word.matches)
        self.assertTrue(all(match.matched_text.casefold() == "cat" for match in whole_word.matches))

        multiline = self.runtime.search(
            ContentSearchQuery(
                pattern=r"def helper\(.*?return f",
                mode=ContentSearchMode.REGEX,
                multiline=True,
                paths=("src/app.py",),
                budget=SearchBudget(
                    maximum_results=10,
                    maximum_candidate_files=10,
                    maximum_scanned_bytes=100_000,
                    maximum_output_chars=10_000,
                ),
            )
        )
        self.assertEqual(len(multiline.matches), 1)
        self.assertEqual(multiline.matches[0].logical_path, "src/app.py")
        self.assertGreater(multiline.matches[0].line_end, multiline.matches[0].line_start)

        paged = self.runtime.search(
            ContentSearchQuery(
                pattern="recovery",
                budget=SearchBudget(
                    maximum_results=1,
                    maximum_candidate_files=10,
                    maximum_scanned_bytes=100_000,
                    maximum_output_chars=10_000,
                    head_limit=1,
                ),
            )
        )
        self.assertEqual(len(paged.matches), 1)
        self.assertTrue(paged.truncated)
        self.assertGreater(paged.next_offset, 0)

    def test_python_ast_and_structural_fallback_supply_symbol_operations(self) -> None:
        document = self.runtime.symbols(
            SymbolQuery(
                capability=SymbolCapability.DOCUMENT_SYMBOL,
                logical_path="src/app.py",
            )
        )
        names = {item.name for item in document.symbols}
        self.assertTrue({"RecoveryService", "recover", "helper"}.issubset(names))
        self.assertTrue(
            all(item.provenance is SymbolProvenance.PYTHON_AST for item in document.symbols)
        )

        definition = self.runtime.symbols(
            SymbolQuery(capability=SymbolCapability.DEFINITION, name="helper")
        )
        self.assertEqual(definition.symbols[0].qualified_name, "helper")

        references = self.runtime.symbols(
            SymbolQuery(capability=SymbolCapability.REFERENCES, name="helper")
        )
        self.assertTrue(any(item.location.logical_path == "src/app.py" for item in references.references))

        calls = self.runtime.symbols(
            SymbolQuery(capability=SymbolCapability.CALL_HIERARCHY, name="helper")
        )
        self.assertTrue(any(item.callee_name == "helper" for item in calls.call_edges))

        typescript = self.runtime.symbols(
            SymbolQuery(
                capability=SymbolCapability.DOCUMENT_SYMBOL,
                logical_path="src/service.ts",
            )
        )
        self.assertTrue({"LeaseState", "publishGeneration"}.issubset({item.name for item in typescript.symbols}))
        self.assertTrue(
            all(item.provenance is SymbolProvenance.STRUCTURAL_FALLBACK for item in typescript.symbols)
        )

    def test_injected_lsp_is_runtime_reachable_and_degrades_to_local_index(self) -> None:
        source = BoundWorkspaceSource.for_test(self.root, binding_revision=1)

        class Client:
            server_id = "test-language-server"

            def __init__(self) -> None:
                self.outside_workspace = False

            def request(self, method, params):
                self.last_request = (method, params)
                target = (
                    self.root.parent / "outside.py"
                    if self.outside_workspace
                    else self.root / "src" / "app.py"
                )
                return [
                    {
                        "name": "helper",
                        "kind": 12,
                        "location": {
                            "uri": target.as_uri(),
                            "range": {
                                "start": {"line": 8, "character": 0},
                                "end": {"line": 8, "character": 6},
                            },
                        },
                    }
                ]

            def notify(self, method, params):
                del method, params

        client = Client()
        client.root = self.root
        runtime = CodeIndexRuntime(
            source,
            index_path=self.root / "state" / "lsp-code-index.sqlite3",
            lsp_adapter=InjectedLspAdapter(
                source,
                client,
                capabilities=(SymbolCapability.DEFINITION,),
            ),
        )
        runtime.rebuild()
        result = runtime.symbols(
            SymbolQuery(capability=SymbolCapability.DEFINITION, name="helper")
        )
        self.assertEqual(result.provenance, SymbolProvenance.LSP)
        self.assertEqual(result.symbols[0].location.logical_path, "src/app.py")
        self.assertGreater(result.generation, 0)
        self.assertTrue(runtime.status()["lsp"]["available"])
        self.assertFalse(runtime.status()["lsp"]["auto_install"])
        self.assertFalse(runtime.status()["lsp"]["auto_start_process"])

        client.outside_workspace = True
        degraded = runtime.symbols(
            SymbolQuery(capability=SymbolCapability.DEFINITION, name="helper")
        )
        self.assertEqual(degraded.provenance, SymbolProvenance.PYTHON_AST)
        self.assertTrue(degraded.degraded)
        self.assertIn("invalid_lsp_result", degraded.degradation_reason)
        self.assertIn("fallback:python_ast", degraded.degradation_reason)
        self.assertEqual(degraded.symbols[0].qualified_name, "helper")

    def test_patch_invalidation_rebuilds_revision_and_changes_test_selection(self) -> None:
        selected = self.runtime.select_tests(("src/app.py",))
        self.assertIn("tests/test_app.py", selected.selected_tests)
        old_generation = selected.generation

        app = self.root / "src" / "app.py"
        app.write_text(app.read_text(encoding="utf-8").replace("exact cat token", "new otter token"), encoding="utf-8")
        source_v2 = BoundWorkspaceSource.for_test(self.root, binding_revision=2)
        runtime_v2 = CodeIndexRuntime(source_v2, index_path=self.index_path)
        rebuilt = runtime_v2.invalidate_patch(
            transaction_id="transaction-2",
            changed_paths=("src/app.py",),
        )
        self.assertEqual(rebuilt.generation, old_generation + 1)
        self.assertFalse(runtime_v2.search(ContentSearchQuery(pattern="exact cat token")).matches)
        self.assertTrue(runtime_v2.search(ContentSearchQuery(pattern="new otter token")).matches)
        with self.assertRaises(StaleWorkspaceError):
            self.runtime.search(ContentSearchQuery(pattern="recovery"))

    def test_windows_and_traversal_path_bypasses_are_rejected(self) -> None:
        policy = WorkspacePathPolicy(self.root)
        invalid = (
            "../outside.py",
            "C:/outside.py",
            "C:\\outside.py",
            "\\\\server\\share\\x.py",
            "//server/share/x.py",
            "src/app.py:secret",
            "src/../outside.py",
            "NUL.txt",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(PathPolicyError):
                policy.normalize_logical(value)

    def test_patch_event_dereferences_canonical_transaction_and_is_idempotent(self) -> None:
        app = self.root / "src" / "app.py"
        app.write_text(
            app.read_text(encoding="utf-8").replace("exact cat token", "canonical otter token"),
            encoding="utf-8",
        )
        runtime_v2 = CodeIndexRuntime(
            BoundWorkspaceSource.for_test(self.root, binding_revision=2),
            index_path=self.index_path,
        )
        transaction = SimpleNamespace(
            transaction_id="canonical-transaction-3",
            workspace_id="test-workspace",
            phase="committed",
            path_results=(
                SimpleNamespace(
                    logical_path="src/app.py",
                    operation="update",
                    after_hash="canonical-after-hash",
                ),
            ),
        )

        class Resolver:
            def require_transaction(self, transaction_id: str):
                if transaction_id != transaction.transaction_id:
                    raise KeyError(transaction_id)
                return transaction

            def list_transactions(self, workspace_id: str):
                return (transaction,) if workspace_id == "test-workspace" else ()

        bridge = WorkspacePatchIndexBridge(Resolver(), lambda workspace_id: runtime_v2)
        receipt = bridge.apply_event(
            {
                "payload": {
                    "transaction_id": transaction.transaction_id,
                    "changed_paths": ["../payload-must-not-be-trusted.py"],
                }
            }
        )
        duplicate = bridge.apply_event(
            {"payload": {"transaction_id": transaction.transaction_id}}
        )

        self.assertEqual(receipt.disposition, "rebuilt")
        self.assertEqual(receipt.changed_paths, ("src/app.py",))
        self.assertTrue(
            runtime_v2.search(ContentSearchQuery(pattern="canonical otter token")).matches
        )
        self.assertEqual(duplicate.disposition, "recorded")
        self.assertEqual(duplicate.generation, receipt.generation)
        self.assertFalse(runtime_v2.store.pending_invalidations("test-workspace"))


if __name__ == "__main__":
    unittest.main()
