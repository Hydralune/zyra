# M3-S01B-02 config migration and clean-default decision

## Frozen interval

- Slice: `M3-S01B-02`
- Baseline/evidence commit: `9f2bfe85bc8324e2531e0ba636cc4410bda9334e`
- Required predecessor implementation:
  `d65856eebafd2a046953886b4605ec4ed2c06642`
- Required predecessor evidence:
  `docs/reviews/evidence/M3-S01B-01/**`
- Required downstream input:
  `docs/reviews/evidence/M3-S01B-01/downstream/m3_01b.json`
- Minimum conservative effective production additions: `4,500`
- Parent baseline:
  `4b6d0d1d81332886385adcd32ff6205f6403d0f0`
- Parent minimum after this slice: `9,000`
- Predecessor effective production at its implementation revision: `6,510`

The implementation interval begins at the baseline above. This decision is
committed before production code. Documentation, generated receipts,
configuration examples, tests, fixtures, schemas without behavior, adapters,
source-pool material and vendor-like content receive zero production credit.

## Actual gap found before implementation

The protected B01 result proves unique runtime ownership, but the default
composition still has productization gaps assigned to B02:

1. `apps/api/zyra_api/main.py` independently reads and normalizes more than a
   dozen `ZYRA_*` path variables. Defaults are related only by naming
   convention, so one malformed override can split canonical stores across
   unrelated roots.
2. There is no versioned product configuration document with a single
   defaults/file/environment/explicit precedence contract. The API, worker
   and provider process therefore cannot explain the effective configuration
   or distinguish a missing credential from an absent optional feature.
3. Existing owner stores initialize their own tables, but there is no
   product-level migration plan, durable migration journal, preflight,
   checksum-bound backup, crash recovery, rollback or cross-domain restart
   verification.
4. The TypeScript provider store owns provider/credential metadata but does
   not persist an explicit schema version or migration transaction state.
5. Startup, readiness, shutdown, migration and rollback do not share one
   structured log/error/trace envelope or one process-generation identity.
6. A clean checkout can start with defaults, but the behavior is not governed
   by one fail-closed bootstrap boundary and is not proven together with
   restart, partial migration, rollback and module-disable behavior.

These are runtime responsibilities, not inventory findings. They are
reachable from the API composition root and the TypeScript code-worker /
provider store. Removing the new modules must change startup, migration,
readiness and restart behavior.

## Decision

The slice uses `owner_absorption_and_cleanup`. It adds no new domain state
owner and migrates no new upstream implementation quota.

The new Python productization boundary owns:

1. immutable product configuration resolution and validation;
2. credential-presence requirements without credential material custody;
3. product process identity and structured lifecycle/trace/error emission;
4. migration orchestration, plan ordering, transaction journal, backups,
   crash recovery and rollback;
5. domain-adapter admission for existing canonical owners;
6. clean-default bootstrap and restart verification receipts.

The TypeScript provider store remains the only owner of provider catalog,
credential metadata, route leases and dispatch attempts. It gains same-language
schema-version, migration-journal and rollback behavior in that owner.

The product migration journal is coordination metadata. It never stores a
session, checkpoint, artifact, permission decision, provider credential,
worker route or browser/terminal state. A migration adapter may validate,
backup and transform an existing owner store only through an explicit
Zyra-owned path and version contract. It cannot fall back to a source
repository database, `$HOME`, a source `.env`, an opaque process or a second
store.

## Source-language and migration decisions

| Source repository/path | Source language | Target path | Target language | Migration mode | Canonical owner after slice |
| --- | --- | --- | --- | --- | --- |
| Zyra `apps/api/zyra_api/main.py` environment/path composition | Python | existing API composition plus `zyra_runtime.productization` config/bootstrap calls | Python | `owner_absorption_and_cleanup` | existing domain stores; productization resolves configuration only |
| Zyra `packages/runtime/zyra_runtime/productization/**` B01 guard | Python | same package, new configuration/lifecycle/migration/bootstrap modules | Python | `owner_absorption_and_cleanup` | product configuration, migration coordination and lifecycle receipts only |
| Zyra Python session/checkpoint/artifact/permission owner stores | Python | existing stores plus explicit migration adapters | Python | `same_language_owner_cleanup` | existing stores named in B01 owner registry |
| Zyra `packages/runtime/provider-control-plane/src/store.ts` | TypeScript | same store plus in-owner schema migration and journal | TypeScript | `same_language_owner_cleanup` | `ProviderControlPlaneStore` |
| Zyra TypeScript runtime process configuration | TypeScript | provider/control-plane configuration projection and tests | TypeScript | `same_language_owner_cleanup` | existing TypeScript runtime owners |
| B01/M3-01A audit packages | Python | result-consumption and parent/numeric-stage recomputation only | Python | `owner_absorption_and_cleanup` | derived evidence only |

There is no `mixed` or `unknown` entry. Python and TypeScript both have real
same-language owner work, so both must have non-zero effective production
changes. No cross-language exception is used.

No source repository is read for code migration in this slice. Existing
Claude/opencode/OpenHands/browser-use/Oh My Pi/Hermes-derived code remains a
protected Zyra implementation fact. LangGraph remains narrow recovery
conformance; no StateGraph, channel, Pregel, ToolNode, Store, server or SDK
dependency is introduced. OpenClaw remains `excluded_forward_only`.

## Configuration custody

Effective configuration precedence is:

```text
explicit process/CLI overrides
  > explicit ZYRA_* environment values
  > repository-local config file selected by ZYRA_CONFIG
  > versioned Zyra defaults
```

Rules:

- no automatic `$HOME`, parent-repository, source-repository, `.env`, SQLite,
  log or credential discovery;
- relative paths resolve against the Zyra project/config root, never the
  invoking user's home directory;
- unknown keys, path escapes, secret material in config, incompatible schema
  versions and conflicting aliases fail closed;
- environment variables may contain credential material only where an
  existing credential owner already accepts it; the resolved configuration
  exposes presence/fingerprint only;
- optional provider/MCP/edge features are disabled explicitly when their
  required credential is absent; a requested feature with a missing
  credential is a startup error, not a local/demo fallback;
- default clean startup uses only repository-owned modules and local state
  paths.

The default state root remains compatible with the existing project-local
`tmp` layout. This avoids relocating healthy data merely to create a
migration. Version migration is driven by owner schema versions and explicit
legacy roots, not by directory-name heuristics.

## Migration and rollback contract

The migration runtime will:

1. acquire an exclusive, generation-fenced migration lease;
2. recover or reject an incomplete prior transaction before planning;
3. probe every registered owner adapter and construct an ordered immutable
   plan;
4. run preflight checks without mutating data;
5. create checksum-bound backups for every mutable store;
6. journal `prepared -> applying -> verifying -> committed`;
7. verify target version, canonical identities and owner-specific invariants;
8. on error journal the failure, restore backups in reverse order and verify
   rollback;
9. on restart recognize committed operations idempotently and never replay a
   side effect;
10. refuse downgrade/unknown-version/source-store fallback.

Crash points after prepare, backup, apply and verify are explicit test inputs.
An abandoned transaction can be recovered only from its own durable journal
and backups. Missing/corrupt backups fail closed.

## Domain coverage

The parent closes only when restart and rollback validation cover:

- TypeScript session/event store metadata;
- Python graph checkpoint metadata;
- content-addressed artifact state;
- permission state/audit metadata;
- TypeScript provider catalog/credential metadata;
- product configuration and process generation;
- B01's 11-domain owner/default readiness gate.

Tests will seed old **Zyra** formats through adapter-owned fixtures, execute the
real migration runtime, reopen the actual stores and prove the old data is
readable by the selected owners. No source repository store or opaque runtime
is used.

## Lifecycle, error and trace contract

Startup and shutdown share one process-generation identity. Lifecycle records
have monotonic sequence, UTC timestamp, severity, component, phase, event,
trace/span/correlation/causation identities, configuration digest, migration
transaction identity and redacted attributes.

Errors use one typed envelope with stable code, retryability, phase, owner,
trace identity and bounded redacted details. Exceptions never serialize
environment snapshots, credential values, authorization headers or full
config contents.

Readiness is `true` only after configuration validation, migration recovery /
commit, required credential presence and B01 canonical-owner probes succeed.
Shutdown drains registered resources in reverse dependency order and records
partial failures without silently reporting success.

## Default path and fallback rules

The protected main path becomes:

```text
install / API or worker start
  -> load and validate versioned configuration
  -> recover/execute owner migrations
  -> bind lifecycle process generation
  -> compose B01 canonical owners
  -> readiness
  -> API/Web/worker execution
  -> drain/shutdown
  -> restart from committed configuration and migrated stores
```

Forbidden success paths:

- demo/replay/fixture state on default startup;
- source repository package/path/process/database fallback;
- implicit `.env` or user-home configuration;
- local provider fallback after a requested provider credential is missing;
- migration success with an unverified or unavailable owner;
- readiness while a migration is prepared/applying/failed/rolling back;
- rollback that leaves a target-version marker over restored old data.

## Effective-code accounting commitment

The final review will calculate per-file raw and conservative effective
additions from:

- slice baseline to frozen implementation revision;
- M3-01B parent baseline to frozen implementation revision.

Buckets are:

- effective production;
- test;
- docs/decision/review;
- configuration/data/runtime-assets;
- generated evidence;
- schema/DTO-only;
- adapter-only;
- mock/fixture;
- vendor/source-pool.

Every file over 500 raw additions, over 20 percent of effective production or
over 30 percent excluded content receives individual review. The slice cannot
use tests, examples, JSON/TOML, receipts, schemas, repetitive mapping tables or
thin adapters to reach its minimum.

## Validation and aggregate closure commitment

The implementation must run:

1. focused Python tests for configuration precedence, validation, credential
   presence, lifecycle, error redaction, migration plan/journal, partial
   failure, crash recovery, rollback and restart;
2. TypeScript provider migration/rollback/restart tests and adjacent provider
   control-plane tests;
3. API clean-default startup/readiness/shutdown/restart integration in a fresh
   state root;
4. old-Zyra session/checkpoint/artifact/permission/provider migration through
   real owner stores;
5. missing credential, invalid config, corrupt backup, module-disable and
   no-fallback tests;
6. B01 owner readiness, source-boundary and downstream inventory reruns;
7. direct parent effective-line recomputation and M3-01 numeric-stage
   cumulative review;
8. cleanroom install/start checks, dependency/process/path/OpenClaw/LangGraph
   scans, `git diff --check`, Python compile and affected TypeScript
   typechecks/tests.

This slice changes default configuration and migration behavior, so it
performs the matching high-risk cleanroom and aggregate review. It does not
change global permission, scheduler, recovery or compact policy and adds no
external dependency, process, port, Docker context, MCP server, plugin or
dynamic import.
