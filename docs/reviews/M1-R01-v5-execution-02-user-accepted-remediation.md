# M1-R01 Execution-02 User-Accepted Remediation

- Recorded: `2026-07-17`
- Execution: `E02 permission / MCP / skill TypeScript custody cutover`
- Verified baseline retained: `f07fd239dd768f399a36329314da82e90ddce6a4`
- Accepted implementation: `9de572bb993ce154ed588fe12f80870e849493e9`
- Accepted implementation evidence: `bad500f642d71eacd84b77d1ecd9ab09258c2f00`
- Acceptance kind: `user_accepted_remediation`
- Independent PASS: `false`

## Decision

After the v4 independent review rejected implementation/evidence candidate `f7ef18c080533e81ea4e6b412007424696fa7423` / `69eaa206338268ba782dc64655e00ba4df1e88ec`, the user authorized direct repair. The repaired candidate passed its exact-candidate gate, exact-commit cleanroom, 800 E02 tests, 70 target-specific mutations, built default live MCP, multi-process restore, external-effect lost-ACK recovery, and TypeScript-disable probes.

The user explicitly accepted that repaired candidate and waived another fresh-window independent review to avoid duplicating the completed high-cost verification. This is not recorded as an independent PASS and does not rewrite the historical FAIL in `docs/reviews/M1-R01-v4-execution-02-independent-review.md`.

E02 therefore transitions from `implementation_complete_review_pending` to `completed_after_user_accepted_remediation`. E03 may transition from `blocked` to `ready`, but its implementation still begins only in a new window under the user's explicit E03 instruction and the E03 execution document.

`verified_zyra_head` remains unchanged because the workflow reserves that field for an independent PASS. The acceptance commit is recorded separately as `user_accepted_zyra_head` and becomes the implementation baseline for E03.

## Accepted evidence summary

- Candidate verifier: PASS, failures `[]`.
- Exact-commit cleanroom: all eight commands exited zero.
- E02 behavior suite: 800/800 passed across 12 files.
- Mutation corpus: 70/70 killed, zero survived or invalid, production hashes restored.
- Runtime probes: built-only runtime origin/write path, three-PID resume, real stdio MCP lost ACK with one external effect, reconciliation, stable replay, and fail-closed disable all passed.
- Effective accounting: 38,529 changed TypeScript production lines, 7,602 behavior-test lines, 151 unique behavior cases, 109 failure/crash cases, and 5.1921% retained adapter ratio.

## Audit boundary

The root workspace documents and schema-v3 manifests live outside the Zyra Git repository. Their corresponding status and E02 checkpoint are updated separately in `docs/milestones/execution-state.yaml` and `docs/remediations/M1-R01-claude-source-custody/execution-02-permission-mcp-skill-cutover.md`.
