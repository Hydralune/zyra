# M3-S02A-02 live owner binding resume decision

Date: 2026-07-27

Status: implementation authorized

## Correction

The blocked self-review in commit `b571225` searched the evaluation packages but
did not include `apps/api/**`.  That made its statement that Zyra lacked a
product-level live owner composition incorrect.

The product composition already exists in
`apps/api/zyra_api/live_scenario_owners.py` and is reached through
`apps/api/zyra_api/scenario_api.py`.  It binds live scenarios to Zyra-owned task
state, canonical runtime events, artifacts, worker-pool routing, checkpoints,
recovery, memory and delivery verification.  M3-S02A-02 will reuse that
composition instead of creating a second canonical owner.

The remaining implementation gap is the product benchmark port that:

1. starts fresh current-commit live source runs through the existing scenario
   API;
2. loads and verifies their causal archives;
3. executes the seven benchmark capability vectors through the existing
   evidence-backed workload runtime;
4. normalizes product receipts into the M3 benchmark admission, metric,
   statistics and report contracts; and
5. freezes the resulting evidence with integrity and provenance checks.

## Authorized no-new-paid-model boundary

The user confirmed that no paid model API is currently available and authorized
completion under that constraint.

- No new external provider or paid model request may be made.
- Software and research source tasks must still be new current-commit live
  executions using Zyra's deterministic product owner chain.
- Provider/model and local/edge/cloud dimensions must be supported only by
  exact, integrity-checked protected M1 receipts.  They are capability evidence,
  not a claim that the current M3 task issued a new provider request.
- Every current receipt must state `no_new_provider_call: true`.
- Missing or changed protected evidence must fail closed; the runner must not
  synthesize provider responses, endpoints, request IDs or successful dispatch.

## Paired campaign design

The formal campaign contains six fresh source tasks:

- `software-engineering`: repetitions 1, 2 and 3;
- `deep-research`: repetitions 1, 2 and 3.

Each fresh source archive is shared only by the seven paired variants in its
own domain/repetition block:

- single agent;
- static full-connect multi-agent;
- dynamic heterogeneous swarm;
- no scheduler;
- no memory/compact;
- no recovery;
- no low-entropy communication.

This produces 42 independently executed workload cells while preserving a
paired comparison over the same fresh source evidence.  Source run IDs, task
inputs and archive digests must be unique across the six blocks.  Cell run IDs
and variant execution receipts must be unique across all 42 cells.

## Evidence and state boundary

- The current source archive is live evidence, not a historical replay.
- Each variant must execute behaviorally through
  `EvidenceBackedWorkloadRuntime`; relabeling one observation seven times is
  forbidden.
- Effective steps are recomputed from the current archive and must retain
  causal state/route/tool/verification/permission/compact/recovery/delivery
  effects.
- The protected M1 exit evidence is referenced by exact content digest and
  projected receipt digests.
- Formal outputs are written only after all 42 cells pass admission,
  deterministic verification, statistical completeness and integrity checks.

This decision amends the blocker conclusion without rewriting the historical
review.  Code implementation begins only after this decision is committed.
