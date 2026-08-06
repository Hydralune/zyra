---
schema: zyra.skill/v1
name: requirement-change
description: Analyze a running requirement change and prepare a scoped replan input.
when-to-use: When a user changes constraints or deliverables during an active run.
version: 1.0.0
user-invocable: true
model-invocable: true
invocation: {"mode":"inline","max-skill-depth":0}
allowed-tools: ["builtin/trace","builtin/checkpoint","builtin/artifact_write"]
context-budget: {"listing-tokens":96,"body-tokens":4500,"resource-read-tokens":2500,"invocation-total-tokens":8000,"restore-tokens":2200}
resources: ["references/impact-analysis.md"]
---
Convert the new instruction into an impact analysis for the existing requirement-change control path.

Identify changed constraints, affected nodes, reusable results, invalidated artifacts, and required verification. Produce typed evidence for the existing `/change` and `requirement_change` flow. Do not directly mutate graph state from the skill body.
