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

When the task requires a reproducible script, invoke that exact delivered entrypoint from a clean copied workspace, require exit code zero, and compare regenerated outputs byte-for-byte or with an explicit field-level semantic comparison. A sibling implementation in another language does not verify the named entrypoint. For source, evidence, or provenance indexes, independently confirm that every indexed path has a real content digest and a concrete extraction method, and that conclusion records point back to those indexed sources.
