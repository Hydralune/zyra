export interface ProductModelOption {
  providerId: string
  modelId: string
  displayName: string
  family: string
  contextWindow: number
  maximumOutputTokens: number
  reasoning: boolean
  defaultReasoningEffort?: string
  thinkingEnabled: boolean
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new TypeError(`${label} is invalid`)
  return value as Record<string, unknown>
}

function identity(value: unknown, label: string): string {
  if (typeof value !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$/u.test(value)) throw new TypeError(`${label} is invalid`)
  return value
}

export function parseProductModels(value: unknown): readonly ProductModelOption[] {
  const response = record(value, "provider model response")
  if (response.schema !== "zyra.provider-backend-api/v1" || response.state_owner !== "typescript.ProviderControlPlaneStore") {
    throw new TypeError("provider model response does not have the canonical owner")
  }
  const result = Array.isArray(response.result) ? response.result : []
  return Object.freeze(result.map((item, index) => {
    const model = record(item, `provider model ${index}`)
    const capabilities = record(model.capabilities ?? {}, "model capabilities")
    const requestDefaults = record(model.requestDefaults ?? model.request_defaults ?? {}, "model request defaults")
    const thinking = record(requestDefaults.thinking ?? {}, "model thinking defaults")
    const defaultReasoningEffort = typeof requestDefaults.reasoning_effort === "string" && requestDefaults.reasoning_effort.trim()
      ? requestDefaults.reasoning_effort.trim()
      : undefined
    return Object.freeze({
      providerId: identity(model.providerId ?? model.provider_id, "provider id"),
      modelId: identity(model.modelId ?? model.model_id, "model id"),
      displayName: typeof model.displayName === "string" && model.displayName.trim() ? model.displayName.trim() : identity(model.modelId ?? model.model_id, "model id"),
      family: typeof model.family === "string" ? model.family : "",
      contextWindow: Number.isSafeInteger(model.contextWindow) ? Number(model.contextWindow) : 0,
      maximumOutputTokens: Number.isSafeInteger(model.maximumOutputTokens) ? Number(model.maximumOutputTokens) : 0,
      reasoning: capabilities.reasoning === true,
      ...(defaultReasoningEffort ? { defaultReasoningEffort } : {}),
      thinkingEnabled: thinking.type === "enabled",
    })
  }))
}
