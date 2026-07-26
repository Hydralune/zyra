# M3-S01A-01 source role, dependency, and custody audit

## Verdict

The slice is complete as an inventory and release-gate implementation. It does
not claim that Zyra is freeze-ready. The frozen target
`a8273df7601b57cf3fecfb03936c815b9ca63f38` produces a deterministic inventory
receipt and a machine-readable M3-01B remediation queue. Inventory mode is
valid; candidate mode correctly rejects the current repository because
unresolved release debt remains.

The final two full scans, including `vendor/**` and `vendor-runtimes/**`,
produced the same receipt digest:
`sha256:ab935910aa18f3e8ce4bf0d9292b1dae551bc70ab48a72e5d54b6845831048bb`.

## What became Zyra-owned

The implementation lives in
`packages/integrations/zyra_integrations/source_custody/` and is invoked through
the `zyra-source-custody` project command or `scripts/audit_source_custody.py`.
It owns the following executable stages:

1. strict source-role and historical-boundary catalog parsing;
2. repository inventory, archive/binary/minified/generated/symlink detection;
3. Python AST and TypeScript/JavaScript lexical/semantic analysis;
4. Python, npm/Bun, and Rust manifest and lockfile custody;
5. child process, listener port, Docker, MCP, plugin, and runtime-download
   custody;
6. package annotation, vendor map, NOTICE, and historical ledger
   cross-consistency;
7. broad LangGraph, OpenClaw, source-specific, opaque-runtime, and source-pool
   similarity checks;
8. deterministic severity policy and checksum-bound M3-01B work queue;
9. inventory, candidate, and freeze CLI modes with atomic evidence writes.

No runtime dependency, package, process, port, Docker service, MCP server, or
network download was added by this slice. `pyproject.toml` only gained the CLI
entry point.

## Source-role resolution

The catalog contains 28 bounded decisions over 14 capabilities and 12 required
repositories. There are 25 active and 3 inactive entries. Every active
capability has exactly one primary source and no more than two supplementary
sources.

| Source | Active role boundary |
| --- | --- |
| `claude-code-best` | primary CodeWorker loop, permission, MCP, skill/subagent, and console |
| `opencode` | primary provider and session projection; supplementary console |
| `browser-use` | primary browser runtime |
| `OpenHands` | primary workspace; supplementary session projection and console |
| `agentscope` | primary retrieval; supplementary workspace |
| `hermes-agent` | bounded supplementary permission, MCP, skill/subagent, and provider |
| `langgraph` | primary semantic source only for narrow checkpoint exact-resume |
| `oh-my-pi` | bounded supplementary skill/subagent, workspace, provider, and retrieval |
| `agent-framework` | inactive MCP conformance only |
| two `claudecode-related/**` repositories | inactive analysis references only |
| `zyra` | primary dynamic topology and immutable graph state |

OpenClaw has no forward source-role entry. It is represented by exactly one
`excluded_forward_only` historical boundary and one `historical_only` notice.
Fifteen existing OpenClaw path/ledger literals were found in protected
audit/deny/remediation or historical ledger paths and were queued for
classification; no OpenClaw package, manifest dependency, source tree, runtime
role, or restored repository was introduced.

## Final inventory

The full scan covered 6,101 files, 2,222,547 lines, 120,419,510 bytes, and 3,689
vendor-like files. Python parsed 964/964 files. JavaScript/TypeScript parsed
842/842 files after adding regular-expression, division, non-null assertion,
and nested-template handling.

The audit normalized 602 findings into 251 work items:

- 143 release-blocking work items;
- 114 P0, 13 P1, and 124 P2 items;
- 246 items assigned to M3-01B and 5 UI-facing items assigned to M3-02B;
- actions: 21 absorb, 89 declare, 3 externalize, 11 remove, 6 repair,
  120 track, and 1 verify.

The largest finding groups are:

| Finding | Count | Interpretation |
| --- | ---: | --- |
| `ledger_active_target_missing` | 158 | old ledger target bindings no longer resolve exactly |
| `python_process_command_nonliteral` | 105 | process argv/custody cannot be frozen statically |
| `process_use_undeclared` | 56 | process call has no unique declared profile |
| `omp_external_process` | 41 | OMP-shaped external-process boundary needs M3 classification |
| `python_parent_source_path` | 31 | source-repository path literal or cleanroom check needs classification/removal |
| `javascript_source_path_literal` | 27 | TypeScript source path literal needs classification/removal |
| `runtime_sqlite_custody` | 21 | SQLite state requires explicit owner/restore evidence |
| `python_dependency_undeclared` | 20 | direct Python import is absent from the owning manifest |
| `python_dynamic_import_nonliteral` | 18 | dynamic import needs an allowlisted registry |
| `javascript_shell_process_call` | 16 | shell-mediated command needs removal or externalization |
| `javascript_dependency_undeclared` | 16 | direct JS import is absent from the owning manifest |
| `langgraph_forbidden_symbol_reachable` | 9 | broad LangGraph terms remain outside the narrow role |
| `source_license_unresolved` | 5 | active Claude-derived entries retain an unresolved license decision |

The similarity scan was dynamically reached and found one 0.988561 match
between the Zyra browser serializer and its `vendor/browser-use` source-pool
counterpart. This is intentionally queued for a custody decision; the scanner
does not treat directory placement as proof of internalization.

## State, build, and health custody

Fifteen package-level `zyra-source.json` annotations connect active catalog
entries to owners, default reachability, build/health entry points, and state
custody. The process catalog contains 16 release/build/test/sidecar profiles.
The checked-in vendor map contains all 28 catalog digests and the OpenClaw
historical-boundary marker. NOTICE contains 13 source notices.

The audit read 1,294 historical ledger entries over 11 historical sources.
Those entries remain historical facts, not forward source-role decisions.
`ledger_active_target_missing` items are routed to M3-01B rather than silently
rewriting protected M1/M2 records.

## LangGraph and CodeWorker boundaries

Structural checks passed for all four protected domains:

- Zyra-owned dynamic topology mutation;
- immutable branch-local graph state and deterministic commit;
- narrow checkpoint exact-resume semantics;
- cohesive TypeScript CodeWorker reason/tool/observe/revise loop.

Nine broad LangGraph symbol references remain in existing evaluation or ledger
sync code and are P0 removal/classification work. The catalog gives LangGraph
no StateGraph, channel/reducer, Pregel, ToolNode, Store, stream, SDK, server, or
deployment owner.

## Effective-code audit

The gate counts 8,205 effective production lines against the 4,500-line
minimum, a margin of 3,705. It excludes 621 test lines, 1,275 catalog/evidence
data and documentation lines, 216 audit-tool lines, 206 provenance lines, 28
exports, and AST-classified DTO/schema/type/comment lines.

Production allocation is:

| Runtime responsibility | Effective lines |
| --- | ---: |
| dependency/process/repository inventory | 2,429 |
| source-role and custody | 1,663 |
| Python and JavaScript semantic analysis | 1,745 |
| source-specific and opaque risk | 876 |
| deterministic policy and queue | 583 |
| release-audit engine and CLI | 573 |
| audit contracts after DTO exclusion | 336 |

Ten files exceed 500 physical lines. The effective-code report records their
top-level symbols, method lists, physical/effective lines, and split
assessment. Each file owns one audit domain; splitting parser state from its
normalization and finding production would weaken the mutation boundary. No
large file is data-as-code, generated output, or an upstream directory-shaped
copy.

## Adversarial and disable evidence

The integration mutation matrix injects a distinct defect for every rule group.
With all rules enabled, all defects are found. Disabling each group causes only
its dedicated defect to survive:

| Disabled rule | Escaping defect |
| --- | --- |
| `catalog` | unresolved active source license |
| `roles` | duplicate active primary |
| `dependencies` | undeclared Python dependency |
| `processes` | undeclared child process |
| `custody` | missing active package annotation |
| `langgraph` | broad StateGraph import |
| `opaque` | binary in a production tree |
| `source_specific` | Hermes CLI-shaped runtime |

Additional tests cover OpenClaw rejection, inactive reference/runtime
contradictions, source-path imports, JavaScript comment/regex/template
handling, wildcard test-process profiles, cache exclusion, minified JSON
classification, atomic pretty receipts, candidate exit behavior, and
case-variant queue determinism.

## Critical self-review

The implementation was revised in response to five concrete review findings:

1. `.tmp` was initially traversed, making a scan exceed two minutes. The
   repository scanner now excludes ephemeral `.tmp`, with a regression test.
2. generic `.exec()` calls were initially classified as child processes.
   Process recognition now requires a known process import or runtime object;
   regex `.exec()` is a negative test.
3. the JavaScript lexer initially failed on regex literals, non-null division,
   and nested templates. Final full-scan parse coverage is 842/842.
4. single-line machine JSON self-triggered the minified-source rule and direct
   evidence output caused receipt self-reference. Atomic output is now pretty
   JSON, minified checks apply only to source, and release evidence is generated
   under excluded `.tmp` before being copied into the evidence tree.
5. `OpenHands`/`openhands` ties made queue ordering process-hash-dependent.
   A secondary case-sensitive sort and reversed-input regression made two full
   scans byte-semantically deterministic.

No ordinary slice finding was “fixed” by editing protected earlier M1/M2
owners. Existing submission-boundary failures and ledger discrepancies remain
visible in the M3-01B queue.

## Verification

- `python -m pytest -q tests/unit/test_source_custody_audit.py tests/integration/test_source_custody_repository_audit.py`
  passes the slice behavior and eight-rule mutation matrix.
- adjacent source/ledger/vendor tests produced 35 passes and one pre-existing
  `test_submission_boundary` failure. The failing literals are in unchanged
  protected modules; `git diff` from the baseline contains no edit to them.
- `python scripts/audit_m3_s01a01_effective_code_gate.py --target
  a8273df7601b57cf3fecfb03936c815b9ca63f38 --summary-only --fail-on-gate`
  passes at 8,205 effective lines.
- two full vendor-inclusive inventory commands return exit 0 and identical
  receipt/policy digests.
- candidate mode returns exit 2 while release blockers remain.
- `python -m compileall` and `git diff --check` pass for the slice files.
- Ruff was not run because the checked-in virtual environment has no Ruff
  module or executable; no dependency was installed solely for this slice.

## Handoff

M3-01B must consume
`packages/integrations/zyra_integrations/data/m3_01b_source_custody_work_queue.json`.
It should resolve or explicitly disposition every P0/P1 item, starting with
unresolved Claude license custody, broad LangGraph reachability, parent source
paths, opaque process custody, undeclared dependency/process/port items, and
stale active ledger targets. M3-02B owns the five UI-facing queue items.

This slice adds audit custody only. It does not migrate runtime owners, rewrite
protected historical ledger facts, restore OpenClaw, or declare the repository
freeze-ready.
