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

export function formatModelStatus(task?: TaskProjection, configured?: {
  providerId: string
  modelId: string
  defaultReasoningEffort?: string
  reasoningEffort?: string
  supportedReasoningEfforts?: readonly string[]
  thinkingEnabled?: boolean
}): string {
  if (!task) {
    if (!configured) return "model · 默认自动路由\n使用 /model 从 canonical available model catalog 中选择后续 task 的模型。"
    const reasoning = configured.reasoningEffort
      ? `reasoning · 已选择 ${configured.reasoningEffort}`
      : configured.defaultReasoningEffort
        ? `reasoning · provider 默认 ${configured.defaultReasoningEffort}`
      : configured.thinkingEnabled
        ? "reasoning · provider 默认启用 thinking"
        : "reasoning · 使用 provider canonical 默认值"
    return [
      `provider · ${configured.providerId}`,
      `model · ${configured.modelId}`,
      reasoning,
      `推理强度 · ${configured.supportedReasoningEfforts?.length ? `可选 ${configured.supportedReasoningEfforts.join("/")}` : "provider 未声明可覆盖集合"}`,
      "作用域 · 后续新 task；运行中 task 不会被静默改写",
    ].join("\n")
  }
  const route = object(task.metadata.provider_route_ref ?? task.metadata.provider_route)
  const provider = firstText(route.provider_id, route.providerId, task.metadata.provider, task.metadata.model_provider)
  const model = firstText(route.model_id, route.modelId, task.metadata.model)
  const execution = object(task.metadata.product_execution_config)
  const reasoningEffort = firstText(execution.reasoning_effort)
  return [
    `provider · ${provider ?? "canonical task 未公开"}`,
    `model · ${model ?? "canonical task 未公开"}`,
    `reasoning · ${reasoningEffort ?? "provider canonical 默认值"}`,
    "作用域 · 当前 task 的 canonical metadata（只读）",
    "切换 · 运行中 task 固定；/model 只为后续新 task 建立显式配置",
  ].join("\n")
}

export type ProductExecutionMode = "standard" | "sealed_autonomous"

export function formatExecutionMode(task?: TaskProjection, configured: ProductExecutionMode = "standard"): string {
  if (!task) return [
    `execution · ${configured}`,
    `sealed · ${configured === "sealed_autonomous" ? "yes" : "no"}`,
    "作用域 · 后续新 task；创建后写入 canonical task metadata",
    "权限/沙箱 · 由任务 binding 与 canonical custody 决定，CLI 不伪造 session 全局开关",
  ].join("\n")
  const competition = firstText(task.metadata.competition_mode) ?? "standard"
  const sealed = task.metadata.sealed === true || competition === "sealed_autonomous"
  return [
    `execution · ${competition}`,
    `sealed · ${sealed ? "yes" : "no"}`,
    "权限决定 · canonical custody + request binding，缺失时 fail closed",
    "切换 · 当前后端没有 session-scoped permission/sandbox mode mutation 契约",
  ].join("\n")
}
