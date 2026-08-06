---
schema: zyra.skill/v1
name: failure-recovery
description: Classify a failure and produce evidence for deterministic recovery planning.
when-to-use: After a tool, worker, provider, verification, or node failure.
version: 1.0.0
user-invocable: true
model-invocable: true
invocation: {"mode":"fork","agent":"RecoveryAnalyst","max-skill-depth":0}
allowed-tools: ["builtin/trace","builtin/checkpoint","builtin/file_read","builtin/shell","builtin/artifact_write"]
context-budget: {"listing-tokens":96,"body-tokens":4500,"resource-read-tokens":3000,"invocation-total-tokens":8500,"restore-tokens":2200}
resources: ["references/failure-classification.md"]
---
Classify the observed failure and collect the evidence needed by RecoveryPlanner.

Identify the failed boundary, affected state, retry safety, idempotency evidence, remaining valid artifacts, and viable alternate routes. This skill may recommend recovery but does not own the deterministic recovery decision; M1-07C remains the control authority.
