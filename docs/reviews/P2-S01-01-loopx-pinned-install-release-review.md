# P2-S01-01 LoopX pinned install and release review

## Verdict

P2-S01-01 passes at implementation commit
`1e1aa1fa0d242de6ea38e48286a9325f245eed05`.

The slice integrates the complete approved LoopX release package as
`supplementary_implementation` with migration mode
`pinned_package_integration`. The runtime assets remain
`runtime-assets/vendor-like`; their bytes and source-manifest volume are not
counted as Zyra production implementation.

No later P2-01 bridge, long-horizon orchestration, memory, topology, pruning,
operator selection, or scheduler work is started by this slice.

## Pinned identity and package custody

The package lock freezes:

- LoopX version `0.2.4`;
- upstream commit `8e79843704a40d8069a9cab4ede6edc6d29f671b`;
- source-manifest digest
  `3ed673d4f9533c85a502332ebe4694eb1ef0310543372a22e95f50880fd9b390`;
- package-lock content digest
  `f3fe301364a2a6fbeaf0f04ebb4356502bc9d2861d2f866f6852709c5016f8c2`;
- wheel digest
  `906acc18a98d9b7acefb50489b9683b57883382109bd36751928c6dc33b96a7f`;
- source-bundle digest
  `3670963274f3e70b0d5100557dd8a76638fea1901415687bb40e5fd8c7e2293e`.

The deterministic builder refuses a mismatched commit or dirty tracked
upstream tree. Rebuilding twice produced identical wheel, source bundle, source
digest, and lock content.

The source snapshot contains 1,568 manifest-bound upstream files. The wheel
contains 505 entries and preserves the `loopx = loopx.cli:main` entry point.
Both assets are inside the Zyra package and release payload; installed releases
do not require `../long-horizon-systems/loopx`, a Git checkout, or network
access.

## Installation profiles and failure behavior

The installer exposes two deterministic profiles over the same source digest:

| Profile | Artifact | Contract |
|---|---|---|
| `linux_wsl_upstream_semantics` | pinned source snapshot | project-local upstream release layout and environment semantics |
| `windows_release_offline_wheel` | pinned wheel | shell-independent offline install |

Both profiles place installation, state, runtime, manual, launcher, and profile
data under the selected workspace. User shell profiles, user Codex skills,
Claude adapters, global slash commands, system `PATH`, tmux dashboard setup,
and canary links are disabled.

The install is staged and atomically promoted. A valid repeated request returns
an idempotent receipt. Incomplete installs and upgrade drift fail closed rather
than overwriting unknown state. Typed error codes and recovery guidance cover
unwritable workspaces, incompatible Python, missing dependencies, partial
installs, artifact tamper, manifest mismatch, import failure, and CLI failure.

Deep doctor validates package identity, lock self-digest, complete source
manifest, wheel identity and entry point, both profile boundaries, release
independence, installed-file integrity, import origin, CLI version, workspace
state location, and absence of user-level writes.

## Release integration

The existing release builder now adds the LoopX lock, wheel, source bundle,
doctor metadata, profile declarations, and SBOM component when the package
lock is present. Existing release fixtures without LoopX remain supported.

Release verification treats the LoopX artifacts as required when the release
manifest declares a LoopX lock, verifies all digests, and runs deep doctor
after extraction. Cleanroom install performs a real project-local LoopX
installation using the cleanroom interpreter and records its install and doctor
receipts.

Two independent final ZIP builds were byte-identical:

- archive SHA-256:
  `bf2eb9a0c6a3eaa2fd657f4f609690480f30c042272e6166d8d89df2d126fee9`;
- archive size: 37,360,183 bytes;
- archive file count: 2,334;
- payload digest:
  `c4dafcdba80080814a5b7830e2a24fae41401c5f87d3a0644c1016893dad9bda`;
- reproducibility receipt:
  `60f26552ee4471fafa4dcff0767968549c6aa334d252c443a805a2e66761136d`.

The release pipeline also exercised two negative binding gates. An incorrect
source revision failed with `release_pipeline_revision_mismatch`; an incorrect
formal benchmark revision failed with
`release_benchmark_revision_mismatch`. Neither mismatch produced an admitted
release.

## Detached release installation

The final ZIP was expanded into a new directory. Its bundled verification
script installed LoopX into a separate workspace without consulting the source
repository or using an online package manager.

The resulting install contained 507 files with installed-file digest
`99319582fec4c6cb5b398edd1d572360a8240dd5db7695984c8d22e7ffc2b9e3`.
The deep doctor reported zero blockers, imported version `0.2.4` from the
workspace-local module root, returned `loopx 0.2.4` from the CLI, observed zero
user-level writes, and confirmed workspace-local state.

## Risk review

This slice is treated as high risk because it adds a third-party runtime
package and changes the release boundary. Review was performed after the
implementation commit.

No canonical owner moves. LoopX owns only its private package/control state;
Zyra graph, memory, scheduler, permission, lease, event, and artifact owners are
unchanged. No Zyra dependency, long-lived process, port, provider, training
lifecycle, OpenClaw input, or default strongest-profile activation was added.

The only workspace-source path is the deterministic builder's default input.
Runtime and release code do not use it. New subprocesses are bounded argument
arrays with timeouts: interpreter compatibility, isolated import/CLI probes,
and build-time Git reads. No shell execution is used.

Failure and mutation tests cover partial installation, unwritable workspace,
missing dependency, installed-file tamper, upgrade drift, wheel tamper, and
manifest tamper. Review found no remediation defect requiring a follow-up code
fix.

## Verification

- combined LoopX and adjacent release regression:
  `55 passed in 108.02s`;
- LoopX clean-install behavior suite: `5 passed`;
- LoopX release/doctor suite: `3 passed`;
- release productization unit suite: `45 passed`;
- release clean-install adjacent integration: `2 passed`;
- internalization ledger: `audit_ok=true`, zero errors and blockers;
- Phase 2 policy contracts: `valid=true`; LoopX remains unavailable for
  strongest-profile activation as required before later slices;
- source-custody repository audit: `2 passed`;
- internalization-ledger CLI gate: `5 passed`;
- compileall: passed;
- `git diff --check`: passed.

The first release-pipeline invocation used an incorrect full implementation SHA
and the second used an outdated benchmark SHA. Both were rejected by their
intended fail-closed gates. The final invocation used the repository's actual
bindings and passed.

## Effective change buckets

Implementation-commit raw additions:

| Bucket | Lines/bytes | Treatment |
|---|---:|---|
| Zyra production and release scripts | 2,594 lines | production/scripts |
| behavior tests | 333 lines | test |
| package lock and packaging configuration | 9,577 lines | data |
| LoopX wheel and source archive | 11,040,260 bytes | runtime-assets/vendor-like; no production credit |
| generated release ZIP and temporary receipts | 0 committed lines | generated; no credit |
| adapter-only | 0 lines | no credit |
| mock/fixture | 0 lines | no credit |

The package lock's 9,572 lines are primarily the immutable 1,568-file upstream
source manifest. They are data, not implementation. Tests use temporary real
archives, installers, interpreters, release builds, and CLI/import probes; no
pre-recorded success trace is used as acceptance evidence.

## Evidence

- `docs/reviews/evidence/P2-S01-01/release-bundle-summary.json`
- `docs/reviews/evidence/P2-S01-01/detached-install-summary.json`
- `docs/reviews/evidence/P2-S01-01/risk-audit.json`
- `docs/reviews/evidence/P2-S01-01/verification-summary.json`

## Repository boundary

The user-selected slice document and other Phase 2 planning documents are under
`G:\agent-zoo\docs`, outside the Zyra Git repository. They are not modified or
included by the Zyra implementation and evidence commits.
