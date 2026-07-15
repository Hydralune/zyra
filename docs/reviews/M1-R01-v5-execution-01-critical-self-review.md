# M1-R01 V5 Execution-01 Critical Self-Review

## 1. Self-review verdict

`READY_FOR_INDEPENDENT_REVIEW`, not complete.

The immutable implementation candidate is `7e498446d9c42715f99f3fb9c5e36ffe68bc1a0c`. E01 may advance only after a fresh independent reviewer validates a later evidence commit and immutable review target, records `PASS`, and the parent updates final metadata and the root execution state. E02 and E03 remain blocked during this review.

## 2. Candidate identity

| Role | Commit | Treatment |
| --- | --- | --- |
| Verified baseline | `c34535a783e88f9481387ced89cba4fbc333dc74` | Protected prior verified head |
| E01 diff baseline | `0cd21bff5e2d160476f2ce3cef766bf53aab1239` | Direct child of verified baseline; receives zero E01 line credit |
| Implementation candidate `I` | `7e498446d9c42715f99f3fb9c5e36ffe68bc1a0c` | Immutable implementation and cleanroom target |
| Evidence `E` | pending | Must descend from `I` and contain review-only evidence |
| Review target `A` | pending | Must descend from `E` with no production delta |

The root manifests live outside the Zyra Git repository under `G:/agent-zoo/docs/remediations/M1-R01-claude-source-custody/manifests`. Their commit boundary is explicit; raw source hashes are computed from Git blob bytes at Claude snapshot `c57f5a29e88e9a814bea47abeb9a0a6f725dc102`, not the CRLF checkout.

## 3. V4 independent-review failures and repairs

### 3.1 False accepted source semantics

The prior reviewer rejected `snipProjection`, `snipModule`, `cachedMCModule`, and `buildToolNameMap` claims. V5 rejects every occurrence of those source-symbol classes rather than patching only nonce-selected mapping IDs. There are now `290` accepted and `70` rejected source records, with one custody row for every accepted record.

The verifier independently rejects any accepted occurrence of those symbols and the five earlier false symbols: `getPersistenceThreshold`, `currentFlushPromise`, `formatImageRef`, `restoreSessionStateFromLog`, and `createBudgetTracker`.

### 3.2 Illegal continuation roles

All accepted `e01-rej-*` statement-tail continuations now use `source_role=supplementary`, retain `continuation_of_source_symbol`, and include an explicit `supplementary_gap`. The verifier rejects every accepted role outside `primary|supplementary` and every supplementary record without a primary-gap explanation.

### 3.3 Mutation patch identity

The runner canonicalizes the temporary mutant source to LF before applying a frozen edit, compares the actual patch SHA-256 to the manifest before compilation, and restores the original checkout bytes afterward. A patch identity mismatch is now an invalid mutation and a non-zero gate result.

Final result: `46/46` declared, applied, compilable and killed; `46/46` frozen patch hashes match; `46/46` original/restored hashes match.

### 3.4 Cleanroom reproducibility

The redundant root `bun` devDependency was removed. Bun remains pinned by `packageManager=bun@1.2.15` and the execution command. The exact-`I` cleanroom performs `git archive`, fresh extraction, isolated temp/cache, `bun install --frozen-lockfile`, typecheck, build, `386` tests, and built health. It installed `10` packages and passed without the former `bun` postinstall failure.

### 3.5 Node runtime declaration

The invalid Node strip-only source command was removed. `stdio:node` now builds a Node bundle with Bun and starts that bundle with Node. Independent Node bundle health passes; malformed empty stdio reaches the runtime protocol guard rather than failing TypeScript loading.

### 3.6 Review-state health projection

Health no longer hardcodes a stale pending result. It consumes `candidate-metadata.json` and `strict-gate.json` under verification contract `zyra.e01-verification/v5`, fails closed on missing, damaged, stale, or identity-mismatched evidence, and reports complete only for an identity-bound independent PASS with the E01 effective-line floor satisfied.

An exact implementation archive necessarily contains older committed evidence. V5 therefore reports `candidate_metadata_contract_mismatch` in the exact-`I` cleanroom rather than presenting the old candidate as current. The later evidence/metadata commits will supply the current verification contract without changing production code.

## 4. Source custody and effective code

| Gate | Result | Floor |
| --- | ---: | ---: |
| Immutable source records | `360` | structural |
| Accepted / rejected | `290 / 70` | closed partition |
| Accepted executable source lines | `10,711` | `10,587` |
| Five-hop target mappings | `290 / 290` | all accepted |
| Default entries | `1` | defined and reachable |
| Gross changed executable TypeScript | `31,495` | informational |
| Five-token winnowing deduction | `654` | conservative deduction |
| Effective changed TypeScript | `30,841` | `25,416` |
| Final production TypeScript | `48,420` | `34,000` |
| Final test TypeScript | `10,614` | `8,000` |
| Deleted Python owner lines | `35,151` | `2,850` |

The authoritative line and custody result is `docs/reviews/evidence/M1-R01-v3/execution-01/strict-gate.json`. Older `candidate-gate-result.json` and `effective-loc-report.json` files are historical V3 artifacts and are not V5 completion evidence.

## 5. Runtime behavior and failure paths

| Evidence | Result |
| --- | --- |
| TypeScript typecheck | PASS |
| Bun build | PASS, `78` modules, `1.16 MB` entry |
| Bun source and built health | PASS |
| Node built-bundle health | PASS |
| E01 behavior tests | PASS, `386/386`, `1,225` assertions |
| Runtime-origin probe | PASS, default TypeScript owner, journal revision `1` |
| Write-path probe | PASS, revision `1 -> 2` and state digest changed |
| Same-session resume | PASS, three forced kills, epochs `0/1/2`, replayed IDs `[]` |
| Lost-ACK probe | PASS, repeated effect count `0` |
| Disable probe | PASS, default entry deterministically fails when E01 owner is disabled |
| Dependency/path audit | PASS, `96` runtime files, no forbidden paths, symlinks, or relative package links |

The observation-budget runtime remains dynamically reached through `ModelIterationRuntime.buildRevisionMessages`; it constrains provider-visible tool observations while raw result receipts remain owned by `ToolResultRuntime`. Provider stop ordering, deny/ask zero delegation, malformed SSE cleanup, snapshot tamper rejection, and provider-tool-observation-provider behavior are covered by the full test corpus and mutation set.

## 6. Cleanroom and custody boundary

The final cleanroom target is exactly `7e498446d9c42715f99f3fb9c5e36ffe68bc1a0c`. It contains no `.git`, `node_modules`, `dist`, `.tmp`, inherited `NODE_PATH`, root source repository path, vendor runtime, or inspection sidecar dependency. The extracted tree and archive are removed after execution.

No core implementation is delegated to `../claude-code-best`, a package link, a CLI sidecar, a service, or a cached artifact. Source manifests and crosswalks prove provenance only; runtime behavior is owned by Zyra TypeScript modules and their snapshot, permission, journal, provider, tool, compaction, and protocol boundaries.

## 7. Residual gate

The only open E01 gate is fresh independent review. The reviewer must not trust this self-review or reuse the V4 verdict. It must independently audit nonce-selected accepted and rejected mappings, immutable source blobs, source roles, actual mutation fingerprints, exact-`I` cleanroom, Node/Bun paths, health verification projection, runtime behavior, and candidate ancestry.

