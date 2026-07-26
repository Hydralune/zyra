# M3-S01B-02 Config Migration And Clean Default Runtime Review

## Verdict

`M3-S01B-02` is complete at implementation revision
`7ff45a7c52539c920539d398a9469c3ef7c7aeb8`.

The slice closes parent `M3-01B` and the M3-01 numeric-stage aggregate for the
configuration, owner-custody, clean-default, migration, and source-boundary
scope. The reusable freeze audit remains globally `release_ready=false`
because M3-02 and M3-03 requirements are intentionally still open; the
slice-specific runtime receipt is `release_ready=true`.

Frozen boundaries:

- slice baseline: `9f2bfe85bc8324e2531e0ba636cc4410bda9334e`;
- parent baseline: `4b6d0d1d81332886385adcd32ff6205f6403d0f0`;
- implementation: `7ff45a7c52539c920539d398a9469c3ef7c7aeb8`;
- predecessor B01 implementation:
  `d65856eebafd2a046953886b4605ec4ed2c06642`;
- predecessor B01 receipt:
  `sha256:0bc4f7579f2fbab4a939024efb73884eb7d6ab6e69bb7ce5331dd2a85ad88ec9`.

## Product Boundary

The Python `zyra_runtime.productization` package now owns only product
configuration, credential-presence admission, process lifecycle, migration
coordination, backups, rollback, and readiness receipts. It does not become a
second owner for session, checkpoint, artifact, permission, provider, worker,
gateway, memory, MCP, terminal, browser, or graph state.

The default chain is:

```text
repository-local config/defaults/environment/explicit overrides
  -> fail-closed validation and credential-presence checks
  -> migration lease, preflight, backup, apply, verify, commit/rollback
  -> B01 canonical owner composition
  -> API / TypeScript CodeWorker / Web readiness
  -> reverse-order shutdown and restart
```

Configuration resolution no longer discovers `$HOME`, source-repository
`.env`, source SQLite/log files, or sibling repository state. Explicit legacy
state-path overrides remain compatible and derive an isolated state root.
Secrets stay in the existing provisioner/owner boundary; public projections
contain presence and fingerprint only.

The TypeScript provider control-plane remains the sole owner of provider
catalog, credential metadata, leases, and dispatch attempts. Its in-owner
schema migration provides atomic v0-to-v1 adoption, rollback on DDL failure,
prepared-transaction recovery, restart idempotency, and future-version
fail-closed behavior.

## Resume Corrections

The interrupted prior window had already created five implementation commits
and uncommitted API/test/evidence changes. Resuming at that boundary found and
closed three additional issues before freezing the implementation:

1. `/workers/code/inventory`, `/workers/code/session-foundation`, and
   `/workers/code/session-integration` still constructed retired Python
   projections. They now call the canonical TypeScript sidecar contract and
   return `503` with `fallbackUsed=false` when it is unavailable.
2. `os.kill(pid, 0)` was used for migration-lease liveness. On Windows this
   interrupted the probing process. Windows now uses a read-only
   `OpenProcess`/`GetExitCodeProcess` query; POSIX retains signal-zero probing.
3. A protected M3-01A risk item referenced an intentionally deleted Python
   cutover test. The structural source inspector now records a project-local
   absent queued path as `retired`; a path escaping the repository remains an
   unresolved `missing_path`. The full current-tree scan still has to prove no
   parent-source runtime dependency.

## Migration And Failure Semantics

Behavior tests prove:

- immutable ordered plans and checksum-bound backups;
- generation-fenced exclusive migration leases;
- `prepared -> applying -> verifying -> committed` journaling;
- rollback after simulated process termination;
- corrupt-backup restart rejection;
- idempotent restart after commit;
- preserved session/checkpoint SQLite payloads;
- preserved artifact content and versioned manifest;
- preserved permission state and versioned schema;
- provider metadata adoption and restart;
- missing required credential rejection before owner startup;
- migration-disable rejection before owner probes;
- readiness false while any required owner or migration boundary is absent.

The slice-specific verifier reopens the stores after migration, performs a
crash/restart/rollback cycle, and executes all 11 B01 owner-loss proofs. Every
owner loss rejects without fallback.

## Source, Owner, And Causality Aggregate

At the exact implementation revision:

- `11/11` declared event-to-mutation causal links are valid;
- all `11` state domains have a semantic effect and committed mutation link;
- invalid causal links: `0`;
- blocking source-risk records: `0`;
- opaque runtime hits: `0`;
- source receipt revision equals the implementation revision;
- Python and TypeScript graphs have `0` parse errors.

The reusable static audit still reports future default-reachability,
requirement-evidence, and ownership findings assigned to M3-02/M3-03. These
are not converted into B02 success. The B02 verifier consumes only the exact
effective-line, causality, source-risk, B01 predecessor, clean-bootstrap,
rollback, and owner-loss boundaries.

OpenClaw remains `excluded_forward_only`; the cleanroom and source scan find no
OpenClaw runtime dependency. No broad LangGraph runtime owner, StateGraph,
Pregel, channel, ToolNode, SDK, or server dependency was introduced.

## Effective Code Review

Direct slice interval:

- raw additions: `10,060`;
- raw deletions: `2,704`;
- conservative effective production: `6,310`;
- minimum: `4,500`;
- margin: `+1,810`;
- effective language split: Python `6,303`, TypeScript `7`.

Direct parent interval:

- conservative effective production: `13,009`;
- minimum: `9,000`;
- margin: `+4,009`;
- effective language split: Python `12,981`, TypeScript `28`.

The classifier excludes tests (`989` raw), docs (`268`), data/config (`65`),
adapter-only code (`14` raw), imports, DTO/interface fields, blanks, comments,
signatures, and literals. No file exceeds 20 percent of effective production.

Large-file and high-exclusion review:

| File | Effective | Disposition |
| --- | ---: | --- |
| `configuration.py` | 1,008 | real precedence, validation, derivation, redaction, and immutable projection logic |
| `migration_adapters.py` | 1,137 | owner-specific probes, backups, transforms, restore, and verification; no second owner |
| `migration_journal.py` | 962 | durable transaction/lease/step/backup state plus Windows-safe liveness |
| `migration_runtime.py` | 878 | plan, preflight, apply, verify, rollback, restart recovery, and readiness |
| `lifecycle.py` | 686 | process identity, structured trace/error/log records, resource drain, readiness |
| `bootstrap.py` | 463 | configuration-to-migration-to-owner startup and shutdown composition |
| `credentials.py` | 438 | presence, lease, rotation, revoke, zeroization, and redacted status |
| `schema-migration.ts` | 1 | the conservative classifier excludes TypeScript interfaces/types; behavior is independently covered by 25 provider tests and is not used to inflate the line gate |
| `test_m3_config_migration_productization.py` | 0 | all 570 test lines excluded |

`runtime-config.ts` is likewise conservatively counted as `1` effective line
after interface/type exclusions. The executable B02 verifier is counted as
`395`; even excluding that entire audit tool, the slice remains above its
minimum at `5,915`.

## Anti-Fake-Internalization Review

- Dynamic reachability: API bootstrap and CodeWorker health execute the new
  configuration/migration boundary on fresh state.
- Disable changes behavior: missing credential, migration disable, owner
  disable, and TypeScript runtime disconnect all fail closed.
- Semantic effect: migration changes owner schema versions, restores actual
  backups, and changes readiness; it is not an event-only ACK.
- State custody: existing stores remain canonical; the migration journal
  stores coordination metadata only.
- Clean path: the exact-revision archive installs from `bun.lock`, builds a
  local wheel, starts the CodeWorker health entry, and builds Web without
  sibling-source or vendor runtime dependency.
- No fallback: the verifier reports no demo, source store, source process, or
  Python CodeWorker projection fallback.
- Effective buckets: generated evidence, JSON/TOML, docs, tests, adapters, and
  interface-only lines do not close the code gate.

Disconnecting the productization modules breaks migration/readiness tests;
disconnecting the TypeScript CodeWorker makes the three API contract routes
return `503`. These are direct failure witnesses rather than import smoke.

## Validation

Passed at the implementation revision:

- Python config/migration/productization behavior: `21`;
- parent owner/source/gateway adjacent regression: `44`;
- provider control-plane: `25`;
- TypeScript/Bun full suite: `1,268`;
- TypeScript typecheck projects: `9`;
- Web production build;
- boundary and TypeScript CodeWorker route focus: `8`;
- Python compile and `git diff --check`;
- exact-revision B02 verifier.

The exact-revision cleanroom passed:

- frozen-lock Bun install with no network;
- local wheel build/install and cleanroom package import checks;
- archived-source Python focused suite: `21`;
- provider tests: `25`;
- Bun suite: `1,268`;
- CodeWorker clean-default health;
- Web production build.

The bounded full Python repository run stopped after `10` failures and `10`
passes in `414.35` seconds. All failures are in
`tests/integration/test_api_control_commands.py` and assert pre-E02
permission/skill/command response shapes or retired direct Python tool
projections. The predecessor window had already recorded the same cutover
test-debt category before the final implementation corrections. The canonical
TypeScript permission/skill/command behavior passes in the full Bun suite.
This debt is not hidden as a pass and remains blocking before final M3 freeze,
with M3-03 as cleanup owner.

The cleanroom product entry remains repository-source composition. The local
wheel builds, installs, and imports correctly; if standalone wheel-only API
startup is retained as a delivery form, its source-contract packaging must be
closed in M3-03.

## Evidence

- `config-migration-runtime-receipt.json`: exact B01 binding, clean bootstrap,
  restart, rollback, and owner-loss receipt; digest
  `sha256:d9b0ad22c9517a79812799dcf2c1808402681de5f8fb72809ce655951d98edde`.
- `state-owner-reachability-inventory.json`: exact-revision source, owner,
  causality, effective-line, and future-work inventory; digest
  `sha256:689f436ebf1b39c6c918576f2b852aa8e85f04dc8082e075fe48ec6e0fe3a8d2`.
- `cleanroom-result.json`: archive/install/start/build isolation receipt.
- `test-results.json`: focused, adjacent, full-suite, and residual record.
- `downstream/**`: regenerated M3-02A, M3-02B, and M3-03 inputs.

Decision commit: `b579168`.

Implementation commits:
`119cfbc`, `0c3e4bc`, `f403138`, `3a7f66f`, `7ff45a7`.
