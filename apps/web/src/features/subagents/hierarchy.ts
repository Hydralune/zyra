import type {
  SubagentHierarchy,
  SubagentHierarchyNode,
  SubagentRow,
} from "./contracts.ts"
import {
  compareNumber,
  compareText,
  fingerprint,
  sortStable,
  unique,
} from "./value.ts"

interface MutableNode {
  id: string
  row: SubagentRow
  parentId: string
  declaredDepth: number
  childIds: string[]
  path: string[]
  depth: number
  cyclic: boolean
  orphaned: boolean
}

export function buildSubagentHierarchy(
  rows: readonly SubagentRow[],
  rootIdentities: readonly string[],
): SubagentHierarchy {
  const rootSet = new Set(rootIdentities.filter(Boolean))
  const rowsById = new Map(rows.map((row) => [row.id, row]))
  const nodes = new Map<string, MutableNode>()
  for (const row of rows) {
    nodes.set(row.id, {
      id: row.id,
      row,
      parentId: row.parentId,
      declaredDepth: row.depth,
      childIds: [],
      path: [],
      depth: 0,
      cyclic: false,
      orphaned: false,
    })
  }
  for (const node of nodes.values()) {
    const parent = nodes.get(node.parentId)
    if (parent && parent.id !== node.id) parent.childIds.push(node.id)
    if (!parent && !rootSet.has(node.parentId)) node.orphaned = true
  }
  for (const node of nodes.values()) {
    node.childIds = [...new Set(node.childIds)].sort((left, right) =>
      compareRows(rowsById.get(left), rowsById.get(right)))
  }
  const cycleIds = detectCycles(nodes)
  for (const id of cycleIds) {
    const node = nodes.get(id)
    if (node) node.cyclic = true
  }
  const roots = selectRoots(nodes, rootSet, cycleIds)
  const visited = new Set<string>()
  const order: string[] = []
  const walk = (
    id: string,
    path: readonly string[],
    depth: number,
  ) => {
    const node = nodes.get(id)
    if (!node || visited.has(id)) return
    visited.add(id)
    node.path = [...path, id]
    node.depth = depth
    order.push(id)
    for (const childId of node.childIds) {
      walk(childId, node.path, depth + 1)
    }
  }
  for (const id of roots) walk(id, [], 0)
  for (const id of sortStable(nodes.keys(), compareText)) {
    if (!visited.has(id)) walk(id, [], 0)
  }
  const descendants = descendantIndex(nodes)
  const immutable: Record<string, SubagentHierarchyNode> = {}
  let maximumDepth = 0
  for (const id of order) {
    const node = nodes.get(id)!
    const descendantIds = descendants.get(id) ?? []
    const descendantRows = descendantIds
      .map((childId) => rowsById.get(childId))
      .filter((row): row is SubagentRow => Boolean(row))
    maximumDepth = Math.max(maximumDepth, node.depth)
    immutable[id] = Object.freeze({
      id,
      row: node.row,
      parentId: node.parentId,
      depth: node.depth,
      declaredDepth: node.declaredDepth,
      childIds: Object.freeze([...node.childIds]),
      descendantIds: Object.freeze([...descendantIds]),
      activeDescendants: descendantRows.filter((row) => !row.terminal).length,
      terminalDescendants: descendantRows.filter((row) => row.terminal).length,
      budgetExceededDescendants: descendantRows.filter((row) => row.budget.exceeded).length,
      heartbeatRiskDescendants: descendantRows.filter((row) =>
        ["stale", "timed_out", "missing"].includes(row.heartbeat.phase)).length,
      cyclic: node.cyclic,
      orphaned: node.orphaned,
      path: Object.freeze([...node.path]),
    })
  }
  const orphanIds = order.filter((id) => nodes.get(id)?.orphaned)
  return Object.freeze({
    nodes: Object.freeze(immutable),
    roots: Object.freeze([...roots]),
    order: Object.freeze([...order]),
    orphanIds: Object.freeze(orphanIds),
    cycleIds: Object.freeze([...cycleIds].sort(compareText)),
    maximumDepth,
    fingerprint: fingerprint([
      roots,
      order.map((id) => {
        const node = immutable[id]
        return [
          id,
          node.parentId,
          node.depth,
          node.declaredDepth,
          node.childIds,
          node.cyclic,
          node.orphaned,
        ]
      }),
    ]),
  })
}

function selectRoots(
  nodes: ReadonlyMap<string, MutableNode>,
  rootSet: ReadonlySet<string>,
  cycleIds: ReadonlySet<string>,
): readonly string[] {
  const candidates: string[] = []
  for (const node of nodes.values()) {
    if (
      !nodes.has(node.parentId) ||
      rootSet.has(node.parentId) ||
      cycleIds.has(node.id) ||
      node.parentId === node.id
    ) {
      candidates.push(node.id)
    }
  }
  return sortStable(new Set(candidates), (left, right) =>
    compareRows(nodes.get(left)?.row, nodes.get(right)?.row))
}

function detectCycles(
  nodes: ReadonlyMap<string, MutableNode>,
): Set<string> {
  const cycleIds = new Set<string>()
  const visited = new Set<string>()
  const active = new Set<string>()
  const stack: string[] = []
  const visit = (id: string) => {
    if (active.has(id)) {
      const start = stack.indexOf(id)
      for (const member of stack.slice(Math.max(0, start))) cycleIds.add(member)
      cycleIds.add(id)
      return
    }
    if (visited.has(id)) return
    visited.add(id)
    active.add(id)
    stack.push(id)
    const parentId = nodes.get(id)?.parentId
    if (parentId && nodes.has(parentId)) visit(parentId)
    stack.pop()
    active.delete(id)
  }
  for (const id of nodes.keys()) visit(id)
  return cycleIds
}

function descendantIndex(
  nodes: ReadonlyMap<string, MutableNode>,
): ReadonlyMap<string, readonly string[]> {
  const output = new Map<string, readonly string[]>()
  const visit = (
    id: string,
    active: ReadonlySet<string>,
  ): readonly string[] => {
    const cached = output.get(id)
    if (cached) return cached
    if (active.has(id)) return Object.freeze([])
    const node = nodes.get(id)
    if (!node) return Object.freeze([])
    const nextActive = new Set(active)
    nextActive.add(id)
    const selected: string[] = []
    for (const childId of node.childIds) {
      if (nextActive.has(childId)) continue
      selected.push(childId)
      selected.push(...visit(childId, nextActive))
    }
    const result = unique(selected)
    output.set(id, result)
    return result
  }
  for (const id of nodes.keys()) visit(id, new Set())
  return output
}

function compareRows(
  left: SubagentRow | undefined,
  right: SubagentRow | undefined,
): number {
  if (!left && !right) return 0
  if (!left) return 1
  if (!right) return -1
  const lifecycleRank = (row: SubagentRow) => {
    if (row.lifecycle === "running") return 0
    if (["starting", "dispatched", "ready"].includes(row.lifecycle)) return 1
    if (["waiting", "paused", "recovering", "resuming"].includes(row.lifecycle)) return 2
    if (row.lifecycle === "failed") return 4
    if (row.lifecycle === "cancelled" || row.lifecycle === "killed") return 5
    if (row.lifecycle === "completed") return 6
    return 3
  }
  return (
    compareNumber(lifecycleRank(left), lifecycleRank(right)) ||
    compareNumber(left.depth, right.depth) ||
    compareNumber(left.sequence, right.sequence) ||
    compareText(left.id, right.id)
  )
}

export function visibleHierarchyRows(
  hierarchy: SubagentHierarchy,
  expandedIds: ReadonlySet<string>,
  matches: (row: SubagentRow) => boolean,
): readonly SubagentRow[] {
  const visible: SubagentRow[] = []
  const forcedOpen = new Set<string>()
  for (const id of hierarchy.order) {
    const node = hierarchy.nodes[id]
    if (!node || !matches(node.row)) continue
    for (const member of node.path.slice(0, -1)) forcedOpen.add(member)
  }
  const hiddenParents = new Set<string>()
  for (const id of hierarchy.order) {
    const node = hierarchy.nodes[id]
    if (!node) continue
    const parentHidden = node.path
      .slice(0, -1)
      .some((parentId) => hiddenParents.has(parentId))
    if (parentHidden) {
      hiddenParents.add(id)
      continue
    }
    const selfMatches = matches(node.row)
    const descendantMatches = node.descendantIds.some((childId) => {
      const child = hierarchy.nodes[childId]?.row
      return child ? matches(child) : false
    })
    if (selfMatches || descendantMatches) visible.push(node.row)
    if (
      node.childIds.length > 0 &&
      !expandedIds.has(id) &&
      !forcedOpen.has(id)
    ) {
      for (const childId of node.childIds) hiddenParents.add(childId)
    }
  }
  return Object.freeze(visible)
}

export function hierarchyPath(
  hierarchy: SubagentHierarchy,
  childId: string,
): readonly SubagentRow[] {
  const node = hierarchy.nodes[childId]
  if (!node) return Object.freeze([])
  return Object.freeze(
    node.path
      .map((id) => hierarchy.nodes[id]?.row)
      .filter((row): row is SubagentRow => Boolean(row)),
  )
}

export function hierarchyAncestors(
  hierarchy: SubagentHierarchy,
  childId: string,
): readonly SubagentRow[] {
  return Object.freeze(hierarchyPath(hierarchy, childId).slice(0, -1))
}

export function hierarchyDescendants(
  hierarchy: SubagentHierarchy,
  childId: string,
): readonly SubagentRow[] {
  const node = hierarchy.nodes[childId]
  if (!node) return Object.freeze([])
  return Object.freeze(
    node.descendantIds
      .map((id) => hierarchy.nodes[id]?.row)
      .filter((row): row is SubagentRow => Boolean(row)),
  )
}

export function hierarchyScopeViolations(
  hierarchy: SubagentHierarchy,
): Readonly<Record<string, readonly string[]>> {
  const output: Record<string, readonly string[]> = {}
  for (const id of hierarchy.order) {
    const node = hierarchy.nodes[id]
    if (!node) continue
    const violations: string[] = []
    if (node.cyclic) violations.push("parent cycle")
    if (node.orphaned) violations.push("parent absent")
    const root = hierarchy.nodes[node.path[0] ?? id]
    const expectedDepth = (root?.declaredDepth ?? 0) + node.depth
    if (node.declaredDepth !== expectedDepth) violations.push("declared depth mismatch")
    if (node.row.scope.lineage.length > 0) {
      const declared = node.row.scope.lineage
      const expected = node.path
      const suffix = declared.slice(-expected.length)
      if (!suffix.every((member, index) => member === expected[index])) {
        violations.push("scope lineage mismatch")
      }
    }
    if (violations.length > 0) output[id] = Object.freeze(violations)
  }
  return Object.freeze(output)
}
