# M1-R01 v3 Execution-02 Incremental Critical Self-Review

- Execution: `E02 permission / MCP / skill TypeScript custody cutover`
- Verified baseline: `f07fd239dd768f399a36329314da82e90ddce6a4`
- Implementation target: `8db12e16edaa72b810eacefe3ede3f1ead59d321`
- Intermediate corrective target superseded by this review: `1b1ffebda03e4a241ff7e460926e6f7037642d5c`
- Evidence payload: `docs/reviews/evidence/M1-R01-v3/execution-02/**`
- Verdict at this checkpoint: implementation complete; a fresh independent review is still required

## 1. Incremental corrections after the initial implementation commit

The initial E02 gate was green, but an adversarial pass found that browser permission continuation still depended on Python-side request custody and that the verifier did not fail when some stale Python owner symbols were physically absent from the candidate. The final implementation target closes those gaps:

| Finding | Correction | Behavioral consequence |
| --- | --- | --- |
| The browser path could project a TypeScript ASK result but still reconstruct authority from a Python permission store | Added `permission.enforce` and `permission.claim` API-port operations, transported a safe TypeScript `request_binding`, and made the browser continuation consume the authoritative TypeScript request projection | A browser action cannot execute until TypeScript authorizes the exact physical identity, session, workspace, tool, call, and argument digest |
| Python and TypeScript JSON number serialization could yield different argument digests | Preserved the TypeScript canonical digest while retaining a separate Python transport digest only for the payload codec | Cross-language transport remains possible without letting Python redefine the permission identity |
| An approved external action needed exact restart continuity, while ordinary host permits must not survive restart | External approval permits are explicitly typed, persist only with the exact external subject binding, and remain one-use; ordinary unconsumed host permits are revoked at restart | Exact approved browser retry survives one restart; tampering and replay fail; generic permits do not become durable ambient authority |
| Browser worker/API construction could instantiate unrelated permission coordinators | The API owns one shared TypeScript E02 coordinator and injects its browser port/factory into `BrowserWorker` | The approval, claim, enforce, checkpoint, and event paths share one canonical TypeScript state owner |
| Retained continuation code assumed a local Python permission request | Added a narrow `external_permission_authority` mode that requires the caller-supplied TypeScript projection and checks it against the parked record | Python retains parked payload/lease lifecycle only; it cannot synthesize a new policy decision or widen the approved request |
| Four Python skill-owner modules and twenty old owner-behavior test files still represented the retired control plane | Physically deleted them and extended the verifier's frozen deletion checks | The old Python permission/MCP/skill owner cannot be revived by an import or by passing its obsolete tests |

## 2. Final custody boundary

| State or capability | Canonical owner | Retained Python responsibility | Forbidden fallback |
| --- | --- | --- | --- |
| Permission rules, mode, risk, ASK/deny/allow, approval binding, permit issuance and consumption | TypeScript permission runtime plus `E02CapabilityCoordinator` | Typed API transport, parked browser payload, local one-use receipt handoff | Python policy evaluation, local approval synthesis, raw bypass/auto flags |
| MCP auth, connection, transport, catalog, tools, resources, prompts, sampling, elicitation, tasks and recovery | `packages/integrations/claude-mcp/**` TypeScript runtime | API/event/checkpoint projection only | Python live MCP client or in-process canonical peer |
| SkillTool, Markdown resource/frontmatter, command and plugin discovery/invocation | `packages/runtime/claude-runtime/src/{skills,commands,plugins}/**` | API/event/checkpoint projection only | Python skill registry/invocation owner |
| Browser side effect | Python browser worker after an exact TypeScript permit is claimed and enforced | Payload execution and browser-session lifecycle | Executing before TypeScript claim/enforce or reusing a consumed receipt |
| E02 checkpoint and prior-epoch history | TypeScript checkpoint bundle and execution ledger | Durable outer task/session storage and transport | A second Python canonical E02 snapshot |

The default entry remains `apps/code-worker/src/main.ts`, the default capability symbol is `E02CapabilityCoordinator.execute`, and restore occurs before bootstrap. The candidate verifier reports no forbidden dependency or fallback finding.

## 3. Semantic reachability and negative controls

The cutover is reachable from real paths rather than from an inventory-only probe:

1. API construction creates the shared TypeScript coordinator.
2. `BrowserWorker` asks `TypeScriptBrowserPermissionPort` to authorize the physical browser action.
3. An ASK parks the exact request and payload; approval is submitted to TypeScript.
4. Resume claims the TypeScript permit against the original request binding and enforces it immediately before the side effect.
5. The local receipt is consumed once; replay, foreign identity, changed arguments, changed workspace/session, stale epoch, and ordinary-permit restart all fail closed.

The TypeScript suite includes an external approval permit that survives restart only for its exact subject, argument tampering rejection, one-use claim/enforce, replay rejection, and ordinary host-permit revocation. The Python adjacent suite proves the real browser worker is blocked or resumed by this port. The 48 fixed mutations cover permission, MCP, and skill invariants and all 48 are killed; restored production SHA values match.

## 4. Effective line and test accounting

The final candidate gate applies AST executable filtering and clone deduction rather than counting raw files:

| Bucket | Actual | Required | Credit decision |
| --- | ---: | ---: | --- |
| Final non-test TypeScript production | 44,908 | 38,000 | Corroborating final inventory |
| Gross changed executable TypeScript | 43,153 | reference | Before production clone deduction |
| Production clone deduction | 1,358 | n/a | Excluded from production credit |
| Effective changed TypeScript | 41,795 | 35,366 | E02 production credit |
| Effective behavior-test lines | 7,043 | 6,000 | Test evidence only |
| Unique explicit behavior cases | 123 | 120 | Generated matrix files excluded |
| Explicit failure/crash cases | 86 | 45 | Failure evidence only |
| Fixed mutations | 48 | 45 | 16 permission, 22 MCP, 10 skill; 100% killed |
| Remaining logical adapter lines | 2,110 | at most 2,500 | No production credit |
| Adapter ratio | 4.6985% | at most 10% | `adapter / (effective changed + adapter)` |
| Physically deleted Python owner lines | 31,070 | mandatory frozen deletion set | No production credit |

The source ledger contains 1,480 accepted mappings, 20,047 credited executable source lines, 70 unique target symbols, and no target symbol with more than 23 mappings. Claude-primary custody contributes 1,342 rows across 93 files and 18,447 credited executable source lines. These values establish provenance only; they do not replace runtime behavior or production credit.

The final corrective commit itself contains 329 TypeScript production additions, 1,682 Python production/retirement-boundary additions, 2,519 retired Python production deletions, 86 adapter-only additions, 14 adapter deletions, 362 test additions, and 14,850 obsolete-test deletions. The large Python browser port is intentionally reported as a retained integration boundary and is not counted as TypeScript production.

## 5. Validation on the exact implementation target

| Gate | Result |
| --- | --- |
| `typecheck:e02` | PASS |
| Bun and Node production builds | PASS, 180 bundled modules per target |
| Built Bun and Node health | PASS |
| Full E02 TypeScript behavior suite | PASS, 774/774 across 11 files |
| Python browser/API/continuation adjacent suite | PASS, 52 tests plus 21 subtests |
| Python package compile check | PASS |
| Candidate custody verifier | PASS against `8db12e16edaa72b810eacefe3ede3f1ead59d321` |
| Mutation gate | PASS, 48/48 killed, no invalid/surviving mutation, source hashes restored |
| Source-free cleanroom | PASS against the exact implementation commit |

The cleanroom uses `git archive` of the exact implementation target, starts without repository caches or build output, performs a frozen Bun `1.2.15` install, and reruns typecheck, Bun/Node build, built health, and all 774 TypeScript tests. The first attempt was blocked before installation by sandbox network policy; the recorded successful receipt is the rerun with registry access and is not a reuse of the failed result.

An unrelated full-repository Python suite was not run. E02 deleted the obsolete tests for the retired Python owner, while the directly affected browser, API injection, permission continuation, and adjacent behavior paths were rerun. Wider cross-unit regression remains the responsibility of the configured aggregate review layer.

## 6. Clean-directory and source-boundary assessment

- The exact candidate is reconstructable from Git without `../claude-code-best`, `../opencode`, `../OpenClaw`, `../Hermes-Agent`, vendor/source-pool trees, npm links, editable source paths, or cache residue.
- `.tmp/` is now explicitly ignored because the verifier and cleanroom generate archives and package caches there; it is not part of the candidate archive or line credit.
- The source-free cleanroom deletes its extracted directory after the run and records command hashes, timings, exit codes, and bounded output tails.
- The frozen Python deletion manifest is checked against the candidate commit; missing stale owner symbols are accepted only when their entire frozen path is deleted, not when verifier inspection silently skips them.

## 7. Residual risks for the independent reviewer

- The Python browser permission port is a comparatively large transport/continuation boundary. Inspect it for any local allow/deny/ask choice, ambient grant, or fallback that the static verifier may miss.
- Attack the external permit with cross-session, cross-workspace, changed tool/call identity, JSON numeric edge cases, restart epoch changes, duplicate claims, and post-consumption replay.
- Confirm that prior checkpoint epochs remain readable but cannot be injected as a future epoch, and that a normal host permit is revoked across restart while the exact external approval permit is preserved.
- Recompute the 41,795 effective TypeScript lines and 7,043 effective behavior-test lines, including clone exclusions and generated-matrix exclusions.
- Sample source-to-target rows through the real API/browser/MCP/skill path instead of accepting ledger presence as execution evidence.
- Disable the TypeScript `permission.enforce`/claim route and verify the browser side effect cannot occur through a Python fallback.

This self-review does not mark E02 verified and does not advance `verified_zyra_head`. The next authorized action after the evidence/state update is the independent E02 review taskbook; E03 remains blocked.
