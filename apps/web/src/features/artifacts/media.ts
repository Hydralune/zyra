import { ArtifactAssemblyLedger } from "./assembly.ts"
import type { ArtifactCacheEntry } from "./cache.ts"
import {
  artifactRevisionKey,
  isMediaFamily,
  type ArtifactContract,
} from "./contracts.ts"
import { assertArtifactMediaSafe } from "./security.ts"
import {
  buildMediaViewerModel,
  type ArtifactMediaViewerModel,
} from "./viewers.ts"

export interface ArtifactMediaResource {
  key: string
  artifactId: string
  revision: string
  mediaType: string
  sizeBytes: number
  url: string
  createdAt: number
  accessedAt: number
  referenceCount: number
  verified: boolean
}

export interface ArtifactMediaSnapshot {
  resources: number
  bytes: number
  references: number
  created: number
  revoked: number
  rejected: number
  closed: boolean
}

export interface ArtifactMediaViewport {
  zoom: number
  panX: number
  panY: number
  fit: "contain" | "actual" | "custom"
}

export interface ArtifactMediaUrlPlatform {
  createObjectURL(blob: Blob): string
  revokeObjectURL(url: string): void
}

interface MutableMediaResource extends ArtifactMediaResource {
  blob: Blob
}

const MAX_MEDIA_RESOURCES = 16
const MAX_MEDIA_BYTES = 64 * 1024 * 1024
const MAX_SINGLE_MEDIA_BYTES = 32 * 1024 * 1024

export class ArtifactMediaResourceRegistry {
  readonly #platform?: ArtifactMediaUrlPlatform
  readonly #resources = new Map<string, MutableMediaResource>()
  readonly #maximumResources: number
  readonly #maximumBytes: number
  readonly #maximumSingleBytes: number
  #bytes = 0
  #created = 0
  #revoked = 0
  #rejected = 0
  #closed = false

  constructor(options: {
    platform?: ArtifactMediaUrlPlatform
    maximumResources?: number
    maximumBytes?: number
    maximumSingleBytes?: number
  } = {}) {
    this.#platform = options.platform ?? browserMediaUrlPlatform()
    this.#maximumResources = boundedInteger(
      options.maximumResources,
      MAX_MEDIA_RESOURCES,
      1,
      1_000,
    )
    this.#maximumBytes = boundedInteger(
      options.maximumBytes,
      MAX_MEDIA_BYTES,
      1024,
      1024 * 1024 * 1024,
    )
    this.#maximumSingleBytes = boundedInteger(
      options.maximumSingleBytes,
      MAX_SINGLE_MEDIA_BYTES,
      1024,
      this.#maximumBytes,
    )
  }

  async acquire(
    artifact: ArtifactContract,
    entries: readonly ArtifactCacheEntry[],
  ): Promise<ArtifactMediaResource> {
    this.#assertOpen()
    assertArtifactMediaSafe(artifact)
    if (!isMediaFamily(artifact.contentFamily)) {
      throw new ArtifactMediaError(
        "media_family_invalid",
        "Artifact does not belong to an allowlisted media family.",
      )
    }
    if (!this.#platform) {
      throw new ArtifactMediaError(
        "media_platform_unavailable",
        "Browser object URL support is unavailable.",
      )
    }
    if (artifact.sizeBytes > this.#maximumSingleBytes) {
      this.#rejected += 1
      throw new ArtifactMediaError(
        "media_resource_too_large",
        "Artifact media exceeds the bounded in-browser resource limit.",
      )
    }
    const key = artifactRevisionKey(artifact)
    const existing = this.#resources.get(key)
    if (existing) {
      existing.referenceCount += 1
      existing.accessedAt = Date.now()
      this.#touch(existing)
      return cloneResource(existing)
    }
    const assembly = new ArtifactAssemblyLedger(artifact)
    try {
      assembly.admitMany(entries)
      const binary = await assembly.binary()
      if (!binary.verified) {
        this.#rejected += 1
        throw new ArtifactMediaError(
          "media_digest_mismatch",
          "Complete media bytes do not match the canonical artifact digest.",
        )
      }
      const blob = new Blob([binary.bytes], { type: artifact.mediaType })
      if (blob.size !== artifact.sizeBytes) {
        this.#rejected += 1
        throw new ArtifactMediaError(
          "media_blob_size_mismatch",
          "Browser media blob does not match canonical artifact size.",
        )
      }
      this.#evictFor(blob.size)
      if (
        this.#resources.size >= this.#maximumResources
        || this.#bytes + blob.size > this.#maximumBytes
      ) {
        this.#rejected += 1
        throw new ArtifactMediaError(
          "media_registry_capacity",
          "No bounded media resource capacity is available.",
        )
      }
      const url = this.#platform.createObjectURL(blob)
      const now = Date.now()
      const resource: MutableMediaResource = {
        key,
        artifactId: artifact.artifactId,
        revision: artifact.revision,
        mediaType: artifact.mediaType,
        sizeBytes: blob.size,
        url,
        createdAt: now,
        accessedAt: now,
        referenceCount: 1,
        verified: true,
        blob,
      }
      this.#resources.set(key, resource)
      this.#bytes += blob.size
      this.#created += 1
      return cloneResource(resource)
    } finally {
      assembly.close()
    }
  }

  release(
    artifact: Pick<ArtifactContract, "artifactId" | "revision">,
  ): boolean {
    const key = artifactRevisionKey(artifact)
    const resource = this.#resources.get(key)
    if (!resource) return false
    resource.referenceCount = Math.max(0, resource.referenceCount - 1)
    resource.accessedAt = Date.now()
    if (resource.referenceCount === 0) this.#evict()
    return true
  }

  revoke(
    artifact: Pick<ArtifactContract, "artifactId" | "revision">,
  ): boolean {
    return this.#remove(artifactRevisionKey(artifact), true)
  }

  model(
    artifact: ArtifactContract,
    viewport: Partial<ArtifactMediaViewport> = {},
  ): ArtifactMediaViewerModel {
    this.#assertOpen()
    const resource = this.#resources.get(artifactRevisionKey(artifact))
    if (!resource || !resource.verified) {
      throw new ArtifactMediaError(
        "media_resource_missing",
        "Verified artifact media resource is not acquired.",
      )
    }
    resource.accessedAt = Date.now()
    this.#touch(resource)
    return buildMediaViewerModel({
      artifact,
      sourceUrl: resource.url,
      zoom: viewport.fit === "actual" ? 1 : viewport.zoom,
      panX: viewport.panX,
      panY: viewport.panY,
    })
  }

  zoom(
    current: ArtifactMediaViewport,
    delta: number,
    anchor: { x: number; y: number } = { x: 0, y: 0 },
  ): ArtifactMediaViewport {
    this.#assertOpen()
    const previousZoom = clampNumber(current.zoom, 0.1, 16)
    const nextZoom = clampNumber(
      previousZoom * Math.exp(clampNumber(delta, -5, 5) * 0.12),
      0.1,
      16,
    )
    const ratio = nextZoom / previousZoom
    return Object.freeze({
      zoom: nextZoom,
      panX: anchor.x - (anchor.x - current.panX) * ratio,
      panY: anchor.y - (anchor.y - current.panY) * ratio,
      fit: "custom",
    })
  }

  pan(
    current: ArtifactMediaViewport,
    delta: { x: number; y: number },
    bounds: { width: number; height: number } = {
      width: 100_000,
      height: 100_000,
    },
  ): ArtifactMediaViewport {
    this.#assertOpen()
    const width = Math.max(0, finiteNumber(bounds.width, 0))
    const height = Math.max(0, finiteNumber(bounds.height, 0))
    return Object.freeze({
      zoom: clampNumber(current.zoom, 0.1, 16),
      panX: clampNumber(
        current.panX + finiteNumber(delta.x, 0),
        -width,
        width,
      ),
      panY: clampNumber(
        current.panY + finiteNumber(delta.y, 0),
        -height,
        height,
      ),
      fit: "custom",
    })
  }

  fit(mode: "contain" | "actual"): ArtifactMediaViewport {
    this.#assertOpen()
    return Object.freeze({
      zoom: 1,
      panX: 0,
      panY: 0,
      fit: mode,
    })
  }

  snapshot(): ArtifactMediaSnapshot {
    return Object.freeze({
      resources: this.#resources.size,
      bytes: this.#bytes,
      references: [...this.#resources.values()].reduce(
        (sum, resource) => sum + resource.referenceCount,
        0,
      ),
      created: this.#created,
      revoked: this.#revoked,
      rejected: this.#rejected,
      closed: this.#closed,
    })
  }

  close(): void {
    if (this.#closed) return
    this.#closed = true
    for (const key of [...this.#resources.keys()]) this.#remove(key, true)
  }

  #touch(resource: MutableMediaResource): void {
    this.#resources.delete(resource.key)
    this.#resources.set(resource.key, resource)
  }

  #evictFor(requiredBytes: number): void {
    let inspected = 0
    while (
      this.#resources.size >= this.#maximumResources
      || this.#bytes + requiredBytes > this.#maximumBytes
    ) {
      const oldest = this.#resources.values().next().value as
        | MutableMediaResource
        | undefined
      if (!oldest) break
      if (oldest.referenceCount > 0) {
        this.#touch(oldest)
        inspected += 1
        if (inspected >= this.#resources.size) break
        continue
      }
      this.#remove(oldest.key, true)
      inspected = 0
    }
  }

  #evict(): void {
    for (const resource of [...this.#resources.values()]) {
      if (resource.referenceCount === 0) this.#remove(resource.key, true)
    }
  }

  #remove(key: string, revoke: boolean): boolean {
    const resource = this.#resources.get(key)
    if (!resource) return false
    this.#resources.delete(key)
    this.#bytes -= resource.sizeBytes
    if (revoke && this.#platform) {
      this.#platform.revokeObjectURL(resource.url)
      this.#revoked += 1
    }
    return true
  }

  #assertOpen(): void {
    if (this.#closed) {
      throw new ArtifactMediaError(
        "media_registry_closed",
        "Artifact media resource registry is closed.",
      )
    }
  }
}

export class ArtifactMediaError extends Error {
  readonly code: string

  constructor(code: string, message: string) {
    super(message)
    this.name = "ArtifactMediaError"
    this.code = code
  }
}

export function browserMediaUrlPlatform():
  | ArtifactMediaUrlPlatform
  | undefined {
  const url = globalThis.URL
  if (
    typeof Blob === "undefined"
    || typeof url?.createObjectURL !== "function"
    || typeof url?.revokeObjectURL !== "function"
  ) {
    return undefined
  }
  return {
    createObjectURL(blob: Blob): string {
      return url.createObjectURL(blob)
    },
    revokeObjectURL(value: string): void {
      url.revokeObjectURL(value)
    },
  }
}

function cloneResource(resource: ArtifactMediaResource): ArtifactMediaResource {
  return Object.freeze({
    key: resource.key,
    artifactId: resource.artifactId,
    revision: resource.revision,
    mediaType: resource.mediaType,
    sizeBytes: resource.sizeBytes,
    url: resource.url,
    createdAt: resource.createdAt,
    accessedAt: resource.accessedAt,
    referenceCount: resource.referenceCount,
    verified: resource.verified,
  })
}

function boundedInteger(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  const selected = value ?? fallback
  if (!Number.isSafeInteger(selected) || selected < minimum || selected > maximum) {
    throw new ArtifactMediaError(
      "media_limit_invalid",
      `Artifact media limit must be between ${minimum} and ${maximum}.`,
    )
  }
  return selected
}

function clampNumber(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, finiteNumber(value, minimum)))
}

function finiteNumber(value: number, fallback: number): number {
  return Number.isFinite(value) ? value : fallback
}
