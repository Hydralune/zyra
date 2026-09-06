import type { ArtifactProjection } from "../../../../../packages/core/typed-api-client/src/index.ts"

// Only known runtime envelopes belong to evidence. Unknown/user artifacts stay
// visible; security labels describe access policy, not whether a file is useful.
const EVIDENCE_KINDS = new Set(["code_worker_execution", "memory_continuity", "runtime_receipt", "execution_manifest"])
const LEGACY_EVIDENCE_TITLES = new Set(["Physical CodeWorker delivery manifest", "Physical MaAS memory continuity result", "CodeWorker QuerySession Snapshot", "CodeWorker TypeScript Runtime Transcript"])

export function isRuntimeEvidence(artifact: ArtifactProjection): boolean {
  return EVIDENCE_KINDS.has(String(artifact.metadata.domain_result_kind ?? ""))
    || LEGACY_EVIDENCE_TITLES.has(artifact.title ?? "")
    || /^CodeWorker (?:E01 trace workerreq_|tool result call_)/.test(artifact.title ?? "")
}

export function productArtifacts(artifacts: readonly ArtifactProjection[]): ArtifactProjection[] {
  return artifacts.filter((artifact) => !isRuntimeEvidence(artifact))
}
