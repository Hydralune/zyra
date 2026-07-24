export const ARTIFACT_CONTRACT = "zyra.artifact.v2"
export const ARTIFACT_CATALOG_CONTRACT = "zyra.artifact-catalog.v2"
export const ARTIFACT_READ_CONTRACT = "zyra.artifact-read.v2"
export const ARTIFACT_RECEIPT_CONTRACT = "zyra.artifact-read-receipt.v1"

export type ArtifactContentFamily =
  | "text"
  | "markdown"
  | "json"
  | "image"
  | "audio"
  | "video"
  | "binary"
  | "document"
  | "archive"
  | "html"
  | "svg"
  | "executable"

export type ArtifactSecurityLabel =
  | "public"
  | "internal"
  | "confidential"
  | "secret"

export type ArtifactTrustDisposition =
  | "trusted"
  | "untrusted"
  | "quarantined"

export type ArtifactDownloadPolicy = "allow" | "confirm" | "deny"

export type ArtifactIntegrityState =
  | "verified"
  | "legacy_incomplete"
  | "unverified"
  | "missing"
  | "path_refused"
  | "size_mismatch"
  | "digest_mismatch"
  | "revision_mismatch"

export type ArtifactReadPurpose =
  | "preview"
  | "search"
  | "media"
  | "download"
  | "metadata"

export interface ArtifactProducerContract {
  nodeId?: string
  spanId?: string
  toolCallId?: string
  workerId?: string
}

export interface ArtifactSecurityContract {
  label: ArtifactSecurityLabel
  trust: ArtifactTrustDisposition
  downloadPolicy: ArtifactDownloadPolicy
}

export interface ArtifactRetentionContract {
  policy: "ephemeral" | "task" | "run" | "project" | "submission"
  expiresAt?: string
}

export interface ArtifactStatusContract {
  exists: boolean
  isFile: boolean
  integrity: ArtifactIntegrityState
  inlineSafe: boolean
  executableRisk: boolean
  legacyMetadata: boolean
  error?: string
}

export interface ArtifactLinksContract {
  metadata: string
  content: string
  download: string
}

export interface ArtifactContract {
  schema: typeof ARTIFACT_CONTRACT
  artifactId: string
  kind: string
  title: string
  createdAt: string
  immutable: true
  revision: string
  sha256: string
  sizeBytes: number
  mediaType: string
  contentFamily: ArtifactContentFamily
  encoding?: string
  byteOrderMark?: string
  lineEndings: readonly string[]
  producer: ArtifactProducerContract
  security: ArtifactSecurityContract
  retention: ArtifactRetentionContract
  status: ArtifactStatusContract
  links: ArtifactLinksContract
}

export interface ArtifactCatalogFilters {
  nodeIds: readonly string[]
  workerIds: readonly string[]
  mediaTypes: readonly string[]
  contentFamilies: readonly string[]
  revisions: readonly string[]
  createdAfter?: string
  createdBefore?: string
  includeDeleted: boolean
}

export interface ArtifactCatalogPage {
  schema: typeof ARTIFACT_CATALOG_CONTRACT
  taskId: string
  stateOwner: string
  artifactRootDisclosed: false
  total: number
  returned: number
  cursor?: string
  filters: ArtifactCatalogFilters
  artifacts: readonly ArtifactContract[]
}

export interface ArtifactContentPolicy {
  securityLabel: ArtifactSecurityLabel
  trustDisposition: ArtifactTrustDisposition
  downloadPolicy: ArtifactDownloadPolicy
  allowInline: boolean
  allowDownload: boolean
  quarantine: boolean
  refusalCode?: string
  reasons: readonly string[]
}

export interface ArtifactReadRange {
  offset: number
  length: number
  requestedLength: number
  endExclusive: number
  totalBytes: number
  complete: boolean
}

export interface ArtifactRedactionFinding {
  kind: string
  start: number
  end: number
  digest: string
}

export interface ArtifactPromptFinding {
  kind: string
  start: number
  end: number
  digest: string
}

export interface ArtifactContentEnvelope {
  text?: string
  base64?: string
  encoding?: string
  decodeStatus: string
  serverRedacted: boolean
  redactions: readonly ArtifactRedactionFinding[]
  promptFindings: readonly ArtifactPromptFinding[]
  quarantined: boolean
}

export interface ArtifactReadReceipt {
  schema: typeof ARTIFACT_RECEIPT_CONTRACT
  sequence: number
  occurredAt: string
  taskId: string
  artifactId: string
  revision: string
  sha256?: string
  purpose: ArtifactReadPurpose
  decision: "allow" | "deny"
  reason?: string
  range?: ArtifactReadRange
  transformations: readonly string[]
  elapsedUs?: number
  receiptDigest: string
}

export interface ArtifactReadResponse {
  schema: typeof ARTIFACT_READ_CONTRACT
  taskId: string
  artifact: ArtifactContract
  policy: ArtifactContentPolicy
  range?: ArtifactReadRange
  content?: ArtifactContentEnvelope
  receipt: ArtifactReadReceipt
}

export interface ArtifactCatalogRequest {
  taskId: string
  cursor?: string
  limit?: number
  nodeIds?: readonly string[]
  workerIds?: readonly string[]
  mediaTypes?: readonly string[]
  contentFamilies?: readonly ArtifactContentFamily[]
  revisions?: readonly string[]
  createdAfter?: string
  createdBefore?: string
  includeDeleted?: boolean
  signal?: AbortSignal
  timeoutMs?: number
}

export interface ArtifactReadRequest {
  taskId: string
  artifactId: string
  revision?: string
  offset?: number
  length?: number
  purpose?: ArtifactReadPurpose
  signal?: AbortSignal
  timeoutMs?: number
}

export interface ArtifactWireError {
  path: string
  code: string
  message: string
  value?: unknown
}

export class ArtifactContractError extends TypeError {
  readonly errors: readonly ArtifactWireError[]

  constructor(message: string, errors: readonly ArtifactWireError[]) {
    super(message)
    this.name = "ArtifactContractError"
    this.errors = Object.freeze([...errors])
  }
}

const contentFamilies = new Set<ArtifactContentFamily>([
  "text",
  "markdown",
  "json",
  "image",
  "audio",
  "video",
  "binary",
  "document",
  "archive",
  "html",
  "svg",
  "executable",
])

const securityLabels = new Set<ArtifactSecurityLabel>([
  "public",
  "internal",
  "confidential",
  "secret",
])

const trustDispositions = new Set<ArtifactTrustDisposition>([
  "trusted",
  "untrusted",
  "quarantined",
])

const downloadPolicies = new Set<ArtifactDownloadPolicy>([
  "allow",
  "confirm",
  "deny",
])

const integrityStates = new Set<ArtifactIntegrityState>([
  "verified",
  "legacy_incomplete",
  "unverified",
  "missing",
  "path_refused",
  "size_mismatch",
  "digest_mismatch",
  "revision_mismatch",
])

const readPurposes = new Set<ArtifactReadPurpose>([
  "preview",
  "search",
  "media",
  "download",
  "metadata",
])

export function parseArtifactContract(value: unknown): ArtifactContract {
  const errors: ArtifactWireError[] = []
  const source = record(value, "$", errors)
  const schema = stringValue(source.schema, "$.schema", errors)
  if (schema !== ARTIFACT_CONTRACT) {
    errors.push({
      path: "$.schema",
      code: "schema_mismatch",
      message: `Expected ${ARTIFACT_CONTRACT}.`,
      value: schema,
    })
  }
  const statusSource = record(source.status, "$.status", errors)
  const securitySource = record(source.security, "$.security", errors)
  const producerSource = record(source.producer, "$.producer", errors)
  const retentionSource = record(source.retention, "$.retention", errors)
  const linksSource = record(source.links, "$.links", errors)
  const artifactId = identity(source.artifact_id, "$.artifact_id", errors)
  const revision = revisionValue(source.revision, "$.revision", errors)
  const sha256 = sha256Value(source.sha256, "$.sha256", errors)
  const contentFamily = enumValue(
    source.content_family,
    contentFamilies,
    "binary",
    "$.content_family",
    errors,
  )
  const securityLabel = enumValue(
    securitySource.label,
    securityLabels,
    "internal",
    "$.security.label",
    errors,
  )
  const trustDisposition = enumValue(
    securitySource.trust,
    trustDispositions,
    "untrusted",
    "$.security.trust",
    errors,
  )
  const downloadPolicy = enumValue(
    securitySource.download_policy,
    downloadPolicies,
    "deny",
    "$.security.download_policy",
    errors,
  )
  const integrity = enumValue(
    statusSource.integrity,
    integrityStates,
    "unverified",
    "$.status.integrity",
    errors,
  )
  const immutable = booleanValue(source.immutable, "$.immutable", errors)
  if (!immutable) {
    errors.push({
      path: "$.immutable",
      code: "mutable_artifact_refused",
      message: "Artifact contract must be immutable.",
      value: source.immutable,
    })
  }
  const sizeBytes = nonNegativeInteger(
    source.size_bytes,
    "$.size_bytes",
    errors,
  )
  const result: ArtifactContract = {
    schema: ARTIFACT_CONTRACT,
    artifactId,
    kind: boundedString(source.kind, "$.kind", errors, 128),
    title: boundedString(source.title, "$.title", errors, 4096),
    createdAt: timestamp(source.created_at, "$.created_at", errors),
    immutable: true,
    revision,
    sha256,
    sizeBytes,
    mediaType: mediaType(source.media_type, "$.media_type", errors),
    contentFamily,
    encoding: optionalBoundedString(source.encoding, "$.encoding", errors, 64),
    byteOrderMark: optionalBoundedString(
      source.byte_order_mark,
      "$.byte_order_mark",
      errors,
      32,
    ),
    lineEndings: stringArray(
      source.line_endings,
      "$.line_endings",
      errors,
      8,
      32,
    ),
    producer: {
      nodeId: optionalIdentity(producerSource.node_id, "$.producer.node_id", errors),
      spanId: optionalIdentity(producerSource.span_id, "$.producer.span_id", errors),
      toolCallId: optionalIdentity(
        producerSource.tool_call_id,
        "$.producer.tool_call_id",
        errors,
      ),
      workerId: optionalIdentity(
        producerSource.worker_id,
        "$.producer.worker_id",
        errors,
      ),
    },
    security: {
      label: securityLabel,
      trust: trustDisposition,
      downloadPolicy,
    },
    retention: {
      policy: retentionValue(retentionSource.policy, "$.retention.policy", errors),
      expiresAt: optionalTimestamp(
        retentionSource.expires_at,
        "$.retention.expires_at",
        errors,
      ),
    },
    status: {
      exists: booleanValue(statusSource.exists, "$.status.exists", errors),
      isFile: booleanValue(statusSource.is_file, "$.status.is_file", errors),
      integrity,
      inlineSafe: booleanValue(
        statusSource.inline_safe,
        "$.status.inline_safe",
        errors,
      ),
      executableRisk: booleanValue(
        statusSource.executable_risk,
        "$.status.executable_risk",
        errors,
      ),
      legacyMetadata: booleanValue(
        statusSource.legacy_metadata,
        "$.status.legacy_metadata",
        errors,
      ),
      error: optionalBoundedString(statusSource.error, "$.status.error", errors, 4096),
    },
    links: {
      metadata: safeRelativePath(
        linksSource.metadata,
        "$.links.metadata",
        errors,
      ),
      content: safeRelativePath(linksSource.content, "$.links.content", errors),
      download: safeRelativePath(
        linksSource.download,
        "$.links.download",
        errors,
      ),
    },
  }
  if (result.status.integrity === "verified" && result.revision !== `sha256:${result.sha256}`) {
    errors.push({
      path: "$.revision",
      code: "revision_digest_mismatch",
      message: "Verified artifact revision must be derived from its SHA-256.",
      value: result.revision,
    })
  }
  if (result.security.label === "secret" && result.security.downloadPolicy !== "deny") {
    errors.push({
      path: "$.security.download_policy",
      code: "secret_download_policy",
      message: "Secret artifacts must deny download.",
      value: result.security.downloadPolicy,
    })
  }
  throwIfErrors("Artifact contract is invalid.", errors)
  return freezeArtifact(result)
}

export function parseArtifactCatalogPage(value: unknown): ArtifactCatalogPage {
  const errors: ArtifactWireError[] = []
  const source = record(value, "$", errors)
  const schema = stringValue(source.schema, "$.schema", errors)
  if (schema !== ARTIFACT_CATALOG_CONTRACT) {
    errors.push({
      path: "$.schema",
      code: "schema_mismatch",
      message: `Expected ${ARTIFACT_CATALOG_CONTRACT}.`,
      value: schema,
    })
  }
  const rawArtifacts = arrayValue(source.artifacts, "$.artifacts", errors, 500)
  const artifacts: ArtifactContract[] = []
  rawArtifacts.forEach((candidate, index) => {
    try {
      artifacts.push(parseArtifactContract(candidate))
    } catch (error) {
      if (error instanceof ArtifactContractError) {
        errors.push(
          ...error.errors.map((entry) => ({
            ...entry,
            path: `$.artifacts[${index}]${entry.path.slice(1)}`,
          })),
        )
      } else {
        errors.push({
          path: `$.artifacts[${index}]`,
          code: "invalid_artifact",
          message: error instanceof Error ? error.message : String(error),
        })
      }
    }
  })
  const filterSource = record(source.filters, "$.filters", errors)
  const artifactRootDisclosed = booleanValue(
    source.artifact_root_disclosed,
    "$.artifact_root_disclosed",
    errors,
  )
  if (artifactRootDisclosed) {
    errors.push({
      path: "$.artifact_root_disclosed",
      code: "artifact_root_disclosed",
      message: "Artifact catalog must not disclose its custody root.",
      value: artifactRootDisclosed,
    })
  }
  const total = nonNegativeInteger(source.total, "$.total", errors)
  const returned = nonNegativeInteger(source.returned, "$.returned", errors)
  if (returned !== artifacts.length) {
    errors.push({
      path: "$.returned",
      code: "returned_count_mismatch",
      message: "Catalog returned count does not match the artifact array.",
      value: returned,
    })
  }
  if (returned > total) {
    errors.push({
      path: "$.returned",
      code: "returned_exceeds_total",
      message: "Catalog returned count exceeds total.",
      value: returned,
    })
  }
  const seen = new Set<string>()
  for (const artifact of artifacts) {
    const key = `${artifact.artifactId}@${artifact.revision}`
    if (seen.has(key)) {
      errors.push({
        path: "$.artifacts",
        code: "duplicate_revision",
        message: `Duplicate artifact revision ${key}.`,
      })
    }
    seen.add(key)
  }
  const taskId = identity(source.task_id, "$.task_id", errors)
  const stateOwner = boundedString(source.state_owner, "$.state_owner", errors, 512)
  const cursor = optionalCursor(source.cursor, "$.cursor", errors)
  const filters = Object.freeze({
    nodeIds: Object.freeze(
      stringArray(filterSource.node_ids, "$.filters.node_ids", errors, 128, 512),
    ),
    workerIds: Object.freeze(
      stringArray(filterSource.worker_ids, "$.filters.worker_ids", errors, 128, 512),
    ),
    mediaTypes: Object.freeze(
      stringArray(filterSource.media_types, "$.filters.media_types", errors, 128, 256),
    ),
    contentFamilies: Object.freeze(
      stringArray(
        filterSource.content_families,
        "$.filters.content_families",
        errors,
        128,
        64,
      ),
    ),
    revisions: Object.freeze(
      stringArray(filterSource.revisions, "$.filters.revisions", errors, 128, 128),
    ),
    createdAfter: optionalTimestamp(
      filterSource.created_after,
      "$.filters.created_after",
      errors,
    ),
    createdBefore: optionalTimestamp(
      filterSource.created_before,
      "$.filters.created_before",
      errors,
    ),
    includeDeleted: booleanValue(
      filterSource.include_deleted,
      "$.filters.include_deleted",
      errors,
    ),
  })
  throwIfErrors("Artifact catalog contract is invalid.", errors)
  return Object.freeze({
    schema: ARTIFACT_CATALOG_CONTRACT,
    taskId,
    stateOwner,
    artifactRootDisclosed: false,
    total,
    returned,
    cursor,
    filters,
    artifacts: Object.freeze(artifacts),
  })
}

export function parseArtifactReadResponse(value: unknown): ArtifactReadResponse {
  const errors: ArtifactWireError[] = []
  const source = record(value, "$", errors)
  if (stringValue(source.schema, "$.schema", errors) !== ARTIFACT_READ_CONTRACT) {
    errors.push({
      path: "$.schema",
      code: "schema_mismatch",
      message: `Expected ${ARTIFACT_READ_CONTRACT}.`,
      value: source.schema,
    })
  }
  let artifact: ArtifactContract | undefined
  try {
    artifact = parseArtifactContract(source.artifact)
  } catch (error) {
    if (error instanceof ArtifactContractError) {
      errors.push(
        ...error.errors.map((entry) => ({
          ...entry,
          path: `$.artifact${entry.path.slice(1)}`,
        })),
      )
    }
  }
  const policy = parsePolicy(source.policy, "$.policy", errors)
  const range = source.range === undefined || source.range === null
    ? undefined
    : parseRange(source.range, "$.range", errors)
  const content = source.content === undefined || source.content === null
    ? undefined
    : parseContent(source.content, "$.content", errors)
  const receipt = parseReceipt(source.receipt, "$.receipt", errors)
  const taskId = identity(source.task_id, "$.task_id", errors)
  if (receipt.taskId && taskId && receipt.taskId !== taskId) {
    errors.push({
      path: "$.receipt.task_id",
      code: "receipt_task_mismatch",
      message: "Artifact receipt task identity does not match the response.",
      value: receipt.taskId,
    })
  }
  if (artifact && receipt.artifactId && receipt.artifactId !== artifact.artifactId) {
    errors.push({
      path: "$.receipt.artifact_id",
      code: "receipt_artifact_mismatch",
      message: "Artifact receipt identity does not match content.",
      value: receipt.artifactId,
    })
  }
  if (artifact && receipt.revision && receipt.revision !== artifact.revision) {
    errors.push({
      path: "$.receipt.revision",
      code: "receipt_revision_mismatch",
      message: "Artifact receipt revision does not match content.",
      value: receipt.revision,
    })
  }
  if (range && receipt.range) {
    if (
      range.offset !== receipt.range.offset
      || range.endExclusive !== receipt.range.endExclusive
      || range.totalBytes !== receipt.range.totalBytes
    ) {
      errors.push({
        path: "$.receipt.range",
        code: "receipt_range_mismatch",
        message: "Artifact receipt range does not match content range.",
      })
    }
  }
  if (policy.securityLabel === "secret" && content) {
    errors.push({
      path: "$.content",
      code: "secret_content_returned",
      message: "Secret artifact content must never be returned.",
    })
  }
  throwIfErrors("Artifact read contract is invalid.", errors)
  return Object.freeze({
    schema: ARTIFACT_READ_CONTRACT,
    taskId,
    artifact: artifact!,
    policy,
    range,
    content,
    receipt,
  })
}

function parsePolicy(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
): ArtifactContentPolicy {
  const source = record(value, path, errors)
  const securityLabel = enumValue(
    source.security_label,
    securityLabels,
    "internal",
    `${path}.security_label`,
    errors,
  )
  const trustDisposition = enumValue(
    source.trust_disposition,
    trustDispositions,
    "untrusted",
    `${path}.trust_disposition`,
    errors,
  )
  const downloadPolicy = enumValue(
    source.download_policy,
    downloadPolicies,
    "deny",
    `${path}.download_policy`,
    errors,
  )
  return Object.freeze({
    securityLabel,
    trustDisposition,
    downloadPolicy,
    allowInline: booleanValue(source.allow_inline, `${path}.allow_inline`, errors),
    allowDownload: booleanValue(
      source.allow_download,
      `${path}.allow_download`,
      errors,
    ),
    quarantine: booleanValue(source.quarantine, `${path}.quarantine`, errors),
    refusalCode: optionalBoundedString(
      source.refusal_code,
      `${path}.refusal_code`,
      errors,
      256,
    ),
    reasons: Object.freeze(
      stringArray(source.reasons, `${path}.reasons`, errors, 64, 1024),
    ),
  })
}

function parseRange(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
): ArtifactReadRange {
  const source = record(value, path, errors)
  const offset = nonNegativeInteger(source.offset, `${path}.offset`, errors)
  const length = nonNegativeInteger(source.length, `${path}.length`, errors)
  const requestedLength = nonNegativeInteger(
    source.requested_length ?? source.length,
    `${path}.requested_length`,
    errors,
  )
  const endExclusive = nonNegativeInteger(
    source.end_exclusive,
    `${path}.end_exclusive`,
    errors,
  )
  const totalBytes = nonNegativeInteger(
    source.total_bytes,
    `${path}.total_bytes`,
    errors,
  )
  if (endExclusive !== offset + length) {
    errors.push({
      path: `${path}.end_exclusive`,
      code: "range_length_mismatch",
      message: "Artifact range end does not equal offset plus length.",
      value: endExclusive,
    })
  }
  if (endExclusive > totalBytes) {
    errors.push({
      path: `${path}.end_exclusive`,
      code: "range_exceeds_content",
      message: "Artifact range exceeds total content bytes.",
      value: endExclusive,
    })
  }
  return Object.freeze({
    offset,
    length,
    requestedLength,
    endExclusive,
    totalBytes,
    complete: booleanValue(source.complete, `${path}.complete`, errors),
  })
}

function parseContent(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
): ArtifactContentEnvelope {
  const source = record(value, path, errors)
  const text = optionalStringValue(source.text, `${path}.text`, errors)
  const base64 = optionalBase64(source.base64, `${path}.base64`, errors)
  if ((text === undefined) === (base64 === undefined)) {
    errors.push({
      path,
      code: "content_representation",
      message: "Artifact content must contain exactly one text or base64 representation.",
    })
  }
  return Object.freeze({
    text,
    base64,
    encoding: optionalBoundedString(source.encoding, `${path}.encoding`, errors, 64),
    decodeStatus: boundedString(
      source.decode_status,
      `${path}.decode_status`,
      errors,
      128,
    ),
    serverRedacted: booleanValue(
      source.server_redacted,
      `${path}.server_redacted`,
      errors,
    ),
    redactions: Object.freeze(
      findingArray(source.redactions, `${path}.redactions`, errors),
    ),
    promptFindings: Object.freeze(
      findingArray(source.prompt_findings, `${path}.prompt_findings`, errors),
    ),
    quarantined: booleanValue(
      source.quarantined,
      `${path}.quarantined`,
      errors,
    ),
  })
}

function parseReceipt(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
): ArtifactReadReceipt {
  const source = record(value, path, errors)
  if (stringValue(source.schema, `${path}.schema`, errors) !== ARTIFACT_RECEIPT_CONTRACT) {
    errors.push({
      path: `${path}.schema`,
      code: "receipt_schema_mismatch",
      message: `Expected ${ARTIFACT_RECEIPT_CONTRACT}.`,
      value: source.schema,
    })
  }
  const purpose = enumValue(
    source.purpose,
    readPurposes,
    "preview",
    `${path}.purpose`,
    errors,
  )
  const decision = source.decision === "deny" ? "deny" : "allow"
  if (source.decision !== "allow" && source.decision !== "deny") {
    errors.push({
      path: `${path}.decision`,
      code: "receipt_decision",
      message: "Artifact receipt decision is invalid.",
      value: source.decision,
    })
  }
  return Object.freeze({
    schema: ARTIFACT_RECEIPT_CONTRACT,
    sequence: positiveInteger(source.sequence, `${path}.sequence`, errors),
    occurredAt: timestamp(source.occurred_at, `${path}.occurred_at`, errors),
    taskId: identity(source.task_id, `${path}.task_id`, errors),
    artifactId: identity(source.artifact_id, `${path}.artifact_id`, errors),
    revision: revisionValue(source.revision, `${path}.revision`, errors),
    sha256: optionalSha256(source.sha256, `${path}.sha256`, errors),
    purpose,
    decision,
    reason: optionalBoundedString(source.reason, `${path}.reason`, errors, 512),
    range: source.range === undefined || source.range === null
      ? undefined
      : parseRange(source.range, `${path}.range`, errors),
    transformations: Object.freeze(
      stringArray(
        source.transformations,
        `${path}.transformations`,
        errors,
        64,
        256,
      ),
    ),
    elapsedUs: optionalNonNegativeInteger(
      source.elapsed_us,
      `${path}.elapsed_us`,
      errors,
    ),
    receiptDigest: sha256Value(
      source.receipt_digest,
      `${path}.receipt_digest`,
      errors,
    ),
  })
}

function findingArray(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
): ArtifactRedactionFinding[] {
  return arrayValue(value, path, errors, 256).map((candidate, index) => {
    const entryPath = `${path}[${index}]`
    const source = record(candidate, entryPath, errors)
    const start = nonNegativeInteger(source.start, `${entryPath}.start`, errors)
    const end = nonNegativeInteger(source.end, `${entryPath}.end`, errors)
    if (end < start) {
      errors.push({
        path: `${entryPath}.end`,
        code: "finding_range",
        message: "Finding end precedes start.",
        value: end,
      })
    }
    return Object.freeze({
      kind: boundedString(source.kind, `${entryPath}.kind`, errors, 128),
      start,
      end,
      digest: sha256Value(source.digest, `${entryPath}.digest`, errors),
    })
  })
}

export function artifactRevisionKey(
  artifact: Pick<ArtifactContract, "artifactId" | "revision">,
): string {
  return `${artifact.artifactId}@${artifact.revision}`
}

export function artifactRangeKey(
  artifact: Pick<ArtifactContract, "artifactId" | "revision">,
  range: Pick<ArtifactReadRange, "offset" | "endExclusive">,
): string {
  return `${artifactRevisionKey(artifact)}:${range.offset}-${range.endExclusive}`
}

export function artifactDisplayName(
  artifact: Pick<ArtifactContract, "artifactId" | "title" | "revision">,
): string {
  const title = artifact.title.trim() || artifact.artifactId
  const suffix = artifact.revision.startsWith("sha256:")
    ? artifact.revision.slice(7, 15)
    : artifact.revision.slice(0, 8)
  return `${title} · ${suffix}`
}

export function isTextualFamily(family: ArtifactContentFamily): boolean {
  return family === "text" || family === "markdown" || family === "json"
}

export function isMediaFamily(family: ArtifactContentFamily): boolean {
  return family === "image" || family === "audio" || family === "video"
}

export function isActiveContentFamily(family: ArtifactContentFamily): boolean {
  return (
    family === "html"
    || family === "svg"
    || family === "executable"
    || family === "archive"
  )
}

export function canRequestInline(artifact: ArtifactContract): boolean {
  if (!artifact.status.exists || !artifact.status.isFile) return false
  if (artifact.security.label === "secret") return false
  if (artifact.status.executableRisk) return false
  return artifact.status.inlineSafe || artifact.contentFamily === "binary"
}

function freezeArtifact(artifact: ArtifactContract): ArtifactContract {
  return Object.freeze({
    ...artifact,
    lineEndings: Object.freeze([...artifact.lineEndings]),
    producer: Object.freeze({ ...artifact.producer }),
    security: Object.freeze({ ...artifact.security }),
    retention: Object.freeze({ ...artifact.retention }),
    status: Object.freeze({ ...artifact.status }),
    links: Object.freeze({ ...artifact.links }),
  })
}

function record(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
): Record<string, unknown> {
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    return value as Record<string, unknown>
  }
  errors.push({
    path,
    code: "object_required",
    message: "Expected an object.",
    value,
  })
  return {}
}

function arrayValue(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
  maximum: number,
): unknown[] {
  if (!Array.isArray(value)) {
    errors.push({
      path,
      code: "array_required",
      message: "Expected an array.",
      value,
    })
    return []
  }
  if (value.length > maximum) {
    errors.push({
      path,
      code: "array_too_large",
      message: `Array exceeds ${maximum} entries.`,
      value: value.length,
    })
    return value.slice(0, maximum)
  }
  return value
}

function stringValue(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
): string {
  if (typeof value === "string") return value
  errors.push({
    path,
    code: "string_required",
    message: "Expected a string.",
    value,
  })
  return ""
}

function optionalStringValue(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
): string | undefined {
  if (value === undefined || value === null) return undefined
  return stringValue(value, path, errors)
}

function boundedString(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
  maximumBytes: number,
): string {
  const selected = stringValue(value, path, errors)
  if (new TextEncoder().encode(selected).byteLength > maximumBytes) {
    errors.push({
      path,
      code: "string_too_large",
      message: `String exceeds ${maximumBytes} bytes.`,
      value: selected.length,
    })
  }
  return selected
}

function optionalBoundedString(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
  maximumBytes: number,
): string | undefined {
  if (value === undefined || value === null || value === "") return undefined
  return boundedString(value, path, errors, maximumBytes)
}

function booleanValue(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
): boolean {
  if (typeof value === "boolean") return value
  errors.push({
    path,
    code: "boolean_required",
    message: "Expected a boolean.",
    value,
  })
  return false
}

function nonNegativeInteger(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
): number {
  const selected = Number(value)
  if (!Number.isSafeInteger(selected) || selected < 0) {
    errors.push({
      path,
      code: "non_negative_integer_required",
      message: "Expected a non-negative safe integer.",
      value,
    })
    return 0
  }
  return selected
}

function positiveInteger(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
): number {
  const selected = nonNegativeInteger(value, path, errors)
  if (selected < 1) {
    errors.push({
      path,
      code: "positive_integer_required",
      message: "Expected a positive safe integer.",
      value,
    })
  }
  return selected
}

function optionalNonNegativeInteger(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
): number | undefined {
  if (value === undefined || value === null) return undefined
  return nonNegativeInteger(value, path, errors)
}

function identity(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
): string {
  const selected = boundedString(value, path, errors, 512).trim()
  if (!selected || /[\r\n\u0000]/.test(selected)) {
    errors.push({
      path,
      code: "identity_invalid",
      message: "Identity is empty or contains control characters.",
      value,
    })
  }
  return selected
}

function optionalIdentity(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
): string | undefined {
  if (value === undefined || value === null || value === "") return undefined
  return identity(value, path, errors)
}

function revisionValue(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
): string {
  const selected = boundedString(value, path, errors, 128).trim().toLowerCase()
  if (!/^sha256:[0-9a-f]{64}$/.test(selected)) {
    errors.push({
      path,
      code: "revision_invalid",
      message: "Artifact revision must be a SHA-256 revision.",
      value,
    })
  }
  return selected
}

function sha256Value(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
): string {
  const selected = boundedString(value, path, errors, 64).trim().toLowerCase()
  if (!/^[0-9a-f]{64}$/.test(selected)) {
    errors.push({
      path,
      code: "sha256_invalid",
      message: "Expected a lowercase SHA-256 digest.",
      value,
    })
  }
  return selected
}

function optionalSha256(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
): string | undefined {
  if (value === undefined || value === null || value === "") return undefined
  return sha256Value(value, path, errors)
}

function timestamp(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
): string {
  const selected = boundedString(value, path, errors, 128)
  if (!Number.isFinite(Date.parse(selected))) {
    errors.push({
      path,
      code: "timestamp_invalid",
      message: "Expected an ISO timestamp.",
      value,
    })
  }
  return selected
}

function optionalTimestamp(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
): string | undefined {
  if (value === undefined || value === null || value === "") return undefined
  return timestamp(value, path, errors)
}

function mediaType(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
): string {
  const selected = boundedString(value, path, errors, 256)
    .split(";", 1)[0]!
    .trim()
    .toLowerCase()
  if (!/^[a-z0-9][a-z0-9!#$&^_.+-]*\/[a-z0-9][a-z0-9!#$&^_.+-]*$/.test(selected)) {
    errors.push({
      path,
      code: "media_type_invalid",
      message: "Artifact media type is invalid.",
      value,
    })
  }
  return selected || "application/octet-stream"
}

function retentionValue(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
): ArtifactRetentionContract["policy"] {
  const selected = stringValue(value, path, errors)
  if (
    selected === "ephemeral"
    || selected === "task"
    || selected === "run"
    || selected === "project"
    || selected === "submission"
  ) {
    return selected
  }
  errors.push({
    path,
    code: "retention_policy_invalid",
    message: "Artifact retention policy is invalid.",
    value,
  })
  return "task"
}

function safeRelativePath(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
): string {
  const selected = boundedString(value, path, errors, 4096).trim()
  if (
    !selected.startsWith("/")
    || selected.startsWith("//")
    || selected.includes("\\")
    || selected.split("/").some((segment) => segment === "." || segment === "..")
    || /[\r\n\u0000]/.test(selected)
  ) {
    errors.push({
      path,
      code: "relative_path_invalid",
      message: "Artifact API link is not a safe root-relative path.",
      value,
    })
  }
  return selected
}

function optionalCursor(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
): string | undefined {
  if (value === undefined || value === null || value === "") return undefined
  const selected = boundedString(value, path, errors, 4096)
  if (!/^[A-Za-z0-9_-]+$/.test(selected)) {
    errors.push({
      path,
      code: "cursor_invalid",
      message: "Artifact cursor is malformed.",
      value,
    })
  }
  return selected
}

function optionalBase64(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
): string | undefined {
  if (value === undefined || value === null || value === "") return undefined
  const selected = stringValue(value, path, errors)
  if (selected.length > 2_000_000 || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(selected)) {
    errors.push({
      path,
      code: "base64_invalid",
      message: "Artifact base64 payload is malformed or exceeds the client bound.",
      value: selected.length,
    })
  }
  return selected
}

function stringArray(
  value: unknown,
  path: string,
  errors: ArtifactWireError[],
  maximumEntries: number,
  maximumItemBytes: number,
): string[] {
  return arrayValue(value, path, errors, maximumEntries).map((item, index) =>
    boundedString(item, `${path}[${index}]`, errors, maximumItemBytes),
  )
}

function enumValue<T extends string>(
  value: unknown,
  values: ReadonlySet<T>,
  fallback: T,
  path: string,
  errors: ArtifactWireError[],
): T {
  const selected = stringValue(value, path, errors) as T
  if (values.has(selected)) return selected
  errors.push({
    path,
    code: "enum_invalid",
    message: `Value is not one of: ${[...values].join(", ")}.`,
    value,
  })
  return fallback
}

function throwIfErrors(
  message: string,
  errors: ArtifactWireError[],
): void {
  if (errors.length) throw new ArtifactContractError(message, errors)
}
