# P2-S01-04 LoopX v0.2.13 embedded source cutover review

## 1. Verdict

`PASS`.

LoopX `v0.2.13` is now a complete, pinned, auditable source runtime inside the
Zyra release boundary. Development, runtime, release, cleanroom, and first-task
paths no longer depend on a LoopX wheel/tar installer, a user-level package, or
`../long-horizon-systems/loopx`. The P2-S01-03 private state survives a real
cross-version restart without cursor reset, duplicate mutation, duplicate
claim/spend/interaction, or owner transfer.

This closes P2-01. It does not activate `phase2_strongest_v1`; later readiness
slices remain required.

## 2. Commit boundary

- base: `3c4d1092187b1777468cad0ce2a772244d012197`
- implementation: `ba2aceea0d48c75648ae6c3fe0f3f1c2cd51e670`
- review fixes:
  - `79cba8b33c483ec1ae79745ecc59577107260107`
  - `81a79c2c2421576abb04339e5192d79e5249f48c`
  - `5d0f0a6d583c5f04080a14c7a30838a633a11c8f`
  - `1d64d01ee08200aad0b0b0a02407a9e7473b42d9`
  - `89a2def73645936a421a97bf398f67759cb842be`
- evidence: the commit containing this review and `docs/reviews/evidence/P2-S01-04/**`

No earlier P2-S01 history was amended.

## 3. Frozen source and product form

| Field | Final value |
|---|---|
| version | `0.2.13` |
| annotated tag object | `a2c072d412d90839132e1cf39c23dd431c394175` |
| peeled source commit | `7232dca45ec2ca996edc43b2d3558edc802c844e` |
| embedded locator | `packages/integrations/loopx_runtime` |
| source files | 1,925 |
| tree digest | `66af2de0082dbadf7c7cb3ffc85b9b91b889c0433103ea3bc05abb313d831961` |
| source manifest SHA-256 | `7d040170be1624b89076303f0bfb9d227760bf39c667505889981aba1997acd0` |
| package lock digest | `7f3cee74b1e5530ae8574510f3e9cacf3cec893e9f94982f8d27aa28a118a164` |
| migration mode | `pinned_embedded_source_integration` |
| source role | `supplementary_implementation` |

The source inventory contains 46 packages, 3 CLI entry points, 1 extension
entry-point group, 4 extension descriptors, 5 skills, 2 templates, 7 package
data records, and 125 upstream tests. Seven upstream CI files and one upstream
`AGENTS.md` are explicitly excluded because they are not runtime, resource, or
test inputs.

`LoopXRuntimeResolver` is the unique runtime locator. The installer package is
only a compatibility facade and never creates `.zyra/loopx/install`, extracts
an archive, accesses the network, writes user home/profile/PATH, or locates a
workspace-external source tree.

## 4. Runtime, release, and first-task proof

Deep doctor verifies that Python import, `loopx --version`, extension discovery,
and package resources originate from the embedded tree. Source, manifest,
version, entry-point, extension, skill/template, and package-data damage fail
closed without archive or user-package fallback.

The final detached release is bound to target commit
`89a2def73645936a421a97bf398f67759cb842be`:

- 4,291 files, 32,669,854 bytes;
- archive SHA-256
  `0420f0ea215b4e18e430b9f98141a7dfd7e4d819b2dbb4adfc6f9ccc624846a9`;
- two independent builds are byte-identical;
- full payload and Python wheel both pass integrity verification.

Its isolated cleanroom creates a real task and submits `/loopx-connect`. The
receipt is `applied`, cursor is `1`, and probe, API, and runtime origins are all
under the extracted release payload. The LoopX task phase is offline, creates
no retired install, performs no archive extraction, and leaves the synthetic
user home empty. The cleanroom's general Python dependency bootstrap is online
because the repository has no complete offline wheelhouse; this is recorded
separately and is not a LoopX source/runtime dependency.

## 5. Cross-version state and owner proof

The upgrade harness starts the P2-S01-03 evidence commit in one clean checkout,
creates a real task, goal, todo, claim, quota spend, history, interaction,
outbox, ACKs, and cursor, then restarts the final target twice in independent
processes against the same private workspace.

Both target restarts preserve cursor `3`, three canonical mutations, three
ACKs, one claim, one private quota spend, interaction contract, todo, history,
and the historical install directory. The old directory is preserved but
ignored; no new install or extraction occurs. Retry returns a replay receipt
and creates no duplicate canonical commit, claim, spend, interaction, lease, or
side effect. LoopX claim remains distinct from worker lease and LoopX quota
remains distinct from the ResourceScheduler execution budget.

## 6. Independent risk review and fixes

The implementation commit was not accepted on static inspection alone. Review
found and closed five concrete gaps:

1. Added a reusable two-checkout, independent-process upgrade gate.
2. Rejected host-dependent archive bytes and canonicalized Git materialization
   to LF; Windows and Ubuntu 22.04 WSL now produce identical identities and
   digests.
3. Added an actual detached first-task probe; its first run exposed and removed
   a Bun cache write beneath user home.
4. Rejected wheel-only provenance for the TypeScript event spine and bound the
   task to the complete release payload.
5. Rejected a cwd-derived resolver during the final provenance check and made
   the verified payload root explicit.

All fixes are present in the final target, and source integrity,
cross-platform build, cross-version restart, release reproducibility,
cleanroom, and first-task gates were rerun afterward.

## 7. Verification

- all 29 LoopX Python integration cases pass on the final target across the
  split rerun; the combined run passed 28 and encountered one sandbox Git
  subprocess failure, while the isolated release-doctor file passed 2/2;
- clean-install/release affected regression: 7 passed;
- owner, dynamic graph, task graph, continuation, permission, and scheduler:
  33 passed plus 14 subtests;
- Web typecheck passed; LoopX feature 3/3 passed; production build passed with
  375 modules;
- Phase 2 policy contracts: `valid=true`;
- internalization ledger: `audit_ok=true`, zero errors and blockers;
- path audit and `git diff --check`: passed.

The full Web run after Web changes produced 274 passes and the same two
pre-existing MCP elicitation failures documented by P2-S01-03: their fixture
expired on 2026-07-25, before this 2026-07-29 run. All LoopX Web tests passed,
and later review fixes were Python-only. Selected upstream tests produced 64
passes and one Windows-only POSIX executable-bit/shebang/chmod limitation.

An additional expanded code-worker permission probe still expects
`permission_denied` where current runtime reports `permission_pending`; the
same case fails at the P2-S01-03 base and no source-cutover file owns that
behavior. Required adjacent owner/permission/budget unit gates pass, so this is
recorded as a pre-existing out-of-scope issue rather than hidden or rewritten.

## 8. Effective line audit

The implementation target changes 1,972 files, adds 736,401 lines, and deletes
11,963. Only 1,652 added production lines are Zyra-owned runtime and product
integration:

| Bucket | Files | Added | Deleted |
|---|---:|---:|---:|
| production | 20 | 1,652 | 52 |
| test | 8 | 482 | 196 |
| runtime-assets/vendor-like | 1,115 | 443,471 | 0 |
| adapter-only | 6 | 137 | 1,645 |
| generated | 4 | 23,876 | 9,536 |
| data | 689 | 226,106 | 8 |
| mock/fixture | 125 | 39,461 | 0 |
| scripts | 5 | 1,216 | 526 |

Fourteen runtime-asset files are binary. Upstream source, documentation,
examples, data, generated manifests/locks, and upstream tests are not counted
as Zyra deep-internalization code. No training dataset or checkpoint was added.

## 9. Handoff

Later slices must use the embedded locator, package lock, deep doctor, private
state/restart contract, and source-damage fail-closed behavior documented here.
They must not revive LoopX archive installation or activate the strongest
profile before the remaining mechanism readiness gates pass.

Root workspace documents under `G:\agent-zoo\docs/**` and
`G:\agent-zoo\AGENTS.md` are outside the Zyra Git repository and therefore do
not travel with this evidence commit.
