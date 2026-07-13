# M1-S04B-01 Message Manager State Compression Foundation Review

Date: 2026-07-13

Status: `completed`.

Base commit: `3800e2c16b2b5b2c00ab3a312dd3e2f78d2720ef`.

Implementation evidence commit: `0cd61adc9fe6a1f444cb43a436784a1d859e28d7`.

## Final decision

`M1-S04B-01` is complete. The parent `M1-04B` remains in progress; `M1-S04B-02` owns the remaining parent integration and cumulative closure work.

The default productized `BrowserWorkerRuntime` now reads a real live page after successful browser actions through one Zyra-owned state path: mandatory CDP DOMSnapshot + DOM + AX capture, frame and generation identity, deterministic DOM construction, durable selector-map revisions, stale-selector rejection, semantic outline and DOM delta, atomic action-result history, full-state artifact externalization, low-entropy context disclosure, fidelity audit, the existing 02D context-window owner, and candidate-only memory events.

This slice does not claim the canonical task/event store (05C), canonical memory commit or skill-memory promotion (06B/06C), global compact policy (02D), full browser action/controller ownership (04C), full browser process watchdog ownership (04D), API/UI history ownership (04B-02/M2), or competition-gate closure.

## Slice target coverage matrix

| Slice objective | Status | Evidence | Blocking |
| --- | --- | --- | --- |
| Authoritative DOM + DOMSnapshot + AX capture | complete | `browser_state/runtime.py`, `snapshot_decoder.py`, `dom_builder.py`; root responses are mandatory and invalid/missing roots fail closed | no |
| Frame, viewport and generation identity | complete | `BrowserDomCaptureRequest`, `SelectorMapIdentity`, frame-tree and viewport capture, session/target/CDP generation checks | no |
| Durable selector mapping and stale rejection | complete | `selector_store.py`, composite identity, atomic JSON replace, revision CAS, `BrowserDomWatchdog.assert_selector_revision` | no |
| Readable DOM and semantic outline | complete | `models.py`, `serializer.py`, `artifact_serializer.py`, `semantic_sections.py`, `interactivity.py`, `occlusion.py` | no |
| DOM/selector delta | complete | `state_delta.py`; stable-key matching, navigation detection, continuity and meaningful-change metrics | no |
| Action-result projection and tool-pair integrity | complete | `action_result.py`, `history.py`; orphan tool results block, tool-call/result pairs remain atomic under budget | no |
| Low-entropy disclosure and full-state offload | complete | `compressor.py`, `externalizer.py`; bytes/tokens/repetition/offload/selectors/critical-fact metrics and bounded inline context | no |
| Fidelity audit | complete | `fidelity.py`; capture digest, selector refs, action pairs, failures, artifacts, critical facts and trust marker are checked | no |
| 02D next-context integration | complete | `context_port.py` calls `ClaudeContextWindowManager`, then verifies the exact disclosure id and content were selected | no |
| Candidate-only memory handoff | complete | `memory_bridge.py`; events say `committed=false` and preserve `MemoryFabric/M1-06B-M1-06C` as canonical owner | no |
| Default BrowserWorker reachability | complete | `browser_worker.py` invokes the shared `BrowserMessageStateApplication` after successful actions and before session stop | no |
| Restart/idempotency projection | complete | `turn_store.py`; read-once/previous facts and turn identity survive application restart without becoming canonical history | no |
| Failure and disable behavior | complete | six integration tests, ten-component disable matrix, separate selector-store disable and stale-selector tests | no |
| Source-to-target decisions | complete for this slice | 17 browser-use rows: 12 productized, 4 reference-only, 1 deferred; deterministic sync and strict ledger audit | no |
| 10,000 production-line minimum | complete | 10,908 production additions, 4 deletions, 10,904 net; scripts/tests/data/docs/vendor excluded | no |
| Parent 04B integration closure | not in this slice | owner `M1-S04B-02` | does not block this foundation slice |

## Production main path

```text
POST /tasks/{task_id}/workers/browser
  -> BrowserWorkerRuntime
  -> 04A BrowserRuntime / live session, target and CDP generation
  -> successful session-bound actions
  -> BrowserMessageStateApplication
  -> BrowserDomStateRuntime
       -> DOM.getDocument
       -> DOMSnapshot.captureSnapshot
       -> Accessibility.getFullAXTree
       -> Page.getFrameTree
  -> enhanced DOM + selector revision + semantic outline + DOM delta
  -> action-result/history projection
  -> full-state JSON + readable HTML + fidelity report artifacts
  -> bounded low-entropy disclosure
  -> 02D ClaudeContextWindowManager selection
  -> canonical EventRecord projection + candidate-only memory events
  -> BrowserWorker result metadata/artifacts/events
```

The default productized worker has no fallback that synthesizes an empty successful DOM, bypasses the selector owner, or returns success when the disclosure is rejected by 02D. Static and compatibility browser paths are not counted as 04B evidence.

## Main-path module evidence

| Module | Runtime responsibility | Dynamic evidence |
| --- | --- | --- |
| `browser_state/runtime.py` | live CDP capture transaction and generation fencing | fake-CDP behavioral integration plus real Chrome websocket smoke |
| `browser_state/dom_builder.py`, `snapshot_decoder.py`, `models.py` | merge DOM, DOMSnapshot and AX into typed state | non-fixture page inputs produce selectors, facts, frame/viewport and artifacts |
| `browser_state/selector_store.py`, `watchdog.py` | durable revision owner, CAS, identity and stale action preflight | restart, disabled-store and stale-selector failures |
| `browser_state/serializer.py`, `semantic_sections.py`, `state_delta.py` | readable DOM, semantic compression source and page-change model | first/second read changes outline, previous facts and delta |
| `browser_context/action_result.py`, `history.py` | tool result budgets and atomic message history | orphan rejection and pair-preserving trim test |
| `browser_context/externalizer.py`, `fidelity.py` | complete offload and semantic fidelity | three state artifacts are written/read/hashed; tamper-sensitive invariants run before context append |
| `browser_context/compressor.py`, `message_manager.py` | disclosure construction and causal events | bytes/tokens/compression/repetition/artifact-offload metrics in worker metadata/events |
| `browser_context/context_port.py` | 02D compact/context owner port | exact disclosure id/content must appear in selected model messages |
| `browser_context/memory_bridge.py` | evidence-rich memory candidates only | four candidate events in real live run; zero direct memory writes |
| `browser_context/application.py`, `turn_store.py` | one composition root and durable projection index | default worker and restarted application reuse prior facts/idempotency state |

Removing or disabling the DOM runtime, watchdog, selector store, message manager, action projector, compressor, externalizer, fidelity auditor, history normalizer, next-context port, or turn store causes a real worker/read-state behavior to fail or materially change. The tests do not merely import these modules.

## Browser Use source-to-target decisions

| Source | Disposition | Zyra target | Reason/evidence |
| --- | --- | --- | --- |
| `browser_use/screenshots/__init__.py` | reference-only | `browser_state/artifact_serializer.py` | initializer has no state algorithm; 04B preserves existing 04A screenshot refs |
| `dom/serializer/clickable_elements.py` | Zyra module migrated | `interactivity.py`, `selector_ranker.py` | interactive classification and bounded selector disclosure |
| `dom/enhanced_snapshot.py` | Zyra module migrated | `snapshot_decoder.py`, `dom_builder.py`, `runtime.py` | mandatory snapshot/DOM/AX merge and frame identity |
| `dom/serializer/eval_serializer.py` | reference-only | `serializer.py` | evaluate-based authoritative capture rejected in favor of CDP DOMSnapshot + DOM + AX |
| `dom/playground/extraction.py` | reference-only | `semantic_sections.py` | playground code is not a product dependency; Zyra owns deterministic extraction |
| `dom/serializer/html_serializer.py` | Zyra module migrated | `serializer.py` | readable HTML and safe attribute projection |
| `dom/markdown_extractor.py` | Zyra module migrated | `semantic_sections.py` | semantic section/fact outline, repetition and frame metrics |
| `dom/playground/multi_act.py` | deferred | `watchdog.py`; next owner 04D | multi-action registry/controller execution is outside state projection |
| `dom/serializer/paint_order.py` | Zyra module migrated | `occlusion.py`, `serializer.py` | paint-order and occlusion-aware visibility |
| `dom/serializer/serializer.py` | Zyra module migrated | `models.py`, `serializer.py`, `dom_builder.py` | typed DOM tree, stable identities and serializer pipeline |
| `agent/message_manager/service.py` | Zyra module migrated | `message_manager.py`, `compressor.py`, `application.py` | Zyra-owned projection, disclosure and composition root |
| `dom/service.py` | Zyra module migrated | `runtime.py`, `capture_policy.py` | authoritative capture service and fail-closed policy |
| `screenshots/service.py` | reference-only | `artifact_serializer.py`, `externalizer.py` | acquisition remains 04A-owned; 04B preserves/offloads artifact refs |
| `agent/message_manager/utils.py` | Zyra module migrated | `action_result.py`, `history.py` | result normalization and atomic tool pairs |
| `dom/utils.py` | Zyra module migrated | `text.py` | untrusted text/redaction/fact normalization |
| `agent/message_manager/views.py` | Zyra module migrated | `browser_context/models.py`, `history.py` | message and projection contracts |
| `dom/views.py` | Zyra module migrated | `browser_state/models.py`, `contracts.py` | DOM node, capture and selector-revision contracts |

`scripts/sync_browser_message_state_source_ledger.py` deterministically replaces only the `M1-04B` owner group. All active rows are `productized` + `tested_main_path`; reference/deferred rows are excluded from production line accounting. None depends on the root browser-use checkout at runtime.

## Cross-runtime source decisions

| Source mechanism | Decision in 04B-01 | Boundary |
| --- | --- | --- |
| Browser Use `browser/watchdogs/dom_watchdog.py` | selective semantic port | `browser_state/watchdog.py` owns DOM stability/generation/selector preflight only; full watchdog registry/process behavior remains 04D |
| Open Multi-Agent Platform AgentLoop message normalization | selective semantic port | `history.py` adopts atomic tool-call/result and result-budget behavior; OMP session/task state is not an owner |
| OMP AgentSession | reference-only | Zyra 02D/canonical session owners remain authoritative |
| OMP Snapcompact | contract-only experimental reference | `SnapcompactFidelity` is an audit vocabulary, not a second compact engine or state owner |
| opencode part/session event contracts | reference-only | projected through Zyra `EventRecord`; no opencode store/runtime dependency |
| Hermes long-session compression | reference-only | deterministic local disclosure is used; no Hermes process/provider dependency |
| Agent Framework compaction/session contracts | reference-only | existing 02D `ClaudeContextWindowManager` remains the compact/context owner |
| claude-code-best auto-compact/context selection | active existing 02D owner reused | 04B contributes browser disclosure blocks and verifies selection; it does not fork the owner |

## State custody

| State | Zyra owner and persistence rule |
| --- | --- |
| canonical task/run/session | existing task/session owners; browser modules carry causal ids only |
| browser session, target, CDP generation | 04A `BrowserRuntime` / `BrowserSessionRuntime`; 04B uses explicit narrow ports and never creates a second session owner |
| DOM capture | `BrowserDomStateRuntime`; immutable per-capture value, externalized to artifact store |
| selector revisions | `BrowserSelectorMapStore`; durable JSON, revision CAS, composite session/target/frame/generation identity |
| message turn projection | `BrowserMessageManagerRuntime`; immutable result of one capture/action projection |
| read-once/previous facts/idempotency index | `BrowserTurnProjectionStore`; durable projection index, explicitly not canonical conversation history |
| full state/HTML/fidelity bytes | existing `LocalArtifactStore` through `BrowserStateArtifactExternalizer` |
| bounded next context | existing 02D `ClaudeContextWindowManager`; 04B validates acceptance and stores a receipt only |
| canonical memory | `MemoryFabric` and 06B/06C; 04B emits candidates with `committed=false` and performs zero direct writes |
| canonical events | existing API/event owner; 04B emits typed `browser_session_lifecycle` and `agent_message` records with causal ids |

Selector and projection stores recreate owned directories, use atomic replace, validate schemas/digests, and expose explicit disabled/conflict/missing/stale failures. No SQLite/cache/artifact residue is required by the tests.

## Failure and disable evidence

| Disconnected component or condition | Observed semantic effect |
| --- | --- |
| DOM runtime | worker fails instead of returning an empty page state |
| capture policy fail-open request | request rejected before CDP state is accepted |
| privileged scheme without authorization | capture rejected; an explicit allowed scheme from the permission-owned request is honored |
| oversized raw/snapshot state | capture rejected with bounded diagnostic details |
| DOM watchdog | message/state transaction fails closed |
| selector store disabled | real worker fails; stale selector refs are rejected after revision change |
| message manager/action projector/history | turn projection fails; orphan tool results never reach context |
| compressor | no full-state substitution; worker fails when a valid disclosure cannot fit without breaking invariants |
| externalizer | missing/unverifiable artifact write blocks disclosure |
| fidelity auditor | invalid selector/artifact/action/trust relationships block context append |
| 02D context port | worker fails unless the exact disclosure was selected by the canonical context owner |
| durable turn store | read-once/idempotency path fails rather than silently becoming an in-memory fallback |

## Verification

| Command | Result |
| --- | --- |
| `python -m unittest tests.integration.test_browser_message_state_compression_foundation -v` | 6 passed in 54.472s on final code |
| `python -m unittest tests.integration.test_browser_session_productization_foundation tests.integration.test_browser_session_productization_integration tests.unit.test_browser_session_runtime_foundation -v` | 16 passed in 71.175s |
| `.venv\Scripts\python.exe scripts\smoke_browser_session_productization_live.py` | passed in 13.2s on final code; real Chrome websocket, DOM/DOMSnapshot/AX, selector revision, 02D receipt, five total artifacts, four memory candidates, owner-loss/resume and physical cleanup |
| `python scripts\sync_browser_message_state_source_ledger.py --check` | aligned; 17 decisions = 12 migrated + 4 reference + 1 deferred |
| `python scripts\verify_internalization_ledger.py --json` | `ok=true`, 1,097 entries, 0 errors, 0 blockers, 552 broader-repository warnings |
| `python -m compileall -q packages\workers\zyra_workers\browser_context packages\workers\zyra_workers\browser_state scripts\sync_browser_message_state_source_ledger.py` | passed on final code |
| `git diff --check` and dependency/path scan | clean; no root source path, subprocess, dynamic import, TODO/FIXME, mock-only production path or new dependency |

The real Chrome result recorded `browser_context_bytes=5614`, `browser_context_tokens=1560`, `browser_context_compression_ratio=0.9212340006563833`, one selector revision, three state artifacts (full JSON, readable HTML, fidelity report), two existing 04A artifacts (screenshot and trace), four candidate-only memory events, sealed permission mode, and `human_intervention_count=0`. The test page is intentionally small, so this ratio is dynamic reachability evidence rather than the final low-entropy benchmark comparison.

Running the live script with the repository base Python initially failed because that interpreter lacks the already-required `psutil` package. The repository `.venv` contains declared project dependencies and passed. No dependency was added and no fallback masked the environment mismatch.

## Defects found and fixed during implementation/review

- Windows universal-newline translation initially made artifact digests disagree with bytes on disk; externalization now verifies semantic text, hashes actual bytes, and rereads before returning a ref.
- The 02D renderer prefixes external/untrusted context; the port now verifies disclosure metadata and presence inside the rendered content instead of requiring raw string equality.
- Unchanged page state can legitimately reuse a selector revision while the capture id changes; the watchdog now treats capture-id change as non-fatal only when the authoritative state digest is identical.
- The real local file lane needed explicit privileged-scheme authorization; capture policy now honors the request's permission-owned `allowed_schemes` without enabling a default bypass.
- `allowed_schemes` supplied as a string could be interpreted character-by-character; policy normalization now treats it as one scheme.
- The selector-store lookup initially caught all exceptions as “no previous state”; it now catches only the typed missing-revision error so corruption/disable/conflict cannot be hidden.
- Budget trimming changed inline selectors but left selection metrics stale; selected count, frame coverage and frame fidelity are recomputed after every trim.
- The initial ledger still described 04B sources as vendored/planned; all 17 rows now have explicit productized/reference/deferred dispositions and real canonical event types.
- A final file-tail cleanup introduced mixed indentation in a selectively ported serializer; a fresh real-Chrome import caught it, the indentation was corrected, and compile, six behavioral tests and live Chrome were rerun successfully.

## Effective line buckets

| Bucket | Added | Deleted | Net | Completion accounting |
| --- | ---: | ---: | ---: | --- |
| Production `packages/**` excluding ledger data | 10,908 | 4 | 10,904 | counted |
| Tests | 536 | 8 | 528 | excluded |
| Source-ledger synchronization script | 491 | 0 | 491 | excluded conservatively |
| Ledger JSON data | 1,298 | 1,247 | 51 | excluded data/source-map bucket |
| Review/docs | not counted | not counted | excluded | excluded |
| Generated/runtime assets | 0 | 0 | 0 | excluded |
| `vendor/**` + `vendor-runtimes/**` | 0 | 0 | 0 | failure gate satisfied |
| Mock/fixture-only production | 0 | 0 | 0 | excluded/not present |

The production minimum is satisfied without tests, scripts, ledger records, documentation, generated data, source pools, vendor code or a thin external adapter. The 2,348-line selective DOM serializer/model port is integrated through the live runtime and fails behavior tests when disconnected; it is not an inert source dump.

## High-risk escalation assessment

No high-risk escalation trigger applies. This is the first browser-specific DOM/selector/message-state owner implementation allocated to 04B, not a transfer of an existing canonical owner. It does not make an incompatible shared schema migration, alter global 02D compact defaults, change permission/scheduler/recovery defaults, add a dependency/process/port/Docker/MCP/plugin/dynamic import, or change repository/packaging boundaries.

The default BrowserWorker behavior did change within its assigned slice boundary, so the incremental validation was deliberately stronger than import smoke: main-path integration, ten-component disable matrix, stale/restart paths, 16 adjacent 04A regressions and a final real-Chrome run. Full-repository and cleanroom repetition are deferred to the M1-04A-through-04D aggregate review unless a later slice introduces a matching escalation trigger.

## Requirement calibration

This slice advances `REQ-COMM-01` and `SCORE-EFF` infrastructure and dynamic evidence through bytes/tokens, duplicate-fact rate, bounded selector/context disclosure, artifact offload and fidelity metrics. It also supports `REQ-MEM-01` through a verified 02D context handoff and candidate-only memory boundary, and supports `REQ-TRACE-01` through causal capture/disclosure/action/artifact events.

No requirement status is promoted and no competition gate is closed. The two high-completion cross-domain live tasks, 2,000 canonical transitions, sparse-topology baseline, true local/edge/cloud dispatch, multi-provider compatibility, formal fault injection/recovery, UI causal trace and final benchmark comparisons remain with their requirement-matrix owners.

## Residual non-blocking risks and handoff

- `M1-S04B-02` must close parent integration: API/checkpoint/resume projections, broader task history/context consumption, cumulative 18,000-line parent floor and parent acceptance matrix.
- `M1-04C` owns full action registry and exact selector-driven interaction/security behavior.
- `M1-04D` owns complete browser watchdog/process/download/history behavior; 04B's DOM watchdog is intentionally narrow.
- `M1-06B/06C` must decide which browser memory candidates become canonical episodic/semantic/skill memory and prove retrieval effects.
- `M1-05C/M2` must project canonical events/history/artifacts into the durable API and UI without creating another owner.
- The tiny live page demonstrates reachability and causality, not final compression quality. Larger realistic pages and static/full-context comparison remain mandatory for aggregate/benchmark review.
- The broader ledger still reports 552 warnings outside this slice; current entries have zero errors/blockers.

The root agent must update `docs/milestones/execution-state.yaml` separately as the only execution-state source after the evidence commit. This review does not supersede that state file.
