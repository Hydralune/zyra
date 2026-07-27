# M3-S02B-02 release bundle, clean install and CI decision

## Frozen interval

- Slice: `M3-S02B-02`
- Baseline commit:
  `29fa59132c5c84f230e2ff3b94c75df510a4c20d`
- Required predecessor implementation:
  `81b08adaabc00cf18917a89418ff73dea3f6c67f`
- Required predecessor evidence:
  `29fa59132c5c84f230e2ff3b94c75df510a4c20d`
- Slice minimum conservative effective production additions: `6,000`
- Parent M3-02B minimum after both slices: `12,000`
- Numeric-stage aggregate scope: M3-02A and M3-02B

This decision is committed before production implementation. The production
and directly related behavior tests will be frozen in a later implementation
commit. Generated release archives, wheels, SBOMs, checksums, inventories,
receipts, reports, lockfiles, configuration examples, tests, documentation,
thin launchers and declaration-only code receive no effective-production
credit.

## Protected facts and owner boundary

This slice consumes, and does not reopen, the following protected facts:

1. M3-01 froze the source-custody and canonical-owner matrix.
2. M3-02A froze the two-domain live benchmark, comparison variants, effective
   transition accounting, zero-human policy and protected deployment evidence.
3. M3-S02B-01 owns process/profile lifecycle, placement, dispatch, semantic
   health, checkpoint handoff and deployment recovery.
4. Python runtime owners remain Python; TypeScript/Bun runtime owners remain
   TypeScript; Rust/native owners remain in their frozen language boundary.
5. The release layer may invoke public lifecycle and verification contracts,
   but it may not reproduce scheduler, permission, provider, event, memory,
   artifact, checkpoint or deployment state.
6. LangGraph remains limited to the protected checkpoint identity, lineage,
   pending/committed write, atomic commit and exact-resume conformance domain.
   No StateGraph, Pregel, generic reducer/channel, ToolNode, Store, SDK,
   deployment server or second checkpoint owner is introduced.
7. OpenClaw remains `excluded_forward_only`; no repository, package, process,
   image or path is read, restored or packaged.
8. The submission must run from `zyra` alone without parent source repositories,
   editable links, external Docker contexts, ambient caches or residual state.

## Product responsibility inventory

The existing repository has deployment and semantic-health behavior, but lacks
the release product required by this slice:

1. no deterministic bundle planner normalizes paths, modes, timestamps,
   ordering, exclusions and platform metadata;
2. no release boundary policy rejects parent-source paths, symlinks escaping
   the tree, editable dependencies, undeclared native binaries, caches,
   databases, artifacts and build residue before archiving;
3. no immutable bundle manifest relates source revision, Python/JavaScript
   locks, build products, configuration contract, migrations, SBOM, NOTICE and
   benchmark evidence;
4. no checksum runtime builds and independently verifies a canonical digest
   set while detecting missing, added, substituted or mutated files;
5. no Python release-lock parser verifies exact pins, hashes, markers,
   duplicate requirements and editable/path/URL dependencies;
6. no JavaScript lock verifier binds the root and workspace manifests to the
   frozen Bun lock and rejects undeclared or unpinned release dependencies;
7. no dependency license and package inventory creates CycloneDX-compatible
   SBOM components, dependency edges, process/native inventory and NOTICE
   obligations from the actual release inputs;
8. no configuration provisioning runtime separates public configuration,
   secret references and secret values, validates deployment profile needs,
   redacts diagnostics and writes atomic install-local configuration;
9. no installation transaction stages a release, verifies it before mutation,
   journals operations, activates atomically, resumes safely, or rolls back a
   partial install;
10. no schema-migration registry plans forward and rollback operations,
    validates compatibility, fences reapplication and proves state preservation;
11. no cross-platform command planner exposes Windows and Linux install,
    start, stop, doctor, migrate, rollback and uninstall contracts;
12. no clean-install runner builds a wheel and Web/runtime artifacts, creates
    an isolated environment, performs frozen installs, starts the product,
    exercises doctor/health and cleans all processes/state;
13. no offline/limited-network resolver verifies that a declared wheel/cache
    inventory is complete before attempting installation;
14. no uninstall runtime distinguishes product-owned files from operator state
    and verifies removal without deleting undeclared paths;
15. no CI gate DAG records command inputs, timeouts, outputs, artifacts,
    dependency ordering, skipped reasons and a fail-closed submission verdict;
16. no release admission gate requires every mandatory build, test, audit,
    clean-install, semantic-health, benchmark and submission-boundary receipt;
17. no benchmark linker binds the formal M3-S02A-02 report, P50/P95 metrics,
    local/edge/cloud dispatch, provider/model facts and protected revision into
    the release manifest;
18. no release CLI provides build, verify, install, migrate, rollback,
    lifecycle, doctor, uninstall and CI commands through one product entry;
19. no negative product path proves bad checksums, source contamination,
    missing locks, partial installs, failed migrations, occupied ports and
    offline dependency gaps fail closed;
20. no M3-02 aggregate runner freezes an exact target commit and performs
    full source-custody, submission, build, test and detached-cleanroom gates.

These are cohesive product responsibilities, not report templates or schema
padding. Removing the package must break bundle verification, install
transactions, migration/rollback, clean-install lifecycle and CI admission.
They provide sufficient natural implementation work for the fixed 6,000-line
budget, so no planning blocker is declared.

## Migration and language decision

The migration mode is `packaging_and_release_integration_only`.

The new Zyra-owned release product is Python under
`packages/productization/zyra_productization/release/**` because it coordinates
the existing Python build/install, filesystem, deployment and evidence
contracts. It invokes Bun/TypeScript and Rust/native build products through
their committed manifests and commands; it does not port or replace their
control flow. The Web and CodeWorker remain TypeScript/Bun products and are
validated as such.

There is no new upstream `primary_implementation` or
`supplementary_implementation` source role, no cross-language exception, no
semantic owner transfer and no source migration quota. M3 packaging integrates
the frozen Zyra-owned owners and audits every retained implementation language.

## Planned module and responsibility map

| Target module | Executable responsibility | State owner |
| --- | --- | --- |
| `release/errors.py`, `policy.py` | typed fail-closed errors and immutable release policy | release policy |
| `release/paths.py`, `boundary.py` | canonical paths, exclusion decisions, contamination and symlink checks | boundary scan receipt |
| `release/locks.py` | Python and Bun lock parsing, manifest binding and offline closure | dependency lock receipt |
| `release/checksums.py` | canonical digest manifests, verification and tamper diagnosis | checksum receipt |
| `release/sbom.py`, `notice.py` | component/dependency/process/native/license inventory | SBOM/NOTICE receipt |
| `release/configuration.py`, `secrets.py` | public config, secret references, redaction and atomic provisioning | install-local config receipt |
| `release/migrations.py` | migration registry, plan, apply journal and rollback | release migration journal |
| `release/install_store.py` | durable install transaction, revision and ownership ledger | install receipt store |
| `release/installer.py` | stage, verify, activate, resume, rollback and uninstall | install transaction |
| `release/platforms.py`, `commands.py` | Windows/Linux command and environment contracts | platform plan receipt |
| `release/bundle.py` | deterministic archive and immutable release manifest | bundle manifest |
| `release/offline.py` | wheel/cache closure and limited-network admission | offline dependency receipt |
| `release/benchmark.py` | exact-revision benchmark/evidence binding | release benchmark reference |
| `release/cleanroom.py` | detached build/install/start/doctor/health/stop orchestration | cleanroom receipt |
| `release/ci.py`, `admission.py` | dependency-aware gates and submission verdict | CI/release admission receipt |
| `release/cli.py` | default product entrypoint for all release operations | delegates to the owners above |

Declaration-only schema, static report text and generated artifacts are
excluded. Each module must contain runtime validation, state transition,
failure handling, recovery, filesystem/process behavior or admission logic.

## State custody and transaction semantics

The release package owns only:

- immutable bundle and checksum manifests;
- dependency, boundary, SBOM, NOTICE and benchmark-link receipts;
- install transaction revision, operation journal and product-owned path set;
- release migration version, idempotency key and rollback journal;
- cleanroom and CI gate execution receipts.

It does not own canonical task, event, permission, scheduler, provider,
memory, artifact, graph/checkpoint or deployment process state. Deployment
lifecycle is invoked through `zyra-deploy`/`DeploymentOrchestrator`; benchmark
facts are read and digest-bound, not recomputed; source custody is invoked
through the existing auditors.

Install commit follows:

```text
verify archive/checksums/locks/boundary
  -> acquire install transaction lease
  -> extract into a new staging directory
  -> provision public configuration and secret references
  -> plan and apply schema migrations with rollback journal
  -> run installation doctor
  -> atomically activate the staged release
  -> persist product-owned paths and committed receipt
```

Any failed step records a deterministic failure, rolls back applied migrations
and staging mutations, preserves the prior active release and never marks the
new release active. Repeating an idempotency key returns the committed receipt;
reusing it with different inputs is rejected.

## Failure and security semantics

- Archive entries are canonicalized and must stay below one release root.
- Absolute, parent traversal, ADS/device paths, escaping links and duplicate
  normalized paths are rejected.
- Root source repositories, vendor/source-pool content, caches, SQLite,
  artifacts, untracked build residue, editable/link dependencies and
  undeclared executable/native files are release blockers.
- A checksum manifest covers every payload file and rejects missing, added or
  changed files before installation.
- Lock verifiers require exact versions and reject editable, VCS, local path,
  floating URL or duplicated-conflict requirements.
- Secret values never enter manifests, argv, logs, SBOM, NOTICE or receipts.
- Offline mode verifies the complete artifact closure before executing a
  package manager.
- Uninstall removes only paths in the committed ownership ledger and refuses
  traversal or ambiguous ownership.
- CI mandatory gates cannot be disabled by configuration; a missing receipt,
  skipped mandatory gate, stale revision or degraded blocker prevents release.

## Validation and aggregate review decision

This slice changes the package/submission boundary and introduces the final
release/clean-install path, so it performs the full M3-02 numeric-stage
aggregate review. Validation will include:

1. focused unit/integration behavior tests for every release module;
2. bundle reproducibility and checksum mutation tests;
3. lock, SBOM/NOTICE, secret-redaction and source-contamination tests;
4. partial-install, resume, migration failure/rollback and uninstall tests;
5. Windows-native clean install plus Linux command-plan/container-compatible
   path validation;
6. offline complete/missing cache and occupied-port failure paths;
7. exact-revision TypeScript typecheck/build and applicable Python test suites;
8. source-custody, submission-boundary, dependency/process/native and secret
   audits;
9. a detached cleanroom created from the final reviewed target commit, not the
   dirty worktree;
10. parent M3-02B cumulative effective-line review;
11. full M3-02A/M3-02B requirement, owner, benchmark and evidence regression;
12. a critical review-fix cycle if any aggregate finding is discovered.

The implementation commit will precede review prose and generated evidence.
All review fixes, if needed, will be committed before the final cleanroom
target is frozen. The final evidence commit will bind exact commit hashes and
machine-readable receipts.

## Effective-code accounting commitment

The slice interval is
`29fa59132c5c84f230e2ff3b94c75df510a4c20d..<implementation_commit>`.
The parent interval starts at M3-S02B-01 baseline
`f241d33c011e7f0adabb2f390395034ac064c9d6`.

Per-file buckets:

- productized release production behavior;
- test;
- product launcher;
- review/documentation;
- generated report/evidence/archive/SBOM/checksum;
- lock/configuration/data/runtime asset;
- declaration/schema/type-only;
- adapter-only;
- mock/fixture;
- vendor/source-pool.

Every file with more than 500 raw additions, more than 20 percent of effective
production or more than 30 percent excluded content receives an individual
cohesion and exclusion review. The conservative count excludes imports,
docstrings, comments, blank lines, schema-only fields, static mappings,
tests, generated receipts, lockfiles, configuration, data, reports, ordinary
audit runners and thin forwarding code.
