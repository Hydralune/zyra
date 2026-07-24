import type {
  ArtifactProjection,
  CausalEventProjection,
} from "../../../state/contracts.ts"
import {
  type BrowserAction,
  type BrowserActionResult,
  type BrowserCausalityFinding,
  type BrowserDownload,
  type BrowserScope,
  type BrowserScreenshot,
  type BrowserStep,
  type BrowserToolReceipt,
} from "../contracts.ts"
import type { BrowserArtifactAssociation } from "./artifacts.ts"
import type { BrowserRecord, BrowserSpanRecord } from "./reader.ts"

interface CausalIndexes {
  eventById: Map<string, CausalEventProjection>
  eventsByTool: Map<string, CausalEventProjection[]>
  eventsBySpan: Map<string, CausalEventProjection[]>
  eventsByCorrelation: Map<string, CausalEventProjection[]>
  eventsByMutation: Map<string, CausalEventProjection[]>
  eventsByArtifact: Map<string, CausalEventProjection[]>
  recordsById: Map<string, BrowserRecord>
  spansById: Map<string, BrowserSpanRecord>
  artifactsById: Map<string, ArtifactProjection>
}

function append<K, V>(map: Map<K, V[]>, key: K | undefined, value: V): void {
  if (key === undefined || key === null || key === "") return
  const values = map.get(key) ?? []
  values.push(value)
  map.set(key, values)
}

function buildIndexes(
  scope: BrowserScope,
  events: readonly CausalEventProjection[],
  records: readonly BrowserRecord[],
  spans: readonly BrowserSpanRecord[],
  artifacts: readonly ArtifactProjection[],
): CausalIndexes {
  const eventById = new Map<string, CausalEventProjection>()
  const eventsByTool = new Map<string, CausalEventProjection[]>()
  const eventsBySpan = new Map<string, CausalEventProjection[]>()
  const eventsByCorrelation = new Map<string, CausalEventProjection[]>()
  const eventsByMutation = new Map<string, CausalEventProjection[]>()
  const eventsByArtifact = new Map<string, CausalEventProjection[]>()
  for (const event of events) {
    if (event.taskId !== scope.taskId || event.runId !== scope.runId) continue
    eventById.set(event.eventId, event)
    append(eventsByTool, event.toolCallId, event)
    append(eventsBySpan, event.spanId, event)
    append(eventsBySpan, event.parentSpanId, event)
    append(eventsByCorrelation, event.correlationId, event)
    append(eventsByMutation, event.mutationId, event)
    for (const artifactId of event.artifactIds) {
      append(eventsByArtifact, artifactId, event)
    }
  }
  return {
    eventById,
    eventsByTool,
    eventsBySpan,
    eventsByCorrelation,
    eventsByMutation,
    eventsByArtifact,
    recordsById: new Map(records.map((record) => [record.recordId, record])),
    spansById: new Map(spans.map((span) => [span.spanId, span])),
    artifactsById: new Map(
      artifacts
        .filter((artifact) => artifact.taskId === scope.taskId)
        .map((artifact) => [artifact.id, artifact]),
    ),
  }
}

function finding(
  code: string,
  severity: BrowserCausalityFinding["severity"],
  message: string,
  refs: Partial<BrowserCausalityFinding> = {},
): BrowserCausalityFinding {
  return Object.freeze({
    code,
    severity,
    message,
    browserSessionId: refs.browserSessionId,
    stepId: refs.stepId,
    actionId: refs.actionId,
    resultId: refs.resultId,
    artifactId: refs.artifactId,
    eventIds: Object.freeze([...(refs.eventIds ?? [])]),
    recordIds: Object.freeze([...(refs.recordIds ?? [])]),
  })
}

function actionFindings(
  scope: BrowserScope,
  action: BrowserAction,
  indexes: CausalIndexes,
): BrowserCausalityFinding[] {
  const output: BrowserCausalityFinding[] = []
  const events = new Set<CausalEventProjection>()
  for (const eventId of action.sourceEventIds) {
    const event = indexes.eventById.get(eventId)
    if (event) events.add(event)
  }
  for (const event of indexes.eventsByTool.get(action.toolCallId ?? "") ?? []) {
    events.add(event)
  }
  for (const event of indexes.eventsBySpan.get(action.spanId ?? "") ?? []) {
    events.add(event)
  }
  for (
    const event of indexes.eventsByCorrelation.get(action.correlationId ?? "") ?? []
  ) events.add(event)
  if (events.size === 0) {
    output.push(
      finding(
        "browser_action_event_missing",
        "error",
        "Browser action has no canonical event correlation.",
        {
          browserSessionId: scope.browserSessionId,
          stepId: action.stepId,
          actionId: action.actionId,
          recordIds: action.sourceRecordIds,
        },
      ),
    )
  }
  if (action.toolCallId && ![...events].some(
    (event) => event.toolCallId === action.toolCallId,
  )) {
    output.push(
      finding(
        "browser_action_tool_call_mismatch",
        "error",
        "Browser action tool_call_id is absent from related canonical events.",
        {
          browserSessionId: scope.browserSessionId,
          stepId: action.stepId,
          actionId: action.actionId,
          eventIds: [...events].map((event) => event.eventId),
          recordIds: action.sourceRecordIds,
        },
      ),
    )
  }
  if (action.spanId && !indexes.spansById.has(action.spanId)) {
    output.push(
      finding(
        "browser_action_span_missing",
        "warning",
        "Browser action references a span not present in the admitted trace view.",
        {
          browserSessionId: scope.browserSessionId,
          stepId: action.stepId,
          actionId: action.actionId,
          eventIds: [...events].map((event) => event.eventId),
          recordIds: action.sourceRecordIds,
        },
      ),
    )
  }
  if (action.permissionDecision === "allow" && !action.permissionRequestId) {
    output.push(
      finding(
        "browser_action_permission_receipt_incomplete",
        "warning",
        "Allowed browser action has no permission request identity in its projection.",
        {
          browserSessionId: scope.browserSessionId,
          stepId: action.stepId,
          actionId: action.actionId,
          eventIds: [...events].map((event) => event.eventId),
          recordIds: action.sourceRecordIds,
        },
      ),
    )
  }
  return output
}

function resultFindings(
  scope: BrowserScope,
  result: BrowserActionResult,
  actionById: ReadonlyMap<string, BrowserAction>,
  indexes: CausalIndexes,
): BrowserCausalityFinding[] {
  const output: BrowserCausalityFinding[] = []
  const action = result.actionId ? actionById.get(result.actionId) : undefined
  if (!action) {
    output.push(
      finding(
        "browser_result_action_missing",
        "error",
        "Browser result cannot be associated with a browser action.",
        {
          browserSessionId: scope.browserSessionId,
          stepId: result.stepId,
          resultId: result.resultId,
          eventIds: result.sourceEventIds,
          recordIds: result.sourceRecordIds,
        },
      ),
    )
  } else {
    if (
      result.toolCallId
      && action.toolCallId
      && result.toolCallId !== action.toolCallId
    ) {
      output.push(
        finding(
          "browser_result_tool_call_mismatch",
          "error",
          "Browser result and action carry different tool_call_id values.",
          {
            browserSessionId: scope.browserSessionId,
            stepId: result.stepId,
            actionId: action.actionId,
            resultId: result.resultId,
            eventIds: [
              ...new Set([
                ...action.sourceEventIds,
                ...result.sourceEventIds,
              ]),
            ],
            recordIds: [
              ...new Set([
                ...action.sourceRecordIds,
                ...result.sourceRecordIds,
              ]),
            ],
          },
        ),
      )
    }
    if (
      result.spanId
      && action.spanId
      && result.spanId !== action.spanId
    ) {
      const actionSpan = indexes.spansById.get(action.spanId)
      const resultSpan = indexes.spansById.get(result.spanId)
      const parented =
        resultSpan?.parentSpanId === actionSpan?.spanId
        || actionSpan?.parentSpanId === resultSpan?.spanId
      if (!parented) {
        output.push(
          finding(
            "browser_result_span_mismatch",
            "warning",
            "Browser result span is neither the action span nor its parent/child.",
            {
              browserSessionId: scope.browserSessionId,
              stepId: result.stepId,
              actionId: action.actionId,
              resultId: result.resultId,
              eventIds: result.sourceEventIds,
              recordIds: result.sourceRecordIds,
            },
          ),
        )
      }
    }
  }
  for (const mutationId of result.mutationIds) {
    if (!(indexes.eventsByMutation.get(mutationId)?.length)) {
      output.push(
        finding(
          "browser_result_mutation_event_missing",
          "error",
          `Browser result mutation ${mutationId} has no canonical event.`,
          {
            browserSessionId: scope.browserSessionId,
            stepId: result.stepId,
            actionId: result.actionId,
            resultId: result.resultId,
            eventIds: result.sourceEventIds,
            recordIds: result.sourceRecordIds,
          },
        ),
      )
    }
  }
  return output
}

function artifactFindings(
  scope: BrowserScope,
  association: BrowserArtifactAssociation,
  indexes: CausalIndexes,
): BrowserCausalityFinding[] {
  const output: BrowserCausalityFinding[] = []
  const canonical = indexes.artifactsById.get(association.artifactId)
  const related = indexes.eventsByArtifact.get(association.artifactId) ?? []
  if (!canonical) {
    output.push(
      finding(
        "browser_artifact_canonical_missing",
        "error",
        "Browser artifact lineage has no canonical artifact projection.",
        {
          browserSessionId: scope.browserSessionId,
          stepId: association.stepId,
          actionId: association.actionId,
          resultId: association.resultId,
          artifactId: association.artifactId,
          eventIds: association.sourceEventIds,
          recordIds: association.sourceRecordIds,
        },
      ),
    )
  }
  if (related.length === 0 && !association.sourceEventIds.length) {
    output.push(
      finding(
        "browser_artifact_event_missing",
        "error",
        "Browser artifact has no producing canonical event.",
        {
          browserSessionId: scope.browserSessionId,
          stepId: association.stepId,
          actionId: association.actionId,
          resultId: association.resultId,
          artifactId: association.artifactId,
          recordIds: association.sourceRecordIds,
        },
      ),
    )
  }
  if (
    canonical?.producerToolCallId
    && association.toolCallId
    && canonical.producerToolCallId !== association.toolCallId
  ) {
    output.push(
      finding(
        "browser_artifact_tool_call_mismatch",
        "error",
        "Browser artifact producer tool_call_id differs from action/result linkage.",
        {
          browserSessionId: scope.browserSessionId,
          stepId: association.stepId,
          actionId: association.actionId,
          resultId: association.resultId,
          artifactId: association.artifactId,
          eventIds: [
            ...new Set([
              ...association.sourceEventIds,
              ...related.map((event) => event.eventId),
            ]),
          ],
          recordIds: association.sourceRecordIds,
        },
      ),
    )
  }
  for (const issue of association.findings) {
    output.push(
      finding(
        issue.includes("mismatch")
          ? "browser_artifact_integrity_mismatch"
          : "browser_artifact_lineage_incomplete",
        issue.includes("mismatch") ? "error" : "warning",
        issue,
        {
          browserSessionId: scope.browserSessionId,
          stepId: association.stepId,
          actionId: association.actionId,
          resultId: association.resultId,
          artifactId: association.artifactId,
          eventIds: association.sourceEventIds,
          recordIds: association.sourceRecordIds,
        },
      ),
    )
  }
  return output
}

function stepFindings(
  scope: BrowserScope,
  step: BrowserStep,
  actionById: ReadonlyMap<string, BrowserAction>,
  resultById: ReadonlyMap<string, BrowserActionResult>,
): BrowserCausalityFinding[] {
  const output: BrowserCausalityFinding[] = []
  const actions = step.actionIds
    .map((id) => actionById.get(id))
    .filter((value): value is BrowserAction => Boolean(value))
  const results = step.resultIds
    .map((id) => resultById.get(id))
    .filter((value): value is BrowserActionResult => Boolean(value))
  if (actions.length !== step.actionIds.length) {
    output.push(
      finding(
        "browser_step_action_reference_missing",
        "error",
        "Browser step references an action that is not admitted.",
        {
          browserSessionId: scope.browserSessionId,
          stepId: step.stepId,
          eventIds: step.eventIds,
          recordIds: step.recordIds,
        },
      ),
    )
  }
  if (results.length !== step.resultIds.length) {
    output.push(
      finding(
        "browser_step_result_reference_missing",
        "error",
        "Browser step references a result that is not admitted.",
        {
          browserSessionId: scope.browserSessionId,
          stepId: step.stepId,
          eventIds: step.eventIds,
          recordIds: step.recordIds,
        },
      ),
    )
  }
  if (!actions.length && results.length) {
    output.push(
      finding(
        "browser_step_result_only",
        "warning",
        "Browser step is partial because only its result side was observed.",
        {
          browserSessionId: scope.browserSessionId,
          stepId: step.stepId,
          eventIds: step.eventIds,
          recordIds: step.recordIds,
        },
      ),
    )
  }
  if (actions.length && !results.length && step.status !== "running") {
    output.push(
      finding(
        "browser_step_action_only",
        "warning",
        "Browser step has no associated result.",
        {
          browserSessionId: scope.browserSessionId,
          stepId: step.stepId,
          eventIds: step.eventIds,
          recordIds: step.recordIds,
        },
      ),
    )
  }
  const spans = new Set(actions.map((item) => item.spanId).filter(Boolean))
  const resultSpans = new Set(results.map((item) => item.spanId).filter(Boolean))
  if (spans.size && resultSpans.size && ![...spans].some((id) => resultSpans.has(id))) {
    output.push(
      finding(
        "browser_step_span_disconnected",
        "warning",
        "Browser step action and result do not share a directly correlated span.",
        {
          browserSessionId: scope.browserSessionId,
          stepId: step.stepId,
          eventIds: step.eventIds,
          recordIds: step.recordIds,
        },
      ),
    )
  }
  return output
}

function receiptFindings(
  scope: BrowserScope,
  receipt: BrowserToolReceipt,
): BrowserCausalityFinding[] {
  const output: BrowserCausalityFinding[] = []
  if (!receipt.sourceEventIds.length) {
    output.push(
      finding(
        "browser_tool_receipt_event_missing",
        "error",
        "Browser tool receipt has no canonical event evidence.",
        {
          browserSessionId: scope.browserSessionId,
          stepId: receipt.stepId,
          actionId: receipt.actionId,
          eventIds: receipt.sourceEventIds,
          recordIds: receipt.sourceRecordIds,
        },
      ),
    )
  }
  if (receipt.status === "completed" && receipt.errorCode) {
    output.push(
      finding(
        "browser_tool_receipt_status_conflict",
        "error",
        "Completed browser tool receipt also contains an error code.",
        {
          browserSessionId: scope.browserSessionId,
          stepId: receipt.stepId,
          actionId: receipt.actionId,
          eventIds: receipt.sourceEventIds,
          recordIds: receipt.sourceRecordIds,
        },
      ),
    )
  }
  return output
}

function dedupeFindings(
  findings: readonly BrowserCausalityFinding[],
): BrowserCausalityFinding[] {
  const output = new Map<string, BrowserCausalityFinding>()
  for (const item of findings) {
    const key = [
      item.code,
      item.stepId,
      item.actionId,
      item.resultId,
      item.artifactId,
      item.message,
    ].join("\u0000")
    const existing = output.get(key)
    if (!existing) {
      output.set(key, item)
      continue
    }
    output.set(
      key,
      Object.freeze({
        ...existing,
        eventIds: Object.freeze([
          ...new Set([...existing.eventIds, ...item.eventIds]),
        ]),
        recordIds: Object.freeze([
          ...new Set([...existing.recordIds, ...item.recordIds]),
        ]),
      }),
    )
  }
  const severity = { error: 0, warning: 1, info: 2 } as const
  return [...output.values()].sort(
    (left, right) =>
      severity[left.severity] - severity[right.severity]
      || left.code.localeCompare(right.code),
  )
}

export function auditBrowserCausality(input: {
  scope: BrowserScope
  events: readonly CausalEventProjection[]
  records: readonly BrowserRecord[]
  spans: readonly BrowserSpanRecord[]
  canonicalArtifacts: readonly ArtifactProjection[]
  steps: readonly BrowserStep[]
  actions: readonly BrowserAction[]
  results: readonly BrowserActionResult[]
  toolReceipts: readonly BrowserToolReceipt[]
  associations: readonly BrowserArtifactAssociation[]
  screenshots: readonly BrowserScreenshot[]
  downloads: readonly BrowserDownload[]
}): readonly BrowserCausalityFinding[] {
  const indexes = buildIndexes(
    input.scope,
    input.events,
    input.records,
    input.spans,
    input.canonicalArtifacts,
  )
  const actionById = new Map(input.actions.map((action) => [action.actionId, action]))
  const resultById = new Map(input.results.map((result) => [result.resultId, result]))
  const findings: BrowserCausalityFinding[] = []
  for (const action of input.actions) {
    findings.push(...actionFindings(input.scope, action, indexes))
  }
  for (const result of input.results) {
    findings.push(...resultFindings(input.scope, result, actionById, indexes))
  }
  for (const association of input.associations) {
    findings.push(...artifactFindings(input.scope, association, indexes))
  }
  for (const step of input.steps) {
    findings.push(...stepFindings(input.scope, step, actionById, resultById))
  }
  for (const receipt of input.toolReceipts) {
    findings.push(...receiptFindings(input.scope, receipt))
  }
  for (const screenshot of input.screenshots) {
    if (screenshot.integrity === "mismatch") {
      findings.push(
        finding(
          "browser_screenshot_integrity_mismatch",
          "error",
          "Screenshot digest does not agree across history and artifact custody.",
          {
            browserSessionId: input.scope.browserSessionId,
            stepId: screenshot.stepId,
            artifactId: screenshot.artifactId,
            eventIds: screenshot.sourceEventIds,
            recordIds: screenshot.sourceRecordIds,
          },
        ),
      )
    }
  }
  for (const download of input.downloads) {
    if (download.state === "completed" && !download.sourceEventIds.length) {
      findings.push(
        finding(
          "browser_download_event_missing",
          "error",
          "Completed download has no producing canonical event.",
          {
            browserSessionId: input.scope.browserSessionId,
            stepId: download.stepId,
            actionId: download.actionId,
            artifactId: download.artifactId,
            recordIds: download.sourceRecordIds,
          },
        ),
      )
    }
  }
  return Object.freeze(dedupeFindings(findings))
}

export function browserCausalityIsComplete(
  findings: readonly BrowserCausalityFinding[],
): boolean {
  return !findings.some((finding) => finding.severity === "error")
}
