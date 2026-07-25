import type {
  SkillCatalogPage,
  SkillCatalogProjection,
  SkillCatalogQuery,
  SkillProjection,
} from "./contracts.ts"
import {
  compareSkillText,
  skillFingerprint,
  skillInteger,
  skillText,
  uniqueSkillStrings,
} from "./value.ts"

interface CursorPayload {
  revision: number
  offset: number
  limit: number
  queryFingerprint: string
}

export class SkillCatalogIndex {
  #catalog?: SkillCatalogProjection
  #byId = new Map<string, SkillProjection>()
  #tokens = new Map<string, Set<string>>()
  #enabled = true
  #disabledReason = "Skill catalog index is disabled."

  replace(catalog: SkillCatalogProjection): void {
    this.#assertEnabled()
    if (this.#catalog && catalog.revision < this.#catalog.revision) {
      throw new Error("Skill catalog index rejects projection revision regression.")
    }
    const byId = new Map<string, SkillProjection>()
    const tokens = new Map<string, Set<string>>()
    for (const skill of catalog.skills) {
      if (skill.taskId !== catalog.taskId) {
        throw new Error("Skill catalog row belongs to another task.")
      }
      if (byId.has(skill.skillId)) {
        throw new Error(`Duplicate skill projection identity ${skill.skillId}.`)
      }
      byId.set(skill.skillId, skill)
      for (const token of tokenizeSkill(skill)) {
        const ids = tokens.get(token) ?? new Set<string>()
        ids.add(skill.skillId)
        tokens.set(token, ids)
      }
    }
    this.#catalog = catalog
    this.#byId = byId
    this.#tokens = tokens
  }

  get(skillId: string): SkillProjection | undefined {
    this.#assertEnabled()
    return this.#byId.get(skillId)
  }

  page(query: SkillCatalogQuery = {}): SkillCatalogPage {
    this.#assertEnabled()
    const catalog = this.#catalog
    if (!catalog) {
      return Object.freeze({
        rows: Object.freeze([]),
        total: 0,
        offset: 0,
        limit: normalizeLimit(query.limit),
        revision: 0,
        queryFingerprint: queryFingerprint(query),
        facets: emptyFacets(),
      })
    }
    const normalized = normalizeQuery(query)
    const fingerprint = queryFingerprint(normalized)
    const cursor = normalized.cursor
      ? decodeCursor(normalized.cursor, catalog.revision, fingerprint)
      : undefined
    const offset = cursor?.offset ?? 0
    const limit = cursor?.limit ?? normalized.limit ?? 50
    const matchedIds = normalized.text
      ? this.#matchText(normalized.text)
      : new Set(this.#byId.keys())
    const filtered = catalog.skills.filter((skill) =>
      matchedIds.has(skill.skillId) &&
      matchesQuery(skill, normalized))
    const facets = buildFacets(filtered)
    const sorted = [...filtered].sort(skillComparator(normalized))
    const rows = sorted.slice(offset, offset + limit)
    const nextOffset = offset + rows.length
    const previousOffset = Math.max(0, offset - limit)
    return Object.freeze({
      rows: Object.freeze(rows),
      total: sorted.length,
      offset,
      limit,
      nextCursor:
        nextOffset < sorted.length
          ? encodeCursor({
              revision: catalog.revision,
              offset: nextOffset,
              limit,
              queryFingerprint: fingerprint,
            })
          : undefined,
      previousCursor:
        offset > 0
          ? encodeCursor({
              revision: catalog.revision,
              offset: previousOffset,
              limit,
              queryFingerprint: fingerprint,
            })
          : undefined,
      revision: catalog.revision,
      queryFingerprint: fingerprint,
      facets,
    })
  }

  audit(): {
    enabled: boolean
    revision: number
    skillCount: number
    tokenCount: number
    blockedCount: number
    activeInvocationCount: number
  } {
    return Object.freeze({
      enabled: this.#enabled,
      revision: this.#catalog?.revision ?? 0,
      skillCount: this.#byId.size,
      tokenCount: this.#tokens.size,
      blockedCount: [...this.#byId.values()].filter((skill) => !skill.ready).length,
      activeInvocationCount: [...this.#byId.values()].reduce(
        (count, skill) => count + skill.activeInvocationIds.length,
        0,
      ),
    })
  }

  clear(): void {
    this.#assertEnabled()
    this.#catalog = undefined
    this.#byId.clear()
    this.#tokens.clear()
  }

  disable(reason = "Skill catalog index is disabled."): void {
    this.#enabled = false
    this.#disabledReason = skillText(reason, "Skill catalog index is disabled.", 4_096)
    this.#catalog = undefined
    this.#byId.clear()
    this.#tokens.clear()
  }

  enable(): void {
    this.#enabled = true
  }

  #matchText(value: string): Set<string> {
    const terms = normalizeTerms(value)
    if (!terms.length) return new Set(this.#byId.keys())
    let output: Set<string> | undefined
    for (const term of terms) {
      const matches = new Set<string>()
      for (const [token, ids] of this.#tokens) {
        if (token === term || token.startsWith(term) || token.includes(term)) {
          for (const id of ids) matches.add(id)
        }
      }
      if (!output) {
        output = matches
      } else {
        output = new Set([...output].filter((id) => matches.has(id)))
      }
    }
    return output ?? new Set()
  }

  #assertEnabled(): void {
    if (!this.#enabled) throw new Error(this.#disabledReason)
  }
}

function normalizeQuery(query: SkillCatalogQuery): SkillCatalogQuery {
  return Object.freeze({
    text: skillText(query.text, "", 4_096),
    availability: uniqueSkillStrings(query.availability ?? []) as SkillCatalogQuery["availability"],
    sourceKinds: uniqueSkillStrings(query.sourceKinds ?? []),
    tools: uniqueSkillStrings(query.tools ?? []),
    findingSeverities:
      uniqueSkillStrings(query.findingSeverities ?? []) as SkillCatalogQuery["findingSeverities"],
    approvalStages:
      uniqueSkillStrings(query.approvalStages ?? []) as SkillCatalogQuery["approvalStages"],
    activeOnly: Boolean(query.activeOnly),
    blockedOnly: Boolean(query.blockedOnly),
    sort: query.sort ?? "name",
    direction: query.direction ?? "asc",
    limit: normalizeLimit(query.limit),
    cursor: query.cursor,
  })
}

function normalizeLimit(value: number | undefined): number {
  return skillInteger(value, 50, 1, 250)
}

function matchesQuery(
  skill: SkillProjection,
  query: SkillCatalogQuery,
): boolean {
  if (query.availability?.length && !query.availability.includes(skill.availability)) {
    return false
  }
  if (
    query.sourceKinds?.length &&
    !query.sourceKinds.includes(skill.provenance.sourceKind)
  ) {
    return false
  }
  if (
    query.tools?.length &&
    !query.tools.every((tool) =>
      skill.toolScope.allowed.includes(tool) ||
      skill.toolScope.requireApproval.includes(tool))
  ) {
    return false
  }
  if (
    query.findingSeverities?.length &&
    !skill.supplyChain.findings.some((finding) =>
      query.findingSeverities?.includes(finding.severity))
  ) {
    return false
  }
  if (
    query.approvalStages?.length &&
    !query.approvalStages.includes(skill.approval.stage)
  ) {
    return false
  }
  if (query.activeOnly && !skill.activeInvocationIds.length) return false
  if (query.blockedOnly && skill.ready) return false
  return true
}

function skillComparator(
  query: SkillCatalogQuery,
): (left: SkillProjection, right: SkillProjection) => number {
  const direction = query.direction === "desc" ? -1 : 1
  return (left, right) => {
    let comparison = 0
    if (query.sort === "updated") {
      comparison = left.lastUpdatedAt.localeCompare(right.lastUpdatedAt)
    } else if (query.sort === "risk") {
      comparison = left.supplyChain.riskScore - right.supplyChain.riskScore
    } else if (query.sort === "invocations") {
      comparison = left.invocations.length - right.invocations.length
    } else if (query.sort === "source") {
      comparison =
        compareSkillText(left.provenance.sourceKind, right.provenance.sourceKind) ||
        compareSkillText(left.provenance.sourceId, right.provenance.sourceId)
    } else {
      comparison = compareSkillText(left.displayName, right.displayName)
    }
    return (
      comparison * direction ||
      compareSkillText(left.displayName, right.displayName) ||
      compareSkillText(left.skillId, right.skillId)
    )
  }
}

function tokenizeSkill(skill: SkillProjection): Set<string> {
  return new Set(
    normalizeTerms(
      [
        skill.skillId,
        skill.name,
        skill.displayName,
        skill.description,
        skill.body.summary,
        ...skill.body.headings,
        skill.provenance.sourceId,
        skill.provenance.sourceKind,
        skill.provenance.pluginId,
        ...skill.toolScope.allowed,
        ...skill.toolScope.namespaces,
        ...skill.resources.map((resource) => resource.path),
        ...skill.dependencies.nodes.map((dependency) => dependency.name),
        ...skill.supplyChain.findings.map((finding) => finding.code),
      ].filter(Boolean).join(" "),
    ),
  )
}

function normalizeTerms(value: string): string[] {
  return [...new Set(
    skillText(value, "", 256 * 1024)
      .toLowerCase()
      .normalize("NFKC")
      .split(/[^\p{L}\p{N}_:./@+-]+/u)
      .map((term) => term.trim())
      .filter((term) => term.length >= 2)
      .slice(0, 2_048),
  )]
}

function buildFacets(rows: readonly SkillProjection[]): SkillCatalogPage["facets"] {
  const availability: Record<string, number> = {}
  const sourceKinds: Record<string, number> = {}
  const approvalStages: Record<string, number> = {}
  const severities: Record<string, number> = {}
  const tools: Record<string, number> = {}
  for (const row of rows) {
    increment(availability, row.availability)
    increment(sourceKinds, row.provenance.sourceKind)
    increment(approvalStages, row.approval.stage)
    for (const finding of row.supplyChain.findings) increment(severities, finding.severity)
    for (const tool of row.toolScope.allowed) increment(tools, tool)
  }
  return Object.freeze({
    availability: Object.freeze(availability),
    sourceKinds: Object.freeze(sourceKinds),
    approvalStages: Object.freeze(approvalStages),
    severities: Object.freeze(severities),
    tools: Object.freeze(tools),
  })
}

function emptyFacets(): SkillCatalogPage["facets"] {
  return Object.freeze({
    availability: Object.freeze({}),
    sourceKinds: Object.freeze({}),
    approvalStages: Object.freeze({}),
    severities: Object.freeze({}),
    tools: Object.freeze({}),
  })
}

function increment(target: Record<string, number>, key: string): void {
  target[key] = (target[key] ?? 0) + 1
}

function queryFingerprint(query: SkillCatalogQuery): string {
  return skillFingerprint([
    query.text,
    query.availability,
    query.sourceKinds,
    query.tools,
    query.findingSeverities,
    query.approvalStages,
    query.activeOnly,
    query.blockedOnly,
    query.sort,
    query.direction,
    query.limit,
  ])
}

function encodeCursor(payload: CursorPayload): string {
  const raw = JSON.stringify(payload)
  if (typeof btoa !== "function") {
    throw new Error("Skill catalog cursor encoder is unavailable.")
  }
  const bytes = new TextEncoder().encode(raw)
  let binary = ""
  for (let offset = 0; offset < bytes.length; offset += 8_192) {
    binary += String.fromCharCode(...bytes.slice(offset, offset + 8_192))
  }
  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "")
}

function decodeCursor(
  cursor: string,
  revision: number,
  fingerprint: string,
): CursorPayload {
  let raw: string
  try {
    if (typeof atob !== "function") {
      throw new Error("Skill catalog cursor decoder is unavailable.")
    }
    const padded = cursor.replace(/-/g, "+").replace(/_/g, "/")
      .padEnd(Math.ceil(cursor.length / 4) * 4, "=")
    const binary = atob(padded)
    raw = new TextDecoder().decode(
      Uint8Array.from(binary, (character) => character.charCodeAt(0)),
    )
  } catch {
    throw new Error("Skill catalog cursor is malformed.")
  }
  let payload: Partial<CursorPayload>
  try {
    payload = JSON.parse(raw) as Partial<CursorPayload>
  } catch {
    throw new Error("Skill catalog cursor payload is malformed.")
  }
  if (payload.revision !== revision) {
    throw new Error("Skill catalog cursor belongs to another canonical revision.")
  }
  if (payload.queryFingerprint !== fingerprint) {
    throw new Error("Skill catalog cursor belongs to another query.")
  }
  return {
    revision,
    queryFingerprint: fingerprint,
    offset: skillInteger(payload.offset, 0, 0, Number.MAX_SAFE_INTEGER),
    limit: skillInteger(payload.limit, 50, 1, 250),
  }
}
