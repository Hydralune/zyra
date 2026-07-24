import type { ArtifactCacheSnapshot } from "./cache.ts"
import {
  artifactRevisionKey,
  type ArtifactCatalogPage,
  type ArtifactContract,
  type ArtifactReadReceipt,
  type ArtifactReadResponse,
} from "./contracts.ts"

export type ArtifactAuditOperation =
  | "catalog-request"
  | "catalog-page"
  | "select"
  | "metadata"
  | "range-request"
  | "range-admit"
  | "range-refuse"
  | "search"
  | "bookmark"
  | "download-decision"
  | "media-acquire"
  | "cancel"
  | "disconnect"
  | "close"

export interface ArtifactAuditEntry {
  sequence: number
  operation: ArtifactAuditOperation
  taskId: string
  artifactId?: string
  revision?: string
  receiptDigest?: string
  range?: {
    offset: number
    endExclusive: number
    totalBytes: number
  }
  decision: "observe" | "allow" | "deny" | "cancel" | "fail"
  code?: string
  details: Readonly<Record<string, string | number | boolean | null>>
  causationSequence?: number
  occurredAt: number
  previousDigest: string
  digest: string
}

export interface ArtifactAuditSnapshot {
  taskId: string
  entries: number
  firstSequence?: number
  lastSequence?: number
  headDigest: string
  dropped: number
  invalidReceipts: number
  deniedReads: number
  cancelledReads: number
  disconnectedReads: number
  admittedBytes: number
  cache?: ArtifactCacheSnapshot
  closed: boolean
}

export interface ArtifactAuditInput {
  operation: ArtifactAuditOperation
  artifact?: Pick<ArtifactContract, "artifactId" | "revision">
  receipt?: ArtifactReadReceipt
  range?: {
    offset: number
    endExclusive: number
    totalBytes: number
  }
  decision?: ArtifactAuditEntry["decision"]
  code?: string
  details?: Record<string, string | number | boolean | null | undefined>
  causationSequence?: number
}

const AUDIT_GENESIS = "0000000000000000"
const DEFAULT_MAXIMUM = 20_000

export class ArtifactOperationLedger {
  readonly #taskId: string
  readonly #maximum: number
  readonly #entries: ArtifactAuditEntry[] = []
  readonly #receiptOwners = new Map<string, string>()
  #sequence = 0
  #headDigest = AUDIT_GENESIS
  #dropped = 0
  #invalidReceipts = 0
  #deniedReads = 0
  #cancelledReads = 0
  #disconnectedReads = 0
  #admittedBytes = 0
  #cache?: ArtifactCacheSnapshot
  #closed = false

  constructor(taskId: string, maximum = DEFAULT_MAXIMUM) {
    this.#taskId = safeIdentity(taskId)
    this.#maximum = boundedMaximum(maximum)
  }

  append(input: ArtifactAuditInput): ArtifactAuditEntry {
    this.#assertOpen()
    const artifactId = input.artifact?.artifactId
    const revision = input.artifact?.revision
    if ((artifactId === undefined) !== (revision === undefined)) {
      throw new ArtifactAuditError(
        "audit_artifact_identity_partial",
        "Artifact audit identity requires both artifact id and revision.",
      )
    }
    if (artifactId) safeIdentity(artifactId)
    if (revision) safeRevision(revision)
    const receiptDigest = input.receipt?.receiptDigest
    if (input.receipt) {
      this.#validateReceipt(input.receipt, artifactId, revision)
    }
    const range = input.range
      ? normalizeRange(input.range)
      : undefined
    const details = normalizeDetails(input.details)
    const sequence = ++this.#sequence
    if (
      input.causationSequence !== undefined
      && (
        !Number.isSafeInteger(input.causationSequence)
        || input.causationSequence < 1
        || input.causationSequence >= sequence
      )
    ) {
      throw new ArtifactAuditError(
        "audit_causation_invalid",
        "Artifact audit causation must reference an earlier sequence.",
      )
    }
    const payload = {
      sequence,
      operation: input.operation,
      taskId: this.#taskId,
      artifactId,
      revision,
      receiptDigest,
      range,
      decision: input.decision ?? "observe",
      code: input.code,
      details,
      causationSequence: input.causationSequence,
      occurredAt: Date.now(),
      previousDigest: this.#headDigest,
    }
    const digest = auditDigest(payload)
    const entry = Object.freeze({ ...payload, digest })
    this.#entries.push(entry)
    this.#headDigest = digest
    this.#observeCounters(entry)
    if (this.#entries.length > this.#maximum) {
      const drop = this.#entries.length - this.#maximum
      this.#entries.splice(0, drop)
      this.#dropped += drop
    }
    return entry
  }

  catalogRequest(
    filters: Record<string, string | number | boolean | null | undefined>,
  ): ArtifactAuditEntry {
    return this.append({
      operation: "catalog-request",
      details: filters,
    })
  }

  catalogPage(page: ArtifactCatalogPage): ArtifactAuditEntry {
    if (page.taskId !== this.#taskId) {
      throw new ArtifactAuditError(
        "audit_catalog_task_mismatch",
        "Artifact catalog audit page belongs to a different task.",
      )
    }
    return this.append({
      operation: "catalog-page",
      details: {
        total: page.total,
        returned: page.returned,
        has_cursor: Boolean(page.cursor),
        state_owner: page.stateOwner,
      },
    })
  }

  selection(
    artifact: ArtifactContract,
    source: string,
  ): ArtifactAuditEntry {
    return this.append({
      operation: "select",
      artifact,
      details: {
        source: boundedDetail(source),
        media_type: artifact.mediaType,
        family: artifact.contentFamily,
        integrity: artifact.status.integrity,
        security: artifact.security.label,
      },
    })
  }

  metadata(response: ArtifactReadResponse): ArtifactAuditEntry {
    this.#assertResponse(response)
    return this.append({
      operation: "metadata",
      artifact: response.artifact,
      receipt: response.receipt,
      decision: response.receipt.decision,
      code: response.receipt.reason,
      details: {
        integrity: response.artifact.status.integrity,
        inline_safe: response.artifact.status.inlineSafe,
        legacy: response.artifact.status.legacyMetadata,
      },
    })
  }

  range(response: ArtifactReadResponse): ArtifactAuditEntry {
    this.#assertResponse(response)
    const range = response.range
    if (!range) {
      throw new ArtifactAuditError(
        "audit_range_missing",
        "Artifact range audit requires range metadata.",
      )
    }
    return this.append({
      operation:
        response.receipt.decision === "allow"
          ? "range-admit"
          : "range-refuse",
      artifact: response.artifact,
      receipt: response.receipt,
      range,
      decision: response.receipt.decision,
      code: response.receipt.reason,
      details: {
        purpose: response.receipt.purpose,
        transformations: response.receipt.transformations.join(","),
        quarantined: response.content?.quarantined ?? false,
        server_redacted: response.content?.serverRedacted ?? false,
      },
    })
  }

  failure(input: {
    operation: ArtifactAuditOperation
    artifact?: Pick<ArtifactContract, "artifactId" | "revision">
    code: string
    message: string
    disconnected?: boolean
  }): ArtifactAuditEntry {
    return this.append({
      operation: input.disconnected ? "disconnect" : input.operation,
      artifact: input.artifact,
      decision: "fail",
      code: input.code,
      details: {
        message: boundedDetail(input.message, 4096),
        disconnected: input.disconnected ?? false,
      },
    })
  }

  updateCache(snapshot: ArtifactCacheSnapshot): void {
    this.#assertOpen()
    this.#cache = Object.freeze({
      ...snapshot,
      limits: Object.freeze({ ...snapshot.limits }),
    })
  }

  entries(options: {
    artifactId?: string
    operation?: ArtifactAuditOperation
    afterSequence?: number
    limit?: number
  } = {}): readonly ArtifactAuditEntry[] {
    const limit = boundedQueryLimit(options.limit)
    const after = options.afterSequence ?? 0
    return Object.freeze(
      this.#entries
        .filter(
          (entry) =>
            entry.sequence > after
            && (!options.artifactId || entry.artifactId === options.artifactId)
            && (!options.operation || entry.operation === options.operation),
        )
        .slice(-limit),
    )
  }

  verifyChain(): {
    valid: boolean
    checked: number
    firstInvalidSequence?: number
  } {
    let checked = 0
    let previous = this.#dropped
      ? this.#entries[0]?.previousDigest ?? AUDIT_GENESIS
      : AUDIT_GENESIS
    for (const entry of this.#entries) {
      checked += 1
      if (entry.previousDigest !== previous) {
        return {
          valid: false,
          checked,
          firstInvalidSequence: entry.sequence,
        }
      }
      const { digest, ...payload } = entry
      if (auditDigest(payload) !== digest) {
        return {
          valid: false,
          checked,
          firstInvalidSequence: entry.sequence,
        }
      }
      previous = entry.digest
    }
    return { valid: true, checked }
  }

  snapshot(): ArtifactAuditSnapshot {
    return Object.freeze({
      taskId: this.#taskId,
      entries: this.#entries.length,
      firstSequence: this.#entries[0]?.sequence,
      lastSequence: this.#entries.at(-1)?.sequence,
      headDigest: this.#headDigest,
      dropped: this.#dropped,
      invalidReceipts: this.#invalidReceipts,
      deniedReads: this.#deniedReads,
      cancelledReads: this.#cancelledReads,
      disconnectedReads: this.#disconnectedReads,
      admittedBytes: this.#admittedBytes,
      cache: this.#cache,
      closed: this.#closed,
    })
  }

  close(): void {
    if (this.#closed) return
    this.append({
      operation: "close",
      decision: "observe",
      details: {
        retained_entries: this.#entries.length,
      },
    })
    this.#closed = true
    this.#receiptOwners.clear()
  }

  #validateReceipt(
    receipt: ArtifactReadReceipt,
    artifactId?: string,
    revision?: string,
  ): void {
    if (receipt.taskId !== this.#taskId) {
      this.#invalidReceipts += 1
      throw new ArtifactAuditError(
        "audit_receipt_task_mismatch",
        "Artifact receipt belongs to a different task.",
      )
    }
    if (
      artifactId
      && (
        receipt.artifactId !== artifactId
        || receipt.revision !== revision
      )
    ) {
      this.#invalidReceipts += 1
      throw new ArtifactAuditError(
        "audit_receipt_artifact_mismatch",
        "Artifact receipt belongs to a different immutable revision.",
      )
    }
    const owner = `${receipt.taskId}:${receipt.artifactId}@${receipt.revision}`
    const existing = this.#receiptOwners.get(receipt.receiptDigest)
    if (existing && existing !== owner) {
      this.#invalidReceipts += 1
      throw new ArtifactAuditError(
        "audit_receipt_digest_collision",
        "Artifact receipt digest was reused across immutable owners.",
      )
    }
    this.#receiptOwners.set(receipt.receiptDigest, owner)
  }

  #assertResponse(response: ArtifactReadResponse): void {
    if (response.taskId !== this.#taskId) {
      throw new ArtifactAuditError(
        "audit_response_task_mismatch",
        "Artifact response belongs to a different task.",
      )
    }
  }

  #observeCounters(entry: ArtifactAuditEntry): void {
    if (entry.decision === "deny") this.#deniedReads += 1
    if (entry.decision === "cancel") this.#cancelledReads += 1
    if (entry.operation === "disconnect") this.#disconnectedReads += 1
    if (entry.operation === "range-admit" && entry.range) {
      this.#admittedBytes += entry.range.endExclusive - entry.range.offset
    }
  }

  #assertOpen(): void {
    if (this.#closed) {
      throw new ArtifactAuditError(
        "audit_closed",
        "Artifact operation ledger is closed.",
      )
    }
  }
}

export class ArtifactAuditError extends Error {
  readonly code: string

  constructor(code: string, message: string) {
    super(message)
    this.name = "ArtifactAuditError"
    this.code = code
  }
}

function normalizeRange(
  range: ArtifactAuditEntry["range"],
): NonNullable<ArtifactAuditEntry["range"]> {
  if (
    !range
    || !Number.isSafeInteger(range.offset)
    || !Number.isSafeInteger(range.endExclusive)
    || !Number.isSafeInteger(range.totalBytes)
    || range.offset < 0
    || range.endExclusive < range.offset
    || range.totalBytes < range.endExclusive
  ) {
    throw new ArtifactAuditError(
      "audit_range_invalid",
      "Artifact audit range is invalid.",
    )
  }
  return Object.freeze({ ...range })
}

function normalizeDetails(
  details: ArtifactAuditInput["details"],
): Readonly<Record<string, string | number | boolean | null>> {
  const result: Record<string, string | number | boolean | null> = {}
  for (const [key, value] of Object.entries(details ?? {}).slice(0, 128)) {
    const normalizedKey = boundedDetail(key, 128)
    if (value === undefined) continue
    if (typeof value === "string") result[normalizedKey] = boundedDetail(value)
    else if (
      typeof value === "number"
      && Number.isFinite(value)
    ) {
      result[normalizedKey] = value
    } else if (typeof value === "boolean" || value === null) {
      result[normalizedKey] = value
    }
  }
  return Object.freeze(result)
}

function auditDigest(
  value: Omit<ArtifactAuditEntry, "digest">,
): string {
  const canonical = JSON.stringify(canonicalize(value))
  const bytes = new TextEncoder().encode(canonical)
  let left = 0x811c9dc5
  let right = 0x9e3779b9
  for (let index = 0; index < bytes.length; index += 1) {
    left = Math.imul(left ^ bytes[index]!, 0x01000193)
    right = Math.imul(right ^ (bytes[index]! + index), 0x85ebca6b)
  }
  return `${(left >>> 0).toString(16).padStart(8, "0")}${(right >>> 0)
    .toString(16)
    .padStart(8, "0")}`
}

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map((entry) => canonicalize(entry))
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>
    const normalized: Record<string, unknown> = {}
    for (const key of Object.keys(record).sort()) {
      const selected = record[key]
      if (selected !== undefined) normalized[key] = canonicalize(selected)
    }
    return normalized
  }
  return value
}

function safeIdentity(value: string): string {
  const selected = String(value || "").trim()
  if (
    !selected
    || /[\r\n\u0000]/.test(selected)
    || new TextEncoder().encode(selected).byteLength > 512
  ) {
    throw new ArtifactAuditError(
      "audit_identity_invalid",
      "Artifact audit identity is invalid.",
    )
  }
  return selected
}

function safeRevision(value: string): string {
  const selected = String(value || "").trim().toLowerCase()
  if (!/^sha256:[0-9a-f]{64}$/.test(selected)) {
    throw new ArtifactAuditError(
      "audit_revision_invalid",
      "Artifact audit revision is invalid.",
    )
  }
  return selected
}

function boundedDetail(value: string, maximum = 2048): string {
  const selected = String(value || "")
  const bytes = new TextEncoder().encode(selected)
  if (bytes.byteLength <= maximum) return selected
  return new TextDecoder().decode(bytes.slice(0, maximum))
}

function boundedMaximum(value: number): number {
  if (!Number.isSafeInteger(value) || value < 100 || value > 1_000_000) {
    throw new ArtifactAuditError(
      "audit_maximum_invalid",
      "Artifact audit maximum must be between 100 and 1000000.",
    )
  }
  return value
}

function boundedQueryLimit(value: number | undefined): number {
  const selected = value ?? 1_000
  if (!Number.isSafeInteger(selected)) return 1_000
  return Math.max(1, Math.min(20_000, selected))
}
