import type { ArtifactCacheEntry } from "./cache.ts"
import {
  artifactRevisionKey,
  type ArtifactContract,
  type ArtifactReadRange,
} from "./contracts.ts"

export interface ArtifactAssemblySegment {
  key: string
  offset: number
  endExclusive: number
  length: number
  receiptDigest: string
  binary?: Uint8Array
  text?: string
  transformed: boolean
}

export interface ArtifactAssemblySnapshot {
  artifactKey: string
  totalBytes: number
  admittedBytes: number
  coveredBytes: number
  segmentCount: number
  gaps: readonly ArtifactAssemblyGap[]
  overlaps: readonly ArtifactAssemblyOverlap[]
  complete: boolean
  representation: "empty" | "binary" | "text" | "mixed"
}

export interface ArtifactAssemblyGap {
  start: number
  end: number
}

export interface ArtifactAssemblyOverlap {
  leftKey: string
  rightKey: string
  start: number
  end: number
  consistent: boolean
}

export interface ArtifactBinaryAssembly {
  bytes: Uint8Array
  sha256: string
  revision: string
  verified: boolean
  segmentKeys: readonly string[]
}

export interface ArtifactTextAssembly {
  text: string
  transformed: boolean
  segmentKeys: readonly string[]
  gaps: readonly ArtifactAssemblyGap[]
}

interface MutableSegment extends ArtifactAssemblySegment {
  binary?: Uint8Array
}

export class ArtifactAssemblyLedger {
  readonly #artifact: ArtifactContract
  readonly #segments = new Map<string, MutableSegment>()
  #closed = false

  constructor(artifact: ArtifactContract) {
    this.#artifact = artifact
  }

  admit(entry: ArtifactCacheEntry): void {
    this.#assertOpen()
    if (
      entry.artifactId !== this.#artifact.artifactId
      || entry.revision !== this.#artifact.revision
    ) {
      throw new ArtifactAssemblyError(
        "assembly_identity_mismatch",
        "Artifact cache segment belongs to a different immutable revision.",
      )
    }
    if (
      entry.receipt.artifactId !== this.#artifact.artifactId
      || entry.receipt.revision !== this.#artifact.revision
      || entry.receipt.sha256 !== this.#artifact.sha256
    ) {
      throw new ArtifactAssemblyError(
        "assembly_receipt_mismatch",
        "Artifact cache segment receipt does not match the assembly owner.",
      )
    }
    validateRange(entry.range, this.#artifact.sizeBytes)
    if ((entry.binary === undefined) === (entry.text === undefined)) {
      throw new ArtifactAssemblyError(
        "assembly_representation_invalid",
        "Artifact assembly segment must contain exactly one representation.",
      )
    }
    const segment: MutableSegment = {
      key: entry.key,
      offset: entry.range.offset,
      endExclusive: entry.range.endExclusive,
      length: entry.range.length,
      receiptDigest: entry.receipt.receiptDigest,
      binary: entry.binary?.slice(),
      text: entry.text?.text,
      transformed:
        Boolean(entry.text?.clientRedacted)
        || Boolean(entry.text?.serverRedacted)
        || Boolean(entry.text?.quarantined),
    }
    const existing = this.#segments.get(segment.key)
    if (existing) {
      if (!sameSegment(existing, segment)) {
        throw new ArtifactAssemblyError(
          "assembly_segment_conflict",
          "Immutable artifact range changed after assembly admission.",
        )
      }
      return
    }
    for (const candidate of this.#segments.values()) {
      const overlap = overlappingInterval(candidate, segment)
      if (!overlap) continue
      if (!consistentOverlap(candidate, segment, overlap)) {
        throw new ArtifactAssemblyError(
          "assembly_overlap_conflict",
          `Artifact ranges overlap with conflicting content at ${overlap.start}-${overlap.end}.`,
        )
      }
    }
    this.#segments.set(segment.key, segment)
  }

  admitMany(entries: readonly ArtifactCacheEntry[]): void {
    for (const entry of entries) this.admit(entry)
  }

  remove(key: string): boolean {
    this.#assertOpen()
    return this.#segments.delete(key)
  }

  clear(): void {
    this.#segments.clear()
  }

  snapshot(): ArtifactAssemblySnapshot {
    const segments = this.#ordered()
    const gaps = findGaps(segments, this.#artifact.sizeBytes)
    const overlaps: ArtifactAssemblyOverlap[] = []
    for (let leftIndex = 0; leftIndex < segments.length; leftIndex += 1) {
      for (
        let rightIndex = leftIndex + 1;
        rightIndex < segments.length;
        rightIndex += 1
      ) {
        const left = segments[leftIndex]!
        const right = segments[rightIndex]!
        if (right.offset >= left.endExclusive) break
        const overlap = overlappingInterval(left, right)
        if (!overlap) continue
        overlaps.push(
          Object.freeze({
            leftKey: left.key,
            rightKey: right.key,
            start: overlap.start,
            end: overlap.end,
            consistent: consistentOverlap(left, right, overlap),
          }),
        )
      }
    }
    const representation = assemblyRepresentation(segments)
    return Object.freeze({
      artifactKey: artifactRevisionKey(this.#artifact),
      totalBytes: this.#artifact.sizeBytes,
      admittedBytes: segments.reduce((sum, segment) => sum + segment.length, 0),
      coveredBytes: coveredBytes(segments),
      segmentCount: segments.length,
      gaps: Object.freeze(gaps),
      overlaps: Object.freeze(overlaps),
      complete: gaps.length === 0 && coveredBytes(segments) === this.#artifact.sizeBytes,
      representation,
    })
  }

  async binary(): Promise<ArtifactBinaryAssembly> {
    this.#assertOpen()
    const snapshot = this.snapshot()
    if (!snapshot.complete) {
      throw new ArtifactAssemblyError(
        "assembly_incomplete",
        "Complete binary assembly requires every byte range.",
      )
    }
    if (snapshot.representation !== "binary" && snapshot.representation !== "empty") {
      throw new ArtifactAssemblyError(
        "assembly_binary_required",
        "Artifact assembly contains transformed text rather than original bytes.",
      )
    }
    const segments = this.#ordered()
    const bytes = new Uint8Array(this.#artifact.sizeBytes)
    const written = new Uint8Array(this.#artifact.sizeBytes)
    for (const segment of segments) {
      if (!segment.binary) continue
      for (let index = 0; index < segment.binary.length; index += 1) {
        const target = segment.offset + index
        const value = segment.binary[index]!
        if (written[target] && bytes[target] !== value) {
          throw new ArtifactAssemblyError(
            "assembly_write_conflict",
            `Conflicting immutable byte at offset ${target}.`,
          )
        }
        bytes[target] = value
        written[target] = 1
      }
    }
    if (written.some((value) => value === 0)) {
      throw new ArtifactAssemblyError(
        "assembly_unwritten_byte",
        "Binary assembly contains an unwritten byte despite complete coverage.",
      )
    }
    const sha256 = await sha256Bytes(bytes)
    const revision = `sha256:${sha256}`
    return Object.freeze({
      bytes,
      sha256,
      revision,
      verified:
        sha256 === this.#artifact.sha256
        && revision === this.#artifact.revision,
      segmentKeys: Object.freeze(segments.map((segment) => segment.key)),
    })
  }

  text(options: { allowGaps?: boolean } = {}): ArtifactTextAssembly {
    this.#assertOpen()
    const snapshot = this.snapshot()
    if (
      snapshot.representation !== "text"
      && snapshot.representation !== "empty"
    ) {
      throw new ArtifactAssemblyError(
        "assembly_text_required",
        "Artifact assembly contains binary ranges rather than admitted text.",
      )
    }
    if (!options.allowGaps && snapshot.gaps.length) {
      throw new ArtifactAssemblyError(
        "assembly_text_gap",
        "Artifact text assembly has missing byte ranges.",
      )
    }
    const segments = this.#ordered()
    let cursor = 0
    const fragments: string[] = []
    const keys: string[] = []
    for (const segment of segments) {
      if (segment.endExclusive <= cursor) continue
      if (segment.offset > cursor) {
        fragments.push(
          `\n[ZYRA_MISSING_BYTES:${cursor}-${segment.offset}]\n`,
        )
      }
      if (segment.text !== undefined) {
        fragments.push(segment.text)
        keys.push(segment.key)
      }
      cursor = Math.max(cursor, segment.endExclusive)
    }
    if (cursor < this.#artifact.sizeBytes) {
      fragments.push(
        `\n[ZYRA_MISSING_BYTES:${cursor}-${this.#artifact.sizeBytes}]\n`,
      )
    }
    return Object.freeze({
      text: fragments.join(""),
      transformed: segments.some((segment) => segment.transformed),
      segmentKeys: Object.freeze(keys),
      gaps: snapshot.gaps,
    })
  }

  close(): void {
    if (this.#closed) return
    this.#closed = true
    this.clear()
  }

  #ordered(): MutableSegment[] {
    return [...this.#segments.values()].sort(
      (left, right) =>
        left.offset - right.offset
        || left.endExclusive - right.endExclusive
        || left.key.localeCompare(right.key),
    )
  }

  #assertOpen(): void {
    if (this.#closed) {
      throw new ArtifactAssemblyError(
        "assembly_closed",
        "Artifact assembly ledger is closed.",
      )
    }
  }
}

export class ArtifactAssemblyError extends Error {
  readonly code: string

  constructor(code: string, message: string) {
    super(message)
    this.name = "ArtifactAssemblyError"
    this.code = code
  }
}

function validateRange(range: ArtifactReadRange, totalBytes: number): void {
  if (
    !Number.isSafeInteger(range.offset)
    || !Number.isSafeInteger(range.length)
    || !Number.isSafeInteger(range.endExclusive)
    || range.offset < 0
    || range.length < 0
    || range.endExclusive !== range.offset + range.length
    || range.endExclusive > totalBytes
  ) {
    throw new ArtifactAssemblyError(
      "assembly_range_invalid",
      "Artifact assembly range is invalid.",
    )
  }
}

function sameSegment(
  left: MutableSegment,
  right: MutableSegment,
): boolean {
  if (
    left.key !== right.key
    || left.offset !== right.offset
    || left.endExclusive !== right.endExclusive
    || left.receiptDigest !== right.receiptDigest
    || left.text !== right.text
    || left.transformed !== right.transformed
  ) {
    return false
  }
  return sameBytes(left.binary, right.binary)
}

function sameBytes(
  left: Uint8Array | undefined,
  right: Uint8Array | undefined,
): boolean {
  if (!left && !right) return true
  if (!left || !right || left.length !== right.length) return false
  for (let index = 0; index < left.length; index += 1) {
    if (left[index] !== right[index]) return false
  }
  return true
}

function overlappingInterval(
  left: ArtifactAssemblySegment,
  right: ArtifactAssemblySegment,
): ArtifactAssemblyGap | undefined {
  const start = Math.max(left.offset, right.offset)
  const end = Math.min(left.endExclusive, right.endExclusive)
  return end > start ? { start, end } : undefined
}

function consistentOverlap(
  left: ArtifactAssemblySegment,
  right: ArtifactAssemblySegment,
  overlap: ArtifactAssemblyGap,
): boolean {
  if (left.binary && right.binary) {
    for (let offset = overlap.start; offset < overlap.end; offset += 1) {
      const leftValue = left.binary[offset - left.offset]
      const rightValue = right.binary[offset - right.offset]
      if (leftValue !== rightValue) return false
    }
    return true
  }
  if (left.text !== undefined && right.text !== undefined) {
    if (left.offset === right.offset && left.endExclusive === right.endExclusive) {
      return left.text === right.text
    }
    return left.receiptDigest === right.receiptDigest
  }
  return false
}

function findGaps(
  segments: readonly ArtifactAssemblySegment[],
  totalBytes: number,
): ArtifactAssemblyGap[] {
  const gaps: ArtifactAssemblyGap[] = []
  let cursor = 0
  for (const segment of segments) {
    if (segment.offset > cursor) {
      gaps.push(Object.freeze({ start: cursor, end: segment.offset }))
    }
    cursor = Math.max(cursor, segment.endExclusive)
  }
  if (cursor < totalBytes) {
    gaps.push(Object.freeze({ start: cursor, end: totalBytes }))
  }
  return gaps
}

function coveredBytes(
  segments: readonly ArtifactAssemblySegment[],
): number {
  let total = 0
  let cursor = 0
  for (const segment of segments) {
    const start = Math.max(cursor, segment.offset)
    if (segment.endExclusive > start) total += segment.endExclusive - start
    cursor = Math.max(cursor, segment.endExclusive)
  }
  return total
}

function assemblyRepresentation(
  segments: readonly ArtifactAssemblySegment[],
): ArtifactAssemblySnapshot["representation"] {
  if (!segments.length) return "empty"
  const binary = segments.some((segment) => segment.binary !== undefined)
  const text = segments.some((segment) => segment.text !== undefined)
  if (binary && text) return "mixed"
  return binary ? "binary" : "text"
}

async function sha256Bytes(bytes: Uint8Array): Promise<string> {
  if (!globalThis.crypto?.subtle) {
    throw new ArtifactAssemblyError(
      "assembly_crypto_unavailable",
      "Web Crypto is required to verify a complete binary assembly.",
    )
  }
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes)
  return [...new Uint8Array(digest)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("")
}
