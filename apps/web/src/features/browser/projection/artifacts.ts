import type { ArtifactProjection } from "../../../state/contracts.ts"
import type { ArtifactContract } from "../../artifacts/contracts.ts"
import {
  firstDefined,
  optionalString,
  stableDigest,
  type BrowserDownload,
  type BrowserScreenshot,
  type BrowserScope,
  type BrowserStep,
} from "../contracts.ts"
import type { BrowserArtifactLineage, BrowserRecord } from "./reader.ts"
import type { BrowserStepProjection } from "./steps.ts"

export interface BrowserArtifactAssociation {
  artifactId: string
  role: string
  browserSessionId: string
  stepId?: string
  actionId?: string
  resultId?: string
  toolCallId?: string
  spanId?: string
  canonical?: ArtifactProjection
  contract?: ArtifactContract
  lineage: BrowserArtifactLineage
  sourceEventIds: readonly string[]
  sourceRecordIds: readonly string[]
  findings: readonly string[]
}

export interface BrowserArtifactProjection {
  associations: readonly BrowserArtifactAssociation[]
  screenshots: readonly BrowserScreenshot[]
  downloads: readonly BrowserDownload[]
  unmatchedArtifactIds: readonly string[]
  findings: readonly string[]
}

function normalizedDigest(value: string | undefined): string {
  const raw = String(value ?? "").trim().toLowerCase()
  if (!raw) return ""
  return raw.startsWith("sha256:") ? raw.slice(7) : raw
}

function artifactRole(lineage: BrowserArtifactLineage): string {
  const role = lineage.role.toLowerCase().replaceAll("-", "_")
  if (role.includes("screenshot") || lineage.mediaType.startsWith("image/")) {
    return "screenshot"
  }
  if (role.includes("download")) return "download"
  if (role.includes("trace")) return "trace"
  if (role.includes("dom") || role.includes("accessibility")) return "dom"
  if (role.includes("tool")) return "tool_result"
  return role || "browser_artifact"
}

function eventSet(
  lineage: BrowserArtifactLineage,
  canonical?: ArtifactProjection,
): Set<string> {
  const output = new Set(lineage.sourceEventIds)
  if (canonical?.producerEventId) output.add(canonical.producerEventId)
  return output
}

function sourceRecordsFor(
  lineage: BrowserArtifactLineage,
  records: readonly BrowserRecord[],
): Set<string> {
  const output = new Set(lineage.sourceRecordIds)
  if (output.size) return output
  for (const record of records) {
    const artifactIds = [
      firstDefined(record.payload, "artifact_id", "artifactId"),
      ...(Array.isArray(record.payload.artifact_ids)
        ? record.payload.artifact_ids
        : []),
    ]
      .map((value) => String(value ?? "").trim())
      .filter(Boolean)
    if (artifactIds.includes(lineage.artifactId)) output.add(record.recordId)
  }
  return output
}

function matchAction(
  lineage: BrowserArtifactLineage,
  steps: BrowserStepProjection,
  canonical?: ArtifactProjection,
): {
  stepId?: string
  actionId?: string
  resultId?: string
  toolCallId?: string
  spanId?: string
} {
  const eventIds = eventSet(lineage, canonical)
  const recordIds = new Set(lineage.sourceRecordIds)
  const toolCallId =
    canonical?.producerToolCallId
    ?? optionalString(
      firstDefined(lineage.metadata, "tool_call_id", "toolCallId"),
      "$.artifact.tool_call_id",
      512,
    )
  let action = steps.actions.find(
    (candidate) =>
      Boolean(toolCallId && candidate.toolCallId === toolCallId)
      || candidate.sourceEventIds.some((eventId) => eventIds.has(eventId))
      || candidate.sourceRecordIds.some((recordId) => recordIds.has(recordId)),
  )
  const result = steps.results.find(
    (candidate) =>
      Boolean(toolCallId && candidate.toolCallId === toolCallId)
      || candidate.artifactIds.includes(lineage.artifactId)
      || candidate.sourceEventIds.some((eventId) => eventIds.has(eventId))
      || candidate.sourceRecordIds.some((recordId) => recordIds.has(recordId)),
  )
  if (!action && result?.actionId) action = steps.actionById.get(result.actionId)
  const stepId =
    result?.stepId
    ?? action?.stepId
    ?? steps.steps.find(
      (step) =>
        step.eventIds.some((eventId) => eventIds.has(eventId))
        || step.recordIds.some((recordId) => recordIds.has(recordId)),
    )?.stepId
  return {
    stepId,
    actionId: action?.actionId,
    resultId: result?.resultId,
    toolCallId: toolCallId ?? action?.toolCallId ?? result?.toolCallId,
    spanId: action?.spanId ?? result?.spanId,
  }
}

function associationFindings(
  lineage: BrowserArtifactLineage,
  canonical: ArtifactProjection | undefined,
  contract: ArtifactContract | undefined,
  match: ReturnType<typeof matchAction>,
): string[] {
  const findings: string[] = []
  const lineageDigest = normalizedDigest(lineage.sha256)
  const canonicalDigest = normalizedDigest(canonical?.digest)
  const contractDigest = normalizedDigest(contract?.sha256)
  if (
    lineageDigest
    && canonicalDigest
    && lineageDigest !== canonicalDigest
  ) findings.push("lineage/canonical sha256 mismatch")
  if (
    lineageDigest
    && contractDigest
    && lineageDigest !== contractDigest
  ) findings.push("lineage/artifact API sha256 mismatch")
  if (
    canonicalDigest
    && contractDigest
    && canonicalDigest !== contractDigest
  ) findings.push("canonical/artifact API sha256 mismatch")
  if (
    lineage.sizeBytes
    && canonical?.sizeBytes
    && lineage.sizeBytes !== canonical.sizeBytes
  ) findings.push("lineage/canonical size mismatch")
  if (
    lineage.sizeBytes
    && contract?.sizeBytes
    && lineage.sizeBytes !== contract.sizeBytes
  ) findings.push("lineage/artifact API size mismatch")
  if (!canonical) findings.push("canonical artifact projection missing")
  if (!match.stepId) findings.push("browser step association missing")
  if (!lineage.sourceEventIds.length && !canonical?.producerEventId) {
    findings.push("source event association missing")
  }
  if (lineage.quarantined && contract?.security.trust !== "quarantined") {
    findings.push("lineage quarantine is not reflected by artifact API")
  }
  if (
    contract
    && !contract.status.exists
    && contract.status.integrity === "verified"
  ) findings.push("artifact API reports verified content that does not exist")
  return findings
}

function screenshotIntegrity(
  association: BrowserArtifactAssociation,
): BrowserScreenshot["integrity"] {
  if (
    association.findings.some((finding) => finding.includes("sha256 mismatch"))
  ) return "mismatch"
  if (
    normalizedDigest(association.lineage.sha256)
    && (
      association.canonical?.digest
      || association.contract?.sha256
    )
  ) return "verified"
  return "unverified"
}

function screenshotFor(
  association: BrowserArtifactAssociation,
  currentStepId?: string,
): BrowserScreenshot {
  const metadata = association.lineage.metadata
  const canonical = association.canonical
  const contract = association.contract
  return Object.freeze({
    artifactId: association.artifactId,
    browserSessionId: association.browserSessionId,
    stepId: association.stepId,
    targetId: optionalString(
      firstDefined(metadata, "target_id", "targetId"),
      "$.screenshot.target_id",
      512,
    ),
    frameId: optionalString(
      firstDefined(metadata, "frame_id", "frameId"),
      "$.screenshot.frame_id",
      512,
    ),
    mediaType:
      contract?.mediaType
      ?? association.lineage.mediaType
      ?? canonical?.mediaType
      ?? "image/png",
    sha256:
      contract?.sha256
      ?? association.lineage.sha256
      ?? canonical?.digest
      ?? "",
    expectedSha256:
      association.lineage.sha256
      || canonical?.digest
      || undefined,
    sizeBytes:
      contract?.sizeBytes
      ?? association.lineage.sizeBytes
      ?? canonical?.sizeBytes
      ?? 0,
    width: numericMetadata(metadata, "width", "viewport_width", "image_width"),
    height: numericMetadata(metadata, "height", "viewport_height", "image_height"),
    sequence: association.lineage.sequence || canonical?.sequence || 0,
    sourceEventIds: association.sourceEventIds,
    sourceRecordIds: association.sourceRecordIds,
    quarantined:
      association.lineage.quarantined
      || contract?.security.trust === "quarantined",
    integrity: screenshotIntegrity(association),
    current: Boolean(currentStepId && association.stepId === currentStepId),
  })
}

function numericMetadata(
  metadata: Readonly<Record<string, unknown>>,
  ...keys: readonly string[]
): number | undefined {
  for (const key of keys) {
    const value = Number(metadata[key])
    if (Number.isSafeInteger(value) && value > 0 && value <= 100_000) {
      return value
    }
  }
  return undefined
}

function downloadState(
  association: BrowserArtifactAssociation,
): BrowserDownload["state"] {
  if (
    association.lineage.quarantined
    || association.contract?.security.trust === "quarantined"
  ) return "quarantined"
  const raw = String(
    firstDefined(
      association.lineage.metadata,
      "state",
      "status",
      "download_state",
    ) ?? "",
  ).toLowerCase()
  if (raw.includes("fail") || raw.includes("error")) return "failed"
  if (raw.includes("progress") || raw.includes("receiving")) return "in_progress"
  if (raw.includes("request") || raw.includes("start")) return "requested"
  return "completed"
}

function downloadFor(
  association: BrowserArtifactAssociation,
): BrowserDownload {
  const metadata = association.lineage.metadata
  const contract = association.contract
  const filename =
    optionalString(
      firstDefined(
        metadata,
        "filename",
        "file_name",
        "suggested_filename",
        "suggestedFilename",
      ),
      "$.download.filename",
      4_096,
    )
    ?? contract?.title
    ?? association.artifactId
  return Object.freeze({
    artifactId: association.artifactId,
    browserSessionId: association.browserSessionId,
    stepId: association.stepId,
    actionId: association.actionId,
    filename,
    mediaType:
      contract?.mediaType
      ?? association.lineage.mediaType
      ?? "application/octet-stream",
    sha256:
      contract?.sha256
      ?? association.lineage.sha256
      ?? association.canonical?.digest
      ?? "",
    sizeBytes:
      contract?.sizeBytes
      ?? association.lineage.sizeBytes
      ?? association.canonical?.sizeBytes
      ?? 0,
    quarantined:
      association.lineage.quarantined
      || contract?.security.trust === "quarantined",
    url: optionalString(
      firstDefined(metadata, "url", "download_url", "source_url"),
      "$.download.url",
      16 * 1024,
    ),
    suggestedFilename: optionalString(
      firstDefined(metadata, "suggested_filename", "suggestedFilename"),
      "$.download.suggested_filename",
      4_096,
    ),
    state: downloadState(association),
    permissionDecision: optionalString(
      firstDefined(
        metadata,
        "permission_decision",
        "permissionDecision",
        "permission_effect",
      ),
      "$.download.permission_decision",
      128,
    ),
    sourceEventIds: association.sourceEventIds,
    sourceRecordIds: association.sourceRecordIds,
    sequence: association.lineage.sequence || association.canonical?.sequence || 0,
  })
}

function patchStepArtifacts(
  steps: readonly BrowserStep[],
  screenshots: readonly BrowserScreenshot[],
  downloads: readonly BrowserDownload[],
): readonly BrowserStep[] {
  const screenshotsByStep = groupArtifactIds(
    screenshots,
    (item) => item.stepId,
  )
  const downloadsByStep = groupArtifactIds(
    downloads,
    (item) => item.stepId,
  )
  return Object.freeze(
    steps.map((step) =>
      Object.freeze({
        ...step,
        screenshotArtifactIds: Object.freeze([
          ...new Set([
            ...step.screenshotArtifactIds,
            ...(screenshotsByStep.get(step.stepId) ?? []),
          ]),
        ]),
        downloadArtifactIds: Object.freeze([
          ...new Set([
            ...step.downloadArtifactIds,
            ...(downloadsByStep.get(step.stepId) ?? []),
          ]),
        ]),
      }),
    ),
  )
}

function groupArtifactIds<T extends { artifactId: string }>(
  values: readonly T[],
  key: (value: T) => string | undefined,
): Map<string, string[]> {
  const output = new Map<string, string[]>()
  for (const value of values) {
    const selected = key(value)
    if (!selected) continue
    const ids = output.get(selected) ?? []
    if (!ids.includes(value.artifactId)) ids.push(value.artifactId)
    output.set(selected, ids)
  }
  return output
}

export function projectBrowserArtifacts(
  scope: BrowserScope,
  lineage: readonly BrowserArtifactLineage[],
  records: readonly BrowserRecord[],
  canonicalArtifacts: readonly ArtifactProjection[],
  contracts: readonly ArtifactContract[],
  steps: BrowserStepProjection,
): BrowserArtifactProjection {
  const canonicalById = new Map(
    canonicalArtifacts
      .filter((artifact) => artifact.taskId === scope.taskId)
      .map((artifact) => [artifact.id, artifact]),
  )
  const contractById = new Map(
    contracts.map((artifact) => [artifact.artifactId, artifact]),
  )
  const scopedLineage = lineage.filter(
    (item) =>
      item.scope.browserSessionId === scope.browserSessionId
      && item.scope.workerRequestId === scope.workerRequestId,
  )
  const associations: BrowserArtifactAssociation[] = []
  const findings: string[] = []
  for (const item of scopedLineage) {
    const canonical = canonicalById.get(item.artifactId)
    const contract = contractById.get(item.artifactId)
    const match = matchAction(item, steps, canonical)
    const sourceRecordIds = sourceRecordsFor(item, records)
    const itemFindings = associationFindings(
      item,
      canonical,
      contract,
      match,
    )
    const association = Object.freeze({
      artifactId: item.artifactId,
      role: artifactRole(item),
      browserSessionId: scope.browserSessionId,
      stepId: match.stepId,
      actionId: match.actionId,
      resultId: match.resultId,
      toolCallId: match.toolCallId,
      spanId: match.spanId,
      canonical,
      contract,
      lineage: item,
      sourceEventIds: Object.freeze([...eventSet(item, canonical)]),
      sourceRecordIds: Object.freeze([...sourceRecordIds]),
      findings: Object.freeze(itemFindings),
    } satisfies BrowserArtifactAssociation)
    associations.push(association)
    for (const finding of itemFindings) {
      findings.push(`browser_artifact:${item.artifactId}:${finding}`)
    }
  }
  const lastStepId = steps.steps.at(-1)?.stepId
  const screenshots = associations
    .filter((association) => association.role === "screenshot")
    .map((association) => screenshotFor(association, lastStepId))
    .sort(
      (left, right) =>
        left.sequence - right.sequence
        || left.artifactId.localeCompare(right.artifactId),
    )
  const downloads = associations
    .filter((association) => association.role === "download")
    .map(downloadFor)
    .sort(
      (left, right) =>
        left.sequence - right.sequence
        || left.artifactId.localeCompare(right.artifactId),
    )
  const matched = new Set(associations.map((item) => item.artifactId))
  const browserCanonical = canonicalArtifacts.filter(
    (artifact) =>
      artifact.taskId === scope.taskId
      && (
        artifact.workerId?.toLowerCase().includes("browser")
        || artifact.mediaType.startsWith("image/")
        || /browser|download|screenshot/i.test(artifact.title)
      ),
  )
  const unmatchedArtifactIds = browserCanonical
    .filter((artifact) => !matched.has(artifact.id))
    .map((artifact) => artifact.id)
  for (const artifactId of unmatchedArtifactIds) {
    findings.push(`browser_canonical_artifact_without_lineage:${artifactId}`)
  }
  return Object.freeze({
    associations: Object.freeze(associations),
    screenshots: Object.freeze(screenshots),
    downloads: Object.freeze(downloads),
    unmatchedArtifactIds: Object.freeze(unmatchedArtifactIds),
    findings: Object.freeze([...new Set(findings)]),
  })
}

export function browserArtifactRevisionKey(
  association: BrowserArtifactAssociation,
): string {
  return [
    association.artifactId,
    association.contract?.revision ?? association.canonical?.version ?? "unknown",
    normalizedDigest(
      association.contract?.sha256
      ?? association.lineage.sha256
      ?? association.canonical?.digest,
    ),
  ].join("@")
}

export function browserArtifactOpenable(
  association: BrowserArtifactAssociation,
): { allowed: boolean; reason?: string } {
  const contract = association.contract
  if (!contract) {
    return { allowed: false, reason: "Artifact API contract is unavailable." }
  }
  if (!contract.status.exists || !contract.status.isFile) {
    return { allowed: false, reason: "Artifact content is not an available file." }
  }
  if (
    contract.status.integrity === "digest_mismatch"
    || contract.status.integrity === "size_mismatch"
    || association.findings.some((finding) => finding.includes("mismatch"))
  ) {
    return { allowed: false, reason: "Artifact integrity did not verify." }
  }
  if (contract.security.label === "secret") {
    return { allowed: false, reason: "Secret artifact content cannot be opened." }
  }
  if (contract.security.trust === "quarantined") {
    return {
      allowed: contract.status.inlineSafe,
      reason: contract.status.inlineSafe
        ? "Quarantined content is limited to the safe inline viewer."
        : "Quarantined content is not inline safe.",
    }
  }
  return { allowed: true }
}

export function applyBrowserArtifactAssociations(
  steps: readonly BrowserStep[],
  projection: BrowserArtifactProjection,
): readonly BrowserStep[] {
  return patchStepArtifacts(
    steps,
    projection.screenshots,
    projection.downloads,
  )
}
