import type { RuntimeReadiness, TaskProjection } from "@zyra/typed-api-client"

function object(value: unknown): Readonly<Record<string, unknown>> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Readonly<Record<string, unknown>> : {}
}

function firstText(...values: unknown[]): string | undefined {
  return values.find((value): value is string => typeof value === "string" && Boolean(value.trim()))?.trim()
}

export function formatRuntimeReadiness(readiness: RuntimeReadiness): string {
  const ownerEntries = Object.entries(readiness.owners)
  const readyOwners = ownerEntries.filter(([, ready]) => ready).length
  const lines = [
    `daemon · ${readiness.ready ? "ready" : readiness.status} · API ${readiness.apiVersion}`,
    `runtime owners · ${readyOwners}/${ownerEntries.length} ready`,
  ]
  if (readiness.blockers.length) lines.push(...readiness.blockers.slice(0, 12).map((blocker) => `blocker · ${blocker}`))
  return lines.join("\n")
}

export function formatModelStatus(task?: TaskProjection, configured?: { providerId: string; modelId: string }): string {
  if (!task) {
    return configured
      ? `provider · ${configured.providerId}\nmodel · ${configured.modelId}\n作用域 · 后续新 task；运行中 task 不会被静默改写`
      : "model · 默认自动路由\n使用 /model 从 canonical available model catalog 中选择后续 task 的模型。"
  }
  const route = object(task.metadata.provider_route_ref ?? task.metadata.provider_route)
  const provider = firstText(route.provider_id, route.providerId, task.metadata.provider, task.metadata.model_provider)
  const model = firstText(route.model_id, route.modelId, task.metadata.model)
  return [
    `provider · ${provider ?? "canonical task 未公开"}`,
    `model · ${model ?? "canonical task 未公开"}`,
    "作用域 · 当前 task 的 canonical metadata（只读）",
    "切换 · 运行中 task 固定；/model 只为后续新 task 建立显式配置",
  ].join("\n")
}

export function formatExecutionMode(task?: TaskProjection): string {
  if (!task) return "mode · standard\n权限/沙箱模式将在 task 创建后以 canonical metadata 为准。"
  const competition = firstText(task.metadata.competition_mode) ?? "standard"
  const sealed = task.metadata.sealed === true || competition === "sealed_autonomous"
  return [
    `execution · ${competition}`,
    `sealed · ${sealed ? "yes" : "no"}`,
    "权限决定 · canonical custody + request binding，缺失时 fail closed",
    "切换 · 当前后端没有 session-scoped permission/sandbox mode mutation 契约",
  ].join("\n")
}
