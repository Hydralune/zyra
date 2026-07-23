import type { PlanNodeProjection, TaskProjection } from "../../../../packages/core/typed-api-client/src/index.ts"

export interface TaskTreeNode {
  node: PlanNodeProjection
  depth: number
  path: readonly string[]
  children: readonly TaskTreeNode[]
  dependencyBlocked: boolean
  descendantCount: number
}

export interface TaskTree {
  roots: readonly TaskTreeNode[]
  flat: readonly TaskTreeNode[]
  orphans: readonly string[]
  cycles: readonly string[]
  missingDependencies: Readonly<Record<string, readonly string[]>>
  statusCounts: Readonly<Record<string, number>>
  completed: number
  total: number
  progress: number
}

const COMPLETED = new Set(["completed", "succeeded", "verified"])
const FAILED = new Set(["failed", "cancelled", "blocked", "interrupted"])

function clonePlanNode(node: PlanNodeProjection): PlanNodeProjection {
  return {
    ...node,
    dependsOn: [...node.dependsOn],
    artifactIds: [...node.artifactIds],
    metadata: { ...node.metadata },
  }
}

export function buildTaskTree(task: TaskProjection): TaskTree {
  const nodes = new Map(task.planNodes.map((node) => [node.nodeId, clonePlanNode(node)]))
  const children = new Map<string, PlanNodeProjection[]>()
  const roots: PlanNodeProjection[] = []
  const orphans: string[] = []
  const cycles = new Set<string>()
  const missingDependencies: Record<string, readonly string[]> = {}
  const statusCounts: Record<string, number> = {}

  for (const node of nodes.values()) {
    statusCounts[node.status] = (statusCounts[node.status] ?? 0) + 1
    const missing = node.dependsOn.filter((dependency) => !nodes.has(dependency))
    if (missing.length) missingDependencies[node.nodeId] = Object.freeze([...missing])
    if (!node.parentNodeId) {
      roots.push(node)
      continue
    }
    const parent = nodes.get(node.parentNodeId)
    if (!parent) {
      orphans.push(node.nodeId)
      roots.push(node)
      continue
    }
    const values = children.get(parent.nodeId) ?? []
    values.push(node)
    children.set(parent.nodeId, values)
  }

  const ordered = (values: readonly PlanNodeProjection[]): PlanNodeProjection[] =>
    [...values].sort((left, right) => {
      if (left.nodeId === task.rootNodeId) return -1
      if (right.nodeId === task.rootNodeId) return 1
      const updated = Date.parse(left.updatedAt ?? "") - Date.parse(right.updatedAt ?? "")
      return updated || left.nodeId.localeCompare(right.nodeId)
    })

  const flat: TaskTreeNode[] = []
  const visit = (
    node: PlanNodeProjection,
    depth: number,
    path: readonly string[],
    ancestors: ReadonlySet<string>,
  ): TaskTreeNode => {
    if (ancestors.has(node.nodeId)) {
      cycles.add([...path, node.nodeId].join(" -> "))
      const cyclic: TaskTreeNode = {
        node,
        depth,
        path: Object.freeze([...path, node.nodeId]),
        children: Object.freeze([]),
        dependencyBlocked: true,
        descendantCount: 0,
      }
      flat.push(cyclic)
      return cyclic
    }
    const nextAncestors = new Set(ancestors)
    nextAncestors.add(node.nodeId)
    const nextPath = Object.freeze([...path, node.nodeId])
    const nested = ordered(children.get(node.nodeId) ?? []).map((child) =>
      visit(child, depth + 1, nextPath, nextAncestors),
    )
    const dependencyBlocked = node.dependsOn.some((dependency) => {
      const candidate = nodes.get(dependency)
      return !candidate || FAILED.has(candidate.status) || !COMPLETED.has(candidate.status)
    })
    const result: TaskTreeNode = {
      node,
      depth,
      path: nextPath,
      children: Object.freeze(nested),
      dependencyBlocked,
      descendantCount: nested.reduce((total, child) => total + child.descendantCount + 1, 0),
    }
    flat.push(result)
    return result
  }

  const rootNodes = ordered(
    roots.length
      ? roots
      : task.planNodes.filter((node) => node.nodeId === task.rootNodeId),
  )
  const treeRoots = rootNodes.map((root) => visit(root, 0, [], new Set()))
  const visited = new Set(flat.map((entry) => entry.node.nodeId))
  for (const node of ordered(task.planNodes.filter((entry) => !visited.has(entry.nodeId)))) {
    orphans.push(node.nodeId)
    treeRoots.push(visit(node, 0, [], new Set()))
  }
  flat.sort((left, right) => {
    const leftPath = left.path.join("/")
    const rightPath = right.path.join("/")
    return leftPath.localeCompare(rightPath)
  })
  const completed = task.planNodes.filter((node) => COMPLETED.has(node.status)).length
  const total = task.planNodes.length
  return {
    roots: Object.freeze(treeRoots),
    flat: Object.freeze(flat),
    orphans: Object.freeze([...new Set(orphans)].sort()),
    cycles: Object.freeze([...cycles].sort()),
    missingDependencies: Object.freeze(missingDependencies),
    statusCounts: Object.freeze(statusCounts),
    completed,
    total,
    progress: total ? Math.round((completed / total) * 100) : task.terminal ? 100 : 0,
  }
}

export function visibleTaskTree(
  tree: TaskTree,
  collapsed: ReadonlySet<string>,
): TaskTreeNode[] {
  const result: TaskTreeNode[] = []
  const append = (node: TaskTreeNode) => {
    result.push(node)
    if (collapsed.has(node.node.nodeId)) return
    for (const child of node.children) append(child)
  }
  for (const root of tree.roots) append(root)
  return result
}

export function nextTreeNodeId(
  visible: readonly TaskTreeNode[],
  currentId: string | undefined,
  direction: "next" | "previous" | "parent" | "first-child",
): string | undefined {
  if (!visible.length) return undefined
  const index = Math.max(0, visible.findIndex((entry) => entry.node.nodeId === currentId))
  const current = visible[index]!
  if (direction === "next") return visible[Math.min(visible.length - 1, index + 1)]?.node.nodeId
  if (direction === "previous") return visible[Math.max(0, index - 1)]?.node.nodeId
  if (direction === "parent") return current.node.parentNodeId ?? current.node.nodeId
  return current.children[0]?.node.nodeId ?? current.node.nodeId
}
