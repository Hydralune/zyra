export type DiffFileKind =
  | "added"
  | "deleted"
  | "modified"
  | "renamed"
  | "binary"

export type DiffLineKind = "context" | "added" | "deleted" | "notice"

export type DiffViewMode = "diff" | "old" | "new"

export type DiffLayoutMode = "unified" | "split"

export type PatchTransactionPhase =
  | "preflight"
  | "permission_pending"
  | "permission_denied"
  | "stale"
  | "conflicted"
  | "committed"
  | "rolled_back"
  | "rollback_failed"
  | "quarantined"
  | "cancelled"
  | "failed"

export interface DiffSourceBinding {
  taskId: string
  artifactId: string
  artifactRevision: string
  artifactSha256: string
  runId: string
  workspaceId: string
  ownerEpoch: number
  bindingRevision: number
  leaseId: string
}

export interface DiffRepositoryRisk {
  dirty: boolean
  nestedRepository: boolean
  untrackedPaths: number
  modifiedPaths: number
  stagedPaths: number
  conflictedPaths: number
  repositoryRoot: string
  reasonCodes: readonly string[]
}

export interface DiffFileContract {
  fileId: string
  path: string
  previousPath?: string
  kind: DiffFileKind
  binary: boolean
  oversized: boolean
  truncated: boolean
  encoding: string
  lineEnding: "lf" | "crlf" | "cr" | "mixed" | "none" | "binary" | "unknown"
  additions: number
  deletions: number
  hunkCount: number
  pageCount: number
  patchOffset: number
  patchBytes: number
  oldSize: number
  newSize: number
  oldSha256: string
  newSha256: string
  currentSha256: string
  oldMtimeNs: number
  currentMtimeNs: number
  oldMode: number
  currentMode: number
  language: string
  mimeType: string
  risk: DiffRepositoryRisk
}

export interface DiffTotals {
  files: number
  hunks: number
  lines: number
  additions: number
  deletions: number
  binaryFiles: number
  renamedFiles: number
  oversizedFiles: number
  patchBytes: number
}

export interface DiffManifestContract {
  schema: "zyra.diff-review-manifest.v1"
  diffId: string
  generatedAt: string
  source: DiffSourceBinding
  files: readonly DiffFileContract[]
  totals: DiffTotals
  maximumPageBytes: number
  maximumPageLines: number
  physicalPathDisclosed: false
  readOnly: true
}

export interface DiffLineContract {
  lineId: string
  kind: DiffLineKind
  text: string
  oldLine?: number
  newLine?: number
  patchLine: number
  byteOffset: number
  byteLength: number
  noNewline: boolean
}

export interface DiffHunkContract {
  hunkId: string
  fileId: string
  index: number
  header: string
  section: string
  oldStart: number
  oldCount: number
  newStart: number
  newCount: number
  additions: number
  deletions: number
  contextLines: number
  patchStart: number
  patchEnd: number
  lines: readonly DiffLineContract[]
}

export interface DiffPageContract {
  schema: "zyra.diff-review-page.v1"
  diffId: string
  fileId: string
  artifactRevision: string
  pageIndex: number
  pageCount: number
  cursor: string
  nextCursor?: string
  complete: boolean
  hunkStart: number
  hunkEnd: number
  utf8Bytes: number
  contentDigest: string
  hunks: readonly DiffHunkContract[]
  receiptId: string
  generatedAt: string
  physicalPathDisclosed: false
}

export interface DiffFileContentContract {
  schema: "zyra.diff-review-file-content.v1"
  diffId: string
  fileId: string
  artifactRevision: string
  version: "base" | "current"
  text: string
  sha256: string
  mtimeNs: number
  mode: number
  encoding: string
  lineEnding: "lf" | "crlf" | "cr" | "mixed" | "none"
  complete: true
  utf8Bytes: number
  receiptId: string
  generatedAt: string
  physicalPathDisclosed: false
}

export interface DiffReviewLineSelection {
  fileId: string
  hunkId: string
  side: "old" | "new"
  startLine: number
  endLine: number
  anchorLineIds: readonly string[]
}

export interface DiffReviewComment {
  commentId: string
  diffId: string
  fileId: string
  hunkId: string
  selection: DiffReviewLineSelection
  body: string
  authorId: string
  createdAt: string
  updatedAt: string
  state: "draft" | "submitted" | "resolved"
  causationId: string
  revision: number
}

export interface DiffReviewReceipt {
  schema: "zyra.diff-review-receipt.v1"
  receiptId: string
  diffId: string
  taskId: string
  action: "select" | "comment" | "comment_update" | "comment_resolve"
  accepted: boolean
  causationId: string
  eventIds: readonly string[]
  comment?: DiffReviewComment
  selection?: DiffReviewLineSelection
  revision: number
  permission: PatchPermissionProjection
  humanInterventionCount: 0
  deniedManualMutationCount: number
  createdAt: string
}

export interface PatchFilePrecondition {
  fileId: string
  path: string
  previousPath?: string
  kind: DiffFileKind
  baseSha256: string
  currentSha256: string
  proposedSha256: string
  baseMtimeNs: number
  currentMtimeNs: number
  baseMode: number
  currentMode: number
  encoding: string
  lineEnding: string
  binary: boolean
}

export interface PatchApplyRequest {
  schema: "zyra.patch-review-apply.v1"
  taskId: string
  runId: string
  diffId: string
  artifactId: string
  artifactRevision: string
  workspaceId: string
  idempotencyKey: string
  causationId: string
  actorId: string
  sessionId: string
  sessionRevision: number
  workerRequestId: string
  toolCallId: string
  expectedOwnerEpoch: number
  expectedBindingRevision: number
  expectedLeaseId: string
  selectedFileIds: readonly string[]
  preconditions: readonly PatchFilePrecondition[]
  sealed: boolean
  reviewRevision: number
  permissionPermitId?: string
}

export interface PatchRollbackRequest {
  schema: "zyra.patch-review-rollback.v1"
  taskId: string
  runId: string
  diffId: string
  transactionId: string
  snapshotId: string
  idempotencyKey: string
  causationId: string
  actorId: string
  sessionId: string
  sessionRevision: number
  workerRequestId: string
  toolCallId: string
  sealed: boolean
  permissionPermitId?: string
}

export interface PatchPermissionProjection {
  canonicalOwner: "typescript.PermissionCoordinator"
  effect: "allow" | "ask" | "deny"
  decisionId: string
  requestId?: string
  permitId?: string
  reasonCode: string
  policyRevision: number
  modeRevision: number
  requestFingerprint: string
  pending: boolean
  recoveryInput?: Readonly<Record<string, unknown>>
}

export interface PatchPathResult {
  fileId: string
  path: string
  disposition: string
  beforeSha256: string
  afterSha256: string
  bytesBefore: number
  bytesAfter: number
  verified: boolean
}

export interface PatchTransactionReceipt {
  schema: "zyra.patch-review-transaction-receipt.v1"
  receiptId: string
  taskId: string
  runId: string
  diffId: string
  transactionId: string
  phase: PatchTransactionPhase
  accepted: boolean
  committed: boolean
  idempotentReplay: boolean
  stale: boolean
  conflict: boolean
  rolledBack: boolean
  rollbackFailed: boolean
  sealed: boolean
  humanInterventionCount: 0
  deniedManualMutationCount: number
  workspaceId: string
  ownerEpochBefore: number
  ownerEpochAfter: number
  bindingRevisionBefore: number
  bindingRevisionAfter: number
  snapshotId: string
  recoveryInputId: string
  reasonCode: string
  message: string
  pathResults: readonly PatchPathResult[]
  artifactRefs: readonly string[]
  eventIds: readonly string[]
  verificationRefs: readonly string[]
  terminalRefs: readonly string[]
  timelineRefs: readonly string[]
  permission: PatchPermissionProjection
  causationId: string
  createdAt: string
}

export class DiffContractError extends Error {
  readonly code: string
  readonly path: string
  readonly details: Readonly<Record<string, unknown>>

  constructor(
    code: string,
    message: string,
    path = "$",
    details: Readonly<Record<string, unknown>> = {},
  ) {
    super(message)
    this.name = "DiffContractError"
    this.code = code
    this.path = path
    this.details = Object.freeze({ ...details })
  }
}

const identifierPattern = /^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,511}$/
const sha256Pattern = /^(?:sha256:)?[a-f0-9]{64}$/i
const receiptPattern = /^[A-Za-z0-9][A-Za-z0-9_.:@/-]{7,511}$/

function recordValue(value: unknown, path: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new DiffContractError(
      "diff_contract_object_required",
      `${path} must be an object.`,
      path,
    )
  }
  return value as Record<string, unknown>
}

function arrayValue(
  value: unknown,
  path: string,
  maximum: number,
): readonly unknown[] {
  if (!Array.isArray(value)) {
    throw new DiffContractError(
      "diff_contract_array_required",
      `${path} must be an array.`,
      path,
    )
  }
  if (value.length > maximum) {
    throw new DiffContractError(
      "diff_contract_array_budget",
      `${path} exceeds ${maximum} items.`,
      path,
      { actual: value.length, maximum },
    )
  }
  return value
}

function textValue(
  value: unknown,
  path: string,
  maximumBytes: number,
  allowEmpty = false,
): string {
  if (typeof value !== "string") {
    throw new DiffContractError(
      "diff_contract_text_required",
      `${path} must be text.`,
      path,
    )
  }
  const text = allowEmpty ? value : value.trim()
  if (!allowEmpty && !text) {
    throw new DiffContractError(
      "diff_contract_text_empty",
      `${path} must not be empty.`,
      path,
    )
  }
  const bytes = new TextEncoder().encode(text).byteLength
  if (bytes > maximumBytes) {
    throw new DiffContractError(
      "diff_contract_text_budget",
      `${path} exceeds ${maximumBytes} UTF-8 bytes.`,
      path,
      { actual: bytes, maximum: maximumBytes },
    )
  }
  if (/[\u0000]/.test(text)) {
    throw new DiffContractError(
      "diff_contract_text_control",
      `${path} contains a NUL byte.`,
      path,
    )
  }
  return text
}

function optionalText(
  value: unknown,
  path: string,
  maximumBytes: number,
): string | undefined {
  if (value === undefined || value === null || value === "") return undefined
  return textValue(value, path, maximumBytes)
}

function identifierValue(value: unknown, path: string): string {
  const text = textValue(value, path, 512)
  if (!identifierPattern.test(text) || /(?:^|\/)\.\.(?:\/|$)/.test(text)) {
    throw new DiffContractError(
      "diff_contract_identity_invalid",
      `${path} is not a safe identity.`,
      path,
    )
  }
  return text
}

function receiptValue(value: unknown, path: string): string {
  const text = textValue(value, path, 512)
  if (!receiptPattern.test(text)) {
    throw new DiffContractError(
      "diff_contract_receipt_invalid",
      `${path} is not a valid receipt identity.`,
      path,
    )
  }
  return text
}

function sha256Value(
  value: unknown,
  path: string,
  allowEmpty = false,
): string {
  if (allowEmpty && (value === "" || value === null || value === undefined)) {
    return ""
  }
  const text = textValue(value, path, 80)
  if (!sha256Pattern.test(text)) {
    throw new DiffContractError(
      "diff_contract_sha256_invalid",
      `${path} must be a SHA-256 digest.`,
      path,
    )
  }
  return text.startsWith("sha256:") ? text.toLowerCase() : `sha256:${text.toLowerCase()}`
}

function integerValue(
  value: unknown,
  path: string,
  minimum = 0,
  maximum = Number.MAX_SAFE_INTEGER,
): number {
  if (
    typeof value !== "number"
    || !Number.isSafeInteger(value)
    || value < minimum
    || value > maximum
  ) {
    throw new DiffContractError(
      "diff_contract_integer_invalid",
      `${path} must be an integer from ${minimum} through ${maximum}.`,
      path,
      { actual: value, minimum, maximum },
    )
  }
  return value
}

function booleanValue(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") {
    throw new DiffContractError(
      "diff_contract_boolean_required",
      `${path} must be boolean.`,
      path,
    )
  }
  return value
}

function literalFalse(value: unknown, path: string): false {
  if (value !== false) {
    throw new DiffContractError(
      "diff_contract_disclosure_rejected",
      `${path} must be false.`,
      path,
    )
  }
  return false
}

function timestampValue(value: unknown, path: string): string {
  const text = textValue(value, path, 128)
  if (!Number.isFinite(Date.parse(text))) {
    throw new DiffContractError(
      "diff_contract_timestamp_invalid",
      `${path} is not an ISO timestamp.`,
      path,
    )
  }
  return text
}

function enumValue<T extends string>(
  value: unknown,
  path: string,
  allowed: readonly T[],
): T {
  const text = textValue(value, path, 64)
  if (!allowed.includes(text as T)) {
    throw new DiffContractError(
      "diff_contract_enum_invalid",
      `${path} has an unsupported value.`,
      path,
      { actual: text, allowed },
    )
  }
  return text as T
}

function logicalPathValue(value: unknown, path: string): string {
  const text = textValue(value, path, 16 * 1024).replaceAll("\\", "/")
  if (
    text.startsWith("/")
    || /^[A-Za-z]:/.test(text)
    || text.split("/").some((part) => !part || part === "." || part === "..")
  ) {
    throw new DiffContractError(
      "diff_contract_path_invalid",
      `${path} must be a normalized logical path.`,
      path,
    )
  }
  return text
}

function parseRepositoryRisk(
  value: unknown,
  path: string,
): DiffRepositoryRisk {
  const source = recordValue(value, path)
  const reasonCodes = arrayValue(source.reason_codes ?? [], `${path}.reason_codes`, 128)
    .map((item, index) =>
      identifierValue(item, `${path}.reason_codes[${index}]`))
  return Object.freeze({
    dirty: booleanValue(source.dirty ?? false, `${path}.dirty`),
    nestedRepository: booleanValue(
      source.nested_repository ?? false,
      `${path}.nested_repository`,
    ),
    untrackedPaths: integerValue(
      source.untracked_paths ?? 0,
      `${path}.untracked_paths`,
      0,
      10_000_000,
    ),
    modifiedPaths: integerValue(
      source.modified_paths ?? 0,
      `${path}.modified_paths`,
      0,
      10_000_000,
    ),
    stagedPaths: integerValue(
      source.staged_paths ?? 0,
      `${path}.staged_paths`,
      0,
      10_000_000,
    ),
    conflictedPaths: integerValue(
      source.conflicted_paths ?? 0,
      `${path}.conflicted_paths`,
      0,
      10_000_000,
    ),
    repositoryRoot: String(source.repository_root ?? ""),
    reasonCodes: Object.freeze(reasonCodes),
  })
}

function parseDiffSource(value: unknown, path: string): DiffSourceBinding {
  const source = recordValue(value, path)
  return Object.freeze({
    taskId: identifierValue(source.task_id, `${path}.task_id`),
    artifactId: identifierValue(source.artifact_id, `${path}.artifact_id`),
    artifactRevision: identifierValue(
      source.artifact_revision,
      `${path}.artifact_revision`,
    ),
    artifactSha256: sha256Value(
      source.artifact_sha256,
      `${path}.artifact_sha256`,
    ),
    runId: identifierValue(source.run_id, `${path}.run_id`),
    workspaceId: identifierValue(source.workspace_id, `${path}.workspace_id`),
    ownerEpoch: integerValue(
      source.owner_epoch,
      `${path}.owner_epoch`,
      1,
    ),
    bindingRevision: integerValue(
      source.binding_revision,
      `${path}.binding_revision`,
      1,
    ),
    leaseId: identifierValue(source.lease_id, `${path}.lease_id`),
  })
}

function parseDiffFile(
  value: unknown,
  path: string,
): DiffFileContract {
  const source = recordValue(value, path)
  const kind = enumValue<DiffFileKind>(
    source.kind,
    `${path}.kind`,
    ["added", "deleted", "modified", "renamed", "binary"],
  )
  const binary = booleanValue(source.binary, `${path}.binary`)
  if (kind === "binary" && !binary) {
    throw new DiffContractError(
      "diff_contract_binary_kind_mismatch",
      `${path} identifies a binary file without binary=true.`,
      path,
    )
  }
  const previousPath = optionalText(
    source.previous_path,
    `${path}.previous_path`,
    16 * 1024,
  )
  if (kind === "renamed" && !previousPath) {
    throw new DiffContractError(
      "diff_contract_rename_source_missing",
      `${path} renamed file requires previous_path.`,
      path,
    )
  }
  const file: DiffFileContract = {
    fileId: identifierValue(source.file_id, `${path}.file_id`),
    path: logicalPathValue(source.path, `${path}.path`),
    previousPath: previousPath
      ? logicalPathValue(previousPath, `${path}.previous_path`)
      : undefined,
    kind,
    binary,
    oversized: booleanValue(source.oversized ?? false, `${path}.oversized`),
    truncated: booleanValue(source.truncated ?? false, `${path}.truncated`),
    encoding: textValue(source.encoding ?? "unknown", `${path}.encoding`, 128),
    lineEnding: enumValue(
      source.line_ending ?? "unknown",
      `${path}.line_ending`,
      ["lf", "crlf", "cr", "mixed", "none", "binary", "unknown"],
    ),
    additions: integerValue(source.additions, `${path}.additions`, 0),
    deletions: integerValue(source.deletions, `${path}.deletions`, 0),
    hunkCount: integerValue(source.hunk_count, `${path}.hunk_count`, 0),
    pageCount: integerValue(source.page_count, `${path}.page_count`, 0),
    patchOffset: integerValue(source.patch_offset, `${path}.patch_offset`, 0),
    patchBytes: integerValue(source.patch_bytes, `${path}.patch_bytes`, 0),
    oldSize: integerValue(source.old_size ?? 0, `${path}.old_size`, 0),
    newSize: integerValue(source.new_size ?? 0, `${path}.new_size`, 0),
    oldSha256: sha256Value(source.old_sha256, `${path}.old_sha256`, true),
    newSha256: sha256Value(source.new_sha256, `${path}.new_sha256`, true),
    currentSha256: sha256Value(
      source.current_sha256,
      `${path}.current_sha256`,
      true,
    ),
    oldMtimeNs: integerValue(source.old_mtime_ns ?? 0, `${path}.old_mtime_ns`, 0),
    currentMtimeNs: integerValue(
      source.current_mtime_ns ?? 0,
      `${path}.current_mtime_ns`,
      0,
    ),
    oldMode: integerValue(source.old_mode ?? 0, `${path}.old_mode`, 0, 0o7777),
    currentMode: integerValue(
      source.current_mode ?? 0,
      `${path}.current_mode`,
      0,
      0o7777,
    ),
    language: textValue(source.language ?? "text", `${path}.language`, 128),
    mimeType: textValue(
      source.mime_type ?? "text/plain",
      `${path}.mime_type`,
      256,
    ),
    risk: parseRepositoryRisk(source.risk ?? {}, `${path}.risk`),
  }
  if (file.patchOffset + file.patchBytes > Number.MAX_SAFE_INTEGER) {
    throw new DiffContractError(
      "diff_contract_patch_range_overflow",
      `${path} patch byte range overflows.`,
      path,
    )
  }
  if (file.binary && file.hunkCount > 0) {
    throw new DiffContractError(
      "diff_contract_binary_hunks",
      `${path} binary file cannot expose text hunks.`,
      path,
    )
  }
  return Object.freeze(file)
}

function parseTotals(value: unknown, path: string): DiffTotals {
  const source = recordValue(value, path)
  return Object.freeze({
    files: integerValue(source.files, `${path}.files`, 0),
    hunks: integerValue(source.hunks, `${path}.hunks`, 0),
    lines: integerValue(source.lines, `${path}.lines`, 0),
    additions: integerValue(source.additions, `${path}.additions`, 0),
    deletions: integerValue(source.deletions, `${path}.deletions`, 0),
    binaryFiles: integerValue(source.binary_files, `${path}.binary_files`, 0),
    renamedFiles: integerValue(source.renamed_files, `${path}.renamed_files`, 0),
    oversizedFiles: integerValue(
      source.oversized_files,
      `${path}.oversized_files`,
      0,
    ),
    patchBytes: integerValue(source.patch_bytes, `${path}.patch_bytes`, 0),
  })
}

export function parseDiffManifest(value: unknown): DiffManifestContract {
  const source = recordValue(value, "$")
  if (source.schema !== "zyra.diff-review-manifest.v1") {
    throw new DiffContractError(
      "diff_contract_schema_mismatch",
      "Diff manifest schema is not supported.",
      "$.schema",
      { actual: source.schema },
    )
  }
  const binding = parseDiffSource(source.source, "$.source")
  const rawFiles = arrayValue(source.files, "$.files", 10_000)
  const files = rawFiles.map((item, index) =>
    parseDiffFile(item, `$.files[${index}]`))
  const fileIds = new Set<string>()
  const paths = new Set<string>()
  for (const [index, file] of files.entries()) {
    if (fileIds.has(file.fileId)) {
      throw new DiffContractError(
        "diff_contract_duplicate_file_id",
        `Duplicate file id ${file.fileId}.`,
        `$.files[${index}].file_id`,
      )
    }
    if (paths.has(file.path.toLowerCase())) {
      throw new DiffContractError(
        "diff_contract_duplicate_path",
        `Duplicate or case-colliding file path ${file.path}.`,
        `$.files[${index}].path`,
      )
    }
    fileIds.add(file.fileId)
    paths.add(file.path.toLowerCase())
  }
  const totals = parseTotals(source.totals, "$.totals")
  const computed = files.reduce(
    (result, file) => {
      result.hunks += file.hunkCount
      result.additions += file.additions
      result.deletions += file.deletions
      result.binaryFiles += file.binary ? 1 : 0
      result.renamedFiles += file.kind === "renamed" ? 1 : 0
      result.oversizedFiles += file.oversized ? 1 : 0
      return result
    },
    {
      hunks: 0,
      additions: 0,
      deletions: 0,
      binaryFiles: 0,
      renamedFiles: 0,
      oversizedFiles: 0,
    },
  )
  if (
    totals.files !== files.length
    || totals.hunks !== computed.hunks
    || totals.additions !== computed.additions
    || totals.deletions !== computed.deletions
    || totals.binaryFiles !== computed.binaryFiles
    || totals.renamedFiles !== computed.renamedFiles
    || totals.oversizedFiles !== computed.oversizedFiles
  ) {
    throw new DiffContractError(
      "diff_contract_totals_mismatch",
      "Diff manifest totals disagree with its file records.",
      "$.totals",
      { declared: totals, computed },
    )
  }
  if (source.physical_path_disclosed !== false) {
    literalFalse(source.physical_path_disclosed, "$.physical_path_disclosed")
  }
  if (source.read_only !== true) {
    throw new DiffContractError(
      "diff_contract_manifest_not_read_only",
      "Diff manifest must be read-only.",
      "$.read_only",
    )
  }
  return Object.freeze({
    schema: "zyra.diff-review-manifest.v1",
    diffId: identifierValue(source.diff_id, "$.diff_id"),
    generatedAt: timestampValue(source.generated_at, "$.generated_at"),
    source: binding,
    files: Object.freeze(files),
    totals,
    maximumPageBytes: integerValue(
      source.maximum_page_bytes,
      "$.maximum_page_bytes",
      1_024,
      8 * 1_024 * 1_024,
    ),
    maximumPageLines: integerValue(
      source.maximum_page_lines,
      "$.maximum_page_lines",
      1,
      100_000,
    ),
    physicalPathDisclosed: false,
    readOnly: true,
  })
}

function optionalLineNumber(value: unknown, path: string): number | undefined {
  if (value === undefined || value === null) return undefined
  return integerValue(value, path, 1)
}

function parseLine(
  value: unknown,
  path: string,
): DiffLineContract {
  const source = recordValue(value, path)
  const kind = enumValue<DiffLineKind>(
    source.kind,
    `${path}.kind`,
    ["context", "added", "deleted", "notice"],
  )
  const oldLine = optionalLineNumber(source.old_line, `${path}.old_line`)
  const newLine = optionalLineNumber(source.new_line, `${path}.new_line`)
  if (kind === "added" && oldLine !== undefined) {
    throw new DiffContractError(
      "diff_contract_added_old_line",
      `${path} added line cannot have old_line.`,
      path,
    )
  }
  if (kind === "deleted" && newLine !== undefined) {
    throw new DiffContractError(
      "diff_contract_deleted_new_line",
      `${path} deleted line cannot have new_line.`,
      path,
    )
  }
  if (kind === "context" && (oldLine === undefined || newLine === undefined)) {
    throw new DiffContractError(
      "diff_contract_context_coordinates",
      `${path} context line requires old and new coordinates.`,
      path,
    )
  }
  if (kind === "notice" && (oldLine !== undefined || newLine !== undefined)) {
    throw new DiffContractError(
      "diff_contract_notice_coordinates",
      `${path} notice line cannot have file coordinates.`,
      path,
    )
  }
  return Object.freeze({
    lineId: identifierValue(source.line_id, `${path}.line_id`),
    kind,
    text: textValue(source.text ?? "", `${path}.text`, 2 * 1_024 * 1_024, true),
    oldLine,
    newLine,
    patchLine: integerValue(source.patch_line, `${path}.patch_line`, 1),
    byteOffset: integerValue(source.byte_offset, `${path}.byte_offset`, 0),
    byteLength: integerValue(
      source.byte_length,
      `${path}.byte_length`,
      0,
      2 * 1_024 * 1_024,
    ),
    noNewline: booleanValue(source.no_newline ?? false, `${path}.no_newline`),
  })
}

function parseHunk(
  value: unknown,
  path: string,
  expectedFileId: string,
): DiffHunkContract {
  const source = recordValue(value, path)
  const fileId = identifierValue(source.file_id, `${path}.file_id`)
  if (fileId !== expectedFileId) {
    throw new DiffContractError(
      "diff_contract_hunk_file_mismatch",
      `${path} belongs to another file.`,
      `${path}.file_id`,
      { expected: expectedFileId, actual: fileId },
    )
  }
  const rawLines = arrayValue(source.lines, `${path}.lines`, 100_000)
  const lines = rawLines.map((item, index) =>
    parseLine(item, `${path}.lines[${index}]`))
  const lineIds = new Set<string>()
  let additions = 0
  let deletions = 0
  let contexts = 0
  let lastPatchLine = 0
  let expectedOld = integerValue(source.old_start, `${path}.old_start`, 0)
  let expectedNew = integerValue(source.new_start, `${path}.new_start`, 0)
  for (const [index, line] of lines.entries()) {
    if (lineIds.has(line.lineId)) {
      throw new DiffContractError(
        "diff_contract_duplicate_line",
        `${path} repeats line identity ${line.lineId}.`,
        `${path}.lines[${index}].line_id`,
      )
    }
    if (line.patchLine <= lastPatchLine) {
      throw new DiffContractError(
        "diff_contract_patch_line_order",
        `${path} patch line coordinates must increase.`,
        `${path}.lines[${index}].patch_line`,
      )
    }
    if (line.oldLine !== undefined && line.oldLine !== expectedOld) {
      throw new DiffContractError(
        "diff_contract_old_line_gap",
        `${path} old coordinates are not contiguous.`,
        `${path}.lines[${index}].old_line`,
        { expected: expectedOld, actual: line.oldLine },
      )
    }
    if (line.newLine !== undefined && line.newLine !== expectedNew) {
      throw new DiffContractError(
        "diff_contract_new_line_gap",
        `${path} new coordinates are not contiguous.`,
        `${path}.lines[${index}].new_line`,
        { expected: expectedNew, actual: line.newLine },
      )
    }
    if (line.kind === "added") additions += 1
    if (line.kind === "deleted") deletions += 1
    if (line.kind === "context") contexts += 1
    if (line.oldLine !== undefined) expectedOld += 1
    if (line.newLine !== undefined) expectedNew += 1
    lastPatchLine = line.patchLine
    lineIds.add(line.lineId)
  }
  const oldCount = integerValue(source.old_count, `${path}.old_count`, 0)
  const newCount = integerValue(source.new_count, `${path}.new_count`, 0)
  if (expectedOld - integerValue(source.old_start, `${path}.old_start`, 0) !== oldCount) {
    throw new DiffContractError(
      "diff_contract_old_count_mismatch",
      `${path} old_count disagrees with its lines.`,
      `${path}.old_count`,
    )
  }
  if (expectedNew - integerValue(source.new_start, `${path}.new_start`, 0) !== newCount) {
    throw new DiffContractError(
      "diff_contract_new_count_mismatch",
      `${path} new_count disagrees with its lines.`,
      `${path}.new_count`,
    )
  }
  if (
    integerValue(source.additions, `${path}.additions`, 0) !== additions
    || integerValue(source.deletions, `${path}.deletions`, 0) !== deletions
    || integerValue(source.context_lines, `${path}.context_lines`, 0) !== contexts
  ) {
    throw new DiffContractError(
      "diff_contract_hunk_totals_mismatch",
      `${path} hunk totals disagree with line kinds.`,
      path,
    )
  }
  return Object.freeze({
    hunkId: identifierValue(source.hunk_id, `${path}.hunk_id`),
    fileId,
    index: integerValue(source.index, `${path}.index`, 0),
    header: textValue(source.header, `${path}.header`, 32 * 1_024),
    section: textValue(source.section ?? "", `${path}.section`, 32 * 1_024, true),
    oldStart: integerValue(source.old_start, `${path}.old_start`, 0),
    oldCount,
    newStart: integerValue(source.new_start, `${path}.new_start`, 0),
    newCount,
    additions,
    deletions,
    contextLines: contexts,
    patchStart: integerValue(source.patch_start, `${path}.patch_start`, 0),
    patchEnd: integerValue(source.patch_end, `${path}.patch_end`, 0),
    lines: Object.freeze(lines),
  })
}

export function parseDiffPage(
  value: unknown,
  manifest: DiffManifestContract,
): DiffPageContract {
  const source = recordValue(value, "$")
  if (source.schema !== "zyra.diff-review-page.v1") {
    throw new DiffContractError(
      "diff_contract_schema_mismatch",
      "Diff page schema is not supported.",
      "$.schema",
    )
  }
  const diffId = identifierValue(source.diff_id, "$.diff_id")
  if (diffId !== manifest.diffId) {
    throw new DiffContractError(
      "diff_contract_diff_identity_mismatch",
      "Diff page belongs to another manifest.",
      "$.diff_id",
    )
  }
  const fileId = identifierValue(source.file_id, "$.file_id")
  const file = manifest.files.find((candidate) => candidate.fileId === fileId)
  if (!file) {
    throw new DiffContractError(
      "diff_contract_unknown_file",
      "Diff page references an unknown file.",
      "$.file_id",
    )
  }
  const artifactRevision = identifierValue(
    source.artifact_revision,
    "$.artifact_revision",
  )
  if (artifactRevision !== manifest.source.artifactRevision) {
    throw new DiffContractError(
      "diff_contract_revision_mismatch",
      "Diff page artifact revision is stale or cross-bound.",
      "$.artifact_revision",
    )
  }
  const rawHunks = arrayValue(source.hunks, "$.hunks", 10_000)
  const hunks = rawHunks.map((item, index) =>
    parseHunk(item, `$.hunks[${index}]`, fileId))
  let previousIndex = -1
  const hunkIds = new Set<string>()
  for (const [index, hunk] of hunks.entries()) {
    if (hunk.index <= previousIndex) {
      throw new DiffContractError(
        "diff_contract_hunk_order",
        "Hunks must be strictly ordered.",
        `$.hunks[${index}].index`,
      )
    }
    if (hunkIds.has(hunk.hunkId)) {
      throw new DiffContractError(
        "diff_contract_duplicate_hunk",
        `Duplicate hunk identity ${hunk.hunkId}.`,
        `$.hunks[${index}].hunk_id`,
      )
    }
    previousIndex = hunk.index
    hunkIds.add(hunk.hunkId)
  }
  const pageIndex = integerValue(source.page_index, "$.page_index", 0)
  const pageCount = integerValue(source.page_count, "$.page_count", 0)
  if (pageCount !== file.pageCount || pageIndex >= Math.max(1, pageCount)) {
    throw new DiffContractError(
      "diff_contract_page_count_mismatch",
      "Diff page coordinates disagree with the manifest.",
      "$.page_count",
    )
  }
  const hunkStart = integerValue(source.hunk_start, "$.hunk_start", 0)
  const hunkEnd = integerValue(source.hunk_end, "$.hunk_end", hunkStart)
  if (
    hunks.length > 0
    && (hunks[0]!.index !== hunkStart || hunks[hunks.length - 1]!.index + 1 !== hunkEnd)
  ) {
    throw new DiffContractError(
      "diff_contract_hunk_page_range",
      "Diff page hunk range disagrees with its contents.",
      "$.hunk_start",
    )
  }
  const utf8Bytes = integerValue(
    source.utf8_bytes,
    "$.utf8_bytes",
    0,
    manifest.maximumPageBytes,
  )
  if (source.physical_path_disclosed !== false) {
    literalFalse(source.physical_path_disclosed, "$.physical_path_disclosed")
  }
  return Object.freeze({
    schema: "zyra.diff-review-page.v1",
    diffId,
    fileId,
    artifactRevision,
    pageIndex,
    pageCount,
    cursor: receiptValue(source.cursor, "$.cursor"),
    nextCursor: optionalText(source.next_cursor, "$.next_cursor", 8 * 1_024),
    complete: booleanValue(source.complete, "$.complete"),
    hunkStart,
    hunkEnd,
    utf8Bytes,
    contentDigest: sha256Value(source.content_digest, "$.content_digest"),
    hunks: Object.freeze(hunks),
    receiptId: receiptValue(source.receipt_id, "$.receipt_id"),
    generatedAt: timestampValue(source.generated_at, "$.generated_at"),
    physicalPathDisclosed: false,
  })
}

export function parseDiffFileContent(
  value: unknown,
  manifest: DiffManifestContract,
  expectedFileId: string,
  expectedVersion: "base" | "current",
): DiffFileContentContract {
  const source = recordValue(value, "$")
  if (source.schema !== "zyra.diff-review-file-content.v1") {
    throw new DiffContractError(
      "diff_contract_schema_mismatch",
      "Diff file content schema is not supported.",
      "$.schema",
    )
  }
  const diffId = identifierValue(source.diff_id, "$.diff_id")
  const fileId = identifierValue(source.file_id, "$.file_id")
  const artifactRevision = identifierValue(
    source.artifact_revision,
    "$.artifact_revision",
  )
  const version = enumValue(
    source.version,
    "$.version",
    ["base", "current"],
  )
  if (
    diffId !== manifest.diffId
    || fileId !== expectedFileId
    || artifactRevision !== manifest.source.artifactRevision
    || version !== expectedVersion
  ) {
    throw new DiffContractError(
      "diff_contract_file_content_binding",
      "Diff file content does not match its request.",
      "$",
      {
        expected: {
          diffId: manifest.diffId,
          fileId: expectedFileId,
          artifactRevision: manifest.source.artifactRevision,
          version: expectedVersion,
        },
        actual: { diffId, fileId, artifactRevision, version },
      },
    )
  }
  const text = textValue(
    source.text ?? "",
    "$.text",
    64 * 1_024 * 1_024,
    true,
  )
  const utf8Bytes = integerValue(
    source.utf8_bytes,
    "$.utf8_bytes",
    0,
    64 * 1_024 * 1_024,
  )
  if (new TextEncoder().encode(text).byteLength !== utf8Bytes) {
    throw new DiffContractError(
      "diff_contract_file_content_size",
      "Diff file content byte count does not match its text.",
      "$.utf8_bytes",
    )
  }
  if (source.complete !== true) {
    throw new DiffContractError(
      "diff_contract_file_content_partial",
      "Patch preflight requires complete file content.",
      "$.complete",
    )
  }
  if (source.physical_path_disclosed !== false) {
    literalFalse(source.physical_path_disclosed, "$.physical_path_disclosed")
  }
  return Object.freeze({
    schema: "zyra.diff-review-file-content.v1",
    diffId,
    fileId,
    artifactRevision,
    version,
    text,
    sha256: sha256Value(source.sha256, "$.sha256"),
    mtimeNs: integerValue(source.mtime_ns, "$.mtime_ns", 0),
    mode: integerValue(source.mode, "$.mode", 0, 0o7777),
    encoding: textValue(source.encoding, "$.encoding", 128),
    lineEnding: enumValue(
      source.line_ending,
      "$.line_ending",
      ["lf", "crlf", "cr", "mixed", "none"],
    ),
    complete: true,
    utf8Bytes,
    receiptId: receiptValue(source.receipt_id, "$.receipt_id"),
    generatedAt: timestampValue(source.generated_at, "$.generated_at"),
    physicalPathDisclosed: false,
  })
}

export function parseLineSelection(
  value: unknown,
  path = "$",
): DiffReviewLineSelection {
  const source = recordValue(value, path)
  const startLine = integerValue(source.start_line, `${path}.start_line`, 1)
  const endLine = integerValue(source.end_line, `${path}.end_line`, startLine)
  const anchorLineIds = arrayValue(
    source.anchor_line_ids,
    `${path}.anchor_line_ids`,
    10_000,
  ).map((item, index) =>
    identifierValue(item, `${path}.anchor_line_ids[${index}]`))
  if (anchorLineIds.length !== endLine - startLine + 1) {
    throw new DiffContractError(
      "diff_contract_selection_anchor_count",
      `${path} selection coordinates disagree with anchor count.`,
      path,
    )
  }
  if (new Set(anchorLineIds).size !== anchorLineIds.length) {
    throw new DiffContractError(
      "diff_contract_selection_duplicate_anchor",
      `${path} selection repeats an anchor.`,
      path,
    )
  }
  return Object.freeze({
    fileId: identifierValue(source.file_id, `${path}.file_id`),
    hunkId: identifierValue(source.hunk_id, `${path}.hunk_id`),
    side: enumValue(source.side, `${path}.side`, ["old", "new"]),
    startLine,
    endLine,
    anchorLineIds: Object.freeze(anchorLineIds),
  })
}

function parseReviewComment(value: unknown, path: string): DiffReviewComment {
  const source = recordValue(value, path)
  return Object.freeze({
    commentId: identifierValue(source.comment_id, `${path}.comment_id`),
    diffId: identifierValue(source.diff_id, `${path}.diff_id`),
    fileId: identifierValue(source.file_id, `${path}.file_id`),
    hunkId: identifierValue(source.hunk_id, `${path}.hunk_id`),
    selection: parseLineSelection(source.selection, `${path}.selection`),
    body: textValue(source.body, `${path}.body`, 64 * 1_024),
    authorId: identifierValue(source.author_id, `${path}.author_id`),
    createdAt: timestampValue(source.created_at, `${path}.created_at`),
    updatedAt: timestampValue(source.updated_at, `${path}.updated_at`),
    state: enumValue(
      source.state,
      `${path}.state`,
      ["draft", "submitted", "resolved"],
    ),
    causationId: identifierValue(source.causation_id, `${path}.causation_id`),
    revision: integerValue(source.revision, `${path}.revision`, 1),
  })
}

export function parseReviewReceipt(value: unknown): DiffReviewReceipt {
  const source = recordValue(value, "$")
  if (source.schema !== "zyra.diff-review-receipt.v1") {
    throw new DiffContractError(
      "diff_contract_schema_mismatch",
      "Review receipt schema is not supported.",
      "$.schema",
    )
  }
  const action = enumValue<DiffReviewReceipt["action"]>(
    source.action,
    "$.action",
    ["select", "comment", "comment_update", "comment_resolve"],
  )
  const comment = source.comment
    ? parseReviewComment(source.comment, "$.comment")
    : undefined
  const selection = source.selection
    ? parseLineSelection(source.selection, "$.selection")
    : undefined
  const accepted = booleanValue(source.accepted, "$.accepted")
  if (accepted && action === "select" && !selection) {
    throw new DiffContractError(
      "diff_contract_selection_receipt_missing",
      "Selection receipt must include its selection.",
      "$.selection",
    )
  }
  if (accepted && action !== "select" && !comment) {
    throw new DiffContractError(
      "diff_contract_comment_receipt_missing",
      "Comment receipt must include its comment.",
      "$.comment",
    )
  }
  const eventIds = arrayValue(source.event_ids ?? [], "$.event_ids", 1_000)
    .map((item, index) => identifierValue(item, `$.event_ids[${index}]`))
  const humanInterventionCount = integerValue(
    source.human_intervention_count,
    "$.human_intervention_count",
    0,
  )
  if (humanInterventionCount !== 0) {
    throw new DiffContractError(
      "diff_contract_human_intervention_nonzero",
      "Diff review receipts must preserve zero human intervention.",
      "$.human_intervention_count",
    )
  }
  return Object.freeze({
    schema: "zyra.diff-review-receipt.v1",
    receiptId: receiptValue(source.receipt_id, "$.receipt_id"),
    diffId: identifierValue(source.diff_id, "$.diff_id"),
    taskId: identifierValue(source.task_id, "$.task_id"),
    action,
    accepted,
    causationId: identifierValue(source.causation_id, "$.causation_id"),
    eventIds: Object.freeze(eventIds),
    comment,
    selection,
    revision: integerValue(source.revision, "$.revision", 0),
    permission: parsePermission(source.permission, "$.permission"),
    humanInterventionCount: 0,
    deniedManualMutationCount: integerValue(
      source.denied_manual_mutation_count,
      "$.denied_manual_mutation_count",
      0,
    ),
    createdAt: timestampValue(source.created_at, "$.created_at"),
  })
}

function parsePermission(
  value: unknown,
  path: string,
): PatchPermissionProjection {
  const source = recordValue(value, path)
  if (source.canonical_owner !== "typescript.PermissionCoordinator") {
    throw new DiffContractError(
      "diff_contract_permission_owner",
      "Patch permission receipt is not TypeScript-owned.",
      `${path}.canonical_owner`,
    )
  }
  const effect = enumValue(
    source.effect,
    `${path}.effect`,
    ["allow", "ask", "deny"],
  )
  const pending = booleanValue(source.pending, `${path}.pending`)
  if (pending !== (effect === "ask")) {
    throw new DiffContractError(
      "diff_contract_permission_pending_mismatch",
      "Permission pending flag disagrees with effect.",
      `${path}.pending`,
    )
  }
  const recovery = source.recovery_input
  return Object.freeze({
    canonicalOwner: "typescript.PermissionCoordinator",
    effect,
    decisionId: identifierValue(source.decision_id, `${path}.decision_id`),
    requestId: optionalText(source.request_id, `${path}.request_id`, 512),
    permitId: optionalText(source.permit_id, `${path}.permit_id`, 512),
    reasonCode: identifierValue(source.reason_code, `${path}.reason_code`),
    policyRevision: integerValue(
      source.policy_revision,
      `${path}.policy_revision`,
      0,
    ),
    modeRevision: integerValue(
      source.mode_revision,
      `${path}.mode_revision`,
      0,
    ),
    requestFingerprint: identifierValue(
      source.request_fingerprint,
      `${path}.request_fingerprint`,
    ),
    pending,
    recoveryInput: recovery
      ? Object.freeze({ ...recordValue(recovery, `${path}.recovery_input`) })
      : undefined,
  })
}

function parsePathResult(value: unknown, path: string): PatchPathResult {
  const source = recordValue(value, path)
  return Object.freeze({
    fileId: identifierValue(source.file_id, `${path}.file_id`),
    path: logicalPathValue(source.path, `${path}.path`),
    disposition: identifierValue(source.disposition, `${path}.disposition`),
    beforeSha256: sha256Value(
      source.before_sha256,
      `${path}.before_sha256`,
      true,
    ),
    afterSha256: sha256Value(
      source.after_sha256,
      `${path}.after_sha256`,
      true,
    ),
    bytesBefore: integerValue(source.bytes_before, `${path}.bytes_before`, 0),
    bytesAfter: integerValue(source.bytes_after, `${path}.bytes_after`, 0),
    verified: booleanValue(source.verified, `${path}.verified`),
  })
}

function identityList(
  value: unknown,
  path: string,
  maximum = 10_000,
): readonly string[] {
  return Object.freeze(
    arrayValue(value ?? [], path, maximum).map((item, index) =>
      identifierValue(item, `${path}[${index}]`)),
  )
}

export function parsePatchTransactionReceipt(
  value: unknown,
  expected: {
    taskId: string
    runId: string
    diffId: string
    causationId: string
  },
): PatchTransactionReceipt {
  const source = recordValue(value, "$")
  if (source.schema !== "zyra.patch-review-transaction-receipt.v1") {
    throw new DiffContractError(
      "diff_contract_schema_mismatch",
      "Patch transaction receipt schema is not supported.",
      "$.schema",
    )
  }
  const taskId = identifierValue(source.task_id, "$.task_id")
  const runId = identifierValue(source.run_id, "$.run_id")
  const diffId = identifierValue(source.diff_id, "$.diff_id")
  const causationId = identifierValue(source.causation_id, "$.causation_id")
  if (
    taskId !== expected.taskId
    || runId !== expected.runId
    || diffId !== expected.diffId
    || causationId !== expected.causationId
  ) {
    throw new DiffContractError(
      "diff_contract_transaction_binding_mismatch",
      "Patch receipt identity does not match its request.",
      "$",
      {
        expected,
        actual: { taskId, runId, diffId, causationId },
      },
    )
  }
  const phase = enumValue<PatchTransactionPhase>(
    source.phase,
    "$.phase",
    [
      "preflight",
      "permission_pending",
      "permission_denied",
      "stale",
      "conflicted",
      "committed",
      "rolled_back",
      "rollback_failed",
      "quarantined",
      "cancelled",
      "failed",
    ],
  )
  const accepted = booleanValue(source.accepted, "$.accepted")
  const committed = booleanValue(source.committed, "$.committed")
  const stale = booleanValue(source.stale, "$.stale")
  const conflict = booleanValue(source.conflict, "$.conflict")
  const rolledBack = booleanValue(source.rolled_back, "$.rolled_back")
  const rollbackFailed = booleanValue(
    source.rollback_failed,
    "$.rollback_failed",
  )
  if (committed !== (phase === "committed")) {
    throw new DiffContractError(
      "diff_contract_commit_phase_mismatch",
      "Patch receipt commit flag disagrees with phase.",
      "$.committed",
    )
  }
  if (stale !== (phase === "stale")) {
    throw new DiffContractError(
      "diff_contract_stale_phase_mismatch",
      "Patch receipt stale flag disagrees with phase.",
      "$.stale",
    )
  }
  if (conflict !== (phase === "conflicted")) {
    throw new DiffContractError(
      "diff_contract_conflict_phase_mismatch",
      "Patch receipt conflict flag disagrees with phase.",
      "$.conflict",
    )
  }
  if (rolledBack !== (phase === "rolled_back")) {
    throw new DiffContractError(
      "diff_contract_rollback_phase_mismatch",
      "Patch receipt rollback flag disagrees with phase.",
      "$.rolled_back",
    )
  }
  if (rollbackFailed !== (phase === "rollback_failed" || phase === "quarantined")) {
    throw new DiffContractError(
      "diff_contract_rollback_failure_phase_mismatch",
      "Patch receipt rollback-failure flag disagrees with phase.",
      "$.rollback_failed",
    )
  }
  const humanInterventionCount = integerValue(
    source.human_intervention_count,
    "$.human_intervention_count",
    0,
    0,
  )
  if (humanInterventionCount !== 0) {
    throw new DiffContractError(
      "diff_contract_human_intervention",
      "Patch receipt cannot report human intervention.",
      "$.human_intervention_count",
    )
  }
  const pathResults = arrayValue(
    source.path_results ?? [],
    "$.path_results",
    10_000,
  ).map((item, index) => parsePathResult(item, `$.path_results[${index}]`))
  const fileIds = new Set<string>()
  for (const [index, result] of pathResults.entries()) {
    if (fileIds.has(result.fileId)) {
      throw new DiffContractError(
        "diff_contract_duplicate_path_result",
        `Patch receipt repeats file ${result.fileId}.`,
        `$.path_results[${index}].file_id`,
      )
    }
    fileIds.add(result.fileId)
  }
  if (committed && pathResults.some((result) => !result.verified)) {
    throw new DiffContractError(
      "diff_contract_unverified_commit",
      "Committed patch receipt contains an unverified path.",
      "$.path_results",
    )
  }
  return Object.freeze({
    schema: "zyra.patch-review-transaction-receipt.v1",
    receiptId: receiptValue(source.receipt_id, "$.receipt_id"),
    taskId,
    runId,
    diffId,
    transactionId: identifierValue(source.transaction_id, "$.transaction_id"),
    phase,
    accepted,
    committed,
    idempotentReplay: booleanValue(
      source.idempotent_replay,
      "$.idempotent_replay",
    ),
    stale,
    conflict,
    rolledBack,
    rollbackFailed,
    sealed: booleanValue(source.sealed, "$.sealed"),
    humanInterventionCount: 0,
    deniedManualMutationCount: integerValue(
      source.denied_manual_mutation_count,
      "$.denied_manual_mutation_count",
      0,
    ),
    workspaceId: identifierValue(source.workspace_id, "$.workspace_id"),
    ownerEpochBefore: integerValue(
      source.owner_epoch_before,
      "$.owner_epoch_before",
      0,
    ),
    ownerEpochAfter: integerValue(
      source.owner_epoch_after,
      "$.owner_epoch_after",
      0,
    ),
    bindingRevisionBefore: integerValue(
      source.binding_revision_before,
      "$.binding_revision_before",
      0,
    ),
    bindingRevisionAfter: integerValue(
      source.binding_revision_after,
      "$.binding_revision_after",
      0,
    ),
    snapshotId: String(source.snapshot_id ?? ""),
    recoveryInputId: String(source.recovery_input_id ?? ""),
    reasonCode: identifierValue(source.reason_code, "$.reason_code"),
    message: textValue(source.message, "$.message", 64 * 1_024, true),
    pathResults: Object.freeze(pathResults),
    artifactRefs: identityList(source.artifact_refs, "$.artifact_refs"),
    eventIds: identityList(source.event_ids, "$.event_ids"),
    verificationRefs: identityList(
      source.verification_refs,
      "$.verification_refs",
    ),
    terminalRefs: identityList(source.terminal_refs, "$.terminal_refs"),
    timelineRefs: identityList(source.timeline_refs, "$.timeline_refs"),
    permission: parsePermission(source.permission, "$.permission"),
    causationId,
    createdAt: timestampValue(source.created_at, "$.created_at"),
  })
}

export function assertManifestBinding(
  manifest: DiffManifestContract,
  expected: {
    taskId: string
    artifactId: string
    artifactRevision?: string
  },
): void {
  if (manifest.source.taskId !== expected.taskId) {
    throw new DiffContractError(
      "diff_contract_task_binding_mismatch",
      "Diff manifest belongs to another task.",
      "$.source.task_id",
    )
  }
  if (manifest.source.artifactId !== expected.artifactId) {
    throw new DiffContractError(
      "diff_contract_artifact_binding_mismatch",
      "Diff manifest belongs to another artifact.",
      "$.source.artifact_id",
    )
  }
  if (
    expected.artifactRevision
    && manifest.source.artifactRevision !== expected.artifactRevision
  ) {
    throw new DiffContractError(
      "diff_contract_revision_mismatch",
      "Diff manifest revision does not match the selected artifact.",
      "$.source.artifact_revision",
    )
  }
}

export function assertPageContinuation(
  previous: DiffPageContract | undefined,
  current: DiffPageContract,
): void {
  if (!previous) {
    if (current.pageIndex !== 0 || current.hunkStart !== 0) {
      throw new DiffContractError(
        "diff_contract_first_page_gap",
        "First diff page must start at page and hunk zero.",
        "$.page_index",
      )
    }
    return
  }
  if (
    current.diffId !== previous.diffId
    || current.fileId !== previous.fileId
    || current.artifactRevision !== previous.artifactRevision
  ) {
    throw new DiffContractError(
      "diff_contract_page_binding_mismatch",
      "Diff page continuation changed identity.",
      "$",
    )
  }
  if (
    current.pageIndex !== previous.pageIndex + 1
    || current.hunkStart !== previous.hunkEnd
  ) {
    throw new DiffContractError(
      "diff_contract_page_gap",
      "Diff page continuation has a gap or overlap.",
      "$.page_index",
      {
        previousPage: previous.pageIndex,
        currentPage: current.pageIndex,
        previousHunkEnd: previous.hunkEnd,
        currentHunkStart: current.hunkStart,
      },
    )
  }
  if (!previous.nextCursor || current.cursor !== previous.nextCursor) {
    throw new DiffContractError(
      "diff_contract_cursor_mismatch",
      "Diff page continuation cursor does not match.",
      "$.cursor",
    )
  }
}

export function freezeApplyRequest(
  value: PatchApplyRequest,
): PatchApplyRequest {
  if (value.schema !== "zyra.patch-review-apply.v1") {
    throw new DiffContractError(
      "diff_contract_apply_schema",
      "Patch apply request schema is not supported.",
    )
  }
  const selected = [...value.selectedFileIds]
  if (!selected.length || new Set(selected).size !== selected.length) {
    throw new DiffContractError(
      "diff_contract_apply_selection",
      "Patch apply request requires unique selected files.",
    )
  }
  const preconditions = value.preconditions.map((item) =>
    Object.freeze({ ...item }))
  const byFile = new Map(preconditions.map((item) => [item.fileId, item]))
  for (const fileId of selected) {
    if (!byFile.has(fileId)) {
      throw new DiffContractError(
        "diff_contract_apply_precondition_missing",
        `Selected file ${fileId} has no precondition.`,
      )
    }
  }
  return Object.freeze({
    ...value,
    selectedFileIds: Object.freeze(selected),
    preconditions: Object.freeze(preconditions),
  })
}

export function transactionIsTerminal(
  phase: PatchTransactionPhase,
): boolean {
  return new Set<PatchTransactionPhase>([
    "permission_denied",
    "stale",
    "conflicted",
    "committed",
    "rolled_back",
    "rollback_failed",
    "quarantined",
    "cancelled",
    "failed",
  ]).has(phase)
}

export function transactionCanRetry(
  receipt: PatchTransactionReceipt,
): boolean {
  if (receipt.committed || receipt.rolledBack || receipt.rollbackFailed) {
    return false
  }
  if (receipt.phase === "permission_pending") return true
  if (receipt.phase === "stale" || receipt.phase === "conflicted") return false
  return receipt.permission.effect !== "deny"
}
