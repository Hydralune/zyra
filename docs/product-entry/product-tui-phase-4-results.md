# Product TUI Phase 4 — Permissions, files, verification, and results

## Canonical product facts

- Permission cards are rebuilt from permission-custody snapshots and show action, reason, risk, scope, expiry, and request identity.
- Allow/deny responses use the existing challenge-bound proof and canonical receipt. Missing, expired, or invalid custody remains fail-closed.
- Workspace changes come only from `metadata.delivery` with schema `zyra.task-workspace-delivery/v1`.
- Final verification comes from `metadata.canonical_task_outcome.verification`. Missing verifier evidence is shown honestly as “未记录最终验证”; it is never inferred from an assistant message.
- Tool failures use bounded product summaries. Internal node, lease, route, audit, and artifact-maintenance events remain developer-only.
- Task failures use canonical failure metadata and include the exact `zyra resume <task>` recovery command.

## Bounded diff policy

At terminal settlement, the CLI may add a local convenience diff for paths named by the canonical workspace delivery. Paths are validated beneath the active workspace, git output is captured without color or external diff drivers, created-file reads are byte-limited, and the product event is capped at 120 lines. Missing local materialization does not fabricate a diff; `/ui` remains the complete canonical diff-review path.

## Covered failure cases

- permission deny/expiry and custody loss;
- tool failure without promoting a local node failure to task failure;
- verifier failure with bounded failed-condition labels;
- real task failure with recovery guidance;
- absent local diff content and path-escape attempts;
- oversized created files and diff output truncation.
