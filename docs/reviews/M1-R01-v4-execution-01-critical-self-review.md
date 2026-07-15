# M1-R01 V4 Execution-01 Critical Self-Review

Date: 2026-07-16

Verdict before independent review: `READY_FOR_INDEPENDENT_REVIEW`

Implementation candidate: `cb5cc61627a71c60cea179736a8d9dda3f5aac39`

Verified baseline: `c34535a783e88f9481387ced89cba4fbc333dc74`

E01 implementation diff baseline: `0cd21bff5e2d160476f2ce3cef766bf53aab1239`

The implementation diff baseline is the direct child of the verified baseline. It contains the pre-E01 runtime-event checkpoint and receives zero E01 effective-line credit. It is not an allowlisted control-plane commit.

## 1. Prior independent-review failures

The prior V3 candidate failed independent review. V4 treats each finding as a blocking defect rather than a documentation exception.

| Prior finding | V4 disposition | Evidence |
| --- | --- | --- |
| Source hashes bound CRLF checkout bytes | Source hashes now use raw `git show <snapshot>:<path>` blob bytes | `execution-01-source-manifest.jsonl`, `strict-gate.json` |
| Generic source-to-target mappings claimed unrelated behavior | Known false mappings are rejected; retained mappings carry source behavior, target behavior and equivalence claims | 295 accepted / 65 rejected, 295 target records |
| Undefined default entry | Gate profile defines `e01.default-code-worker` and every target record resolves to it | `execution-01-gate-profile.json` |
| Stale candidate metadata | Generator rewrites candidate metadata from the actual implementation HEAD; later evidence fields are frozen in a post-target metadata commit | `candidate-metadata.json` |
| No required 5-token winnowing | V4 uses 5-token shingles, an 8-shingle winnowing window, 0.8 length ratio and 0.8 Jaccard threshold | `strict-gate.json` |
| Pre-E01 production commit was allowlisted as control plane | The real commit is recorded as an explicit zero-credit implementation diff baseline | gate profile and strict scope check |
| Stop-reason order was nondeterministic | Provider responses retain insertion/commit order instead of sorting random response IDs | `runtime.test.ts`, 384-test run |
| Resume evidence replayed transition IDs | Three forced process terminations across two resumes produce zero replayed IDs | `same-session-resume-result.json` |

## 2. Source custody

- Frozen Claude snapshot: `c57f5a29e88e9a814bea47abeb9a0a6f725dc102`.
- Source records: 360 total, 295 accepted, 65 rejected.
- Accepted executable frozen-source lines: 10,735; required minimum: 10,587.
- Accepted continuation records are only tails of the same already accepted function that the old generator split at a mechanical line cutoff.
- Filesystem path helpers, process-global listeners, image-reference formatting, worktree restoration and unrelated post-boundary symbols remain rejected.
- `getPersistenceThreshold`, `formatImageRef`, `restoreSessionStateFromLog`, `currentFlushPromise` and `createBudgetTracker` are not accepted as migrated symbols.
- Source and target manifests are sorted by `mapping_id` and have one-to-one accepted closure.

## 3. Source-to-target five-hop closure

All 295 accepted source records have:

1. An immutable frozen source symbol and range.
2. A concrete Zyra target symbol with candidate blob hash.
3. A registered default entry and real callsite path.
4. A canonical state store and observable state effect.
5. Named success/failure behavior tests and declared mutation coverage.

The V4 verifier reports 295/295 five-hop mappings. Ledger rows without a behavior test or state effect fail the gate.

## 4. New provider-observation budget runtime

`ToolResultRuntime` remains the canonical owner of raw tool receipts, delivery limits and artifact externalization. `ToolObservationBudgetRuntime` has a separate narrow responsibility: selecting and replacing accumulated results before they enter the next provider transcript.

The runtime owns:

- Per-observation and per-round provider-visible character budgets.
- Image and artifact charges.
- Prior replacement decisions and deterministic candidate partitioning.
- Omission receipts containing call identity, digest, size and preview.
- Checksummed snapshots, restoration, audit and decision reconstruction.

`ModelIterationRuntime.buildRevisionMessages` registers all settled observations, enforces one round plan, writes the plan digest into transcript metadata and snapshots the budget owner. The integration test proves the default model-iteration callsite changes provider-visible content and survives a new-run restore.

Disabling the round limit or checksum guard is killed by mutations 045 and 046.

## 5. Effective-line buckets

| Bucket | Result | E01 effective credit |
| --- | ---: | ---: |
| Changed production TypeScript before token winnowing | 31,423 | Candidate |
| 5-token near-clone deduction | 654 | Negative |
| Effective changed production TypeScript | 30,769 | Counted |
| Required effective minimum | 25,416 | Passed by 5,353 |
| Final production TypeScript | 48,348 | Floor evidence |
| Final test TypeScript observed before the final integration addition | 10,498+ | Test-only, not production credit |
| Python deleted lines | 35,151 | Deletion evidence only |
| Generated/data/source-pool/vendor-like | 0 counted | Excluded |
| Docs, manifests, ledgers and evidence | Not counted | Excluded |
| Thin adapter-only or fixture-only code | Not counted | Excluded |

The line gate begins after `0cd21bff5e2d...`; the pre-E01 event-spine checkpoint receives no E01 credit. Passing line counts does not substitute for behavior or source custody.

## 6. Runtime and adversarial validation

- Bun `1.2.15` frozen toolchain: PASS.
- TypeScript `5.8.3` typecheck: PASS.
- Bun build and built-entry health: PASS.
- E01 behavior tests: 384/384 PASS.
- Mutation gate: 46/46 compiled and killed; every mutated file restored to its original SHA.
- Runtime-origin probe: PASS, TypeScript canonical owner.
- Write-path probe: PASS, canonical revision and state digest changed.
- Disable probe: PASS, disabling E01 TypeScript runtime makes the default entry fail.
- Lost-ACK probe: PASS, committed count remains one and repeated effect count remains zero.
- Same-session resume probe: PASS, three forced kills, restart epochs 0/1/2 and zero replayed transition IDs.
- Dependency audit: PASS, no root source-repository paths, relative package links or runtime symlinks.
- Cleanroom: PASS at the implementation candidate using `git archive`, fresh install cache, frozen lockfile, typecheck, build, 384 tests and built health.
- Malformed compatible-provider SSE cleanup, deny/ask gateway fencing and provider observation revision are covered in the adversarial suite.

The first final cleanroom attempt failed before installation because sandbox networking rejected downloads. The same fresh-cache command passed when granted network access; no project cache or inherited `NODE_PATH` was used.

## 7. State custody and recovery

- Session state: `DurableSessionRuntime`.
- History: `SessionHistoryRuntime`.
- Provider request/response: request and response runtimes.
- Raw tool result receipt/delivery: `ToolResultRuntime`.
- Provider-visible cross-result budget: `ToolObservationBudgetRuntime` nested in the model-iteration snapshot.
- Query/model loop: `ModelIterationRuntime` and the TypeScript E01 coordinator.
- Transition/effect/outbox identity: TypeScript journal and effect protocol owners.

Snapshots reject checksum, session, task and nested owner corruption before replacing live state. Same-session restoration increments restart epochs and does not replay committed transition IDs or effects.

## 8. Remaining boundaries

- The built health response intentionally remains `candidate_pending_independent_review` until a fresh independent reviewer records PASS.
- Root remediation manifests and `docs/milestones/execution-state.yaml` are outside the Zyra Git repository. Their hashes and delivery boundary are explicit, but they cannot be included in a Zyra commit.
- E02 and E03 remain blocked until this E01 candidate receives an independent PASS and execution state is updated.
- No claim is made that rejected process-global Claude helpers were migrated.

## 9. Self-review conclusion

The V4 candidate closes the specific independent-review failures without lowering any threshold. It has a deterministic default runtime, immutable source identity, explicit scope baseline, semantic source disposition, dynamic reachability, state effects, failure behavior, mutation evidence, exact-resume evidence and cleanroom reproducibility. It is ready for a fresh independent review, not yet self-declared verified complete.
