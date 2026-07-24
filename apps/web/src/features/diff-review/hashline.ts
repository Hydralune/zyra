import type {
  DiffFileContract,
  DiffHunkContract,
  PatchFilePrecondition,
} from "./contracts.ts"
import {
  applyFileHunks,
  compactHunkPreview,
  type PatchApplication,
} from "./unified-diff.ts"
import {
  threeWayMerge,
  type ThreeWayMergeResult,
} from "./merge.ts"

export interface DiffSnapshot {
  snapshotId: string
  fileId: string
  path: string
  text: string
  sha256: string
  mtimeNs: number
  mode: number
  encoding: string
  lineEnding: string
  observedLineIds: ReadonlySet<string>
  observedAt: number
  bytes: number
}

export interface DiffSnapshotOptions {
  maximumFiles?: number
  maximumVersionsPerFile?: number
  maximumBytes?: number
}

export interface HashlinePreflightInput {
  file: DiffFileContract
  hunks: readonly DiffHunkContract[]
  baseSnapshot: DiffSnapshot
  current: {
    text: string
    sha256: string
    mtimeNs: number
    mode: number
  }
  allowThreeWay: boolean
}

export interface HashlinePreflightReceipt {
  receiptId: string
  fileId: string
  path: string
  state:
    | "clean"
    | "stale"
    | "conflicted"
    | "binary_rejected"
    | "unobserved_rejected"
    | "apply_failed"
  accepted: boolean
  baseSha256: string
  currentSha256: string
  proposedSha256: string
  baseMtimeNs: number
  currentMtimeNs: number
  baseMode: number
  currentMode: number
  application?: PatchApplication
  merge?: ThreeWayMergeResult
  precondition?: PatchFilePrecondition
  previews: readonly string[]
  reasonCode: string
  message: string
  createdAt: number
}

export class HashlinePreflightError extends Error {
  readonly code: string
  readonly details: Readonly<Record<string, unknown>>

  constructor(
    code: string,
    message: string,
    details: Readonly<Record<string, unknown>> = {},
  ) {
    super(message)
    this.name = "HashlinePreflightError"
    this.code = code
    this.details = Object.freeze({ ...details })
  }
}

const encoder = new TextEncoder()

function bounded(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
  name: string,
): number {
  if (value === undefined) return fallback
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new HashlinePreflightError(
      "hashline_option_invalid",
      `${name} must be an integer from ${minimum} through ${maximum}.`,
      { actual: value },
    )
  }
  return value
}

function hex(bytes: Uint8Array): string {
  let result = ""
  for (const byte of bytes) result += byte.toString(16).padStart(2, "0")
  return result
}

export async function contentSha256(value: string | Uint8Array): Promise<string> {
  const bytes = typeof value === "string" ? encoder.encode(value) : value
  const digest = await crypto.subtle.digest("SHA-256", bytes)
  return `sha256:${hex(new Uint8Array(digest))}`
}

function stableIdentity(kind: string, values: readonly unknown[]): string {
  const text = JSON.stringify(values)
  let left = 0x811c9dc5
  let right = 0x01000193
  for (let index = 0; index < text.length; index += 1) {
    left ^= text.charCodeAt(index)
    left = Math.imul(left, 0x01000193)
    right ^= text.charCodeAt(text.length - index - 1)
    right = Math.imul(right, 0x85ebca6b)
  }
  return `${kind}:${(left >>> 0).toString(16).padStart(8, "0")}${(right >>> 0).toString(16).padStart(8, "0")}`
}

function normalizedDigest(value: string, name: string): string {
  const digest = String(value || "").toLowerCase()
  if (!/^(?:sha256:)?[a-f0-9]{64}$/.test(digest)) {
    throw new HashlinePreflightError(
      "hashline_digest_invalid",
      `${name} must be a SHA-256 digest.`,
      { value },
    )
  }
  return digest.startsWith("sha256:") ? digest : `sha256:${digest}`
}

function safeTimestamp(value: number, name: string): number {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new HashlinePreflightError(
      "hashline_timestamp_invalid",
      `${name} must be a non-negative safe integer.`,
      { value },
    )
  }
  return value
}

function safeMode(value: number, name: string): number {
  if (!Number.isSafeInteger(value) || value < 0 || value > 0o7777) {
    throw new HashlinePreflightError(
      "hashline_mode_invalid",
      `${name} must be a valid file mode.`,
      { value },
    )
  }
  return value
}

function snapshotKey(fileId: string, sha256: string): string {
  return `${fileId}:${sha256}`
}

export class DiffSnapshotStore {
  readonly #maximumFiles: number
  readonly #maximumVersionsPerFile: number
  readonly #maximumBytes: number
  readonly #versions = new Map<string, DiffSnapshot[]>()
  #bytes = 0
  #closed = false

  constructor(options: DiffSnapshotOptions = {}) {
    this.#maximumFiles = bounded(
      options.maximumFiles,
      64,
      1,
      10_000,
      "maximumFiles",
    )
    this.#maximumVersionsPerFile = bounded(
      options.maximumVersionsPerFile,
      4,
      1,
      64,
      "maximumVersionsPerFile",
    )
    this.#maximumBytes = bounded(
      options.maximumBytes,
      64 * 1024 * 1024,
      64 * 1024,
      2 * 1024 * 1024 * 1024,
      "maximumBytes",
    )
  }

  async record(input: {
    fileId: string
    path: string
    text: string
    sha256?: string
    mtimeNs: number
    mode: number
    encoding: string
    lineEnding: string
    observedLineIds?: Iterable<string>
    observedAt?: number
  }): Promise<DiffSnapshot> {
    this.#requireOpen()
    const fileId = String(input.fileId || "").trim()
    const path = String(input.path || "").trim()
    if (!fileId || !path) {
      throw new HashlinePreflightError(
        "hashline_snapshot_identity_missing",
        "Snapshot requires file and path identity.",
      )
    }
    const bytes = encoder.encode(input.text).byteLength
    if (bytes > this.#maximumBytes) {
      throw new HashlinePreflightError(
        "hashline_snapshot_too_large",
        "A single snapshot exceeds the complete snapshot budget.",
        { bytes, maximum: this.#maximumBytes },
      )
    }
    const sha256 = input.sha256
      ? normalizedDigest(input.sha256, "sha256")
      : await contentSha256(input.text)
    const observedLineIds = new Set(
      [...(input.observedLineIds ?? [])]
        .map((value) => String(value || "").trim())
        .filter(Boolean),
    )
    const history = this.#versions.get(fileId) ?? []
    const existing = history.find(
      (snapshot) => snapshot.sha256 === sha256 && snapshot.text === input.text,
    )
    if (existing) {
      const merged = new Set([
        ...existing.observedLineIds,
        ...observedLineIds,
      ])
      const refreshed = Object.freeze({
        ...existing,
        observedLineIds: merged,
        observedAt: input.observedAt ?? Date.now(),
      })
      const next = [
        refreshed,
        ...history.filter((snapshot) => snapshot !== existing),
      ]
      this.#versions.delete(fileId)
      this.#versions.set(fileId, next)
      return refreshed
    }
    const snapshot: DiffSnapshot = Object.freeze({
      snapshotId: stableIdentity("diff-snapshot", [
        fileId,
        path,
        sha256,
        input.mtimeNs,
        input.mode,
      ]),
      fileId,
      path,
      text: input.text,
      sha256,
      mtimeNs: safeTimestamp(input.mtimeNs, "mtimeNs"),
      mode: safeMode(input.mode, "mode"),
      encoding: String(input.encoding || "utf-8"),
      lineEnding: String(input.lineEnding || "unknown"),
      observedLineIds,
      observedAt: input.observedAt ?? Date.now(),
      bytes,
    })
    while (this.#versions.size >= this.#maximumFiles && !this.#versions.has(fileId)) {
      const oldestFile = this.#versions.keys().next().value as string | undefined
      if (!oldestFile) break
      this.invalidate(oldestFile)
    }
    const next = [snapshot, ...history].slice(0, this.#maximumVersionsPerFile)
    const removed = history.filter((candidate) => !next.includes(candidate))
    this.#bytes -= removed.reduce((total, candidate) => total + candidate.bytes, 0)
    this.#versions.delete(fileId)
    this.#versions.set(fileId, next)
    this.#bytes += bytes
    this.#evictToBudget(fileId)
    return snapshot
  }

  head(fileId: string): DiffSnapshot | undefined {
    const history = this.#versions.get(fileId)
    if (!history?.length) return undefined
    this.#versions.delete(fileId)
    this.#versions.set(fileId, history)
    return history[0]
  }

  bySha256(fileId: string, sha256Value: string): DiffSnapshot | undefined {
    const sha256 = normalizedDigest(sha256Value, "sha256")
    const history = this.#versions.get(fileId)
    const snapshot = history?.find((candidate) => candidate.sha256 === sha256)
    if (snapshot && history) {
      this.#versions.delete(fileId)
      this.#versions.set(fileId, history)
    }
    return snapshot
  }

  byIdentity(snapshotId: string): DiffSnapshot | undefined {
    for (const history of this.#versions.values()) {
      const snapshot = history.find(
        (candidate) => candidate.snapshotId === snapshotId,
      )
      if (snapshot) return snapshot
    }
    return undefined
  }

  observe(
    fileId: string,
    sha256Value: string,
    lineIds: Iterable<string>,
  ): DiffSnapshot | undefined {
    const snapshot = this.bySha256(fileId, sha256Value)
    if (!snapshot) return undefined
    const history = this.#versions.get(fileId)!
    const observedLineIds = new Set([
      ...snapshot.observedLineIds,
      ...[...lineIds].map(String),
    ])
    const updated = Object.freeze({ ...snapshot, observedLineIds })
    this.#versions.set(
      fileId,
      history.map((candidate) =>
        candidate.snapshotId === snapshot.snapshotId ? updated : candidate),
    )
    return updated
  }

  invalidate(fileId: string): void {
    const history = this.#versions.get(fileId)
    if (!history) return
    this.#bytes -= history.reduce(
      (total, snapshot) => total + snapshot.bytes,
      0,
    )
    this.#versions.delete(fileId)
  }

  clear(): void {
    this.#versions.clear()
    this.#bytes = 0
  }

  close(): void {
    if (this.#closed) return
    this.#closed = true
    this.clear()
  }

  snapshot(): {
    files: number
    versions: number
    bytes: number
    maximumBytes: number
    keys: readonly string[]
  } {
    const keys: string[] = []
    let versions = 0
    for (const [fileId, history] of this.#versions) {
      versions += history.length
      for (const snapshot of history) {
        keys.push(snapshotKey(fileId, snapshot.sha256))
      }
    }
    return Object.freeze({
      files: this.#versions.size,
      versions,
      bytes: this.#bytes,
      maximumBytes: this.#maximumBytes,
      keys: Object.freeze(keys),
    })
  }

  #evictToBudget(protectedFileId: string): void {
    while (this.#bytes > this.#maximumBytes && this.#versions.size > 1) {
      const candidate = [...this.#versions.keys()]
        .find((fileId) => fileId !== protectedFileId)
      if (!candidate) break
      this.invalidate(candidate)
    }
    const history = this.#versions.get(protectedFileId)
    while (this.#bytes > this.#maximumBytes && history && history.length > 1) {
      const removed = history.pop()!
      this.#bytes -= removed.bytes
    }
    if (this.#bytes > this.#maximumBytes) {
      this.invalidate(protectedFileId)
      throw new HashlinePreflightError(
        "hashline_snapshot_budget_exhausted",
        "Snapshot store cannot admit content within its global budget.",
      )
    }
  }

  #requireOpen(): void {
    if (this.#closed) {
      throw new HashlinePreflightError(
        "hashline_snapshot_store_closed",
        "Snapshot store is closed.",
      )
    }
  }
}

function allHunkLineIds(hunks: readonly DiffHunkContract[]): readonly string[] {
  return Object.freeze(
    hunks.flatMap((hunk) => hunk.lines.map((line) => line.lineId)),
  )
}

function changedAnchorIds(
  hunks: readonly DiffHunkContract[],
): readonly string[] {
  return Object.freeze(
    hunks.flatMap((hunk) =>
      hunk.lines
        .filter((line) => line.kind === "deleted" || line.kind === "context")
        .map((line) => line.lineId)),
  )
}

function previews(hunks: readonly DiffHunkContract[]): readonly string[] {
  return Object.freeze(
    hunks.flatMap((hunk) => compactHunkPreview(hunk)),
  )
}

function receipt(
  input: Omit<HashlinePreflightReceipt, "receiptId" | "createdAt">,
): HashlinePreflightReceipt {
  const createdAt = Date.now()
  return Object.freeze({
    ...input,
    receiptId: stableIdentity("hashline-preflight", [
      input.fileId,
      input.state,
      input.baseSha256,
      input.currentSha256,
      input.proposedSha256,
      input.reasonCode,
    ]),
    createdAt,
  })
}

function rejectedReceipt(
  input: HashlinePreflightInput,
  state: HashlinePreflightReceipt["state"],
  reasonCode: string,
  message: string,
  currentSha256: string,
  proposedSha256 = "",
  merge?: ThreeWayMergeResult,
): HashlinePreflightReceipt {
  return receipt({
    fileId: input.file.fileId,
    path: input.file.path,
    state,
    accepted: false,
    baseSha256: input.baseSnapshot.sha256,
    currentSha256,
    proposedSha256,
    baseMtimeNs: input.baseSnapshot.mtimeNs,
    currentMtimeNs: input.current.mtimeNs,
    baseMode: input.baseSnapshot.mode,
    currentMode: input.current.mode,
    merge,
    previews: previews(input.hunks),
    reasonCode,
    message,
  })
}

export async function preflightHashlinePatch(
  input: HashlinePreflightInput,
): Promise<HashlinePreflightReceipt> {
  const currentSha256 = normalizedDigest(
    input.current.sha256,
    "current.sha256",
  )
  if (input.file.binary) {
    return rejectedReceipt(
      input,
      "binary_rejected",
      "hashline.binary_text_patch_rejected",
      "Binary files cannot use text hunk preflight.",
      currentSha256,
    )
  }
  const requiredAnchors = changedAnchorIds(input.hunks)
  const missingAnchors = requiredAnchors.filter(
    (lineId) => !input.baseSnapshot.observedLineIds.has(lineId),
  )
  if (missingAnchors.length) {
    return rejectedReceipt(
      input,
      "unobserved_rejected",
      "hashline.anchor_not_observed",
      "Patch references lines that were not present in the verified review snapshot.",
      currentSha256,
    )
  }
  let application: PatchApplication
  try {
    application = applyFileHunks(input.baseSnapshot.text, input.hunks)
  } catch (error) {
    return rejectedReceipt(
      input,
      "apply_failed",
      String((error as { code?: unknown })?.code || "hashline.apply_failed"),
      error instanceof Error ? error.message : String(error),
      currentSha256,
    )
  }
  const proposedSha256 = await contentSha256(application.text)
  const baseChanged =
    currentSha256 !== input.baseSnapshot.sha256
    || input.current.mtimeNs !== input.baseSnapshot.mtimeNs
    || input.current.mode !== input.baseSnapshot.mode
  if (baseChanged) {
    if (!input.allowThreeWay) {
      return rejectedReceipt(
        input,
        "stale",
        "hashline.base_snapshot_stale",
        "File hash, mtime, or mode changed after the reviewed snapshot.",
        currentSha256,
        proposedSha256,
      )
    }
    const merge = threeWayMerge(
      input.baseSnapshot.text,
      input.current.text,
      application.text,
    )
    if (merge.state === "conflicted") {
      return rejectedReceipt(
        input,
        "conflicted",
        "hashline.three_way_conflict",
        "Current and proposed edits overlap and require a new reviewed patch.",
        currentSha256,
        proposedSha256,
        merge,
      )
    }
    const mergedSha256 = await contentSha256(merge.text)
    const precondition: PatchFilePrecondition = Object.freeze({
      fileId: input.file.fileId,
      path: input.file.path,
      previousPath: input.file.previousPath,
      kind: input.file.kind,
      baseSha256: input.baseSnapshot.sha256,
      currentSha256,
      proposedSha256: mergedSha256,
      baseMtimeNs: input.baseSnapshot.mtimeNs,
      currentMtimeNs: input.current.mtimeNs,
      baseMode: input.baseSnapshot.mode,
      currentMode: input.current.mode,
      encoding: input.baseSnapshot.encoding,
      lineEnding: input.baseSnapshot.lineEnding,
      binary: false,
    })
    return receipt({
      fileId: input.file.fileId,
      path: input.file.path,
      state: "clean",
      accepted: true,
      baseSha256: input.baseSnapshot.sha256,
      currentSha256,
      proposedSha256: mergedSha256,
      baseMtimeNs: input.baseSnapshot.mtimeNs,
      currentMtimeNs: input.current.mtimeNs,
      baseMode: input.baseSnapshot.mode,
      currentMode: input.current.mode,
      application: Object.freeze({ ...application, text: merge.text }),
      merge,
      precondition,
      previews: previews(input.hunks),
      reasonCode: "hashline.three_way_clean",
      message: "Stale reviewed patch merged cleanly onto current content.",
    })
  }
  const precondition: PatchFilePrecondition = Object.freeze({
    fileId: input.file.fileId,
    path: input.file.path,
    previousPath: input.file.previousPath,
    kind: input.file.kind,
    baseSha256: input.baseSnapshot.sha256,
    currentSha256,
    proposedSha256,
    baseMtimeNs: input.baseSnapshot.mtimeNs,
    currentMtimeNs: input.current.mtimeNs,
    baseMode: input.baseSnapshot.mode,
    currentMode: input.current.mode,
    encoding: input.baseSnapshot.encoding,
    lineEnding: input.baseSnapshot.lineEnding,
    binary: false,
  })
  return receipt({
    fileId: input.file.fileId,
    path: input.file.path,
    state: "clean",
    accepted: true,
    baseSha256: input.baseSnapshot.sha256,
    currentSha256,
    proposedSha256,
    baseMtimeNs: input.baseSnapshot.mtimeNs,
    currentMtimeNs: input.current.mtimeNs,
    baseMode: input.baseSnapshot.mode,
    currentMode: input.current.mode,
    application,
    precondition,
    previews: previews(input.hunks),
    reasonCode: "hashline.preflight_clean",
    message: "Reviewed patch is bound to the current file snapshot.",
  })
}

export function assertPreflightSet(
  files: readonly DiffFileContract[],
  receipts: readonly HashlinePreflightReceipt[],
): readonly PatchFilePrecondition[] {
  const byFile = new Map(receipts.map((receipt) => [receipt.fileId, receipt]))
  const preconditions: PatchFilePrecondition[] = []
  for (const file of files) {
    const selected = byFile.get(file.fileId)
    if (!selected) {
      throw new HashlinePreflightError(
        "hashline_preflight_missing",
        `File ${file.path} has no preflight receipt.`,
      )
    }
    if (!selected.accepted || !selected.precondition) {
      throw new HashlinePreflightError(
        "hashline_preflight_rejected",
        `File ${file.path} failed preflight: ${selected.message}`,
        {
          fileId: file.fileId,
          state: selected.state,
          reasonCode: selected.reasonCode,
        },
      )
    }
    preconditions.push(selected.precondition)
  }
  return Object.freeze(preconditions)
}

export function snapshotCoversHunks(
  snapshot: DiffSnapshot,
  hunks: readonly DiffHunkContract[],
): {
  complete: boolean
  missingLineIds: readonly string[]
  observedCount: number
  requiredCount: number
} {
  const required = allHunkLineIds(hunks)
  const missing = required.filter(
    (lineId) => !snapshot.observedLineIds.has(lineId),
  )
  return Object.freeze({
    complete: missing.length === 0,
    missingLineIds: Object.freeze(missing),
    observedCount: required.length - missing.length,
    requiredCount: required.length,
  })
}
