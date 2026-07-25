import type {
  SkillBodyProjection,
  SkillBodySection,
  SkillResourceProjection,
  SkillToolScopeProjection,
} from "./contracts.ts"
import {
  firstSkillArray,
  firstSkillRecord,
  firstSkillText,
  optionalSkillHash,
  skillArray,
  skillBoolean,
  skillFingerprint,
  skillIdentity,
  skillInteger,
  skillRecord,
  skillRecords,
  skillStrings,
  skillText,
  uniqueSkillStrings,
} from "./value.ts"
import type { JsonObject } from "../../events/ingress/index.ts"

const MAX_BODY_BYTES = 768 * 1024
const RESOURCE_REFERENCE = /(?:^|[\s("'`])(?:\.{0,2}\/)?((?:resources?|assets?|references?)\/[a-zA-Z0-9_.@/+ -]{1,240})/g
const MARKDOWN_LINK = /!?\[[^\]]*]\(([^)\s]+)(?:\s+["'][^"']*["'])?\)/g
const SECRET_DIRECTIVE = /(?:ignore\s+(?:all\s+)?previous|reveal\s+(?:the\s+)?(?:secret|token|credential)|print\s+(?:the\s+)?environment|disable\s+(?:the\s+)?(?:permission|sandbox)|bypass\s+(?:the\s+)?(?:approval|policy)|curl\s+[^|]+\|\s*(?:sh|bash)|powershell\s+-enc)/i

export function parseSkillBody(
  raw: unknown,
  expectedDigest?: string,
): SkillBodyProjection {
  const source = typeof raw === "string" ? raw.replace(/\r\n?/g, "\n") : ""
  const encoder = new TextEncoder()
  const originalBytes = encoder.encode(source).byteLength
  const truncated = originalBytes > MAX_BODY_BYTES
  const markdown = truncated
    ? truncateUtf8(source, MAX_BODY_BYTES)
    : source
  const lines = markdown.split("\n")
  const sections = buildSections(lines)
  const headings = sections
    .filter((section) => section.heading)
    .map((section) => section.heading)
  const linkTargets = scanLinks(markdown)
  const resourceRefs = scanResources(markdown)
  const suspiciousFragments = scanSuspicious(lines)
  const codeBlockCount = sections.reduce((count, section) => count + section.codeBlocks, 0)
  const digest = expectedDigest
    ? optionalSkillHash(expectedDigest, "skill body digest") ??
      skillFingerprint([markdown])
    : skillFingerprint([markdown])
  return Object.freeze({
    markdown,
    summary: bodySummary(lines),
    sections: Object.freeze(sections),
    headings: Object.freeze(headings),
    codeBlockCount,
    linkTargets: Object.freeze(linkTargets),
    resourceRefs: Object.freeze(resourceRefs),
    suspiciousFragments: Object.freeze(suspiciousFragments),
    truncated,
    byteLength: originalBytes,
    digest,
  })
}

function buildSections(lines: readonly string[]): SkillBodySection[] {
  const boundaries: { index: number; level: number; heading: string }[] = []
  for (let index = 0; index < lines.length; index += 1) {
    const match = lines[index]?.match(/^(#{1,6})\s+(.+?)\s*#*\s*$/)
    if (!match) continue
    boundaries.push({
      index,
      level: match[1]?.length ?? 1,
      heading: skillText(match[2], "", 1_024),
    })
  }
  if (!boundaries.length || boundaries[0]?.index !== 0) {
    boundaries.unshift({ index: 0, level: 0, heading: "" })
  }
  const result: SkillBodySection[] = []
  for (let boundaryIndex = 0; boundaryIndex < boundaries.length; boundaryIndex += 1) {
    const boundary = boundaries[boundaryIndex]!
    const next = boundaries[boundaryIndex + 1]
    const start = boundary.level > 0 ? boundary.index + 1 : boundary.index
    const end = (next?.index ?? lines.length) - 1
    const bodyLines = lines.slice(start, end + 1)
    const body = bodyLines.join("\n").trim()
    if (!body && !boundary.heading) continue
    const links = scanLinks(body)
    const resources = scanResources(body)
    result.push(Object.freeze({
      id: skillFingerprint([
        boundary.level,
        boundary.heading,
        boundary.index,
        body.slice(0, 4_096),
      ]),
      level: boundary.level,
      heading: boundary.heading || "Preamble",
      body,
      lineStart: boundary.index + 1,
      lineEnd: Math.max(boundary.index + 1, end + 1),
      codeBlocks: countCodeBlocks(bodyLines),
      linkTargets: Object.freeze(links),
      resourceRefs: Object.freeze(resources),
      digest: skillFingerprint([boundary.heading, body]),
    }))
  }
  return result
}

function countCodeBlocks(lines: readonly string[]): number {
  let fences = 0
  let indented = false
  for (const line of lines) {
    if (/^\s*(```|~~~)/.test(line)) fences += 1
    if (/^(?: {4}|\t)\S/.test(line)) indented = true
  }
  return Math.floor(fences / 2) + (indented ? 1 : 0)
}

function scanLinks(markdown: string): string[] {
  const result = new Set<string>()
  MARKDOWN_LINK.lastIndex = 0
  let match: RegExpExecArray | null
  while ((match = MARKDOWN_LINK.exec(markdown)) !== null && result.size < 512) {
    const target = skillText(match[1], "", 4_096)
    if (target) result.add(target)
  }
  return [...result].sort()
}

function scanResources(markdown: string): string[] {
  const result = new Set<string>()
  RESOURCE_REFERENCE.lastIndex = 0
  let match: RegExpExecArray | null
  while ((match = RESOURCE_REFERENCE.exec(markdown)) !== null && result.size < 512) {
    const target = skillText(match[1], "", 1_024)
      .replace(/[),.;:'"`]+$/g, "")
      .replace(/\\/g, "/")
    if (target) result.add(target)
  }
  return [...result].sort()
}

function scanSuspicious(lines: readonly string[]): string[] {
  const result: string[] = []
  let fenced = false
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index] ?? ""
    if (/^\s*(```|~~~)/.test(line)) {
      fenced = !fenced
      continue
    }
    if (!fenced && SECRET_DIRECTIVE.test(line)) {
      result.push(`line ${index + 1}: ${line.trim().slice(0, 180)}`)
    }
    if (result.length >= 64) break
  }
  return result
}

function bodySummary(lines: readonly string[]): string {
  const candidates: string[] = []
  let fenced = false
  for (const line of lines) {
    if (/^\s*(```|~~~)/.test(line)) {
      fenced = !fenced
      continue
    }
    if (fenced || /^\s*(?:#|[-*+]\s|\d+[.)]\s|>)/.test(line)) continue
    const normalized = line.trim()
    if (!normalized) continue
    candidates.push(normalized)
    if (candidates.join(" ").length >= 360) break
  }
  return candidates.join(" ").slice(0, 360)
}

function truncateUtf8(value: string, maximum: number): string {
  const encoder = new TextEncoder()
  const decoder = new TextDecoder()
  const encoded = encoder.encode(value)
  if (encoded.byteLength <= maximum) return value
  return `${decoder.decode(encoded.slice(0, Math.max(0, maximum - 3)))}…`
}

export function projectSkillResources(
  records: readonly JsonObject[],
  body: SkillBodyProjection,
): readonly SkillResourceProjection[] {
  const raw = firstSkillArray(records, [
    "resources",
    "skill_resources",
    "resource_dependencies",
    "runtime_assets",
  ])
  const canonical = skillRecords(raw)
  const byPath = new Map<string, SkillResourceProjection>()
  for (let index = 0; index < canonical.length; index += 1) {
    const item = canonical[index]!
    const path = safeResourcePath(
      firstSkillText([item], ["path", "relative_path", "uri", "name"]),
    )
    if (!path) continue
    const resourceId = safeResourceIdentity(
      firstSkillText([item], ["resource_id", "resourceId", "id"], `resource-${index}`),
      path,
    )
    const digest = optionalSkillHash(
      firstSkillText([item], ["digest", "content_digest", "hash"]),
      "skill resource digest",
    )
    const warnings: string[] = []
    const privateResource = skillBoolean(item.private ?? item.sensitive, false)
    const available = skillBoolean(
      item.available ?? item.exists ?? item.resolved,
      true,
    )
    if (!available) warnings.push("resource is absent from the canonical owner")
    if (skillBoolean(item.symlink_escape ?? item.outside_root, false)) {
      warnings.push("resource resolves outside the owning skill root")
    }
    if (!digest) warnings.push("resource has no canonical digest")
    const projected: SkillResourceProjection = Object.freeze({
      resourceId,
      path,
      kind: firstSkillText([item], ["kind", "type"], inferResourceKind(path)),
      required: skillBoolean(item.required, true),
      maximumBytes: optionalPositiveInteger(item.maximum_bytes ?? item.maximumBytes),
      sizeBytes: optionalPositiveInteger(item.size_bytes ?? item.sizeBytes),
      mediaType: firstSkillText([item], ["media_type", "mediaType"]) || undefined,
      charset: firstSkillText([item], ["charset"]) || undefined,
      digest,
      available,
      private: privateResource,
      source: firstSkillText([item], ["source", "source_kind"], "canonical"),
      artifactId: firstSkillText([item], ["artifact_id", "artifactId"]) || undefined,
      warnings: Object.freeze(warnings),
    })
    byPath.set(path, preferResource(byPath.get(path), projected))
  }
  for (const reference of body.resourceRefs) {
    const path = safeResourcePath(reference)
    if (!path || byPath.has(path)) continue
    byPath.set(path, Object.freeze({
      resourceId: safeResourceIdentity("body-reference", path),
      path,
      kind: inferResourceKind(path),
      required: true,
      available: false,
      private: false,
      source: "body-reference",
      warnings: Object.freeze([
        "body references a resource absent from the canonical resource list",
      ]),
    }))
  }
  return Object.freeze(
    [...byPath.values()].sort((left, right) =>
      Number(right.required) - Number(left.required) ||
      left.path.localeCompare(right.path)),
  )
}

function preferResource(
  previous: SkillResourceProjection | undefined,
  next: SkillResourceProjection,
): SkillResourceProjection {
  if (!previous) return next
  if (!previous.available && next.available) return next
  if (!previous.digest && next.digest) return next
  if (previous.required !== next.required) return next.required ? next : previous
  return next
}

function safeResourcePath(value: string): string {
  const normalized = value.trim().replace(/\\/g, "/").replace(/^\.\/+/, "")
  if (
    !normalized ||
    normalized.startsWith("/") ||
    /^[a-zA-Z]:\//.test(normalized) ||
    normalized.split("/").includes("..") ||
    /[\u0000\r\n]/.test(normalized)
  ) {
    return ""
  }
  return normalized.slice(0, 1_024)
}

function safeResourceIdentity(value: string, path: string): string {
  try {
    return skillIdentity(value, "skill resource identity")
  } catch {
    return `resource:${skillFingerprint([path]).slice("skill:".length)}`
  }
}

function inferResourceKind(path: string): string {
  const extension = path.split(".").pop()?.toLowerCase()
  if (["md", "mdx"].includes(extension ?? "")) return "markdown"
  if (["json"].includes(extension ?? "")) return "json"
  if (["yaml", "yml"].includes(extension ?? "")) return "yaml"
  if (["png", "jpg", "jpeg", "gif", "webp", "svg"].includes(extension ?? "")) return "image"
  if (["txt", "csv", "tsv", "xml", "html", "css", "js", "ts", "py", "rs"].includes(extension ?? "")) {
    return "text"
  }
  return "binary"
}

function optionalPositiveInteger(value: unknown): number | undefined {
  if (value === undefined || value === null || value === "") return undefined
  return skillInteger(value, 0, 0, Number.MAX_SAFE_INTEGER)
}

export function projectSkillToolScope(
  records: readonly JsonObject[],
): SkillToolScopeProjection {
  const nested = firstSkillRecord(records, [
    "tool_scope",
    "toolScope",
    "effective_tool_scope",
    "allowed_tools_policy",
  ])
  const sources = [nested, ...records]
  const allowed = normalizeToolNames(
    firstSkillArray(sources, ["allowed", "allowed_tools", "allowedTools"]),
  )
  const denied = normalizeToolNames(
    firstSkillArray(sources, ["denied", "denied_tools", "disallowed_tools"]),
  )
  const namespaces = skillStrings(
    firstSkillArray(sources, ["namespaces", "tool_namespaces"]),
    128,
    256,
  )
  const mcpServers = skillStrings(
    firstSkillArray(sources, ["mcp_servers", "mcpServers"]),
    128,
    256,
  )
  const requireApproval = normalizeToolNames(
    firstSkillArray(sources, ["require_approval", "requireApproval"]),
  )
  const warnings: string[] = []
  for (const tool of allowed) {
    if (denied.includes(tool)) warnings.push(`tool ${tool} is both allowed and denied`)
  }
  for (const tool of requireApproval) {
    if (!allowed.includes(tool) && allowed.length) {
      warnings.push(`approval tool ${tool} is outside the declared allowlist`)
    }
  }
  const maximumCalls = optionalPositiveInteger(
    nested.maximum_calls ?? nested.maximumCalls,
  )
  const maximumParallel = skillInteger(
    nested.maximum_parallel ?? nested.maximumParallel,
    1,
    1,
    256,
  )
  if (!allowed.length) warnings.push("skill declares no explicit tool allowlist")
  return Object.freeze({
    allowed,
    denied,
    namespaces,
    mcpServers,
    requireApproval,
    readOnly: skillBoolean(nested.read_only ?? nested.readOnly, false),
    inheritParent: skillBoolean(nested.inherit_parent ?? nested.inheritParent, true),
    maximumCalls,
    maximumParallel,
    warnings: Object.freeze(warnings),
  })
}

function normalizeToolNames(value: unknown): readonly string[] {
  const source = skillArray(value).length ? value : skillStrings(value)
  const result: string[] = []
  for (const item of Array.isArray(source) ? source : []) {
    const record = skillRecord(item)
    const raw = Object.keys(record).length
      ? firstSkillText([record], ["name", "tool", "id"])
      : skillText(item, "", 512)
    const normalized = raw.trim()
    if (!normalized || /[\u0000\r\n\s]/.test(normalized)) continue
    if (!/^[a-zA-Z0-9][a-zA-Z0-9_.:/@*+-]{0,255}$/.test(normalized)) continue
    result.push(normalized)
  }
  return uniqueSkillStrings(result)
}
