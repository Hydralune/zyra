# P2-S02A-01 Legacy Vendor Retirement Review

## Verdict

`PASS_WITH_NONBLOCKING_RESIDUALS` at
`c8436744a86f4218c8d9675e441214fd5591cb99`.

`vendor/**` and `vendor-runtimes/**` are absent from the target tree and
worktree. Their 3,684 files and 30,656,839 bytes remain reproducible through
the checksum-bound retirement manifest and the frozen base Git objects. The
current normalized ledger contains zero legacy target bindings; runtime,
doctor, audit, clean-source, release and clean-install paths do not use a
filesystem fallback.

## Frozen provenance and deletion

The deletion was intentionally split from provenance:

- base: `eff77b612c5d73dacc8b34960c601740fa8669a9`
- provenance commit: `e0e31f0`
- manifest digest:
  `a860a1c20c0c23cecf52776371e7a4c3a839cae94ca9bcecb01fdb629c52ab53`
- `vendor`: 3,310 files, 25,811,199 bytes, Git tree
  `d94012ca6ac6508f366b3fb321c65a7040814fbe`
- `vendor-runtimes`: 374 files, 4,845,640 bytes, Git tree
  `8fbfc1bf477249a86a903b04ae1c08fe1270a879`

Every retired path records Git mode, blob, byte count and SHA-256. Source
identity remains bound to the exact `claude-code-best`, `browser-use` and
Claude runtime versions/commits and their recorded license basis. The
protected Phase 1 ledger seed and historical evidence were not rewritten.

## Runtime and gate migration

The source-pool manifest API was replaced by metadata-only retired source
identities. BrowserWorker and `/doctor` report the current formal owner,
`filesystem_required=false` and `fallback_available=false`. Browser runtime
availability is now a real package import probe.

Historical extraction and crosswalk writers fail closed instead of recreating
`vendor-runtimes`. Runtime scaffolds point to
`packages/runtime/claude-runtime` and `apps/code-worker/src/main.ts`.
The current ledger overlay removes all active legacy target bindings while
preserving 273 historical bindings in immutable provenance.

Release admission now has a mandatory `legacy-source-retirement` gate before
clean install. The gate verifies the exact candidate commit, immutable
manifest, zero current legacy targets, no reintroduced/renamed source pool and
no active runtime reference. The release deny-list continues to exclude
`vendor`, `vendor-runtimes`, `third_party`, `source-pool` and
`runtime-sources`.

Two historical smoke entrypoints were repaired during review:

- `verify_claude_productization_integration.py` now builds and exercises the
  formal TypeScript runtime in a clean source tree.
- `verify_m2.py` now uses current TypeScript command/runtime owners and
  permission-correct CodeWorker behavior rather than removed Python owners or
  static browser fixtures.

## Review fixes

The first exact-candidate retirement verification found one false positive:
the base already contained `third_party/NOTICE.md`. Commit `d79380c` changed
the gate to allow only the unchanged base path/blob pair. Newly added or
changed paths under forbidden pool segments and copied legacy blobs remain
blocked.

Deleting `vendor/browser-use` also exposed a local editable installation
pointing at the deleted tree. The development environment was repaired by
installing the exact `browser-use==0.13.3` wheel. This is environment-only and
does not restore a repository source pool or fallback.

## Verification

The final target passed:

- retirement verifier: valid, 0 findings, 0 current legacy targets;
- affected behavior/mutation regression: 74 passed plus 6 subtests;
- `/doctor` legacy retirement behavior: 1 passed;
- ledger: 0 blockers/errors, normalized current legacy targets 0;
- source-language custody: TypeScript and Python owners present, 0 violations;
- source-to-target audit: 17 entries, 60 source files, 81 targets, 0 blocking
  findings;
- submission boundary, TypeScript custody, formal CodeWorker and migrated M2
  smoke;
- clean-source formal CodeWorker build and contract probes;
- two byte-identical release archives;
- isolated clean install, build, first task, start, health, stop and uninstall.

The release archive is 29,197,310 bytes with SHA-256
`389643eb0abfe6f4af16a828a6e45e22e7e81421db585708b9fce50eac8a3490`.
Direct scans found zero legacy-root paths among 4,719 archive entries and
2,192 wheel entries, and zero retired path text in SBOM, NOTICE and runtime
inventory. Clean install digest is
`02683f2fa0f2f0a5254a441e1623f6a374e9395428d21e056b41e72eed6afc43`.

## High-risk scope review

Base-to-target classification is recorded in `line-buckets.json`. Deleted
runtime assets, tests, audit scripts, data and the 50,322-line provenance
manifest are not counted as Zyra production implementation. LoopX changed
file count is zero. OpenClaw changed file count is zero. No graph, memory,
scheduler, permission, lease, event or artifact canonical owner changed.

The following limitations are explicit:

- A repository-wide `tests/` run exceeded the 60-minute command budget near
  88 percent and contained protected cross-stage failures/errors. No
  full-suite PASS is claimed.
- The formal release policy already excludes stale Python-owned
  `test_api_control_commands.py` contracts after the TypeScript E02 cutover.
  The new doctor behavior passes independently.
- The generic M3 source-custody candidate scan remains repository-wide
  release debt. Its custody section had zero findings; slice-specific custody,
  ledger and target audits pass.
- The reproducible release pipeline used `--skip-ci`. The exact retirement
  gate and formal clean-install/product lifecycle were then executed
  separately and passed. No full 13-gate release CI PASS is claimed.

These residuals do not restore either retired root, create a runtime fallback,
alter the protected history or invalidate the slice-specific release and
cleanroom evidence.

## Evidence

- `docs/reviews/evidence/P2-S02A-01/legacy-source-pool-retirement-manifest.json`
- `docs/reviews/evidence/P2-S02A-01/retirement-verification.json`
- `docs/reviews/evidence/P2-S02A-01/repository-retirement-audit.json`
- `docs/reviews/evidence/P2-S02A-01/source-language-custody-input.json`
- `docs/reviews/evidence/P2-S02A-01/line-buckets.json`
- `docs/reviews/evidence/P2-S02A-01/release-cleanroom.json`
- `docs/reviews/evidence/P2-S02A-01/risk-review.json`
- `docs/reviews/evidence/P2-S02A-01/verification-summary.json`
