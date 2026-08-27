---
schema: zyra.skill/v1
name: report-writing
description: Assemble a concise report whose claims trace to runtime evidence and artifacts.
when-to-use: When producing a final technical, competition, or decision report.
version: 1.0.0
user-invocable: true
model-invocable: true
invocation: {"mode":"inline","max-skill-depth":0}
allowed-tools: ["builtin/file_read","builtin/checkpoint","builtin/trace","builtin/artifact_write"]
context-budget: {"listing-tokens":80,"body-tokens":4500,"resource-read-tokens":3000,"invocation-total-tokens":8500,"restore-tokens":2200}
resources: ["references/traceability.md","templates/report-outline.md"]
---
Write the report from verified evidence rather than from implementation intent.

Lead with the outcome, keep claims proportional to evidence, and link dynamic behavior to tests, events, artifacts, and metrics. Distinguish current completion from planned downstream work. Do not use code volume, static screenshots, or source ledgers as substitutes for runtime evidence.

When delivering a source or evidence index, give every indexed input, script, and output path a real sha256/digest plus the method that extracted or generated it. Keep conclusion-to-source references machine-readable; do not substitute a prose role description for provenance.
