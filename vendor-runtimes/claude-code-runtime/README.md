# Zyra Claude Code Productized Runtime

## Source

- Primary source repository: `claude-code-best`
- Productized source mount: `vendor-runtimes/claude-code-runtime/productized/claude-code-best`
- Inventory: `metadata/productized_source_inventory.json`
- Manifest: `src/zyra-productized-manifest.mjs`

The copied runtime source is selected from `claude-code-best` only. The two auxiliary repositories under
`claudecode-related` are reference-only inputs used to check boundaries and omissions.

## Purpose

This runtime boundary gives Zyra a local, submission-contained source base for the M1 Claude Code
productization path: QueryEngine, query/session flow, tool orchestration, context/compact, permission,
MCP, SkillTool, AgentTool, and core tool implementations. `apps/code-worker` reads this productized
runtime before falling back to the broader vendor snapshot.

## Entry Points

- Health: `node vendor-runtimes/claude-code-runtime/src/zyra-productized-smoke.mjs`
- Inventory: `node vendor-runtimes/claude-code-runtime/src/zyra-productized-smoke.mjs --inventory`
- Extraction: `python scripts/zyra_source_extract.py productize-claude-code --write-scaffold --write-crosswalk --write-ledger --update-seed`
- Sidecar verification: `python scripts/verify_code_worker_sidecar.py`

## Auxiliary Reference Repositories

`claudecode-related/claude-reviews-claude` supplies the section-level checklist for QueryEngine,
tool system, context/compact, startup/bootstrap, services, subagent, and command coverage.
`claudecode-related/Dive-into-Claude-Code` is used to sanity-check harness and runtime boundaries.
Neither repository is copied into this runtime, required at runtime, or counted as effective code.
The reference mapping is recorded in `metadata/reference_crosswalk.json`.

## Effective Code Accounting

Generated inventories, crosswalks, and ledger seeds are data artifacts and are excluded from effective
line counts. Upstream files that contain `Auto-generated type stub` are retained only when they are
part of the selected Claude Code import boundary; their lines are reported separately and excluded
from effective line counts.

## Replacement Plan

M1-02B, M1-02C, and M1-02D bind this productized source to Zyra's query loop, tool loop, session
lifecycle, compact/restore, and CodeWorker API. M1-03A through M1-03D continue permission, MCP,
SkillTool, and subagent integration. The runtime must remain inside `zyra`; it must not depend on
the parent-level Claude Code checkout or the auxiliary repositories at submission time.
