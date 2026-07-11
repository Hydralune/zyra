---
schema: zyra.skill/v1
name: codebase-analysis
description: Understand repository structure, dependencies, ownership, and the safe change surface.
when-to-use: Before planning a non-trivial code change or architecture review.
version: 1.0.0
user-invocable: true
model-invocable: true
invocation: {"mode":"inline","max-skill-depth":0}
allowed-tools: ["builtin/file_read","builtin/shell","builtin/checkpoint","builtin/trace","builtin/artifact_write"]
context-budget: {"listing-tokens":96,"body-tokens":4500,"resource-read-tokens":3000,"invocation-total-tokens":9000,"restore-tokens":2500}
resources: ["references/analysis-checklist.md"]
---
Build an evidence-backed codebase map before recommending changes.

Read the project instructions and state owners first. Trace production imports and runtime entry points. Separate observed behavior from inference. Record relevant files, risks, tests, and downstream consumers. Do not write files while this skill is active unless a later, separately authorized change skill is invoked.
