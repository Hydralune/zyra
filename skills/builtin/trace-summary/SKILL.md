---
schema: zyra.skill/v1
name: trace-summary
description: Compress long event traces into facts, decisions, evidence, and unresolved work.
when-to-use: Before compact or when a long run needs a low-entropy handoff.
version: 1.0.0
user-invocable: true
model-invocable: true
invocation: {"mode":"fork","agent":"MemoryCurator","max-skill-depth":0}
allowed-tools: ["builtin/trace","builtin/checkpoint","builtin/artifact_write"]
context-budget: {"listing-tokens":80,"body-tokens":4000,"resource-read-tokens":2500,"invocation-total-tokens":7500,"restore-tokens":2200}
resources: ["references/event-taxonomy.md"]
---
Summarize the run without inventing missing transitions.

Preserve goals, constraints, real state mutations, route decisions, tool and verification outcomes, permission decisions, compact boundaries, failures and recoveries, artifact references, and unresolved work. Exclude heartbeat, UI repaint, repeated logs, and no-op records from effective-step claims.

For structured handoff, include the producing role, receiving role, claim identifiers, source and artifact refs, digests, uncertainties, exact verification commands, and unresolved gates. Never turn a planned role or static plan document into an executed-role claim.
