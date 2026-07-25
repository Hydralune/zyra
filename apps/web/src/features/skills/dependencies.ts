import type { JsonObject } from "../../events/ingress/index.ts"
import type {
  SkillDependencyEdge,
  SkillDependencyGraph,
  SkillDependencyProjection,
} from "./contracts.ts"
import {
  firstSkillArray,
  firstSkillText,
  optionalSkillHash,
  optionalSkillIdentity,
  skillBoolean,
  skillFingerprint,
  skillRecord,
  skillRecords,
  skillText,
  uniqueSkillStrings,
} from "./value.ts"

const DEPENDENCY_KINDS = new Set([
  "skill",
  "plugin",
  "mcp",
  "tool",
  "resource",
  "package",
  "unknown",
])

export function buildSkillDependencyGraph(
  skillId: string,
  records: readonly JsonObject[],
): SkillDependencyGraph {
  const raw = collectDependencyRecords(records)
  const nodes = new Map<string, SkillDependencyProjection>()
  const edges: SkillDependencyEdge[] = []
  for (let index = 0; index < raw.length; index += 1) {
    const item = raw[index]!
    const parent = safeDependencyIdentity(
      firstSkillText([item], ["parent_id", "from", "owner_skill_id"], skillId),
      skillId,
    )
    const name = firstSkillText(
      [item],
      ["name", "dependency_name", "skill_name", "plugin_id", "server_id", "tool_name", "path"],
      `dependency-${index}`,
    )
    const kind = normalizeDependencyKind(
      firstSkillText([item], ["kind", "type", "dependency_kind"], "unknown"),
    )
    const dependencyId = safeDependencyIdentity(
      firstSkillText([item], [
        "dependency_id",
        "dependencyId",
        "id",
        "resolved_id",
      ]),
      `${kind}:${name}`,
    )
    const warnings: string[] = []
    const required = skillBoolean(item.required, !skillBoolean(item.optional, false))
    const optional = !required || skillBoolean(item.optional, false)
    const available = skillBoolean(
      item.available ?? item.resolved ?? item.exists,
      Boolean(item.resolved_id ?? item.resolved_version ?? item.digest),
    )
    const approved = skillBoolean(
      item.approved ?? item.reviewed ?? item.admitted,
      false,
    )
    const versionRange = firstSkillText(
      [item],
      ["version_range", "versionRange", "requirement", "requested_version"],
    ) || undefined
    const resolvedVersion = firstSkillText(
      [item],
      ["resolved_version", "resolvedVersion", "version"],
    ) || undefined
    if (required && !available) warnings.push("required dependency is unavailable")
    if (!approved) warnings.push("dependency has not passed staged approval")
    if (versionRange && resolvedVersion && !versionSatisfies(resolvedVersion, versionRange)) {
      warnings.push(`resolved version ${resolvedVersion} does not satisfy ${versionRange}`)
    }
    if (parent === dependencyId) warnings.push("dependency self-cycle")
    const projected: SkillDependencyProjection = Object.freeze({
      dependencyId,
      name,
      kind,
      versionRange,
      resolvedVersion,
      resolvedId: optionalDependencyIdentity(
        firstSkillText([item], ["resolved_id", "resolvedId"]),
      ),
      required,
      optional,
      approved,
      available,
      digest: optionalSkillHash(
        firstSkillText([item], ["digest", "content_digest", "manifest_digest"]),
        "skill dependency digest",
      ),
      source: firstSkillText([item], ["source", "provenance"]) || undefined,
      warnings: Object.freeze(warnings),
    })
    const previous = nodes.get(dependencyId)
    nodes.set(dependencyId, mergeDependency(previous, projected))
    edges.push(Object.freeze({
      from: parent,
      to: dependencyId,
      kind,
      required,
      approved,
    }))
  }
  const normalizedEdges = deduplicateEdges(edges)
  const cycles = dependencyCycles(skillId, normalizedEdges)
  const topologicalOrder = topologicalDependencies(
    skillId,
    [...nodes.keys()],
    normalizedEdges,
  )
  const missing = [...nodes.values()]
    .filter((node) => node.required && !node.available)
    .map((node) => node.dependencyId)
    .sort()
  const unapproved = [...nodes.values()]
    .filter((node) => !node.approved)
    .map((node) => node.dependencyId)
    .sort()
  const versionConflicts = [...nodes.values()]
    .filter((node) =>
      Boolean(
        node.versionRange &&
        node.resolvedVersion &&
        !versionSatisfies(node.resolvedVersion, node.versionRange),
      ))
    .map((node) => node.dependencyId)
    .sort()
  const maximumDepth = dependencyDepth(skillId, normalizedEdges)
  const outputNodes = [...nodes.values()].sort((left, right) =>
    left.kind.localeCompare(right.kind) ||
    left.name.localeCompare(right.name) ||
    left.dependencyId.localeCompare(right.dependencyId))
  const core = {
    rootId: skillId,
    nodes: outputNodes,
    edges: normalizedEdges,
    topologicalOrder,
    missing,
    unapproved,
    versionConflicts,
    cycles,
    maximumDepth,
  }
  return Object.freeze({
    ...core,
    nodes: Object.freeze(outputNodes),
    edges: Object.freeze(normalizedEdges),
    topologicalOrder: Object.freeze(topologicalOrder),
    missing: Object.freeze(missing),
    unapproved: Object.freeze(unapproved),
    versionConflicts: Object.freeze(versionConflicts),
    cycles: Object.freeze(cycles.map((cycle) => Object.freeze(cycle))),
    complete:
      missing.length === 0 &&
      unapproved.length === 0 &&
      versionConflicts.length === 0 &&
      cycles.length === 0,
    digest: skillFingerprint([core]),
  })
}

function collectDependencyRecords(records: readonly JsonObject[]): JsonObject[] {
  const result: JsonObject[] = []
  for (const record of records) {
    for (const key of [
      "dependencies",
      "dependency_graph",
      "resource_dependencies",
      "requires",
    ]) {
      const value = record[key]
      if (
        value &&
        typeof value === "object" &&
        !Array.isArray(value) &&
        Array.isArray((value as JsonObject).nodes)
      ) {
        const graph = value as JsonObject
        const edgeRecords = skillRecords(graph.edges)
        const edgeByTarget = new Map<string, JsonObject>()
        for (const edge of edgeRecords) {
          const target = firstSkillText([edge], ["to", "target", "dependency_id"])
          if (target) edgeByTarget.set(target, edge)
        }
        for (const node of skillRecords(graph.nodes)) {
          const id = firstSkillText([node], ["dependency_id", "id", "name"])
          result.push(Object.freeze({
            ...edgeByTarget.get(id),
            ...node,
          }))
        }
        continue
      }
      result.push(...skillRecords(value))
    }
  }
  return result
}

function mergeDependency(
  previous: SkillDependencyProjection | undefined,
  next: SkillDependencyProjection,
): SkillDependencyProjection {
  if (!previous) return next
  return Object.freeze({
    ...previous,
    ...next,
    required: previous.required || next.required,
    optional: previous.optional && next.optional,
    approved: previous.approved && next.approved,
    available: previous.available || next.available,
    digest: next.digest ?? previous.digest,
    versionRange: next.versionRange ?? previous.versionRange,
    resolvedVersion: next.resolvedVersion ?? previous.resolvedVersion,
    resolvedId: next.resolvedId ?? previous.resolvedId,
    warnings: Object.freeze(
      uniqueSkillStrings([...previous.warnings, ...next.warnings]),
    ),
  })
}

function deduplicateEdges(
  edges: readonly SkillDependencyEdge[],
): SkillDependencyEdge[] {
  const byIdentity = new Map<string, SkillDependencyEdge>()
  for (const edge of edges) {
    const key = `${edge.from}\u001f${edge.to}\u001f${edge.kind}`
    const previous = byIdentity.get(key)
    byIdentity.set(key, Object.freeze({
      ...edge,
      required: (previous?.required ?? false) || edge.required,
      approved: (previous?.approved ?? true) && edge.approved,
    }))
  }
  return [...byIdentity.values()].sort((left, right) =>
    left.from.localeCompare(right.from) ||
    left.to.localeCompare(right.to) ||
    left.kind.localeCompare(right.kind))
}

function dependencyCycles(
  rootId: string,
  edges: readonly SkillDependencyEdge[],
): string[][] {
  const outgoing = edgeAdjacency(edges)
  const seen = new Set<string>()
  const active = new Set<string>()
  const stack: string[] = []
  const cycles: string[][] = []
  const visit = (node: string): void => {
    if (active.has(node)) {
      const start = stack.indexOf(node)
      const cycle = [...stack.slice(Math.max(0, start)), node]
      const canonical = canonicalCycle(cycle)
      const key = canonical.join("\u001f")
      if (!cycles.some((entry) => entry.join("\u001f") === key)) cycles.push(canonical)
      return
    }
    if (seen.has(node)) return
    seen.add(node)
    active.add(node)
    stack.push(node)
    for (const child of outgoing.get(node) ?? []) visit(child)
    stack.pop()
    active.delete(node)
  }
  visit(rootId)
  for (const node of outgoing.keys()) visit(node)
  return cycles.sort((left, right) => left.join().localeCompare(right.join()))
}

function canonicalCycle(cycle: readonly string[]): string[] {
  const body = cycle.slice(0, -1)
  if (!body.length) return [...cycle]
  let best = body
  for (let index = 1; index < body.length; index += 1) {
    const rotated = [...body.slice(index), ...body.slice(0, index)]
    if (rotated.join("\u001f") < best.join("\u001f")) best = rotated
  }
  return [...best, best[0]!]
}

function topologicalDependencies(
  rootId: string,
  nodeIds: readonly string[],
  edges: readonly SkillDependencyEdge[],
): string[] {
  const all = new Set([rootId, ...nodeIds, ...edges.flatMap((edge) => [edge.from, edge.to])])
  const indegree = new Map([...all].map((id) => [id, 0]))
  const outgoing = edgeAdjacency(edges)
  for (const edge of edges) indegree.set(edge.to, (indegree.get(edge.to) ?? 0) + 1)
  const queue = [...all].filter((id) => (indegree.get(id) ?? 0) === 0).sort()
  const output: string[] = []
  while (queue.length) {
    const node = queue.shift()!
    output.push(node)
    for (const child of outgoing.get(node) ?? []) {
      indegree.set(child, (indegree.get(child) ?? 0) - 1)
      if (indegree.get(child) === 0) {
        queue.push(child)
        queue.sort()
      }
    }
  }
  for (const node of [...all].sort()) {
    if (!output.includes(node)) output.push(node)
  }
  return output
}

function dependencyDepth(
  rootId: string,
  edges: readonly SkillDependencyEdge[],
): number {
  const outgoing = edgeAdjacency(edges)
  let maximum = 0
  const visit = (node: string, depth: number, path: ReadonlySet<string>): void => {
    maximum = Math.max(maximum, depth)
    if (depth >= 64) return
    for (const child of outgoing.get(node) ?? []) {
      if (path.has(child)) continue
      visit(child, depth + 1, new Set([...path, child]))
    }
  }
  visit(rootId, 0, new Set([rootId]))
  return maximum
}

function edgeAdjacency(
  edges: readonly SkillDependencyEdge[],
): Map<string, string[]> {
  const output = new Map<string, string[]>()
  for (const edge of edges) {
    const values = output.get(edge.from) ?? []
    if (!values.includes(edge.to)) values.push(edge.to)
    values.sort()
    output.set(edge.from, values)
  }
  return output
}

function normalizeDependencyKind(
  value: string,
): SkillDependencyProjection["kind"] {
  const normalized = value.toLowerCase().replace(/[\s-]+/g, "_")
  if (DEPENDENCY_KINDS.has(normalized)) {
    return normalized as SkillDependencyProjection["kind"]
  }
  if (/server|mcp/.test(normalized)) return "mcp"
  if (/asset|file|resource/.test(normalized)) return "resource"
  if (/npm|pip|crate|package/.test(normalized)) return "package"
  return "unknown"
}

function safeDependencyIdentity(value: string, fallback: string): string {
  for (const candidate of [value, fallback]) {
    try {
      const identity = optionalSkillIdentity(candidate, "skill dependency identity")
      if (identity) return identity
    } catch {
      // Try the bounded deterministic identity below.
    }
  }
  return `dependency:${skillFingerprint([fallback]).slice("skill:".length)}`
}

function optionalDependencyIdentity(value: string): string | undefined {
  try {
    return optionalSkillIdentity(value, "resolved dependency identity")
  } catch {
    return undefined
  }
}

export function versionSatisfies(version: string, range: string): boolean {
  const normalizedVersion = parseVersion(version)
  const normalizedRange = skillText(range, "", 256).trim()
  if (!normalizedRange || normalizedRange === "*" || normalizedRange.toLowerCase() === "latest") {
    return true
  }
  const alternatives = normalizedRange.split(/\s*\|\|\s*/).filter(Boolean)
  return alternatives.some((alternative) => {
    const conditions = alternative.split(/\s+/).filter(Boolean)
    return conditions.every((condition) =>
      evaluateVersionCondition(normalizedVersion, condition))
  })
}

function evaluateVersionCondition(
  version: readonly number[],
  condition: string,
): boolean {
  const match = condition.match(/^(<=|>=|<|>|=|~|\^)?v?(\d+(?:\.\d+){0,3})(?:[-+].*)?$/)
  if (!match) return false
  const operator = match[1] ?? "="
  const expected = parseVersion(match[2]!)
  const comparison = compareVersionParts(version, expected)
  if (operator === ">") return comparison > 0
  if (operator === ">=") return comparison >= 0
  if (operator === "<") return comparison < 0
  if (operator === "<=") return comparison <= 0
  if (operator === "^") {
    return comparison >= 0 && version[0] === expected[0]
  }
  if (operator === "~") {
    return comparison >= 0 &&
      version[0] === expected[0] &&
      version[1] === expected[1]
  }
  return comparison === 0
}

function parseVersion(value: string): number[] {
  const match = skillText(value, "0", 128).match(/\d+(?:\.\d+){0,3}/)
  return (match?.[0] ?? "0").split(".").map((item) => Number.parseInt(item, 10) || 0)
}

function compareVersionParts(
  left: readonly number[],
  right: readonly number[],
): number {
  for (let index = 0; index < Math.max(left.length, right.length, 3); index += 1) {
    const delta = (left[index] ?? 0) - (right[index] ?? 0)
    if (delta) return delta
  }
  return 0
}
