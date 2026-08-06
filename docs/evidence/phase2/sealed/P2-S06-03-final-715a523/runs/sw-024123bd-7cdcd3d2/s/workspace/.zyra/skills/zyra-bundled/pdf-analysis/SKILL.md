---
schema: zyra.skill/v1
name: pdf-analysis
description: Extract PDF structure and trace claims back to pages and evidence.
when-to-use: When requirements or evidence are stored in PDF documents.
version: 1.0.0
user-invocable: true
model-invocable: true
invocation: {"mode":"fork","agent":"DataWorker","max-skill-depth":0}
allowed-tools: ["builtin/file_read","builtin/shell","builtin/trace","builtin/artifact_write"]
context-budget: {"listing-tokens":80,"body-tokens":4000,"resource-read-tokens":3000,"invocation-total-tokens":8000,"restore-tokens":2000}
resources: ["references/pdf-extraction.md"]
---
Extract PDF content with page-level traceability.

Use only available, authorized PDF inspection capabilities. If no PDF backend exists, return capability unavailable instead of fabricating extraction. Preserve page numbers, tables, figures, and OCR uncertainty. Store large extraction output as an artifact and pass references to other agents.
