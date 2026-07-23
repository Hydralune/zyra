import type { TaskProjection } from "../../../../packages/core/typed-api-client/src/index.ts"

export type TaskSort = "updated-desc" | "updated-asc" | "created-desc" | "goal" | "status"

export interface TaskQuery {
  text?: string
  statuses?: readonly string[]
  activeOnly?: boolean
  terminalOnly?: boolean
  hasArtifacts?: boolean
  sort?: TaskSort
}

function searchableTask(task: TaskProjection): string {
  return [
    task.taskId,
    task.runId,
    task.sessionId,
    task.userGoal,
    task.status,
    ...task.planNodes.flatMap((node) => [
      node.nodeId,
      node.title,
      node.description,
      node.assignedWorkerId,
    ]),
    ...task.artifacts.flatMap((artifact) => [
      artifact.artifactId,
      artifact.title,
      artifact.kind,
      artifact.path,
    ]),
  ].filter(Boolean).join(" ").toLowerCase()
}

function queryTerms(value: string | undefined): string[] {
  return value?.trim().toLowerCase().split(/\s+/g).filter(Boolean) ?? []
}

export function queryTasks(tasks: readonly TaskProjection[], query: TaskQuery): TaskProjection[] {
  const terms = queryTerms(query.text)
  const statuses = query.statuses?.length
    ? new Set(query.statuses.map((status) => status.trim().toLowerCase()))
    : undefined
  const filtered = tasks.filter((task) => {
    if (statuses && !statuses.has(task.status)) return false
    if (query.activeOnly && !task.active) return false
    if (query.terminalOnly && !task.terminal) return false
    if (query.hasArtifacts && !task.artifacts.length) return false
    if (terms.length) {
      const haystack = searchableTask(task)
      if (!terms.every((term) => haystack.includes(term))) return false
    }
    return true
  })
  const sort = query.sort ?? "updated-desc"
  return [...filtered].sort((left, right) => {
    if (sort === "updated-desc") {
      return Date.parse(right.updatedAt) - Date.parse(left.updatedAt) || left.taskId.localeCompare(right.taskId)
    }
    if (sort === "updated-asc") {
      return Date.parse(left.updatedAt) - Date.parse(right.updatedAt) || left.taskId.localeCompare(right.taskId)
    }
    if (sort === "created-desc") {
      return Date.parse(right.createdAt) - Date.parse(left.createdAt) || left.taskId.localeCompare(right.taskId)
    }
    if (sort === "goal") {
      return left.userGoal.localeCompare(right.userGoal) || left.taskId.localeCompare(right.taskId)
    }
    return left.status.localeCompare(right.status) || left.taskId.localeCompare(right.taskId)
  })
}

export function taskQuerySummary(
  all: readonly TaskProjection[],
  visible: readonly TaskProjection[],
  query: TaskQuery,
): string {
  if (all.length === visible.length && !query.text && !query.statuses?.length) {
    return `${all.length} task${all.length === 1 ? "" : "s"}`
  }
  const filters: string[] = []
  if (query.text) filters.push(`matching “${query.text.trim()}”`)
  if (query.statuses?.length) filters.push(`with status ${query.statuses.join(", ")}`)
  if (query.activeOnly) filters.push("that are active")
  if (query.terminalOnly) filters.push("that are terminal")
  if (query.hasArtifacts) filters.push("with artifacts")
  return `${visible.length} of ${all.length} tasks ${filters.join(" ")}`
}
