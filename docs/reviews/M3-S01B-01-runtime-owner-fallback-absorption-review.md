# M3-S01B-01 Runtime Owner And Fallback Absorption Review

## Verdict

`M3-S01B-01` is complete at implementation revision
`d65856eebafd2a046953886b4605ec4ed2c06642`.

The slice consumes the checksum-bound M3-01A input without changing its
identities:

- queue digest:
  `sha256:f5b931f50c960856dfa7c8197d1c22d7c4ec3f6110832ed5114143ef4eb3d4d6`;
- parent receipt digest:
  `sha256:8d2e533a42681301cf5b5b142f0bbf391c0c78042ee9c06adcaeb25ba478cdec`;
- population: `119`;
- blocking population parsed from the protected queue: `92`.

The preimplementation decision text grouped `100` P0/P1 items as blocking.
That was a counting error: priority is not the queue's `blocking` field. The
protected JSON and its digest were not changed; all validation uses the actual
`92` blocking flags and the full `119` item population.

## What Changed

The new `zyra_runtime.productization` boundary contains:

1. immutable 11-domain owner/default contracts;
2. a unique, sealed owner registry with generation-fenced leases;
3. fail-closed default-entry probes supplied by the real API composition root;
4. committed-mutation-to-canonical-event verification;
5. structural process/import/parent-source/archive/opaque-boundary
   classification;
6. exact protected-queue admission and absorption receipts.

The boundary is derived control/evidence. It does not persist or replay
session, permission, memory, scheduler, artifact, graph, provider, MCP,
gateway, terminal, or browser mutations. Removing it disables release
readiness and admission proof; it does not create a fallback owner.

The API readiness path now composes all 11 existing owners. A fresh-state
probe verifies each real store/runtime and each declared default entry.
Disabling any registered owner yields `canonical_owner_unavailable`; none of
the 11 owner-loss probes succeeds through fallback. Disabling the whole
productization enforcement boundary makes readiness fail closed for all 11
domains.

## Canonical Owner Correction

During implementation, an attempted compatibility repair exposed that the old
Python `QueryInputProcessor` had been deliberately deleted by the E01
TypeScript cutover. It was not retained in the final diff. The final
session/event chain is:

```text
apps/code-worker/src/main.ts
  -> TypeScript ClaudeRuntimeCore
  -> DurableSessionRuntime
  -> RuntimeSourceMapper
  -> RuntimeEventSqliteStore
  -> Web projection
```

This preserves the selected TypeScript QueryEngine owner and removes the old
Python `agent_message` catalog claim. `turn_completed` is emitted only after
the TypeScript session mutation and carries mutation/revision identity. No
Python QueryEngine, session-decision fallback, or second canonical store was
introduced.

## Causality And Source Boundary Results

The candidate inventory at the exact implementation revision reports:

- `11/11` declared causal links valid;
- `11/11` domains with a valid event-to-mutation link;
- `0` invalid causal links;
- `0` blocking source-risk records;
- source receipt revision equal to the implementation revision.

Real owner repairs add missing identity/effect fields to permission, MCP,
provider, recovery, worker-route, gateway recovery, and artifact events.
Gateway artifact writes now emit `artifact.created` after the
`WorkspaceEditPort` commit. MCP artifact spilling first establishes the same
canonical gateway session, so event emission is not an optional replay-only
path.

The source audit now distinguishes package test/build commands from dependency
acquisition, audit/test dynamic imports from runtime plugin loading, actual
process calls from provenance text, and declared virtual-environment
interpreters from opaque executables. The rules are structural; no finding
fingerprint allowlist was added.

The reusable M3-01A inventory receipt remains globally `release_ready=false`.
This is expected: its static reachability rule does not consume the new
dynamic composition probes, the M3-01B parent minimum is not due until B02,
and M3-02/M3-03 requirement items remain open. The slice-specific
`runtime-absorption-receipt.json` consumes only B01-assigned boundaries and is
`release_ready=true`. The regenerated downstream `m3_01b.json` is retained as
input to B02 rather than being silently discarded.

## Effective Code Review

The direct baseline-to-implementation audit reports:

- raw additions: `8,740`;
- raw deletions: `153`;
- effective production: `6,510`;
- slice minimum: `4,500`;
- margin: `+2,010`;
- effective language split: Python `6,489`, TypeScript `21`.

Tests (`619` raw), docs (`217`), data (`43`), imports, DTO/schema fields,
comments, blanks, signatures, and literals are excluded. The parent currently
has `6,510/9,000`; B02 owns the remaining parent minimum.

Every file over 500 additions was reviewed:

| File | Effective | Review |
| --- | ---: | --- |
| `source_boundary.py` | 1,148 | executable structural analysis and dispositions; no path/fingerprint allowlist |
| `defaults.py` | 1,013 | 11 concrete owner/default bindings and real entry probes; no state owner |
| `owner_registry.py` | 891 | uniqueness, generation leases, disable/fail-closed behavior |
| `contracts.py` | 782 | effective logic after 247 schema/blank exclusions; strict digest and identity validation |
| `causality.py` | 778 | bounded ledgers, committed receipt checks, semantic classification |
| `absorption.py` | 786 | exact queue consumption and deterministic resolution receipts |
| `composition.py` | 359 | composition/gate orchestration; 146 non-effective lines excluded |

No file exceeds 20 percent of effective production. The export-only
`productization/__init__.py` has a high exclusion ratio and is not used to
claim implementation depth.

## Anti-Fake-Internalization Review

- Dynamic reachability: `/runtime/readiness` and API composition execute real
  owner/store probes; tests use fresh temporary state.
- Disable changes behavior: all 11 owner disconnects reject; global
  enforcement disable blocks all domains.
- Semantic effect: real event fields are emitted after corresponding store,
  route, permission, recovery, gateway, provider, or MCP effects.
- State custody: domain owners remain in their M1/M2 modules; the new guard
  owns only leases/readiness/verification receipts.
- Dependency boundary: no new package, process, port, Docker context, dynamic
  import, parent-source runtime path, OpenClaw path, or broad LangGraph owner.
- Fallback: no owner-loss proof reports fallback success.
- Test-only witnesses: the unit coordinator uses synthetic mutation receipts
  only to test receipt validation. Production causal evidence comes from the
  candidate inventory's real source/event/mutation graph, not those witnesses.

## Validation

Final focused validation passed:

- Python owner/source/gateway suites: `43`;
- TypeScript E01 QueryEngine/session suites: `120`;
- permission custody: `20`;
- MCP custody: `9`;
- provider control plane: `21`;
- affected TypeScript typechecks and Python compilation;
- checksum-bound runtime absorption verifier.

Two recovery API tests fail with the same
`HTTP 422 continuation_dispatch_failed` at both the protected baseline and the
candidate. A detached baseline worktree reproduced both failures. They are
recorded as pre-existing, not hidden as passes and not expanded into this
slice.

Full commands and results are in
`docs/reviews/evidence/M3-S01B-01/test-results.json`.

## Evidence

- `runtime-absorption-receipt.json`: clean-state 11-domain owner/default and
  no-fallback proof; digest
  `sha256:0bc4f7579f2fbab4a939024efb73884eb7d6ab6e69bb7ce5331dd2a85ad88ec9`.
- `state-owner-reachability-inventory.json`: exact-revision source,
  causality, effective-line, static reachability, and future-work inventory;
  digest
  `sha256:49f992b78300cf362a8c27d034e1ec246892fc8cfe9311b98a2cb3b0b8ba5446`.
- `downstream/m3_01b.json`: regenerated B02 input.
- `test-results.json`: behavior, failure, typecheck, and baseline-comparison
  record.

Decision commit:
`fad816d`.

Implementation commits:
`eb88677`, `d65856e`.
