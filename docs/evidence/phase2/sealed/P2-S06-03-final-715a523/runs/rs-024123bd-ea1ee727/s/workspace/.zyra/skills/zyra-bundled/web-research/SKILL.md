---
schema: zyra.skill/v1
name: web-research
description: Research web sources, preserve provenance, and return an evidence bundle.
when-to-use: When a task requires current or externally sourced facts.
version: 1.0.0
user-invocable: true
model-invocable: true
invocation: {"mode":"fork","agent":"Researcher","max-skill-depth":0}
allowed-tools: ["builtin/web_search","builtin/browser","builtin/file_read","builtin/trace","builtin/artifact_write"]
context-budget: {"listing-tokens":96,"body-tokens":4500,"resource-read-tokens":3000,"invocation-total-tokens":9000,"restore-tokens":2200}
resources: ["references/source-quality.md"]
---
Collect current evidence with explicit source provenance.

Prefer primary sources. Record publication and event dates when recency matters. Keep quotations short and separate source claims from inference. Return a structured evidence bundle with direct references and unresolved conflicts. Network and browser actions remain subject to 03A permission and domain constraints.
