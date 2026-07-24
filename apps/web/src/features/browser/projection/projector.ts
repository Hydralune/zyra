import type {
  RecoveryProjection,
} from "../../../state/contracts.ts"
import type { ArtifactContract } from "../../artifacts/contracts.ts"
import {
  BROWSER_VIEWER_SCHEMA,
  BrowserViewerPhase,
  normalizedIdentity,
  type BrowserCausalityFinding,
  type BrowserProjectionInputs,
  type BrowserSessionProjection,
  type BrowserViewerProjection,
} from "../contracts.ts"
import {
  applyBrowserArtifactAssociations,
  projectBrowserArtifacts,
} from "./artifacts.ts"
import {
  auditBrowserCausality,
  browserCausalityIsComplete,
} from "./causality.ts"
import { projectBrowserDom } from "./dom.ts"
import { projectBrowserHealth } from "./health.ts"
import {
  readBrowserObservability,
  type BrowserReadResult,
} from "./reader.ts"
import { projectBrowserSteps } from "./steps.ts"
import { projectBrowserTargets } from "./targets.ts"

export interface BrowserProjectorOptions {
  artifactContracts?: readonly ArtifactContract[]
  recoveries?: readonly RecoveryProjection[]
  selectedSessionId?: string
  selectedStepId?: string
  selectedActionId?: string
}

function translatedFinding(
  code: string,
  message: string,
  scope?: { browserSessionId?: string },
  severity: BrowserCausalityFinding["severity"] = "warning",
): BrowserCausalityFinding {
  return Object.freeze({
    code,
    severity,
    message,
    browserSessionId: scope?.browserSessionId,
    eventIds: Object.freeze([]),
    recordIds: Object.freeze([]),
  })
}

function readFindings(
  read: BrowserReadResult,
): BrowserCausalityFinding[] {
  return read.findings.map((message) =>
    translatedFinding(
      message.split(":")[0] || "browser_observability_finding",
      message,
      undefined,
      /mismatch|cross_task|invalid|regression/.test(message)
        ? "error"
        : "warning",
    ),
  )
}

function sessionProjection(
  input: BrowserProjectionInputs,
  read: BrowserReadResult,
  scope: BrowserReadResult["scopes"][number],
  options: BrowserProjectorOptions,
): BrowserSessionProjection {
  const records = read.records.filter(
    (record) =>
      record.scope.browserSessionId === scope.browserSessionId
      && record.scope.workerRequestId === scope.workerRequestId,
  )
  const spans = read.spans.filter(
    (span) =>
      span.scope.browserSessionId === scope.browserSessionId
      && span.scope.workerRequestId === scope.workerRequestId,
  )
  const lineage = read.artifacts.filter(
    (artifact) =>
      artifact.scope.browserSessionId === scope.browserSessionId
      && artifact.scope.workerRequestId === scope.workerRequestId,
  )
  const targets = projectBrowserTargets(scope, records)
  const domSummaries = projectBrowserDom(scope, records)
  const steps = projectBrowserSteps(
    scope,
    records,
    spans,
    input.events,
    domSummaries,
  )
  const artifacts = projectBrowserArtifacts(
    scope,
    lineage,
    records,
    input.artifacts,
    options.artifactContracts ?? [],
    steps,
  )
  const patchedSteps = applyBrowserArtifactAssociations(
    steps.steps,
    artifacts,
  )
  const health = projectBrowserHealth(
    scope,
    read,
    input.events,
    options.recoveries,
  )
  const causality = auditBrowserCausality({
    scope,
    events: input.events,
    records,
    spans,
    canonicalArtifacts: input.artifacts,
    steps: patchedSteps,
    actions: steps.actions,
    results: steps.results,
    toolReceipts: steps.toolReceipts,
    associations: artifacts.associations,
    screenshots: artifacts.screenshots,
    downloads: artifacts.downloads,
  })
  const findings = [
    ...targets.findings.map((message) =>
      translatedFinding(
        message.split(":")[0] || "browser_target_finding",
        message,
        scope,
      ),
    ),
    ...steps.findings.map((message) =>
      translatedFinding(
        message.split(":")[0] || "browser_step_finding",
        message,
        scope,
      ),
    ),
    ...artifacts.findings.map((message) =>
      translatedFinding(
        message.split(":")[0] || "browser_artifact_finding",
        message,
        scope,
        message.includes("mismatch") ? "error" : "warning",
      ),
    ),
    ...causality,
  ]
  const sequences = [
    ...records.map((record) => record.sequence),
    ...patchedSteps.flatMap((step) => [step.startSequence, step.endSequence]),
    ...artifacts.screenshots.map((artifact) => artifact.sequence),
    ...artifacts.downloads.map((artifact) => artifact.sequence),
  ]
  return Object.freeze({
    scope,
    url: targets.url ?? patchedSteps.at(-1)?.url,
    title: targets.title ?? patchedSteps.at(-1)?.title,
    activeTargetId: targets.activeTargetId,
    status: health.phase,
    targets: targets.targets,
    frames: targets.frames,
    steps: Object.freeze(patchedSteps),
    actions: steps.actions,
    results: steps.results,
    screenshots: artifacts.screenshots,
    domSummaries,
    downloads: artifacts.downloads,
    toolReceipts: steps.toolReceipts,
    health,
    findings: Object.freeze(dedupeFindings(findings)),
    firstSequence: sequences.length ? Math.min(...sequences) : 0,
    lastSequence: sequences.length ? Math.max(...sequences) : 0,
    headDigest:
      records
        .filter((record) => record.contentDigest)
        .sort((left, right) => right.sequence - left.sequence)[0]?.contentDigest,
  })
}

function dedupeFindings(
  findings: readonly BrowserCausalityFinding[],
): BrowserCausalityFinding[] {
  const output = new Map<string, BrowserCausalityFinding>()
  for (const finding of findings) {
    const key = [
      finding.code,
      finding.browserSessionId,
      finding.stepId,
      finding.actionId,
      finding.resultId,
      finding.artifactId,
      finding.message,
    ].join("\u0000")
    const existing = output.get(key)
    if (!existing) {
      output.set(key, finding)
      continue
    }
    output.set(
      key,
      Object.freeze({
        ...existing,
        severity:
          existing.severity === "error" || finding.severity === "error"
            ? "error"
            : existing.severity === "warning"
              || finding.severity === "warning"
              ? "warning"
              : "info",
        eventIds: Object.freeze([
          ...new Set([...existing.eventIds, ...finding.eventIds]),
        ]),
        recordIds: Object.freeze([
          ...new Set([...existing.recordIds, ...finding.recordIds]),
        ]),
      }),
    )
  }
  const severity = { error: 0, warning: 1, info: 2 } as const
  return [...output.values()].sort(
    (left, right) =>
      severity[left.severity] - severity[right.severity]
      || left.code.localeCompare(right.code)
      || (left.stepId ?? "").localeCompare(right.stepId ?? ""),
  )
}

function sessionIdentity(
  session: BrowserSessionProjection,
): string {
  return [
    session.scope.browserSessionId,
    session.scope.workerRequestId,
  ].join("\u0000")
}

function mergeSessionAttempts(
  sessions: readonly BrowserSessionProjection[],
): BrowserSessionProjection[] {
  const selected = new Map<string, BrowserSessionProjection>()
  for (const session of sessions) {
    const key = sessionIdentity(session)
    const existing = selected.get(key)
    if (!existing || session.lastSequence >= existing.lastSequence) {
      selected.set(key, session)
    }
  }
  return [...selected.values()].sort(
    (left, right) =>
      left.firstSequence - right.firstSequence
      || left.scope.browserSessionId.localeCompare(right.scope.browserSessionId)
      || left.scope.workerRequestId.localeCompare(right.scope.workerRequestId),
  )
}

function activeSession(
  sessions: readonly BrowserSessionProjection[],
): BrowserSessionProjection | undefined {
  return [...sessions].sort(
    (left, right) =>
      Number(right.status !== "stopped") - Number(left.status !== "stopped")
      || Number(right.status === "healthy") - Number(left.status === "healthy")
      || right.lastSequence - left.lastSequence
      || right.scope.browserSessionId.localeCompare(left.scope.browserSessionId),
  )[0]
}

function selectionExists(
  sessions: readonly BrowserSessionProjection[],
  options: BrowserProjectorOptions,
): {
  selectedSessionId?: string
  selectedStepId?: string
  selectedActionId?: string
} {
  const selectedSession = options.selectedSessionId
    ? sessions.find(
        (session) =>
          session.scope.browserSessionId === options.selectedSessionId,
      )
    : undefined
  const session = selectedSession ?? activeSession(sessions)
  const selectedStep = options.selectedStepId
    ? session?.steps.find((step) => step.stepId === options.selectedStepId)
    : undefined
  const step = selectedStep ?? session?.steps.at(-1)
  const selectedAction = options.selectedActionId
    ? session?.actions.find(
        (action) => action.actionId === options.selectedActionId,
      )
    : undefined
  const action =
    selectedAction
    ?? (step
      ? [...session?.actions ?? []]
        .filter((candidate) => candidate.stepId === step.stepId)
        .at(-1)
      : undefined)
  return {
    selectedSessionId: session?.scope.browserSessionId,
    selectedStepId: step?.stepId,
    selectedActionId: action?.actionId,
  }
}

export function projectBrowserViewer(
  input: BrowserProjectionInputs,
  options: BrowserProjectorOptions = {},
): BrowserViewerProjection {
  const taskId = normalizedIdentity(input.taskId, "task_id")
  const runId = normalizedIdentity(input.runId, "run_id")
  if (input.envelopes.some((envelope) => envelope.taskId !== taskId)) {
    throw new TypeError("Browser viewer cannot admit an envelope from another task.")
  }
  if (input.events.some((event) => event.taskId !== taskId)) {
    throw new TypeError("Browser viewer canonical events crossed task ownership.")
  }
  if (input.artifacts.some((artifact) => artifact.taskId !== taskId)) {
    throw new TypeError("Browser viewer canonical artifacts crossed task ownership.")
  }
  const read = readBrowserObservability(input.envelopes)
  const sessions = mergeSessionAttempts(
    read.scopes
      .filter((scope) => scope.taskId === taskId && scope.runId === runId)
      .map((scope) => sessionProjection(input, read, scope, options)),
  )
  const active = activeSession(sessions)
  const selection = selectionExists(sessions, options)
  const findings = dedupeFindings([
    ...readFindings(read),
    ...sessions.flatMap((session) => session.findings),
  ])
  const partial =
    sessions.some((session) =>
      session.steps.some((step) => step.partial),
    )
    || !browserCausalityIsComplete(findings)
  const phase =
    sessions.length === 0
      ? input.envelopes.length
        ? BrowserViewerPhase.PARTIAL
        : BrowserViewerPhase.IDLE
      : sessions.every((session) => session.status === "stopped")
        ? BrowserViewerPhase.READY
        : partial
          ? BrowserViewerPhase.PARTIAL
          : BrowserViewerPhase.READY
  return Object.freeze({
    schema: BROWSER_VIEWER_SCHEMA,
    taskId,
    runId,
    phase,
    sessions: Object.freeze(sessions),
    activeSessionId: active?.scope.browserSessionId,
    ...selection,
    totalSteps: sessions.reduce(
      (total, session) => total + session.steps.length,
      0,
    ),
    totalActions: sessions.reduce(
      (total, session) => total + session.actions.length,
      0,
    ),
    totalDownloads: sessions.reduce(
      (total, session) => total + session.downloads.length,
      0,
    ),
    totalScreenshots: sessions.reduce(
      (total, session) => total + session.screenshots.length,
      0,
    ),
    projectionRevision: input.projectionRevision,
    refreshedAt: input.refreshedAt ?? Date.now(),
    partial,
    findings: Object.freeze(findings),
  })
}

export function selectedBrowserSession(
  projection: BrowserViewerProjection,
): BrowserSessionProjection | undefined {
  return projection.sessions.find(
    (session) =>
      session.scope.browserSessionId === projection.selectedSessionId,
  )
}

export function browserProjectionChanged(
  left: BrowserViewerProjection,
  right: BrowserViewerProjection,
): boolean {
  if (
    left.taskId !== right.taskId
    || left.runId !== right.runId
    || left.phase !== right.phase
    || left.projectionRevision !== right.projectionRevision
    || left.selectedSessionId !== right.selectedSessionId
    || left.selectedStepId !== right.selectedStepId
    || left.selectedActionId !== right.selectedActionId
    || left.sessions.length !== right.sessions.length
    || left.findings.length !== right.findings.length
  ) return true
  for (let index = 0; index < left.sessions.length; index += 1) {
    const before = left.sessions[index]!
    const after = right.sessions[index]!
    if (
      sessionIdentity(before) !== sessionIdentity(after)
      || before.lastSequence !== after.lastSequence
      || before.status !== after.status
      || before.steps.length !== after.steps.length
      || before.screenshots.length !== after.screenshots.length
      || before.downloads.length !== after.downloads.length
    ) return true
  }
  return false
}
