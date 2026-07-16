# M1-R01 Execution-01 V10 Critical Self-Review

## 1. Review identity and verdict

- Execution: `E01`, runtime core TypeScript cutover.
- Implementation candidate (`I`): `ad0479228541e99594aba87934cd2ae0a6f5ca5b`.
- Evidence commit (`E`): `c26868c`.
- Verified baseline remains `c34535a783e88f9481387ced89cba4fbc333dc74` until an independent reviewer returns `PASS` against the immutable review chain.
- Self-review verdict: `VALIDATION_PASS; INDEPENDENT_REVIEW_REQUIRED`.
- This review does not authorize E02 and does not mark E01 complete.

## 2. V9 failure closure

| V9 finding | V10 correction | Evidence |
| --- | --- | --- |
| Root receipt, G0 summary, source map, and mutation counts disagreed | V6 now produces one `280 accepted / 80 rejected / 280 mappings / 123 mutations` closure and freezes byte-exact copies of all six root manifests in the Zyra evidence tree | `g0-summary.json`, `root-manifest-index.json`, `root-manifests/**` |
| Candidate gate retained a legacy top-level `ok:false` | Both strict and candidate reports are native V4 verifier outputs and return `ok:true` | `strict-gate.json`, `candidate-gate-result.json` |
| Eight outside-curated symbols were improperly credited | V6 permits only six explicitly authorized post-cutoff retry symbols; V4 independently rejects any unauthorized outside-curated accepted range | Root source manifest plus V4 reports |
| Recognized source coverage was below the threshold | V4 recognizes `10,626` executable source lines against the `10,587` threshold | Both V4 reports |
| `getAnthropicClient` was a descriptor rather than a material factory | The factory now resolves Anthropic, Bedrock, Foundry, and Vertex construction, protected/custom headers, credential routing, retry/timeout policy, region/project state, and secret-externalized operation; the default request path consumes the constructed client state | Provider custody and default-path behavior tests |
| Auto compact and session memory shadowed their source mechanisms | Auto plan, partial boundary, streaming attempts, session-memory result, pending-edit consumption, and their restore state are now canonical custody fields consumed by the default context path | Compact custody/runtime behavior tests and mutation targets |
| Provider switching retained stale endpoint state | Provider change rebuilds the provider default endpoint and consumes client-factory endpoint/header/timeout output into prepared request state | Provider behavior tests and mutations |
| Post-compact read lineage was inverted | Preserved reads are excluded from reinjection, plan/memory paths remain excluded, unread paths remain eligible, and the truncation marker is included inside the token budget | Context behavior tests; these auxiliary symbols receive no outside-curated source credit |
| V4 was not the root authority | The gate profile, baseline receipt, G0 summary, strict gate, and candidate gate all name V6 generation plus V4 immutable-blob verification; legacy V3 artifacts are historical only | Frozen root closure and both gate reports |

## 3. Quantitative gates

| Gate | Result | Assessment |
| --- | ---: | --- |
| Effective changed production TypeScript | `32,381` | Passes the `25,416` E01 minimum after `663` duplicate lines are deducted |
| Final production TypeScript | `49,964` | Informational; not substituted for effective changed code |
| Final test TypeScript | `11,170` | Kept separate from production credit |
| Recognized upstream executable lines | `10,626 / 10,587` | Pass, but only a `39`-line margin |
| Source ranges | `280 accepted / 80 rejected / 360 total` | Closed |
| Five-hop source mappings | `280 / 280` | Every accepted range has source, target symbol, default callsite, state effect, and behavior test |
| Mutation targets | `123 / 123 killed` | No survivor or invalid patch; every patch compiled and restored to the original hash |
| E01 behavior suite | `421 pass / 0 fail / 1,350 expectations` | Bun `1.2.15` |
| Toolchain commands | `9 / 9` | Install, typecheck, Bun build/health, Node build/health, and tests |
| Exact-candidate cleanroom commands | `9 / 9` | Target is exactly `I`; temporary checkout was removed |

The source-credit margin is narrow. If an independent reviewer invalidates even one accepted range whose executable size exceeds `39` lines, the source threshold fails. V10 therefore must be reviewed at the accepted-symbol level, not accepted from aggregate counts.

## 4. Internalization and state custody

The migrated mechanisms no longer run as an upstream CLI, sidecar, vendor tree, or relative workspace dependency. They are split across Zyra-owned runtime modules:

- `provider/cache-custody-runtime.ts` owns provider client construction, credential routing, protected headers, retry/timeout policy, and serializable provider custody.
- `provider/model-runtime.ts` owns the prepared model request and consumes provider custody into the actual default request path.
- `compact/compaction-custody-runtime.ts` owns compact plan/result state, pending edit consumption, streaming attempts, session-memory results, snapshot, and restore.
- `compact/context-runtime.ts` owns effective context limits, auto/partial compact decisions, compact result consumption, post-compact reinjection, and bounded truncation.
- `e01/coordinator.ts` connects effective model context and runtime event settlement to the canonical TypeScript journal.
- `apps/code-worker/src/main.ts` is the real Bun/Node entry rather than a probe-only adapter.

Canonical session and transition custody remains the TypeScript E01 coordinator/journal. Provider request state and compact state are serialized into the runtime snapshot rather than stored only in local closures. Runtime events flow through `ClaudeRuntimeCore.run -> E01RuntimeCoordinator.recordRuntimeEvent -> Journal.commit` and change both revision and state digest.

## 5. Dynamic reachability and semantic effects

- Runtime-origin probe starts the actual `apps/code-worker/src/main.ts --e01-inventory` default entry and observes canonical owner `typescript`.
- Write-path probe changes journal revision from `1` to `2` and changes the state digest.
- Same-session resume force-kills three worker processes over two restarts, advances revisions `2 -> 4 -> 6`, advances restart epochs `0 -> 1 -> 2`, and reports zero replayed transition IDs.
- Lost-ACK probe retries a committed transition without advancing revision and reports `repeated_effect_count=0`.
- Disable probe sets `ZYRA_DISABLE_E01_TYPESCRIPT_RUNTIME=1`; the default entry exits nonzero with `e01_typescript_runtime_disabled`.
- The 123 mutation tests alter concrete target state effects. Disabling or corrupting those mechanisms changes behavior and is detected, rather than merely changing a ledger or health payload.

## 6. Effective-line buckets

- Counted production: `32,381` effective changed executable TypeScript lines.
- Duplicate deduction: `663` lines identified by token winnowing.
- Reported separately: `11,170` final test TypeScript lines.
- Excluded from effective production credit: generated files, data, docs, root/frozen manifests, source maps, ledger records, vendor/source pools, adapter-only code, mocks, and fixture-only code.
- Root manifest copies and the `935,690`-byte five-hop JSONL are evidence, not production code credit.
- The source executable-line count is an upstream coverage gate and is not added to target production LOC.

## 7. Source-role discipline

- Only accepted source ranges in the frozen manifest contribute the `10,626` source lines.
- The eight V9 outside-curated symbols receive zero credit in V10.
- Query wrappers and post-compact support code remain valid Zyra production code where reachable, but outside-curated helpers are not described as upstream-equivalent and do not receive source coverage credit.
- `getAnthropicClient` is credited because V10 adds and consumes material provider-factory semantics; it is not credited for its name or descriptor shape.
- Supplementary code does not take a second canonical session, journal, provider-request, or compact-state owner.

## 8. Dependency and cleanroom boundary

- Dependency audit scanned `101` runtime files and found no forbidden root-repository paths, runtime symlinks, or relative package links.
- The runtime requires neither `../claude-code-best` nor a vendor runtime, legacy inspection sidecar, external service, Docker image, local port, or dynamic upstream import.
- Both Bun and Node builds execute from the Zyra package tree with the locked `bun.lock` and TypeScript toolchain.
- `G:/agent-zoo/docs/remediations/**` is outside the Zyra Git repository. Exact root-manifest bytes are therefore frozen inside evidence commit `E`; the workspace originals still require an explicit final non-Git update after independent review.

## 9. Residual risks and non-claims

- The `39`-line source-coverage margin is the primary residual acceptance risk.
- Passing mutation proves the declared 123 target effects are observed; it does not prove every possible defect in the 32,381 effective lines has a mutant.
- The cleanroom test is deterministic for the locked local dependency graph; it is not a claim about arbitrary future Bun, Node, or provider SDK versions.
- Provider construction tests use controlled credentials/configuration and do not make a paid live cloud call. E01 claims runtime custody and request preparation, not M3 live multi-provider benchmark closure.
- E01 does not claim the cross-domain live-task, 2,000-transition autonomous run, dynamic topology, real local/edge/cloud dispatch, or final competition-delivery gates assigned to later milestones.
- The verified baseline must not advance, and E02/E03 must remain blocked, unless an independent review validates the immutable `I/E/A/M` chain and returns `PASS`.

## 10. Self-review conclusion

V10 corrects every concrete V9 failure without counting journal templates, repeated tests, root manifests, or outside-curated helper symbols as source coverage. The candidate passes the local engineering gates and is ready for an adversarial independent review. E01 remains incomplete pending that review.
