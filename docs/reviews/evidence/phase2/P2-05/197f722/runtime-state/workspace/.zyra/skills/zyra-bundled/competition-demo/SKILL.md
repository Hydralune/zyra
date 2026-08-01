---
schema: zyra.skill/v1
name: competition-demo
description: Audit a live scenario against competition evidence gates and delivery constraints.
when-to-use: Before or after a competition-aligned clean-state scenario run.
version: 1.0.0
user-invocable: true
model-invocable: true
invocation: {"mode":"fork","agent":"EvaluationHarness","max-skill-depth":0}
allowed-tools: ["builtin/file_read","builtin/trace","builtin/checkpoint","builtin/shell","builtin/artifact_write"]
context-budget: {"listing-tokens":96,"body-tokens":7000,"resource-read-tokens":3500,"invocation-total-tokens":12000,"restore-tokens":3500}
resources: ["references/competition-gates.md","templates/demo-runbook.md"]
---
Evaluate a real scenario against the competition and engineering gates.

Require live inputs and real state transitions. Check zero human intervention, long-run continuity, dynamic sparse topology, low-entropy communication, local/edge/cloud dispatch, multi-model behavior, fault/change/node-loss recovery, causal trace, artifacts, and delivery reproducibility. This skill audits evidence; it is not the scenario runner itself.
