import type { RuntimeReadiness, TaskProjection } from "@zyra/typed-api-client"

function object(value: unknown): Readonly<Record<string, unknown>> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Readonly<Record<string, unknown>> : {}
}

function firstText(...values: unknown[]): string | undefined {
  return values.find((value): value is string => typeof value === "string" && Boolean(value.trim()))?.trim()
}

export interface ProviderConfigurationStatus {
  configured: boolean
  providerIds: readonly string[]
  selectedProviderId?: string
  selectedModelId?: string
}

export function providerConfigurationFromReadiness(
  readiness: RuntimeReadiness,
): ProviderConfigurationStatus | undefined {
  const details = object(readiness.raw.details)
  const configuration = object(details.provider_configuration)
  if (typeof configuration.configured !== "boolean") return undefined
  const providerIds = Array.isArray(configuration.provider_ids)
    ? configuration.provider_ids.filter((value): value is string => typeof value === "string" && Boolean(value.trim()))
    : []
  return Object.freeze({
    configured: configuration.configured,
    providerIds: Object.freeze(providerIds),
    selectedProviderId: firstText(configuration.selected_provider_id),
    selectedModelId: firstText(configuration.selected_model_id),
  })
}

export function formatRuntimeReadiness(readiness: RuntimeReadiness): string {
  const ownerEntries = Object.entries(readiness.owners)
  const readyOwners = ownerEntries.filter(([, ready]) => ready).length
  const lines = [
    `本地服务 · ${readiness.ready ? "已就绪" : `未就绪（${readiness.status}）`} · API ${readiness.apiVersion}`,
    `执行组件 · ${readyOwners}/${ownerEntries.length} 已就绪`,
  ]
  const provider = providerConfigurationFromReadiness(readiness)
  lines.push(provider
    ? provider.configured
      ? `模型连接 · 已配置${provider.selectedProviderId ? `（${provider.selectedProviderId}/${provider.selectedModelId ?? "自动"}）` : ""}`
      : "模型连接 · 未配置"
    : "模型连接 · 当前 daemon 未提供配置状态；请重启 daemon")
  if (readiness.blockers.length) lines.push(...readiness.blockers.slice(0, 12).map((blocker) => `阻塞项 · ${blocker}`))
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
    if (!configured) return "模型 · 自动选择\n使用 /model 为之后的新任务选择模型。"
    const reasoning = configured.reasoningEffort
      ? `推理强度 · ${configured.reasoningEffort}`
      : configured.defaultReasoningEffort
        ? `推理强度 · 模型默认（${configured.defaultReasoningEffort}）`
      : configured.thinkingEnabled
        ? "推理强度 · 使用模型默认值（支持深度思考）"
        : "推理强度 · 使用模型默认值"
    return [
      `提供方 · ${configured.providerId}`,
      `模型 · ${configured.modelId}`,
      reasoning,
      `可选强度 · ${configured.supportedReasoningEfforts?.length ? configured.supportedReasoningEfforts.join("/") : "由模型决定"}`,
      "生效范围 · 之后创建的新任务；不会改变正在运行的任务",
    ].join("\n")
  }
  const route = object(task.metadata.provider_route_ref ?? task.metadata.provider_route)
  const provider = firstText(route.provider_id, route.providerId, task.metadata.provider, task.metadata.model_provider)
  const model = firstText(route.model_id, route.modelId, task.metadata.model)
  const execution = object(task.metadata.product_execution_config)
  const reasoningEffort = firstText(execution.reasoning_effort)
  return [
    `提供方 · ${provider ?? "当前任务未提供"}`,
    `模型 · ${model ?? "当前任务未提供"}`,
    `推理强度 · ${reasoningEffort ?? "使用模型默认值"}`,
    "生效范围 · 当前任务（只读）",
    "切换说明 · 运行中不能切换；/model 只影响之后的新任务",
  ].join("\n")
}

export type ProductExecutionMode = "standard" | "sealed_autonomous"

export function formatExecutionMode(task?: TaskProjection, configured: ProductExecutionMode = "standard"): string {
  if (!task) return [
    `执行模式 · ${configured === "sealed_autonomous" ? "封闭自治" : "标准"}`,
    `封闭运行 · ${configured === "sealed_autonomous" ? "是" : "否"}`,
    `本地目录 · ${configured === "sealed_autonomous" ? "不挂载 CLI 当前目录" : "按本地执行环境受控挂载"}`,
    "生效范围 · 之后创建的新任务",
    "权限与沙箱 · 由每个任务的实际策略决定",
  ].join("\n")
  const competition = firstText(task.metadata.competition_mode) ?? "standard"
  const sealed = task.metadata.sealed === true || competition === "sealed_autonomous"
  return [
    `执行模式 · ${sealed ? "封闭自治" : competition === "standard" ? "标准" : competition}`,
    `封闭运行 · ${sealed ? "是" : "否"}`,
    `本地目录 · ${sealed ? "隔离任务工作区；不挂载 CLI 当前目录" : "按任务执行环境策略挂载"}`,
    "权限决定 · 每次决定都与当前请求严格绑定；无法确认时拒绝执行",
    "权限策略 · 使用 /permissions mode 查看或修改",
  ].join("\n")
}
