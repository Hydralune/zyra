# M1-R01 V7 Execution-01 Critical Self-Review

## Verdict

Internal verdict: **PASS, pending independent review**.

Implementation candidate: `050c68e1e80cbe942b248111288981bffd6f3be5`

Verified baseline retained until an independent PASS: `c34535a783e88f9481387ced89cba4fbc333dc74`

## Prior FAIL closure

The V6 independent review was correct to fail the candidate. V7 treats each V6 finding as a release blocker rather than accepting file count, generated journals, or repeated tests as custody evidence.

| V6 failure | V7 closure | Evidence |
| --- | --- | --- |
| Dirty or mismatched candidate receipt | All final probes and gates bind to immutable candidate `050c68e...`; cleanroom uses a fresh Git archive of that exact commit. | `cleanroom-result.json`, `candidate-gate-result.json` |
| Fail-open generic/strict custody gate | Both gates require a clean candidate receipt, complete five-hop evidence, and an exact mutation link. | `candidate-gate-result.json`, `source-to-target-five-hop.jsonl` |
| Generic journal templates inflated source credit | Generator accepts 280 materially adopted symbols and explicitly rejects 80 unadopted symbols; only accepted executable source lines receive custody credit. | `source-to-target-five-hop.jsonl`, `validation-summary-v7.json` |
| Missing symbol-to-behavior chain | Every accepted source entry records source symbol, default entry, immediate callsite, target symbol, state effect, behavior test, and mutation. | `source-to-target-five-hop.jsonl` |
| Repeated tests did not prove semantic ownership | The final corpus contains 103 canonical mutations: 52 semantic/static mutations and 51 exact target-disconnect mutations, all killed by their declared behavior tests. | `mutation-results.json` |
| Node strip-types was the implicit runtime | Frozen Bun install, TypeScript typecheck, bundle build, runtime tests, and built health all pass in both the toolchain probe and cleanroom. | `toolchain-result.json`, `cleanroom-result.json` |
| Journal/resume replayed transition IDs | The same-session probe spans three process epochs and two forced restarts with no replayed transition IDs. | `same-session-resume-result.json` |
| Token continuation and gateway semantics were documentary only | Continuation state and provider gateway observations are now owned by reachable runtime modules and exercised through default query behavior. | `source-to-target-five-hop.jsonl`, `mutation-results.json` |
| Child read permission bypassed operation custody | Child tool invocation derives read/write operation from tool metadata and passes the same permission boundary as the default runtime path. | `mutation-results.json`, full test result in `validation-summary-v7.json` |

## Adversarial checks

- Default-entry disable probe fails the actual runtime entry rather than a ledger, fixture, or source scanner.
- Lost-ack recovery produces no repeated external effect.
- Runtime-origin probe rejects legacy Python ownership on the default path.
- Write-path probe rejects a legacy writer and confirms the TypeScript journal owner.
- Dependency audit finds no root-source relative path, symlink, editable link, or forbidden runtime dependency.
- Cleanroom starts without `node_modules`, `dist`, `.tmp`, or Git metadata and rebuilds from the frozen lockfile.
- Mutation success requires both a non-zero mutant test result and observation of the declared killer test; a generic suite failure is not counted as a kill.

## Effective-line review

The strict profile records 10,613 accepted upstream executable lines against the 10,587 threshold, 31,099 effective changed TypeScript lines, 48,662 final production TypeScript lines, 10,730 test TypeScript lines, and 35,151 deleted Python lines. The generic profile uses different exclusions and reports 13,024 accepted upstream executable lines, 26,663 effective changed TypeScript lines, and 36,720 final non-test TypeScript lines. These profiles are intentionally not merged or substituted for one another.

Generated data, custody ledgers, manifests, fixtures, repeated templates, source pools, adapter-only code, and rejected source mechanisms do not receive accepted source-executable credit.

## Residual risks for independent review

- The 280 mappings concentrate on 51 target symbols, with a maximum fan-in of 47. The independent reviewer must sample high-fan-in targets and reject mappings that only share a broad domain label.
- The candidate metadata remains pending until the evidence commit and independent review commit are known. Built health must not report verified completion before that binding step.
- The root remediation documents and manifest mirrors are outside the Zyra Git repository. Their final state must be updated explicitly and disclosed as an uncommitted repository boundary.
- Network access was required only to materialize frozen lockfile dependencies in the cleanroom. The dependency/path audit must remain the authority for rejecting workspace-source leakage.

## Release decision

Do not advance `verified_head`, mark E01 complete, or unblock E02 until a fresh independent review returns PASS against the exact implementation and evidence identities. A review failure reopens E01 and requires another fixed candidate; it cannot be waived by this self-review.
