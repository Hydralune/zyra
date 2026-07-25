import type {
  SkillProvenanceProjection,
  SkillVersionProjection,
} from "./contracts.ts"
import type { JsonObject } from "../../events/ingress/index.ts"
import {
  firstSkillRecord,
  firstSkillText,
  optionalSkillHash,
  optionalSkillIdentity,
  optionalSkillTimestamp,
  skillFingerprint,
  skillHash,
  skillInteger,
  skillText,
  uniqueSkillStrings,
} from "./value.ts"

const TRUSTED_SOURCE_KINDS = new Set([
  "managed",
  "project",
  "bundled",
  "plugin",
  "user",
  "session",
])

const SIGNATURE_PASS = new Set([
  "verified",
  "valid",
  "trusted",
  "not_required",
  "local",
])

export function projectSkillProvenance(
  records: readonly JsonObject[],
  canonicalOwner: string,
): SkillProvenanceProjection {
  const source = firstSkillRecord(records, [
    "source",
    "provenance",
    "skill_source",
    "source_file",
    "execution_provenance",
  ])
  const metadata = firstSkillRecord(records, ["metadata"])
  const sources = [source, metadata, ...records]
  const sourceKind = firstSkillText(
    sources,
    ["source_kind", "sourceKind", "kind", "root_kind"],
    "unknown",
  ).toLowerCase()
  const sourceId = firstSkillText(
    sources,
    ["source_id", "sourceId", "source", "root_id"],
    "unknown-source",
  )
  const signatureStatus = firstSkillText(
    sources,
    ["signature_status", "signatureStatus", "signature", "verification"],
    sourceKind === "project" || sourceKind === "user" ? "local" : "unknown",
  ).toLowerCase()
  const relativePath = safeRelativePath(
    firstSkillText(sources, ["relative_path", "relativePath", "manifest_path"]),
  )
  const repository = safeRepository(
    firstSkillText(sources, ["repository", "repository_url", "repo"]),
  )
  const signer = safeSigner(
    firstSkillText(sources, ["signer", "signed_by", "publisher"]),
  )
  const warnings: string[] = []
  if (!TRUSTED_SOURCE_KINDS.has(sourceKind)) {
    warnings.push(`source kind ${sourceKind} is not recognized by browser admission`)
  }
  if (!SIGNATURE_PASS.has(signatureStatus)) {
    warnings.push(`signature status is ${signatureStatus}`)
  }
  if (!relativePath && sourceKind !== "bundled") {
    warnings.push("canonical projection omits a relative source path")
  }
  if (relativePath && relativePath.split("/").includes("..")) {
    warnings.push("source path contains a parent traversal")
  }
  const ownerTrust = /^typescript(?:$|[.:/])/i.test(canonicalOwner) ||
    /^03c(?:\s+|[.:/-])skillcoordinator$/i.test(canonicalOwner)
  if (!ownerTrust) warnings.push("canonical owner is outside the SkillTool custody set")
  const trustworthy =
    ownerTrust &&
    TRUSTED_SOURCE_KINDS.has(sourceKind) &&
    SIGNATURE_PASS.has(signatureStatus) &&
    !warnings.some((warning) => /traversal|outside/.test(warning))
  const core = {
    sourceId,
    sourceKind,
    sourcePriority: skillInteger(
      source.source_priority ?? source.sourcePriority ?? source.priority,
      0,
      -100_000,
      100_000,
    ),
    pluginId: optionalSafeIdentity(
      firstSkillText(sources, ["plugin_id", "pluginId"]),
    ),
    relativePath: relativePath || undefined,
    rootCategory: firstSkillText(
      sources,
      ["root_category", "rootCategory", "root_kind"],
      sourceKind,
    ),
    canonicalOwner,
    capabilityOwner: firstSkillText(
      sources,
      ["capability_owner", "capabilityOwner"],
    ) || undefined,
    registryOwner: firstSkillText(
      sources,
      ["registry_owner", "registryOwner"],
    ) || undefined,
    signatureStatus,
    signer: signer || undefined,
    repository: repository || undefined,
    revision: firstSkillText(
      sources,
      ["source_revision", "git_revision", "commit", "revision"],
    ) || undefined,
    discoveredAt: optionalSkillTimestamp(
      source.discovered_at ?? source.discoveredAt,
    ),
    parsedAt: optionalSkillTimestamp(
      records[0]?.parsed_at ?? records[0]?.parsedAt,
    ),
    modifiedAt: optionalSkillTimestamp(
      source.modified_at ?? source.modifiedAt,
    ),
  }
  return Object.freeze({
    ...core,
    provenanceDigest: optionalSkillHash(
      firstSkillText(sources, [
        "provenance_digest",
        "source_digest",
        "manifest_digest",
      ]),
      "skill provenance digest",
    ) ?? skillFingerprint([core]),
    trustworthy,
    warnings: Object.freeze(uniqueSkillStrings(warnings)),
  })
}

export function projectSkillVersion(
  records: readonly JsonObject[],
  input: {
    bodyDigest: string
    eventId: string
    observedAt: string
    sourceRevision: number
  },
): SkillVersionProjection {
  const versionRecord = firstSkillRecord(records, [
    "version_info",
    "revision",
    "skill_revision_info",
  ])
  const sources = [versionRecord, ...records]
  const contentHash =
    optionalSkillHash(
      firstSkillText(sources, [
        "content_hash",
        "contentHash",
        "content_digest",
        "skill_digest",
        "digest",
      ]),
      "skill content hash",
    ) ?? normalizeDerivedDigest(input.bodyDigest)
  const descriptorDigest =
    optionalSkillHash(
      firstSkillText(sources, [
        "descriptor_digest",
        "descriptorDigest",
        "manifest_digest",
      ]),
      "skill descriptor digest",
    ) ?? contentHash
  const bodyDigest =
    optionalSkillHash(
      firstSkillText(sources, ["body_digest", "bodyDigest"]),
      "skill body digest",
    ) ?? normalizeDerivedDigest(input.bodyDigest)
  const registryRevision = skillInteger(
    sources
      .map((record) =>
        record.registry_revision ??
        record.registryRevision ??
        record.skill_revision ??
        record.revision_number)
      .find((value) => value !== undefined),
    input.sourceRevision,
    0,
    Number.MAX_SAFE_INTEGER,
  )
  return Object.freeze({
    version: firstSkillText(sources, ["version", "semantic_version", "semver"], "0.0.0"),
    registryRevision,
    revisionId: firstSkillText(
      sources,
      ["revision_id", "revisionId", "head_revision_id"],
    ) || undefined,
    parentRevisionId: firstSkillText(
      sources,
      ["parent_revision_id", "parentRevisionId"],
    ) || undefined,
    contentHash,
    descriptorDigest,
    bodyDigest,
    frontmatterDigest: optionalSkillHash(
      firstSkillText(sources, ["frontmatter_digest", "frontmatterDigest"]),
      "skill frontmatter digest",
    ),
    resourceDigest: optionalSkillHash(
      firstSkillText(sources, ["resource_digest", "resources_digest"]),
      "skill resource digest",
    ),
    observedAt: input.observedAt,
    eventId: input.eventId,
  })
}

export function compareSkillVersions(
  left: SkillVersionProjection,
  right: SkillVersionProjection,
): number {
  if (left.registryRevision !== right.registryRevision) {
    return left.registryRevision - right.registryRevision
  }
  const semantic = compareSemanticVersion(left.version, right.version)
  if (semantic) return semantic
  if (left.contentHash !== right.contentHash) {
    return left.observedAt.localeCompare(right.observedAt)
  }
  return left.eventId.localeCompare(right.eventId)
}

export function detectSkillVersionDrift(
  history: readonly SkillVersionProjection[],
): readonly string[] {
  const warnings: string[] = []
  const ordered = [...history].sort(compareSkillVersions)
  for (let index = 1; index < ordered.length; index += 1) {
    const previous = ordered[index - 1]!
    const current = ordered[index]!
    if (
      current.registryRevision === previous.registryRevision &&
      current.contentHash !== previous.contentHash
    ) {
      warnings.push(
        `registry revision ${current.registryRevision} maps to multiple content hashes`,
      )
    }
    if (
      current.registryRevision > previous.registryRevision &&
      current.contentHash === previous.contentHash
    ) {
      warnings.push(
        `registry revision advanced from ${previous.registryRevision} to ${current.registryRevision} without content change`,
      )
    }
    if (
      current.registryRevision < previous.registryRevision &&
      current.observedAt > previous.observedAt
    ) {
      warnings.push("a later observation regressed the skill registry revision")
    }
  }
  return Object.freeze(uniqueSkillStrings(warnings))
}

function compareSemanticVersion(left: string, right: string): number {
  const parse = (value: string): number[] =>
    value.replace(/^v/i, "").split(/[.+-]/).slice(0, 4).map((item) => {
      const number = Number.parseInt(item, 10)
      return Number.isFinite(number) ? number : 0
    })
  const leftParts = parse(left)
  const rightParts = parse(right)
  for (let index = 0; index < Math.max(leftParts.length, rightParts.length); index += 1) {
    const delta = (leftParts[index] ?? 0) - (rightParts[index] ?? 0)
    if (delta) return delta
  }
  return left.localeCompare(right)
}

function safeRelativePath(value: string): string {
  const normalized = value.replace(/\\/g, "/").replace(/^\.\/+/, "")
  if (!normalized || normalized.startsWith("/") || /^[a-zA-Z]:\//.test(normalized)) return ""
  if (/[\u0000\r\n]/.test(normalized)) return ""
  return normalized.slice(0, 2_048)
}

function safeRepository(value: string): string {
  if (!value || /[\u0000\r\n]/.test(value)) return ""
  if (/^(?:https:\/\/|ssh:\/\/|git@|urn:|local:)/i.test(value)) {
    return value.replace(/:\/\/[^/@\s]+:[^/@\s]+@/, "://[redacted]@").slice(0, 2_048)
  }
  return ""
}

function safeSigner(value: string): string {
  if (!value || /[\u0000\r\n]/.test(value)) return ""
  return value.slice(0, 512)
}

function optionalSafeIdentity(value: string): string | undefined {
  try {
    return optionalSkillIdentity(value)
  } catch {
    return undefined
  }
}

function normalizeDerivedDigest(value: string): string {
  try {
    return skillHash(value)
  } catch {
    const suffix = skillFingerprint([value]).slice("skill:".length)
    return `sha256:${suffix.padEnd(64, "0")}`
  }
}
