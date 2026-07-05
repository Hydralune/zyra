# Tests

Tests are organized by blast radius:

- `unit` for schema and pure module checks
- `integration` for multi-module runtime behavior
- `scenarios` for competition-aligned harness tests

Current scenario coverage:

- `tests.scenarios.test_m2_runtime_acceptance` runs a cross-module M2 acceptance path covering CodeWorker file/edit/shell execution, BrowserWorker local page extraction, trace/checkpoint tools, and context session clearing.
- `tests.scenarios.test_m2_demo_scenarios` verifies the reusable M2 scenario runner for software engineering, browser research, and dynamic requirement/failure injection reports.
