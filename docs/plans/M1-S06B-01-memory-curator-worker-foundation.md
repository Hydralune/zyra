# M1-S06B-01 Memory Curator Worker Foundation

## State-custody map

This map is the implementation boundary for the foundation slice.  A store being
durable does not make it canonical for another domain.

| State domain | Canonical owner | Durable representation | Mutation authority | Recovery rule |
| --- | --- | --- | --- | --- |
| Task/event evidence | existing `SQLiteStore` event log | `events` rows | existing runtime/event writers | curator reads immutable event identity and payload digest; it never rewrites evidence |
| Artifact evidence | existing artifact store | artifact refs plus content at the artifact URI | existing artifact pipeline | curator resolves only task-bound refs and verifies content hash before admission |
| Extracted evidence bundle | `CuratorCandidateStore` | `curator_evidence_bundles` | `EventArtifactTraceExtractor` | bundles are content-addressed and may be reconstructed from canonical evidence |
| Memory candidates | `CuratorCandidateStore` | `memory_candidates` plus candidate-evidence refs | extractor, deterministic decision runtime, optional model proposer | candidates are proposals only; retry reuses the candidate id/idempotency key |
| Curator jobs, leases and watermarks | `CuratorJobStore` | `curator_jobs` and `curator_job_attempts` | scheduler/worker under owner-token and lease fence | expired claims are fenced and requeued without advancing the success watermark |
| Consolidation conflicts | `CuratorCandidateStore` | `candidate_relations` | deterministic consolidator | collision becomes explicit merge/supersede/reject relation; no last-write-wins |
| Validation decisions | `MemoryDecisionStore` | `memory_decisions` | `MemoryDecisionValidator` only | repeated validation is deterministic for evidence/revision/policy inputs |
| Canonical memory | existing `SQLiteStore.memory_records` | `memory_records` | `MemoryCommitRuntime` after validator admission | one transaction performs revision CAS and writes commit receipt/event outbox intent |
| Memory revision | `MemoryCommitRuntime` over canonical store | `memory_record_revisions` | commit runtime | expected revision mismatch rejects or requests explicit merge; it never overwrites |
| Curator event delivery | `CuratorOutbox` | `memory_curator_outbox` | commit runtime creates; dispatcher settles | replay is idempotent by outbox/event id and cannot duplicate the memory mutation |
| Retrieval-index admission | existing 06A `MemoryIndexRuntime` | existing derived index job/store | outbox dispatcher requests synchronization after canonical commit | failed dispatch remains pending; rebuilding uses canonical `memory_records` |
| Skill candidates | `CuratorCandidateStore` | candidate kind `skill_candidate` | `SkillCandidateProjector` | no skill file or skill registry mutation occurs in this slice |
| Failure patterns | `CuratorCandidateStore` | candidate kind `failure_pattern` | `FailurePatternMiner` | patterns require repeated, task-bound evidence and remain candidates until admission |

## Transaction and trust boundary

1. Extraction captures an immutable evidence range and hashes the normalized
   evidence.  The candidate stores both individual refs and the bundle hash.
2. Candidate generation and model-assisted classification cannot write canonical
   memory.  Model output is treated as untrusted proposal data.
3. Validation re-resolves every evidence ref, checks the hash/range, verifies
   task/run/scope/trust/provenance, redacts secrets, applies retention rules and
   checks duplicate, contradiction and expected revision.
4. Commit owns the only canonical write path.  The canonical memory row, its
   revision, the decision receipt and the event/index outbox intents are written
   in one SQLite transaction.
5. Event and index delivery occur after commit.  A crash leaves durable outbox
   work; replay cannot duplicate the canonical write because candidate id and
   idempotency key are unique.

## Source-role decision

- Primary: Hermes memory provider/manager and trajectory compression mechanisms.
  Retained responsibilities are serialized lifecycle work, bounded pre-compress
  extraction, protected evidence boundaries, non-blocking failure isolation and
  deterministic degradation.  Hermes file memory, provider registry and session
  database do not become Zyra owners.
- Supplementary: oh-my-pi local-memory job ownership, input/success watermarks,
  lease/heartbeat/retry and Mnemopi candidate consolidation/veracity mechanisms.
  These are retained as TypeScript domain logic behind the language-neutral
  candidate protocol.  Its SQLite schema, filesystem memory and direct model-to-
  file consolidation path are excluded.
- Conformance/reference only: AgentScope, LangGraph, opencode and Claude-derived
  runtime.  They create no production migration quota or competing state owner.
- Forward excluded: OpenClaw.  No source, package, path, runtime or test dependency
  is introduced.

## Foundation exit contract

The real task trace can be scheduled manually or at task end, claimed by a
`MemoryCuratorWorker`, extracted into evidence-backed candidates, deterministically
validated and committed through the single MemoryFabric owner.  The same commit
causes an event outbox record and a 06A derived-index synchronization request.
Forged evidence, secrets, untrusted rule mutation, duplicate delivery,
contradiction, stale revision and lease loss fail closed.  The parent M1-06B
remains open for the integration slice.
