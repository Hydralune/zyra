import type {
  CanonicalProjectionState,
  CausalEventProjection,
  ProjectionSelector,
  SchedulerProjection,
} from "../../state/contracts.ts"
import { projectionSelector } from "../../state/selectors.ts"
import {
  bounded,
  compareNumber,
  compareText,
  decimalFrom,
  fingerprint,
  integerFrom,
  listFrom,
  mean,
  mergeRecords,
  omitSecrets,
  ratio,
  record,
  sortStable,
  sum,
  text,
  textFrom,
  truthFrom,
  unique,
  values,
} from "../session/value.ts"

export type ProviderHealth = "healthy" | "degraded" | "rate-limited" | "exhausted" | "offline" | "unknown"
export type RouteAdmission = "selected" | "eligible" | "rejected" | "fallback" | "failed"

export interface ProviderCapability {
  id: string
  available: boolean
  limit?: number
  units?: string
  source: string
}

export interface ProviderQuota {
  limit: number
  remaining: number
  used: number
  resetAt?: string
  utilization: number
  exhausted: boolean
  source: string
}

export interface RateLimitWindow {
  name: string
  limit: number
  remaining: number
  resetAt?: string
  windowSeconds?: number
  exhausted: boolean
}

export interface ModelRow {
  id: string
  providerId: string
  name: string
  family?: string
  contextWindow: number
  maxOutputTokens: number
  inputPricePerMillion?: number
  outputPricePerMillion?: number
  cachedPricePerMillion?: number
  supportsTools: boolean
  supportsVision: boolean
  supportsAudio: boolean
  supportsReasoning: boolean
  supportsStreaming: boolean
  supportsJson: boolean
  supportsEmbeddings: boolean
  supportsBatch: boolean
  available: boolean
  deprecated: boolean
  credentialPresent: boolean
  capabilityIds: readonly string[]
  integrationIds: readonly string[]
  quota?: ProviderQuota
  rateLimits: readonly RateLimitWindow[]
  metadata: Readonly<Record<string, unknown>>
}

export interface ProviderRow {
  id: string
  name: string
  health: ProviderHealth
  enabled: boolean
  credentialPresent: boolean
  credentialVersion?: number
  credentialFingerprint?: string
  integrationIds: readonly string[]
  capabilities: readonly ProviderCapability[]
  models: readonly ModelRow[]
  quota?: ProviderQuota
  rateLimits: readonly RateLimitWindow[]
  lastError?: string
  lastEventId?: string
  endpointKind?: string
  locality?: string
  secretLeakDetected: boolean
}

export interface ProviderRouteCandidate {
  id: string
  providerId: string
  modelId: string
  routeId?: string
  admission: RouteAdmission
  rank: number
  score: number
  latencyEstimateMs?: number
  costEstimateUsd?: number
  requiredCapabilities: readonly string[]
  missingCapabilities: readonly string[]
  reasons: readonly string[]
  quotaAvailable: boolean
  credentialPresent: boolean
  health: ProviderHealth
  sourceSchedulerId?: string
}

export interface ProviderUsage {
  providerId: string
  modelId?: string
  inputTokens: number
  outputTokens: number
  cachedTokens: number
  requestCount: number
  failureCount: number
  costUsd: number
  latencyMs: number
  averageLatencyMs: number
  sourceEventIds: readonly string[]
}

export interface ProviderRoute {
  routeId: string
  providerId: string
  modelId: string
  sessionId?: string
  turnId?: string
  transportId?: string
  catalogRevision?: number
  credentialVersion?: number
  credentialFingerprint?: string
  checksum?: string
  selectedAt?: string
  sourceEventId?: string
  fallbackFromProviderId?: string
  fallbackReason?: string
  degraded: boolean
}

export interface ProviderConsoleProjection {
  taskId: string
  providers: readonly ProviderRow[]
  models: readonly ModelRow[]
  candidates: readonly ProviderRouteCandidate[]
  selectedRoute?: ProviderRoute
  fallbackChain: readonly ProviderRoute[]
  usage: readonly ProviderUsage[]
  totalCostUsd: number
  totalInputTokens: number
  totalOutputTokens: number
  totalFailures: number
  credentialPresenceCount: number
  secretLeakCount: number
  connected: boolean
  committedSequence: number
  revision: number
  fingerprint: string
}

export interface ProviderProjectionOptions {
  requiredCapabilities?: readonly string[]
  purpose?: string
  maximumCostUsd?: number
  maximumLatencyMs?: number
}

export function selectProviderConsole(
  taskId: string,
  options: ProviderProjectionOptions = {},
): ProjectionSelector<ProviderConsoleProjection> {
  const normalized = normalizeOptions(options)
  return projectionSelector(
    `provider-console:${taskId}:${fingerprint([normalized])}`,
    [
      `task:${taskId}`,
      "domain:scheduler",
      "domain:session",
      "domain:worker",
      "domain:event",
      "domain:command",
      "domain:recovery",
    ],
    (state) => buildProviderConsoleProjection(state, taskId, normalized),
    (left, right) => left === right || left.fingerprint === right.fingerprint,
  )
}

export function buildProviderConsoleProjection(
  state: CanonicalProjectionState,
  taskId: string,
  options: ProviderProjectionOptions = {},
): ProviderConsoleProjection {
  const normalized = normalizeOptions(options)
  const schedulers = Object.values(state.schedulers)
    .filter((scheduler) => scheduler.taskId === taskId)
  const events = (state.causality.byTask[taskId] ?? [])
    .map((eventId) => state.causality.byEvent[eventId])
    .filter((event): event is CausalEventProjection => Boolean(event))
    .sort((left, right) => left.sequence - right.sequence || left.eventId.localeCompare(right.eventId))
  const catalog = providerCatalog(schedulers, events)
  const providers = catalog.providers
  const models = providers.flatMap((provider) => provider.models)
  const selectedRoute = selectedProviderRoute(schedulers, events)
  const fallbackChain = buildFallbackChain(events, selectedRoute)
  const candidates = providerCandidates(
    schedulers,
    providers,
    models,
    selectedRoute,
    normalized,
  )
  const usage = providerUsage(events, providers, models)
  const runtime = state.runtimes[taskId]
  const cursor = state.cursors[taskId]
  const resultFingerprint = fingerprint([
    state.revision,
    normalized,
    providers.map((provider) => [provider.id, provider.health, provider.models.length]),
    candidates.map((candidate) => [candidate.id, candidate.admission, candidate.score]),
    selectedRoute,
    usage.map((item) => [item.providerId, item.modelId, item.requestCount, item.costUsd]),
  ])
  return Object.freeze({
    taskId,
    providers: Object.freeze(providers),
    models: Object.freeze(models),
    candidates: Object.freeze(candidates),
    selectedRoute,
    fallbackChain: Object.freeze(fallbackChain),
    usage: Object.freeze(usage),
    totalCostUsd: sum(usage.map((item) => item.costUsd)),
    totalInputTokens: sum(usage.map((item) => item.inputTokens)),
    totalOutputTokens: sum(usage.map((item) => item.outputTokens)),
    totalFailures: sum(usage.map((item) => item.failureCount)),
    credentialPresenceCount: providers.filter((provider) => provider.credentialPresent).length,
    secretLeakCount: providers.filter((provider) => provider.secretLeakDetected).length,
    connected: runtime?.connected ?? false,
    committedSequence: cursor?.committedSequence ?? runtime?.lastSequence ?? 0,
    revision: state.revision,
    fingerprint: resultFingerprint,
  })
}

function providerCatalog(
  schedulers: readonly SchedulerProjection[],
  events: readonly CausalEventProjection[],
): { providers: ProviderRow[] } {
  const providerRecords = new Map<string, Record<string, unknown>>()
  const modelRecords: Record<string, unknown>[] = []
  for (const scheduler of schedulers) {
    const attributes = mergeRecords(scheduler.attributes, scheduler.metadata)
    if (scheduler.providerId) {
      providerRecords.set(
        scheduler.providerId,
        mergeRecords(providerRecords.get(scheduler.providerId), attributes, {
          id: scheduler.providerId,
          model_id: scheduler.modelId,
          route_id: scheduler.routeId,
          last_event_id: scheduler.lastEventId,
        }),
      )
    }
    for (const provider of values(attributes.providers ?? attributes.provider_catalog)) {
      const selected = record(provider)
      const id = textFrom(selected, ["id", "provider_id", "providerId"])
      if (!id) continue
      providerRecords.set(id, mergeRecords(providerRecords.get(id), selected))
    }
    for (const model of values(attributes.models ?? attributes.model_catalog)) {
      modelRecords.push(record(model))
    }
  }
  for (const event of events.filter((entry) => /provider|model|route|dispatch/i.test(entry.eventType))) {
    for (const ref of event.entityRefs) {
      if (!ref.startsWith("provider:")) continue
      const id = ref.slice("provider:".length)
      providerRecords.set(id, mergeRecords(providerRecords.get(id), {
        id,
        last_event_id: event.eventId,
        last_event_type: event.eventType,
        last_summary: event.summary,
      }))
    }
  }
  for (const model of modelRecords) {
    const providerId = textFrom(model, ["provider_id", "providerId", "provider"])
    if (!providerId) continue
    const provider = providerRecords.get(providerId) ?? { id: providerId }
    const models = values(provider.models)
    models.push(model)
    providerRecords.set(providerId, { ...provider, models })
  }
  const providers = [...providerRecords.entries()].map(([id, source]) =>
    buildProviderRow(id, source))
  return {
    providers: sortStable(providers, (left, right) =>
      healthRank(left.health) - healthRank(right.health) ||
      compareText(left.name, right.name) ||
      compareText(left.id, right.id)),
  }
}

function buildProviderRow(id: string, source: Record<string, unknown>): ProviderRow {
  const redacted = omitSecrets(source)
  const credential = record(source.credential ?? source.credentials)
  const credentialPresent =
    truthFrom(source, ["credential_present", "has_credential", "credentials_configured"]) ||
    truthFrom(credential, ["present", "configured", "available"])
  const models = values(source.models)
    .map((entry) => buildModelRow(id, record(entry), source))
  const implicitModel = textFrom(source, ["model_id", "modelId"])
  if (implicitModel && !models.some((model) => model.id === implicitModel)) {
    models.push(buildModelRow(id, { id: implicitModel }, source))
  }
  const health = providerHealth(source)
  const capabilities = providerCapabilities(source)
  const quota = providerQuota(source)
  const rateLimits = rateLimitWindows(source)
  return Object.freeze({
    id,
    name: textFrom(source, ["name", "title", "label"], id),
    health,
    enabled: truthFrom(source, ["enabled"], health !== "offline"),
    credentialPresent,
    credentialVersion: integerFrom(source, ["credential_version"], -1) >= 0
      ? integerFrom(source, ["credential_version"])
      : undefined,
    credentialFingerprint: textFrom(source, ["credential_fingerprint"]) || undefined,
    integrationIds: Object.freeze(listFrom(source, ["integration_ids", "integrations"])),
    capabilities: Object.freeze(capabilities),
    models: Object.freeze(models),
    quota,
    rateLimits: Object.freeze(rateLimits),
    lastError: textFrom(source, ["last_error", "error", "failure_reason"]) || undefined,
    lastEventId: textFrom(source, ["last_event_id"]) || undefined,
    endpointKind: textFrom(source, ["endpoint_kind", "transport_kind"]) || undefined,
    locality: textFrom(source, ["locality", "deployment", "location"]) || undefined,
    secretLeakDetected: JSON.stringify(redacted).includes("[present]") &&
      Object.keys(redacted).some((key) => /secret|token|api.?key|password/i.test(key)),
  })
}

function buildModelRow(
  providerId: string,
  source: Record<string, unknown>,
  provider: Record<string, unknown>,
): ModelRow {
  const id = textFrom(source, ["id", "model_id", "modelId", "name"], "unknown-model")
  const capabilities = unique([
    ...listFrom(source, ["capabilities", "capability_ids"]),
    ...listFrom(provider, ["capabilities", "capability_ids"]),
  ])
  const supports = (name: string, aliases: readonly string[] = []) =>
    truthFrom(source, [`supports_${name}`, name], capabilities.some((item) =>
      [name, ...aliases].includes(item.toLowerCase())))
  return Object.freeze({
    id,
    providerId,
    name: textFrom(source, ["name", "label"], id),
    family: textFrom(source, ["family", "model_family"]) || undefined,
    contextWindow: integerFrom(source, ["context_window", "contextWindow", "max_context_tokens"]),
    maxOutputTokens: integerFrom(source, ["max_output_tokens", "maxOutputTokens"]),
    inputPricePerMillion: optionalDecimal(source, ["input_price_per_million", "input_cost"]),
    outputPricePerMillion: optionalDecimal(source, ["output_price_per_million", "output_cost"]),
    cachedPricePerMillion: optionalDecimal(source, ["cached_price_per_million", "cache_cost"]),
    supportsTools: supports("tools", ["tool_use", "function_calling"]),
    supportsVision: supports("vision", ["image"]),
    supportsAudio: supports("audio"),
    supportsReasoning: supports("reasoning", ["thinking"]),
    supportsStreaming: supports("streaming", ["stream"]),
    supportsJson: supports("json", ["structured_output"]),
    supportsEmbeddings: supports("embeddings", ["embedding"]),
    supportsBatch: supports("batch"),
    available: truthFrom(source, ["available", "enabled"], true),
    deprecated: truthFrom(source, ["deprecated"], false),
    credentialPresent:
      truthFrom(source, ["credential_present"]) ||
      truthFrom(provider, ["credential_present", "has_credential"]),
    capabilityIds: Object.freeze(capabilities),
    integrationIds: Object.freeze(listFrom(source, ["integration_ids", "integrations"])),
    quota: providerQuota(source) ?? providerQuota(provider),
    rateLimits: Object.freeze([
      ...rateLimitWindows(source),
      ...rateLimitWindows(provider),
    ]),
    metadata: Object.freeze(omitSecrets(source)),
  })
}

function providerCapabilities(source: Record<string, unknown>): ProviderCapability[] {
  const explicit = values(source.capability_details ?? source.capabilities)
  const rows: ProviderCapability[] = []
  for (const entry of explicit) {
    if (typeof entry === "string") {
      rows.push({
        id: entry,
        available: true,
        source: "catalog",
      })
      continue
    }
    const selected = record(entry)
    const id = textFrom(selected, ["id", "name", "capability"])
    if (!id) continue
    rows.push({
      id,
      available: truthFrom(selected, ["available", "enabled", "supported"], true),
      limit: optionalInteger(selected, ["limit", "maximum"]),
      units: textFrom(selected, ["units", "unit"]) || undefined,
      source: textFrom(selected, ["source"], "catalog"),
    })
  }
  return sortStable(rows, (left, right) => compareText(left.id, right.id))
}

function providerQuota(source: Record<string, unknown>): ProviderQuota | undefined {
  const quota = mergeRecords(source.quota, {
    limit: source.quota_limit,
    remaining: source.quota_remaining,
    used: source.quota_used,
    reset_at: source.quota_reset_at,
  })
  const limit = integerFrom(quota, ["limit", "maximum"], -1)
  const remaining = integerFrom(quota, ["remaining"], -1)
  const used = integerFrom(quota, ["used"], limit >= 0 && remaining >= 0 ? limit - remaining : -1)
  if (limit < 0 && remaining < 0 && used < 0) return undefined
  const normalizedLimit = Math.max(0, limit)
  const normalizedUsed = Math.max(0, used)
  const normalizedRemaining = remaining >= 0
    ? remaining
    : Math.max(0, normalizedLimit - normalizedUsed)
  return Object.freeze({
    limit: normalizedLimit,
    remaining: normalizedRemaining,
    used: normalizedUsed,
    resetAt: textFrom(quota, ["reset_at", "resetAt"]) || undefined,
    utilization: ratio(normalizedUsed, normalizedLimit),
    exhausted: normalizedLimit > 0 && normalizedRemaining <= 0,
    source: textFrom(quota, ["source"], "provider-control-plane"),
  })
}

function rateLimitWindows(source: Record<string, unknown>): RateLimitWindow[] {
  const explicit = values(source.rate_limits ?? source.rateLimits)
  const rows = explicit.map((entry, index) => {
    const selected = record(entry)
    const limit = integerFrom(selected, ["limit", "maximum"])
    const remaining = integerFrom(selected, ["remaining"], limit)
    return Object.freeze({
      name: textFrom(selected, ["name", "kind"], `window-${index}`),
      limit,
      remaining,
      resetAt: textFrom(selected, ["reset_at", "resetAt"]) || undefined,
      windowSeconds: optionalInteger(selected, ["window_seconds", "windowSeconds"]),
      exhausted: limit > 0 && remaining <= 0,
    })
  })
  const rpm = integerFrom(source, ["requests_per_minute", "rpm"], -1)
  if (rpm >= 0 && !rows.some((row) => row.name === "requests")) {
    rows.push(Object.freeze({
      name: "requests",
      limit: rpm,
      remaining: integerFrom(source, ["requests_remaining"], rpm),
      resetAt: textFrom(source, ["requests_reset_at"]) || undefined,
      windowSeconds: 60,
      exhausted: rpm > 0 && integerFrom(source, ["requests_remaining"], rpm) <= 0,
    }))
  }
  return rows
}

function providerHealth(source: Record<string, unknown>): ProviderHealth {
  const selected = textFrom(source, ["health", "status", "state"]).toLowerCase()
  if (/rate.?limit/.test(selected)) return "rate-limited"
  if (/exhaust|quota/.test(selected)) return "exhausted"
  if (/offline|unavailable|disabled|down/.test(selected)) return "offline"
  if (/degrad|warning|partial/.test(selected)) return "degraded"
  if (/healthy|ready|available|ok|active/.test(selected)) return "healthy"
  if (truthFrom(source, ["rate_limited"])) return "rate-limited"
  if (truthFrom(source, ["quota_exhausted"])) return "exhausted"
  if (truthFrom(source, ["available", "enabled"], true)) return "healthy"
  return "unknown"
}

function selectedProviderRoute(
  schedulers: readonly SchedulerProjection[],
  events: readonly CausalEventProjection[],
): ProviderRoute | undefined {
  const selected = [...schedulers]
    .filter((scheduler) => scheduler.providerId && scheduler.modelId)
    .sort((left, right) => right.sequence - left.sequence)[0]
  if (selected) {
    const attributes = mergeRecords(selected.attributes, selected.metadata)
    return Object.freeze({
      routeId: selected.routeId ?? textFrom(attributes, ["route_id"], selected.id),
      providerId: selected.providerId!,
      modelId: selected.modelId!,
      sessionId: selected.sessionId,
      turnId: textFrom(attributes, ["turn_id"]) || undefined,
      transportId: textFrom(attributes, ["transport_id"]) || undefined,
      catalogRevision: optionalInteger(attributes, ["catalog_revision"]),
      credentialVersion: optionalInteger(attributes, ["credential_version"]),
      credentialFingerprint: textFrom(attributes, ["credential_fingerprint"]) || undefined,
      checksum: textFrom(attributes, ["route_checksum", "checksum"]) || undefined,
      selectedAt: selected.updatedAt,
      sourceEventId: selected.lastEventId,
      fallbackFromProviderId: textFrom(attributes, ["fallback_from_provider_id"]) || undefined,
      fallbackReason: textFrom(attributes, ["fallback_reason"]) || undefined,
      degraded: truthFrom(attributes, ["degraded", "fallback"]),
    })
  }
  const event = [...events].reverse().find((entry) =>
    /provider.*route.*selected|route.*provider.*selected|provider_dispatch/i.test(entry.eventType))
  if (!event) return undefined
  const providerId = entityId(event.entityRefs, "provider")
  const modelId = entityId(event.entityRefs, "model")
  if (!providerId || !modelId) return undefined
  return Object.freeze({
    routeId: entityId(event.entityRefs, "route") || `route:${event.eventId}`,
    providerId,
    modelId,
    sessionId: event.sessionId,
    selectedAt: event.createdAt,
    sourceEventId: event.eventId,
    degraded: /fallback|degrad/i.test(event.eventType),
  })
}

function buildFallbackChain(
  events: readonly CausalEventProjection[],
  selected?: ProviderRoute,
): ProviderRoute[] {
  const chain: ProviderRoute[] = []
  for (const event of events.filter((entry) =>
    /provider.*(fail|fallback|degrad|route)|model.*fallback/i.test(entry.eventType))) {
    const providerId = entityId(event.entityRefs, "provider")
    const modelId = entityId(event.entityRefs, "model")
    if (!providerId && !modelId) continue
    chain.push(Object.freeze({
      routeId: entityId(event.entityRefs, "route") || `route:${event.eventId}`,
      providerId: providerId || "unknown-provider",
      modelId: modelId || "unknown-model",
      sessionId: event.sessionId,
      selectedAt: event.createdAt,
      sourceEventId: event.eventId,
      fallbackReason: event.summary,
      degraded: true,
    }))
  }
  if (selected && !chain.some((entry) => entry.routeId === selected.routeId)) chain.push(selected)
  return chain.slice(-20)
}

function providerCandidates(
  schedulers: readonly SchedulerProjection[],
  providers: readonly ProviderRow[],
  models: readonly ModelRow[],
  selected: ProviderRoute | undefined,
  options: Required<ProviderProjectionOptions>,
): ProviderRouteCandidate[] {
  const rows: ProviderRouteCandidate[] = []
  for (const model of models) {
    const provider = providers.find((entry) => entry.id === model.providerId)
    if (!provider) continue
    const missing = options.requiredCapabilities.filter((capability) =>
      !modelCapability(model, capability))
    const reasons: string[] = []
    if (!provider.enabled) reasons.push("provider disabled")
    if (provider.health === "offline") reasons.push("provider offline")
    if (provider.health === "rate-limited") reasons.push("provider rate limited")
    if (provider.health === "exhausted") reasons.push("provider quota exhausted")
    if (!provider.credentialPresent && !model.credentialPresent) reasons.push("credential absent")
    if (!model.available) reasons.push("model unavailable")
    if (model.deprecated) reasons.push("model deprecated")
    if (missing.length) reasons.push(`missing capabilities: ${missing.join(", ")}`)
    const estimatedCost = estimateModelCost(model, 1000, 500)
    if (options.maximumCostUsd > 0 && estimatedCost > options.maximumCostUsd) {
      reasons.push("estimated cost exceeds limit")
    }
    const scheduler = schedulers.find((entry) =>
      entry.providerId === model.providerId && entry.modelId === model.id)
    const latency = scheduler?.latencyEstimateMs
    if (options.maximumLatencyMs > 0 && (latency ?? 0) > options.maximumLatencyMs) {
      reasons.push("estimated latency exceeds limit")
    }
    const selectedMatch =
      selected?.providerId === model.providerId && selected?.modelId === model.id
    const admission: RouteAdmission = selectedMatch
      ? "selected"
      : reasons.length
        ? "rejected"
        : selected?.fallbackFromProviderId === model.providerId
          ? "fallback"
          : "eligible"
    const score = candidateScore(provider, model, missing, estimatedCost, latency)
    rows.push(Object.freeze({
      id: `${model.providerId}:${model.id}`,
      providerId: model.providerId,
      modelId: model.id,
      routeId: scheduler?.routeId,
      admission,
      rank: 0,
      score,
      latencyEstimateMs: latency,
      costEstimateUsd: estimatedCost,
      requiredCapabilities: Object.freeze([...options.requiredCapabilities]),
      missingCapabilities: Object.freeze(missing),
      reasons: Object.freeze(reasons),
      quotaAvailable: !provider.quota?.exhausted && !model.quota?.exhausted,
      credentialPresent: provider.credentialPresent || model.credentialPresent,
      health: provider.health,
      sourceSchedulerId: scheduler?.id,
    }))
  }
  const sorted = sortStable(rows, (left, right) =>
    admissionRank(left.admission) - admissionRank(right.admission) ||
    compareNumber(right.score, left.score) ||
    compareText(left.id, right.id))
  return sorted.map((candidate, index) => Object.freeze({
    ...candidate,
    rank: index + 1,
  }))
}

function modelCapability(model: ModelRow, capability: string): boolean {
  const selected = capability.toLowerCase()
  if (model.capabilityIds.some((item) => item.toLowerCase() === selected)) return true
  if (["tools", "tool_use", "function_calling"].includes(selected)) return model.supportsTools
  if (["vision", "image"].includes(selected)) return model.supportsVision
  if (selected === "audio") return model.supportsAudio
  if (["reasoning", "thinking"].includes(selected)) return model.supportsReasoning
  if (["stream", "streaming"].includes(selected)) return model.supportsStreaming
  if (["json", "structured_output"].includes(selected)) return model.supportsJson
  if (["embedding", "embeddings"].includes(selected)) return model.supportsEmbeddings
  if (selected === "batch") return model.supportsBatch
  return false
}

function candidateScore(
  provider: ProviderRow,
  model: ModelRow,
  missing: readonly string[],
  cost: number,
  latency: number | undefined,
): number {
  const health = {
    healthy: 1,
    degraded: 0.7,
    "rate-limited": 0.3,
    exhausted: 0.1,
    offline: 0,
    unknown: 0.5,
  }[provider.health]
  const credential = provider.credentialPresent || model.credentialPresent ? 1 : 0
  const quota = provider.quota ? 1 - provider.quota.utilization : 0.5
  const capability = missing.length ? 0 : 1
  const costScore = 1 / (1 + Math.max(0, cost))
  const latencyScore = latency === undefined ? 0.5 : 1 / (1 + latency / 1000)
  return bounded(
    health * 0.25 +
    credential * 0.2 +
    quota * 0.15 +
    capability * 0.2 +
    costScore * 0.1 +
    latencyScore * 0.1,
    0,
    1,
  )
}

function providerUsage(
  events: readonly CausalEventProjection[],
  providers: readonly ProviderRow[],
  models: readonly ModelRow[],
): ProviderUsage[] {
  const groups = new Map<string, {
    providerId: string
    modelId?: string
    inputTokens: number
    outputTokens: number
    cachedTokens: number
    requestCount: number
    failureCount: number
    costUsd: number
    latencies: number[]
    eventIds: string[]
  }>()
  for (const event of events.filter((entry) =>
    /provider|model|llm|completion|inference/i.test(entry.eventType))) {
    const providerId = entityId(event.entityRefs, "provider") ||
      providers.find((provider) => event.summary.includes(provider.id))?.id
    if (!providerId) continue
    const modelId = entityId(event.entityRefs, "model") ||
      models.find((model) => event.summary.includes(model.id))?.id
    const key = `${providerId}\u001f${modelId ?? ""}`
    const group = groups.get(key) ?? {
      providerId,
      modelId,
      inputTokens: 0,
      outputTokens: 0,
      cachedTokens: 0,
      requestCount: 0,
      failureCount: 0,
      costUsd: 0,
      latencies: [],
      eventIds: [],
    }
    group.requestCount += /request|dispatch|completion|inference/i.test(event.eventType) ? 1 : 0
    group.failureCount += /fail|error|timeout|rate.?limit/i.test(event.eventType) ? 1 : 0
    group.eventIds.push(event.eventId)
    groups.set(key, group)
  }
  return [...groups.values()].map((group) => Object.freeze({
    providerId: group.providerId,
    modelId: group.modelId,
    inputTokens: group.inputTokens,
    outputTokens: group.outputTokens,
    cachedTokens: group.cachedTokens,
    requestCount: group.requestCount,
    failureCount: group.failureCount,
    costUsd: group.costUsd,
    latencyMs: sum(group.latencies),
    averageLatencyMs: mean(group.latencies),
    sourceEventIds: Object.freeze(group.eventIds),
  }))
}

function estimateModelCost(model: ModelRow, inputTokens: number, outputTokens: number): number {
  const input = (model.inputPricePerMillion ?? 0) * inputTokens / 1_000_000
  const output = (model.outputPricePerMillion ?? 0) * outputTokens / 1_000_000
  return input + output
}

function optionalDecimal(source: Record<string, unknown>, keys: readonly string[]): number | undefined {
  const value = decimalFrom(source, keys, Number.NaN)
  return Number.isFinite(value) ? value : undefined
}

function optionalInteger(source: Record<string, unknown>, keys: readonly string[]): number | undefined {
  const value = integerFrom(source, keys, -1)
  return value >= 0 ? value : undefined
}

function entityId(refs: readonly string[], domain: string): string {
  const prefix = `${domain}:`
  const ref = refs.find((entry) => entry.startsWith(prefix))
  return ref?.slice(prefix.length) ?? ""
}

function healthRank(health: ProviderHealth): number {
  return ["healthy", "degraded", "rate-limited", "exhausted", "offline", "unknown"].indexOf(health)
}

function admissionRank(admission: RouteAdmission): number {
  return ["selected", "fallback", "eligible", "rejected", "failed"].indexOf(admission)
}

function normalizeOptions(options: ProviderProjectionOptions): Required<ProviderProjectionOptions> {
  return {
    requiredCapabilities: Object.freeze(
      unique((options.requiredCapabilities ?? []).map((item) => text(item)).filter(Boolean)).sort(),
    ),
    purpose: text(options.purpose, "general"),
    maximumCostUsd: bounded(options.maximumCostUsd ?? 0, 0, 1000000),
    maximumLatencyMs: bounded(options.maximumLatencyMs ?? 0, 0, 86_400_000),
  }
}
