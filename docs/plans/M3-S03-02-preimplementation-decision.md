# M3-S03-02 Pre-implementation Decision

Date: 2026-07-28

## Frozen boundaries

| Boundary | Frozen value |
| --- | --- |
| Slice | `M3-S03-02 Final Freeze Submission And Handoff` |
| Slice baseline commit | `98a001a44f2e506c0ef0144e912c3ba55699f11b` |
| M3-03 numeric-stage baseline | `944846fd484b465b3c4e2b4ec87565752b4baf67` |
| Parent minimum | 9,000 conservative effective production lines |
| Slice minimum | 4,500 conservative effective production lines |
| Review scope | M3-03 incremental implementation and its affected paths only |
| Runtime mutation | Forbidden; M1/M2 and M3-01/02 protected owners are read-only evidence inputs |
| Migration mode | `report_and_freeze_only` |
| Target implementation language | Python |

The user explicitly limited the cumulative critical review to the M3-03
increment.  The final gate still verifies inherited first-stage evidence,
release identities, live-run receipts, requirement coverage, clean-install
receipts, and source-custody receipts, but it does not reopen the
implementation diffs or protected completion facts of M1, M2, M3-01, or
M3-02.  A contradiction or missing inherited receipt blocks freeze; it is not
silently rewritten.

## Product responsibilities frozen before implementation

The slice has enough real product responsibility to implement the documented
minimum without report templates, generated findings, repeated launchers, or
data-as-code:

1. A final critical-review engine that consumes independently verifiable M3
   inputs, evaluates bug/regression, owner, source, runtime, evidence,
   schedule, clean-release, and submission risks, and emits a blocking
   verdict.
2. A reverse-schedule gate for RC1, rehearsal/feature freeze, final
   bundle/checksum/checklist, and official submission, including owner,
   status, evidence, blocker, transition, and late-change enforcement.
3. A submission custody runtime for deterministic manifests, naming rules,
   material inventory, checksum trees, dual-review attestations, email
   checklist records, bundle assembly, and independent tamper verification.
4. A rehearsal runtime that executes bounded health, evidence-navigation,
   fault/recovery, offline, restart, and provider-failure drills against the
   normal product entrypoints and rejects root-source dependencies, manual
   state edits, or special demo runtimes.
5. A typed evidence-navigation and demonstration runbook builder that links
   default entry, short health, both formal cases, canonical mutations,
   fault/recovery, and final artifacts.
6. A second-stage handoff classifier that distinguishes first-stage blockers,
   CI hardening, and pure optimizations and forbids first-stage blockers from
   being deferred.
7. A final-freeze orchestrator and independent verifier that bind the
   M3-S03-01 archive, release/benchmark/custody receipts, review verdict,
   schedule, rehearsal, submission bundle, handoff, target commit, and hash
   chain into one immutable decision.
8. Product CLI entrypoints for build, verify, dry-run, and rehearsal, plus the
   required `verify_first_stage.py` entrypoint.

Generated JSON, Markdown, ZIP archives, run logs, raw metrics, manifests,
checksums, reports, and archive members are evidence/data outputs and are
excluded from production line credit.  Test code is also excluded.

## Source, language, and migration decision

M3-S03-02 creates no new upstream implementation role and migrates no source
runtime.  Existing active sources and owners remain frozen evidence inputs.

| Input role | Source repository / code boundary | Source language | Zyra target | Target language | Migration mode | Canonical owner | Decision |
| --- | --- | --- | --- | --- | --- | --- | --- |
| protected runtime evidence | Existing Zyra owners derived from `claude-code-best`, `opencode`, browser-use, OpenHands, AgentScope, Hermes, Oh My Pi, and narrow LangGraph recovery semantics | Python, TypeScript/TSX, Rust/native as recorded by M3-01/02 | Read-only receipt adapters in `packages/evaluation/zyra_evaluation/final_freeze` | Python | `report_and_freeze_only` | Existing M1/M2 owners | No runtime code migration or owner transfer |
| report/archive evidence | M3-S03-01 `freeze_reporting` outputs | Python | `packages/evaluation/zyra_evaluation/final_freeze` | Python | `same_language_module_integration` | `final_freeze` for freeze-decision state only | Reuse verified output contracts; do not redefine task success |
| release/deployment evidence | M3-S02B release and semantic-health receipts | Python and TypeScript/TSX | Final freeze admission and rehearsal ports | Python | `protocol_adapter` | Existing release/deployment owners | Read and verify immutable receipts only |
| benchmark evidence | M3-S02A formal benchmark and ablation receipts | Python | Critical-review and navigation ports | Python | `protocol_adapter` | Existing benchmark owner | Read and verify immutable receipts only |
| source/owner evidence | M3-S01A/B custody and owner receipts | Python and TypeScript/TSX references | Critical-review owner/source gates | Python | `protocol_adapter` | Existing source-audit/productization owners | Read and verify immutable receipts only |

The `same_language_module_integration` row applies only to Zyra's existing
Python `freeze_reporting` product contracts and therefore requires non-zero
Python executable production.  It does not create a quota for any upstream
runtime language.  Protocol adapters are excluded unless a line contains
substantive validation, error handling, custody, state transition, or
fail-closed control flow.

OpenClaw remains `excluded_forward_only`; only protected provenance, license,
and no-runtime-dependency results may appear.  LangGraph remains limited to
checkpoint lineage, pending/committed writes, side-effect fencing, and exact
resume semantics.  StateGraph, Pregel, channels/reducers, ToolNode, Store,
stream controllers, SDK, and server surfaces are invalid release imports.

## State and side-effect ownership

- Existing task, session, permission, memory, scheduler, recovery, artifact,
  checkpoint, provider, MCP/skill, terminal/browser, deployment, release, and
  benchmark owners remain unchanged.
- `final_freeze` owns only review observations, schedule/checklist state,
  rehearsal receipts, dual-review attestations, submission manifest/checksum
  state, handoff classification, and the immutable freeze decision.
- Bundle writes use create-new staging and atomic replacement.  Hashes are
  computed before a manifest is admitted.  Verification never trusts a
  generation receipt.
- Commands are bounded, timeout-controlled, cwd-scoped to the Zyra repository,
  environment-whitelisted, output-limited, and secret-redacted.
- A freeze decision may transition only from `candidate` to `blocked` or
  `frozen`.  Evidence changes after freeze produce a tamper verdict; they do
  not mutate the accepted decision.

## Required behavior and mutation evidence

- Removing a required score, live-run, release, source-custody, schedule,
  checksum, reviewer, or rehearsal receipt blocks freeze.
- Reclassifying a first-stage blocker as CI hardening or optimization is
  rejected.
- A tampered bundle member, manifest, checksum tree, freeze decision, or
  M3-S03-01 archive is rejected by an independent verifier.
- A missing default entry, source repository reference, special demo runtime,
  manual state-edit step, unbounded command, or provider-required false
  success blocks rehearsal.
- Disabling the final critical-review, schedule, rehearsal, submission, or
  handoff gate causes the corresponding negative test to fail or the freeze
  admission to be denied.
- The final verifier checks identities and bytes from disk and does not call
  the builder's success path.

## Commit lifecycle

1. This document is committed as the pre-implementation decision.
2. Production implementation and directly related tests are committed as the
   implementation commit.
3. M3-03-only review, cleanroom/rehearsal, effective-line audit, generated
   evidence, state update, and handoff records are committed later as the
   evidence commit.

