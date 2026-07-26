# M3-S01A-01 Source-role / dependency / custody audit decision

Date: 2026-07-26

## 1. Frozen implementation boundary

- Slice: `M3-S01A-01`
- Baseline commit: `1d19ea39a8313091dcfbc00f78c79fdfdeab7cf4`
- Minimum effective production code: 4,500 lines
- Target responsibility: release/freeze source-role, dependency, process, path,
  license, package-annotation, ledger, and opaque-runtime audit.
- Migration mode: `audit_only_no_runtime_migration`
- Canonical runtime owner transfer: none
- Runtime default-policy change: none
- New external dependency, service, process, port, MCP server, plugin, Docker
  runtime, or dynamic installer: none

This decision is frozen before production implementation. The audit runtime may
read repository source, manifests, lockfiles, package annotations, notice files,
the bundled internalization ledger, and prior frozen source-role evidence. It may
only produce derived findings, receipts, reports, and an M3-01B work queue. It
must not become the source of truth for task, session, permission, topology,
checkpoint, scheduler, memory, worker, artifact, or UI state.

## 2. Source and language decision

This slice does not migrate upstream production code. Its inputs retain their
actual source languages and roles; the Zyra-owned audit implementation is
Python. `target_language=Python` below describes the audit implementation, not a
rewrite of the audited runtime.

| Audited source/input | Source role in this slice | Source language / format | Target | Target language | Migration mode | Canonical owner |
| --- | --- | --- | --- | --- | --- | --- |
| Zyra Python runtime and scripts | audit input | Python | `packages/integrations/zyra_integrations/source_custody/**` | Python | `audit_only_no_runtime_migration` | unchanged M1/M2 owners |
| Zyra TypeScript/TSX/JavaScript runtime | audit input | TypeScript, TSX, JavaScript | same audit package | Python | `audit_only_no_runtime_migration` | unchanged M1/M2 owners |
| Zyra Rust/native build inputs, when present | audit input | Rust and native build metadata | same audit package | Python | `audit_only_no_runtime_migration` | unchanged selected package owners |
| manifests, locks, MCP/plugin/process/deploy config | audit authority input | JSON, TOML, YAML, JSONL, text | same audit package | Python | `audit_only_no_runtime_migration` | package/build owners remain canonical |
| bundled internalization ledger and M2 source-role disposition | historical/audit authority input | JSON | normalized custody view and findings | Python | `audit_only_no_runtime_migration` | historical ledger facts remain protected |
| upstream source repositories named by frozen source decisions | provenance identity only | actual per-entry language | source catalog validation only | Python | `audit_only_no_runtime_migration` | no upstream runtime custody |

No source row uses `mixed` or `unknown`. Multi-language sources are represented
as explicit language sets, for example `["python", "typescript", "tsx"]`.

## 3. Forward source-role boundaries

- `claude-code-best` remains the principal TypeScript source for the cohesive
  CodeWorker query/tool/session/permission/compact/skill/subagent boundary.
- `claudecode-related/claude-reviews-claude` and
  `claudecode-related/Dive-into-Claude-Code` are reference-only analysis aids.
  They receive no runtime, vendor, effective-line, or migration quota.
- `opencode`, browser-use, OpenHands, AgentScope, Agent Framework, Hermes,
  LangGraph, and Oh My Pi are recorded per capability with their frozen
  primary/supplementary/conformance/reference roles and landing states.
- LangGraph is eligible only as the narrow primary semantic source for
  checkpoint identity, lineage, pending/committed separation, atomic commit,
  interrupt/resume correlation, and exact restore. StateGraph, Pregel, generic
  channels/reducers, ToolNode/prebuilt, Store, stream controller, SDK, server,
  and deploy surfaces are forbidden from the default production path.
- OpenClaw receives no source-role row. A separate historical-boundary record
  permits only pre-exclusion provenance/license retention and verifies that no
  root-source path, package, process, Docker context, or runtime dependency was
  reintroduced. The deleted source repository will not be read or restored.

Each active capability may have exactly one primary implementation source and at
most two supplementary implementation sources. Conformance, reference,
experimental, deferred, and rejected rows are inactive and their lack of
migration is not a missing-capability finding.

## 4. Product responsibilities and module plan

The 4,500-line minimum is assigned to executable audit behavior:

1. strict catalog parsing, normalization, identity, path, language, role, status,
   owner, runtime-entry, test-entry, and rationale validation;
2. role cardinality and landing-state policy with explicit inactive semantics;
3. repository inventory and ignore/candidate classification;
4. Python AST import/call/process/dynamic-import/path analysis;
5. TypeScript/JavaScript lexical import/process/port/download/dynamic-install
   analysis with comment/string-aware tokenization;
6. manifest and lockfile dependency resolution for pip, setuptools, npm/Bun,
   workspaces, editable/path/link dependencies, and undeclared packages;
7. MCP/plugin/process/port/Docker/config declaration and call-site custody;
8. vendor map, NOTICE, package annotation, legacy ledger, and catalog
   cross-consistency;
9. LangGraph forbidden broad-runtime and narrow exact-resume checks;
10. source-specific risk rules for OpenHands external SDK, Hermes HOME/SQLite/
    CLI, OpenCode V1/V2/dynamic install/job, Oh My Pi JSONL/SQLite/Bun/native,
    and the OpenClaw historical boundary;
11. opaque/minified/binary/archive/vendor-like/source-similarity/undeclared
    download detection;
12. normalized finding policy, blocker calculation, M3-01B work-queue routing,
    deterministic receipts, release/freeze CLI, and mutation/disable probes.

Planned production modules:

- `source_custody/model.py`
- `source_custody/catalog.py`
- `source_custody/repository.py`
- `source_custody/python_analyzer.py`
- `source_custody/javascript_analyzer.py`
- `source_custody/dependencies.py`
- `source_custody/processes.py`
- `source_custody/custody.py`
- `source_custody/risks.py`
- `source_custody/policy.py`
- `source_custody/engine.py`
- `source_custody/cli.py`

Large files are permitted only when their symbol map, main-path position,
mutable state, error paths, failure behavior, and concentration are reviewed in
the slice effective-line report. DTO-only, generated, report data, fixture,
mock, test, documentation, package annotation, source map, NOTICE, and thin
launcher lines do not count toward the minimum.

## 5. Main path and fail-closed behavior

```text
release/freeze CLI
  -> resolve clean Zyra repository boundary
  -> parse custody catalog and protected historical ledger
  -> inventory repository/manifests/config/source/artifacts
  -> run role/dependency/process/custody/risk analyzers
  -> normalize and apply deterministic policy
  -> emit checksum-bound receipt and M3-01B work queue
  -> exit non-zero when release blockers exist
```

Missing active entry/target/test/owner, duplicate primary, more than two
supplements, inactive role used as runtime, undeclared dependency/process/port/
download, black-box core decision, parent-source path, OpenClaw reintroduction,
and forbidden broad LangGraph runtime usage fail closed. An inactive source with
a bounded rationale and no runtime dependency does not fail.

Disabling the role, dependency, custody, LangGraph, process, or opaque rules must
cause the corresponding injected negative case to be missed. Mutation tests
must therefore fail the mutation-survival gate when any required rule is
disabled.

## 6. Validation boundary

This ordinary audit slice runs focused unit/integration behavior, injected
negative fixtures, disable/mutation probes, actual-repository reachability,
ledger/catalog/NOTICE/package-annotation consistency, source-language custody,
dependency/path scans, and the strict effective-line audit. It does not
mechanically repeat the full M3-01 numeric-stage cleanroom or all-repository
test matrix. If implementation changes the default release/install/start
process graph or adds an external dependency, the slice is upgraded before
completion.
