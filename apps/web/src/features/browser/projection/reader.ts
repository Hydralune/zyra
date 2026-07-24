import {
  BrowserContractError,
  booleanValue,
  firstDefined,
  integerValue,
  isRecord,
  nestedRecord,
  optionalRecord,
  optionalString,
  parseBrowserScope,
  recordArray,
  safeObservedUrl,
  stableDigest,
  stringArray,
  type BrowserObservabilityEnvelope,
  type BrowserScope,
} from "../contracts.ts"

export interface BrowserRecord {
  recordId: string
  scope: BrowserScope
  kind: string
  sequence: number
  createdAt?: string
  contentDigest?: string
  previousDigest?: string
  payload: Readonly<Record<string, unknown>>
  raw: Readonly<Record<string, unknown>>
  sourceView: string
}

export interface BrowserSpanRecord {
  spanId: string
  scope: BrowserScope
  parentSpanId?: string
  toolCallId?: string
  actionId?: string
  correlationId?: string
  causationId?: string
  name: string
  status: string
  startSequence: number
  endSequence: number
  startedAt?: string
  completedAt?: string
  eventIds: readonly string[]
  recordIds: readonly string[]
  attributes: Readonly<Record<string, unknown>>
}

export interface BrowserArtifactLineage {
  artifactId: string
  role: string
  scope: BrowserScope
  sha256: string
  sizeBytes: number
  mediaType: string
  quarantined: boolean
  sourceEventIds: readonly string[]
  sourceRecordIds: readonly string[]
  parentArtifactIds: readonly string[]
  receiptId?: string
  metadata: Readonly<Record<string, unknown>>
  sequence: number
}

export interface BrowserReadResult {
  scopes: readonly BrowserScope[]
  records: readonly BrowserRecord[]
  spans: readonly BrowserSpanRecord[]
  artifacts: readonly BrowserArtifactLineage[]
  health: readonly Readonly<Record<string, unknown>>[]
  trajectories: readonly Readonly<Record<string, unknown>>[]
  commits: readonly Readonly<Record<string, unknown>>[]
  findings: readonly string[]
}

function normalizedKind(value: unknown): string {
  return String(value ?? "unknown")
    .trim()
    .toLowerCase()
    .replaceAll("-", "_")
    .replaceAll(".", "_")
}

function recordIdentity(
  source: Readonly<Record<string, unknown>>,
  scope: BrowserScope,
  sequence: number,
): string {
  const direct = optionalString(
    firstDefined(source, "record_id", "recordId", "id"),
    "$.record_id",
    512,
  )
  if (direct) return direct
  return `browser-record:${stableDigest({
    scope: scope.browserSessionId,
    worker: scope.workerRequestId,
    sequence,
    kind: firstDefined(source, "kind", "record_kind", "type"),
    payload: source.payload,
  })}`
}

function admittedPayload(
  source: Readonly<Record<string, unknown>>,
): Readonly<Record<string, unknown>> {
  const payload =
    optionalRecord(source.payload)
    ?? optionalRecord(source.data)
    ?? optionalRecord(source.body)
    ?? {}
  return Object.freeze({ ...payload })
}

function readHistoryRecord(
  value: Readonly<Record<string, unknown>>,
  scope: BrowserScope,
  sourceView: string,
  index: number,
): BrowserRecord {
  const sequence = integerValue(
    firstDefined(value, "sequence", "history_sequence", "ordinal")
      ?? index + 1,
    "$.record.sequence",
    { minimum: 0, maximum: Number.MAX_SAFE_INTEGER },
  )
  const recordId = recordIdentity(value, scope, sequence)
  const kind = normalizedKind(
    firstDefined(value, "kind", "record_kind", "event_type", "type"),
  )
  const payload = admittedPayload(value)
  return Object.freeze({
    recordId,
    scope,
    kind,
    sequence,
    createdAt: optionalString(
      firstDefined(value, "created_at", "createdAt", "timestamp"),
      "$.record.created_at",
      512,
    ),
    contentDigest: optionalString(
      firstDefined(value, "content_digest", "contentDigest", "digest"),
      "$.record.content_digest",
      512,
    ),
    previousDigest: optionalString(
      firstDefined(value, "previous_digest", "previousDigest", "parent_digest"),
      "$.record.previous_digest",
      512,
    ),
    payload,
    raw: Object.freeze({ ...value }),
    sourceView,
  })
}

function scopeFromProjection(
  projection: Readonly<Record<string, unknown>>,
): BrowserScope {
  return parseBrowserScope(
    projection.scope
      ?? nestedRecord(projection, "trajectory", "scope")
      ?? nestedRecord(projection, "projection", "scope"),
  )
}

function readSpan(
  value: Readonly<Record<string, unknown>>,
  scope: BrowserScope,
  index: number,
): BrowserSpanRecord {
  const attributes =
    optionalRecord(value.attributes)
    ?? optionalRecord(value.metadata)
    ?? optionalRecord(value.payload)
    ?? {}
  const startSequence = integerValue(
    firstDefined(
      value,
      "start_sequence",
      "startSequence",
      "sequence",
    ) ?? index,
    "$.span.start_sequence",
    { minimum: 0, maximum: Number.MAX_SAFE_INTEGER },
  )
  const endSequence = integerValue(
    firstDefined(value, "end_sequence", "endSequence") ?? startSequence,
    "$.span.end_sequence",
    { minimum: startSequence, maximum: Number.MAX_SAFE_INTEGER },
  )
  const spanId =
    optionalString(
      firstDefined(value, "span_id", "spanId", "id"),
      "$.span.span_id",
      512,
    )
    ?? `browser-span:${stableDigest({
      session: scope.browserSessionId,
      worker: scope.workerRequestId,
      startSequence,
      endSequence,
      index,
    })}`
  return Object.freeze({
    spanId,
    scope,
    parentSpanId: optionalString(
      firstDefined(value, "parent_span_id", "parentSpanId"),
      "$.span.parent_span_id",
      512,
    ),
    toolCallId: optionalString(
      firstDefined(value, "tool_call_id", "toolCallId"),
      "$.span.tool_call_id",
      512,
    ),
    actionId: optionalString(
      firstDefined(value, "action_id", "actionId"),
      "$.span.action_id",
      512,
    ),
    correlationId: optionalString(
      firstDefined(value, "correlation_id", "correlationId"),
      "$.span.correlation_id",
      512,
    ),
    causationId: optionalString(
      firstDefined(value, "causation_id", "causationId"),
      "$.span.causation_id",
      512,
    ),
    name:
      optionalString(
        firstDefined(value, "name", "operation", "action_name"),
        "$.span.name",
        1024,
      ) ?? "browser action",
    status:
      optionalString(
        firstDefined(value, "status", "phase"),
        "$.span.status",
        128,
      ) ?? "unknown",
    startSequence,
    endSequence,
    startedAt: optionalString(
      firstDefined(value, "started_at", "startedAt"),
      "$.span.started_at",
      512,
    ),
    completedAt: optionalString(
      firstDefined(value, "completed_at", "completedAt", "ended_at"),
      "$.span.completed_at",
      512,
    ),
    eventIds: stringArray(
      firstDefined(value, "event_ids", "eventIds", "source_event_ids"),
    ),
    recordIds: stringArray(
      firstDefined(value, "record_ids", "recordIds", "source_record_ids"),
    ),
    attributes: Object.freeze({ ...attributes }),
  })
}

function readArtifact(
  value: Readonly<Record<string, unknown>>,
  scope: BrowserScope,
  index: number,
): BrowserArtifactLineage {
  const metadata = optionalRecord(value.metadata) ?? {}
  const artifactId =
    optionalString(
      firstDefined(value, "artifact_id", "artifactId", "id"),
      "$.artifact.artifact_id",
      512,
    )
    ?? `browser-artifact:${stableDigest({
      scope: scope.browserSessionId,
      role: value.role,
      digest: value.sha256,
      index,
    })}`
  return Object.freeze({
    artifactId,
    role: normalizedKind(
      firstDefined(value, "role", "artifact_role", "kind"),
    ),
    scope,
    sha256:
      optionalString(
        firstDefined(value, "sha256", "digest", "content_digest"),
        "$.artifact.sha256",
        512,
      ) ?? "",
    sizeBytes: integerValue(
      firstDefined(value, "size_bytes", "sizeBytes", "size") ?? 0,
      "$.artifact.size_bytes",
      { minimum: 0, maximum: Number.MAX_SAFE_INTEGER },
    ),
    mediaType:
      optionalString(
        firstDefined(value, "media_type", "mediaType", "mime_type"),
        "$.artifact.media_type",
        512,
      ) ?? "application/octet-stream",
    quarantined: booleanValue(value.quarantined),
    sourceEventIds: stringArray(
      firstDefined(value, "source_event_ids", "event_ids", "sourceEventIds"),
    ),
    sourceRecordIds: stringArray(
      firstDefined(value, "source_record_ids", "record_ids", "sourceRecordIds"),
    ),
    parentArtifactIds: stringArray(
      firstDefined(
        value,
        "parent_artifact_ids",
        "parentArtifactIds",
        "parents",
      ),
    ),
    receiptId: optionalString(
      firstDefined(
        value,
        "lineage_receipt_id",
        "receipt_id",
        "receiptId",
      ),
      "$.artifact.receipt_id",
      512,
    ),
    metadata: Object.freeze({ ...metadata }),
    sequence: integerValue(
      firstDefined(value, "history_sequence", "sequence") ?? index,
      "$.artifact.sequence",
      { minimum: 0, maximum: Number.MAX_SAFE_INTEGER },
    ),
  })
}

function scopesFromSummary(
  envelope: BrowserObservabilityEnvelope,
  findings: string[],
): BrowserScope[] {
  const output: BrowserScope[] = []
  for (const [index, entry] of envelope.scopes.entries()) {
    const scopeValue = entry.scope ?? entry
    try {
      output.push(parseBrowserScope(scopeValue, `$.scopes[${index}].scope`))
    } catch (error) {
      findings.push(
        error instanceof Error
          ? `scope_rejected:${error.message}`
          : "scope_rejected:unknown",
      )
    }
  }
  return output
}

function uniqueScopes(scopes: readonly BrowserScope[]): BrowserScope[] {
  const byKey = new Map<string, BrowserScope>()
  for (const scope of scopes) {
    const key = [
      scope.taskId,
      scope.runId,
      scope.browserSessionId,
      scope.workerRequestId,
    ].join("\u0000")
    byKey.set(key, scope)
  }
  return [...byKey.values()].sort(
    (left, right) =>
      left.browserSessionId.localeCompare(right.browserSessionId)
      || left.workerRequestId.localeCompare(right.workerRequestId),
  )
}

function readProjectionRecords(
  envelope: BrowserObservabilityEnvelope,
  projection: Readonly<Record<string, unknown>>,
  scope: BrowserScope,
  findings: string[],
): BrowserRecord[] {
  const rawRecords =
    recordArray(projection.records, 5_000).length
      ? recordArray(projection.records, 5_000)
      : recordArray(nestedRecord(projection, "history")?.records, 5_000)
  const output: BrowserRecord[] = []
  for (const [index, record] of rawRecords.entries()) {
    try {
      output.push(readHistoryRecord(record, scope, envelope.view, index))
    } catch (error) {
      findings.push(
        `history_record_rejected:${scope.browserSessionId}:${index}:${
          error instanceof Error ? error.message : "unknown"
        }`,
      )
    }
  }
  return output
}

function readProjectionSpans(
  projection: Readonly<Record<string, unknown>>,
  scope: BrowserScope,
  findings: string[],
): BrowserSpanRecord[] {
  const raw =
    recordArray(projection.spans, 5_000).length
      ? recordArray(projection.spans, 5_000)
      : recordArray(nestedRecord(projection, "trace")?.spans, 5_000)
  const output: BrowserSpanRecord[] = []
  for (const [index, span] of raw.entries()) {
    try {
      output.push(readSpan(span, scope, index))
    } catch (error) {
      findings.push(
        `trace_span_rejected:${scope.browserSessionId}:${index}:${
          error instanceof Error ? error.message : "unknown"
        }`,
      )
    }
  }
  return output
}

function readProjectionArtifacts(
  projection: Readonly<Record<string, unknown>>,
  scope: BrowserScope,
  findings: string[],
): BrowserArtifactLineage[] {
  const raw =
    recordArray(projection.artifacts, 5_000).length
      ? recordArray(projection.artifacts, 5_000)
      : recordArray(
          nestedRecord(projection, "artifact_lineage")?.artifacts,
          5_000,
        )
  const output: BrowserArtifactLineage[] = []
  for (const [index, artifact] of raw.entries()) {
    try {
      output.push(readArtifact(artifact, scope, index))
    } catch (error) {
      findings.push(
        `artifact_lineage_rejected:${scope.browserSessionId}:${index}:${
          error instanceof Error ? error.message : "unknown"
        }`,
      )
    }
  }
  return output
}

function digestChainFindings(records: readonly BrowserRecord[]): string[] {
  const findings: string[] = []
  const byScope = new Map<string, BrowserRecord[]>()
  for (const record of records) {
    const key = `${record.scope.browserSessionId}:${record.scope.workerRequestId}`
    const values = byScope.get(key) ?? []
    values.push(record)
    byScope.set(key, values)
  }
  for (const [key, values] of byScope) {
    values.sort(
      (left, right) =>
        left.sequence - right.sequence
        || left.recordId.localeCompare(right.recordId),
    )
    let previous: BrowserRecord | undefined
    const seen = new Set<string>()
    for (const value of values) {
      if (seen.has(value.recordId)) {
        findings.push(`duplicate_history_record:${key}:${value.recordId}`)
        continue
      }
      seen.add(value.recordId)
      if (previous && value.sequence < previous.sequence) {
        findings.push(
          `history_sequence_regression:${key}:${previous.sequence}:${value.sequence}`,
        )
      }
      if (
        previous?.contentDigest
        && value.previousDigest
        && value.previousDigest !== previous.contentDigest
      ) {
        findings.push(
          `history_digest_gap:${key}:${previous.recordId}:${value.recordId}`,
        )
      }
      previous = value
    }
  }
  return findings
}

function dedupeRecords(records: readonly BrowserRecord[]): BrowserRecord[] {
  const selected = new Map<string, BrowserRecord>()
  for (const record of records) {
    const existing = selected.get(record.recordId)
    if (!existing) {
      selected.set(record.recordId, record)
      continue
    }
    const existingRichness = Object.keys(existing.payload).length
    const candidateRichness = Object.keys(record.payload).length
    if (
      candidateRichness > existingRichness
      || (
        candidateRichness === existingRichness
        && record.sourceView === "history"
        && existing.sourceView !== "history"
      )
    ) {
      selected.set(record.recordId, record)
    }
  }
  return [...selected.values()].sort(
    (left, right) =>
      left.sequence - right.sequence
      || left.recordId.localeCompare(right.recordId),
  )
}

function dedupeSpans(spans: readonly BrowserSpanRecord[]): BrowserSpanRecord[] {
  const selected = new Map<string, BrowserSpanRecord>()
  for (const span of spans) {
    const existing = selected.get(span.spanId)
    if (!existing || span.eventIds.length + span.recordIds.length >
      existing.eventIds.length + existing.recordIds.length) {
      selected.set(span.spanId, span)
    }
  }
  return [...selected.values()].sort(
    (left, right) =>
      left.startSequence - right.startSequence
      || left.spanId.localeCompare(right.spanId),
  )
}

function dedupeArtifacts(
  artifacts: readonly BrowserArtifactLineage[],
): BrowserArtifactLineage[] {
  const selected = new Map<string, BrowserArtifactLineage>()
  for (const artifact of artifacts) {
    const key = artifact.receiptId ?? artifact.artifactId
    const existing = selected.get(key)
    if (
      !existing
      || artifact.sourceEventIds.length + artifact.sourceRecordIds.length >
        existing.sourceEventIds.length + existing.sourceRecordIds.length
    ) {
      selected.set(key, artifact)
    }
  }
  return [...selected.values()].sort(
    (left, right) =>
      left.sequence - right.sequence
      || left.artifactId.localeCompare(right.artifactId),
  )
}

export function readBrowserObservability(
  envelopes: readonly BrowserObservabilityEnvelope[],
): BrowserReadResult {
  const findings: string[] = []
  const scopes: BrowserScope[] = []
  const records: BrowserRecord[] = []
  const spans: BrowserSpanRecord[] = []
  const artifacts: BrowserArtifactLineage[] = []
  const health: Readonly<Record<string, unknown>>[] = []
  const trajectories: Readonly<Record<string, unknown>>[] = []
  const commits: Readonly<Record<string, unknown>>[] = []

  const taskIds = new Set(envelopes.map((item) => item.taskId))
  if (taskIds.size > 1) {
    throw new BrowserContractError(
      "browser_observability_cross_task",
      "Browser observability envelopes cannot cross task ownership",
      "$.task_id",
      [...taskIds],
    )
  }

  for (const envelope of envelopes) {
    if (envelope.view === "summary") {
      scopes.push(...scopesFromSummary(envelope, findings))
      const summaryHealth = optionalRecord(envelope.raw.health)
      if (summaryHealth) health.push(Object.freeze({ ...summaryHealth }))
      continue
    }
    for (const [index, projection] of envelope.scopes.entries()) {
      let scope: BrowserScope
      try {
        scope = scopeFromProjection(projection)
      } catch (error) {
        findings.push(
          `scope_projection_rejected:${envelope.view}:${index}:${
            error instanceof Error ? error.message : "unknown"
          }`,
        )
        continue
      }
      scopes.push(scope)
      if (envelope.view === "history" || envelope.view === "replay") {
        records.push(
          ...readProjectionRecords(envelope, projection, scope, findings),
        )
      }
      if (envelope.view === "trace") {
        spans.push(...readProjectionSpans(projection, scope, findings))
      }
      if (
        envelope.view === "artifacts"
        || envelope.view === "downloads"
        || envelope.view === "screenshots"
      ) {
        artifacts.push(
          ...readProjectionArtifacts(projection, scope, findings),
        )
      }
      if (envelope.view === "health") {
        health.push(Object.freeze({ ...projection }))
        records.push(
          ...readProjectionRecords(envelope, projection, scope, findings),
        )
      }
      if (envelope.view === "trajectory") {
        trajectories.push(Object.freeze({ ...projection }))
        records.push(
          ...readProjectionRecords(envelope, projection, scope, findings),
        )
      }
      if (envelope.view === "commits") {
        commits.push(Object.freeze({ ...projection }))
      }
    }
  }

  const admittedRecords = dedupeRecords(records)
  findings.push(...digestChainFindings(admittedRecords))
  return Object.freeze({
    scopes: Object.freeze(uniqueScopes(scopes)),
    records: Object.freeze(admittedRecords),
    spans: Object.freeze(dedupeSpans(spans)),
    artifacts: Object.freeze(dedupeArtifacts(artifacts)),
    health: Object.freeze(health),
    trajectories: Object.freeze(trajectories),
    commits: Object.freeze(commits),
    findings: Object.freeze([...new Set(findings)].sort()),
  })
}

export function valuesDeep(
  value: unknown,
  keys: readonly string[],
  maximumDepth = 8,
  maximumValues = 1_000,
): readonly unknown[] {
  const wanted = new Set(keys.map((key) => key.toLowerCase()))
  const output: unknown[] = []
  const queue: { value: unknown; depth: number }[] = [{ value, depth: 0 }]
  const seen = new WeakSet<object>()
  while (queue.length && output.length < maximumValues) {
    const entry = queue.shift()!
    if (entry.depth > maximumDepth) continue
    if (Array.isArray(entry.value)) {
      for (const child of entry.value.slice(0, 2_000)) {
        queue.push({ value: child, depth: entry.depth + 1 })
      }
      continue
    }
    if (!isRecord(entry.value)) continue
    if (seen.has(entry.value)) continue
    seen.add(entry.value)
    for (const [key, child] of Object.entries(entry.value)) {
      if (wanted.has(key.toLowerCase())) output.push(child)
      if (child && typeof child === "object") {
        queue.push({ value: child, depth: entry.depth + 1 })
      }
    }
  }
  return Object.freeze(output)
}

export function firstDeep(
  value: unknown,
  keys: readonly string[],
  maximumDepth = 8,
): unknown {
  return valuesDeep(value, keys, maximumDepth, 1)[0]
}

export function deepString(
  value: unknown,
  keys: readonly string[],
  maximum = 256 * 1024,
): string | undefined {
  const candidate = firstDeep(value, keys)
  return optionalString(candidate, `$.${keys[0]}`, maximum)
}

export function deepStrings(
  value: unknown,
  keys: readonly string[],
  maximumValues = 1_000,
): readonly string[] {
  const output = new Set<string>()
  for (const candidate of valuesDeep(value, keys, 8, maximumValues)) {
    for (const item of stringArray(candidate, maximumValues)) output.add(item)
    if (!Array.isArray(candidate) && candidate !== undefined && candidate !== null) {
      const text = String(candidate).trim()
      if (text) output.add(text)
    }
  }
  return Object.freeze([...output])
}

export function observedUrl(value: unknown): string | undefined {
  const raw = firstDeep(value, [
    "url",
    "page_url",
    "target_url",
    "current_url",
  ])
  const result = safeObservedUrl(raw)
  return result || undefined
}
