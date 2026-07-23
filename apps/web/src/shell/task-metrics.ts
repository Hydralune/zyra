import type { TaskProjection } from "../../../../packages/core/typed-api-client/src/index.ts"

export interface TaskMetrics {
  elapsedMs: number
  nodeCount: number
  completedNodeCount: number
  activeNodeCount: number
  failedNodeCount: number
  blockedNodeCount: number
  artifactCount: number
  artifactBytes: number
  workerCount: number
  maximumDepth: number
  dependencyEdges: number
  progress: number
}

const COMPLETED = new Set(["completed", "succeeded", "verified"])
const ACTIVE = new Set(["running", "active", "dispatched"])
const FAILED = new Set(["failed", "cancelled", "interrupted"])
const BLOCKED = new Set(["blocked", "paused", "waiting"])

function timestamp(value: string): number {
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : 0
}

export function taskMetrics(task: TaskProjection, now = Date.now()): TaskMetrics {
  const created = timestamp(task.createdAt)
  const updated = timestamp(task.updatedAt)
  const end = task.terminal ? updated : Math.max(updated, now)
  const workers = new Set<string>()
  const nodes = new Map(task.planNodes.map((node) => [node.nodeId, node]))
  let completedNodeCount = 0
  let activeNodeCount = 0
  let failedNodeCount = 0
  let blockedNodeCount = 0
  let maximumDepth = 0
  let dependencyEdges = 0
  for (const node of task.planNodes) {
    if (COMPLETED.has(node.status)) completedNodeCount += 1
    if (ACTIVE.has(node.status)) activeNodeCount += 1
    if (FAILED.has(node.status)) failedNodeCount += 1
    if (BLOCKED.has(node.status)) blockedNodeCount += 1
    if (node.assignedWorkerId) workers.add(node.assignedWorkerId)
    dependencyEdges += node.dependsOn.length
    let depth = 0
    let current = node
    const seen = new Set([current.nodeId])
    while (current.parentNodeId) {
      const parent = nodes.get(current.parentNodeId)
      if (!parent || seen.has(parent.nodeId)) break
      seen.add(parent.nodeId)
      depth += 1
      current = parent
    }
    maximumDepth = Math.max(maximumDepth, depth)
  }
  const artifactBytes = task.artifacts.reduce(
    (total, artifact) => total + Math.max(0, artifact.sizeBytes ?? 0),
    0,
  )
  const nodeCount = task.planNodes.length
  return {
    elapsedMs: created ? Math.max(0, end - created) : 0,
    nodeCount,
    completedNodeCount,
    activeNodeCount,
    failedNodeCount,
    blockedNodeCount,
    artifactCount: task.artifacts.length,
    artifactBytes,
    workerCount: workers.size,
    maximumDepth,
    dependencyEdges,
    progress: nodeCount
      ? Math.round((completedNodeCount / nodeCount) * 100)
      : task.terminal
        ? 100
        : 0,
  }
}

export function formatDuration(milliseconds: number): string {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000))
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ${minutes % 60}m`
  const days = Math.floor(hours / 24)
  return `${days}d ${hours % 24}h`
}

export function taskHealthLabel(metrics: TaskMetrics): string {
  if (metrics.failedNodeCount) {
    return `${metrics.failedNodeCount} failed node${metrics.failedNodeCount === 1 ? "" : "s"}`
  }
  if (metrics.blockedNodeCount) {
    return `${metrics.blockedNodeCount} blocked node${metrics.blockedNodeCount === 1 ? "" : "s"}`
  }
  if (metrics.activeNodeCount) {
    return `${metrics.activeNodeCount} active node${metrics.activeNodeCount === 1 ? "" : "s"}`
  }
  if (metrics.nodeCount && metrics.completedNodeCount === metrics.nodeCount) return "All nodes complete"
  return "Awaiting runtime progress"
}
