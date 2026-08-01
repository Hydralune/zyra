---
schema: zyra.skill/v1
name: verification
description: Independently verify an implementation and preserve reproducible evidence.
when-to-use: After code changes or before a completion decision.
version: 1.0.0
user-invocable: true
model-invocable: true
invocation: {"mode":"fork","agent":"Verifier","max-skill-depth":0}
allowed-tools: ["builtin/file_read","builtin/shell","builtin/checkpoint","builtin/trace","builtin/artifact_write"]
context-budget: {"listing-tokens":96,"body-tokens":4500,"resource-read-tokens":3500,"invocation-total-tokens":9000,"restore-tokens":2500}
resources: ["references/verification-matrix.md","templates/verification-report.md"]
---
Verify the requested behavior from a clean and skeptical perspective.

Run real commands against non-fixture inputs where possible. Cover the happy path, invalid input, permission or lifecycle failures, concurrency or replay risks, and disable-to-fail semantics. Do not modify production files. Report exact commands, observed results, and any blocker.
