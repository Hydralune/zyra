# M1-R01 Execution-02 Remediated Implementation Self-Review

- Execution: `E02 permission / MCP / skill TypeScript custody cutover`
- Verified baseline: `f07fd239dd768f399a36329314da82e90ddce6a4`
- Rejected implementation/evidence candidates: `8db12e16edaa72b810eacefe3ede3f1ead59d321` / `8f31f6a38af71bac3eddf94fd35956499bda21c9`, then `f7ef18c080533e81ea4e6b412007424696fa7423` / `69eaa206338268ba782dc64655e00ba4df1e88ec`
- Latest independent FAIL report: `docs/reviews/M1-R01-v4-execution-02-independent-review.md` at `7bbba3664fd1c019e1331e91f562bde06a7946de`
- Remediated implementation target: `9de572bb993ce154ed588fe12f80870e849493e9`
- Evidence payload: `docs/reviews/evidence/M1-R01-v3/execution-02/**`
- Checkpoint verdict: implementation complete; a fresh independent review is required

This record does not revise either historical FAIL. The current review window found the v4 defects and then implemented the user-authorized repairs, so it cannot independently accept its own repaired candidate.

## 1. V4 independent-review findings and direct repairs

| Finding | Repair | Enforced behavior |
| --- | --- | --- |
| Custody rows were assigned by ordinal rotation, reused three generic assertions, and referenced mutations for other targets | The generator now selects bounded semantic routes from source path/symbol tokens, limits one base source to at most two targets, caps each target at 40 mappings, and gives every target its own state/success/failure IDs and mutation | The verifier rejects mutation-target mismatch, source spray, missing semantic anchors, low assertion cardinality, target hash drift, and non-executable five-hop links |
| Live stdio MCP passed policy booleans into durable request identity | `McpClientRuntime` now constructs an explicit `McpRequestIdentity` containing only durable physical identity fields | A real projected tool call reaches the local stdio server without an invalid-identity failure |
| The journal persisted coordinator placeholder `dynamic` instead of `tools/call` | The physical JSON-RPC method is bound before the request is journaled | Durable identity and replay are tied to the executed MCP operation |
| Restart replay reauthorized a committed request and hit an idempotency mismatch | `E02CapabilityCoordinator` looks up and validates exact prior execution/digest state before authorization | A committed execution replays across a new process without issuing a second effect or a second authorization |
| A process could die after an external effect while the effect-start fact existed only in memory | The coordinator checkpoints `capability_effect_prepared` before invoking the external port; restore converts an indeterminate prior-epoch effect to `recovery_required` | The retry is refused, `reexecute_without_receipt=false`, and only evidence-bearing reconciliation can commit the result |
| Built health projected E01 prerequisite PASS as E02 completion | Built stdio health now exposes a separate `e02CapabilityRuntime` projection bound to the explicit E02 candidate and `implementation_complete_review_pending`; application health requires those exact fields | E01 remains a prerequisite only and cannot satisfy E02 readiness |
| Reviewer probes imported TypeScript source and simulated lost ACK with memory objects | The probe now spawns `node dist/code-worker-node/main.js --e02-api`, a real local stdio MCP child, real OS processes, an external effect marker, force-kill/restart, reconciliation, and a built disable subprocess; it has no production-source imports | Runtime origin, write path, same-session resume, lost ACK, and fail-closed behavior are observed on the built default entry |
| Candidate gate trusted declarations and weakly bound receipts | The verifier now binds baseline, candidate, profile, receipt hashes, control-plane commits, built probe candidate/nonce/hash, cleanroom projection, source-to-target semantics, target-specific tests, and restored evidence | A receipt from another candidate, generic assertion loop, fabricated source-import probe, or mismatched mutation is rejected |
| Cleanroom corrupted the first unstaged porcelain path by trimming its leading status column | Command capture preserves leading status bytes with `trimEnd()` | Evidence-only dirt is correctly allowed while any implementation dirt remains a hard failure |

The repaired custody corpus contains 1,481 accepted rows over 70 unique target symbols. Every row's mutation targets the same target path/symbol; target-specific assertion cardinality is 70 state, 70 failure, and 73 success IDs. A base source symbol reaches no more than two targets, all rows have a semantic anchor, and target fan-in is between 6 and 40.

## 2. Canonical runtime and state custody

| Domain | Canonical TypeScript owner | Retained Python boundary | Forbidden fallback |
| --- | --- | --- | --- |
| Permission rules, risk, mode, ASK/deny/allow, approval binding and permits | Permission runtime plus `E02CapabilityCoordinator` | Typed transport and parked outer payload/session lifecycle | Python policy evaluation or approval synthesis |
| MCP transport, auth, catalog, projection, invocation, sampling, elicitation, tasks and recovery | `packages/integrations/claude-mcp/**` plus E02 coordinator/ledger | Typed API/event/checkpoint projection | Python MCP client or second request journal owner |
| SkillTool, Markdown/frontmatter, commands and plugins | `packages/runtime/claude-runtime/src/{skills,commands,plugins}/**` | Typed API/event/checkpoint projection | Python registry, invocation or hook dispatch |
| External effect fencing and exact replay | E02 execution ledger, transition journal and checkpoint bundle | Physical payload execution after TypeScript authorization | Re-execution of an indeterminate prior-epoch effect |

The built default entry is `dist/code-worker-node/main.js --e02-api`, reached from `apps/code-worker/src/main.ts`; `E02CapabilityCoordinator.execute` remains the canonical capability callsite. Restore occurs before bootstrap. No runtime dependency points to a root source repository, source pool, editable path, dynamic link, or opaque upstream process.

## 3. Dynamic reachability and lost-ACK result

The formal probe used reviewer nonce `e9b75d4dcc984c9fb2c8429eda1289f4` and source imports were explicitly disabled.

- Runtime-origin/write-path: the built API created and persisted one E02 execution through the canonical TypeScript owner.
- Same-session resume: three distinct PIDs produced runtime epochs `1,2,3`; restore flags were `false,true,true`; stable replay flags were `false,true,true`; repeated effects remained zero.
- Lost ACK: the first process was killed after the real MCP child incremented an external marker. The second process restored `execution_phase=recovery_required`, `transition_phase=effect_started`, rejected retry with `e02_execution_recovery_required`, and did not re-execute. Evidence-bearing reconciliation committed the result; final replay was stable and the external effect count remained exactly one.
- Disable: `ZYRA_DISABLE_E02_TYPESCRIPT_RUNTIME=1` prevented the built owner from becoming ready and exposed no Python fallback.

These probes are both semantic-effect and disconnect-to-fail evidence: removing the coordinator fence, ledger restore, physical method binding, or built API owner changes or fails the observed behavior.

## 4. Effective line and test accounting

The exact-candidate verifier performs executable-line filtering and clone deduction; manifests, generated matrices, fixtures, receipts, scripts and docs receive no production credit.

| Bucket | Actual | Requirement | Decision |
| --- | ---: | ---: | --- |
| Final non-test TypeScript | 44,986 | 38,000 | Pass; corroborating inventory |
| Gross changed executable TypeScript | 43,232 | reference | Before clone deduction |
| Production clone deduction | 4,703 | n/a | Excluded |
| Effective changed TypeScript | 38,529 | 35,366 | Pass; production credit |
| Gross explicit behavior-test lines | 7,620 | reference | Before clone deduction |
| Test clone deduction | 18 | n/a | Excluded |
| Effective behavior-test lines | 7,602 | 6,000 | Pass; test credit |
| Unique behavior cases | 151 | 120 | Pass |
| Explicit failure/crash cases | 109 | 45 | Pass |
| Fixed target-specific mutations | 70 | 45 | 20 permission, 30 MCP, 20 skill; 70/70 killed |
| Remaining adapter lines | 2,110 | at most 2,500 | No production credit |
| Adapter ratio | 5.1921% | at most 10% | Pass |
| Deleted Python owner lines | 31,070 across 61 paths | mandatory frozen set | Pass; no production credit |

The source ledger independently recomputes 20,049 credited executable source lines from 23,394 physical selected lines. Claude-primary custody contributes 1,342 rows, 93 files, and 18,448 credited executable lines. These are provenance facts, not substitutes for runtime tests.

## 5. Exact-candidate validation

| Gate | Result |
| --- | --- |
| `typecheck:e02` | PASS |
| Bun/Node production build | PASS |
| Full E02 TypeScript behavior suite | PASS, 800/800 across 12 files |
| Built runtime-origin/write-path/resume/lost-ACK/disable probe | PASS; exact candidate and one nonce bound in all receipts |
| Mutation runner | PASS, 70/70 killed, 0 survived/invalid, all target hashes restored |
| Exact-commit cleanroom | PASS against `9de572bb993ce154ed588fe12f80870e849493e9` |
| Candidate verifier | PASS against `9de572bb993ce154ed588fe12f80870e849493e9`; failures `[]` |

The cleanroom archived the exact implementation commit, extracted it outside the tracked tree, ran a frozen Bun 1.2.15 install, E02 typecheck, build, explicit E02 built health, the built-only live probe, and all 800 E02 tests. All eight commands exited zero. It recorded implementation cleanliness, the pre-existing evidence-only dirty allowlist, forbidden root-source dependency names, exact candidate identity, cleanup, hashes, timings and bounded output tails.

An unrelated full-repository suite was not repeated. This repair triggered and ran the matching high-risk checks: exact cleanroom, full E02 suite, all custody manifests, all 70 mutations, built default live MCP, multiprocess restore, external lost-ACK effect, health provenance, and candidate/control-plane binding.

## 6. Independent-review handoff

A new reviewer should independently regenerate a nonce and attack at least:

- real stdio and HTTP/SSE calls with boolean policy fields, physical method mismatch, digest tampering and restart replay;
- kill after external effect but before receipt, including retry before reconciliation and forged reconciliation evidence;
- source-to-target semantic anchors, base-symbol split limits, target-specific mutation and test identities;
- E01 prerequisite versus E02 readiness provenance and candidate hash mismatch;
- the retained Python transport boundary for any local policy choice or fallback.

`verified_zyra_head` must remain `f07fd239dd768f399a36329314da82e90ddce6a4`. The remediated candidate is only `implementation_complete_review_pending`; E03 remains blocked and the next entry is the independent-review taskbook.
