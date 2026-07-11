---
schema: zyra.skill/v1
name: code-change
description: Implement a scoped code change, preserve safety invariants, and produce verification evidence.
when-to-use: After analysis identifies a concrete implementation target.
version: 1.0.0
user-invocable: true
model-invocable: true
invocation: {"mode":"inline","max-skill-depth":0}
allowed-tools: ["builtin/file_read","builtin/file_write","builtin/file_edit","builtin/shell","builtin/checkpoint","builtin/trace","builtin/artifact_write"]
context-budget: {"listing-tokens":96,"body-tokens":7000,"resource-read-tokens":3500,"invocation-total-tokens":12000,"restore-tokens":3500}
resources: ["references/change-safety.md","templates/change-report.md"]
---
Implement only the requested change inside the authorized workspace.

Preserve unrelated user changes. Re-read stale files before editing, prefer patch-based edits, and keep state ownership explicit. Every write and shell action remains subject to the normal permission runtime even though it appears in this skill's capability ceiling. Verify the behavior and failure path before reporting completion.
