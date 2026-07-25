import type {
  CanonicalProjectionState,
  CausalEventProjection,
  ProjectionSelector,
  SchedulerProjection,
  WorkerProjection,
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
  ratio,
  sortStable,
  sum,
  text,
  textFrom,
  truthFrom,
  unique,
  values,
} from "../session/value.ts"

export type PlacementTier = "device" | "edge" | "cloud" | "unknown"
export type Sensitivity = "public" | "internal" | "confidential" | "restricted"
export type PlacementAdmission = "selected" | "eligible" | "rejected" | "migrating" | "failed"

export interface ResourceCapacity {
  cpuCores: number
  cpuUtilization: number
  gpuCount: number
  gpuMemoryMb: number
  gpuUtilization: number
  memoryMb: number
  memoryUsedMb: number
  diskMb: number
  diskUsedMb: number
  networkMbps: number
  networkLatencyMs: number
  processSlots: number
  processSlotsUsed: number
}

export interface PlacementProfile {
  id: string
  tier: PlacementTier
  label: string
  workerIds: readonly string[]
  routeIds: readonly string[]
  providerIds: readonly string[]
  modelIds: readonly string[]
  region?: string
  zone?: string
  online: boolean
  isolated: boolean
  encrypted: boolean
  trustedExecution: boolean
  supportsSecrets: boolean
  supportedSensitivity: readonly Sensitivity[]
  capabilities: readonly string[]
  capacity: ResourceCapacity
  health: number
  costPerHourUsd: number
  carbonGramsPerHour?: number
  lastEventId?: string
}

export interface PlacementConstraint {
  sensitivity: Sensitivity
  dataResidency?: string
  maximumLatencyMs?: number
  maximumCostUsd?: number
  minimumMemoryMb?: number
  minimumGpuMemoryMb?: number
  requiredCapabilities: readonly string[]
  forbiddenTiers: readonly PlacementTier[]
  allowedRegions: readonly string[]
  requiresIsolation: boolean
  requiresEncryption: boolean
  requiresTrustedExecution: boolean
  allowCloud: boolean
  allowEdge: boolean
  allowDevice: boolean
}

export interface PlacementCandidate {
  id: string
  profileId: string
  tier: PlacementTier
  admission: PlacementAdmission
  rank: number
  score: number
  resourceScore: number
  privacyScore: number
  latencyScore: number
  costScore: number
  capabilityScore: number
  availabilityScore: number
  estimatedLatencyMs: number
  estimatedCostUsd: number
  missingCapabilities: readonly string[]
  violations: readonly string[]
  reasons: readonly string[]
  sourceSchedulerId?: string
  routeId?: string
  providerId?: string
  modelId?: string
}

export interface ModelSplitLeg {
  id: string
  role: string
  profileId?: string
  tier: PlacementTier
  providerId?: string
  modelId?: string
  fraction: number
  inputSensitivity: Sensitivity
  redacted: boolean
  encrypted: boolean
  routeId?: string
  sourceEventIds: readonly string[]
}

export interface PlacementMigration {
  id: string
  fromProfileId?: string
  toProfileId?: string
  fromTier: PlacementTier
  toTier: PlacementTier
  workerId?: string
  nodeId?: string
  routeId?: string
  reason: string
  phase: "planned" | "started" | "committed" | "failed" | "rolled-back"
  checkpointId?: string
  sourceEventId: string
  correlationId: string
  sequence: number
  createdAt: string
}

export interface PlacementViolation {
  id: string
  severity: "info" | "warning" | "error" | "critical"
  code: string
  message: string
  profileId?: string
  workerId?: string
  nodeId?: string
  eventId?: string
  recoverable: boolean
  recoveryId?: string
}

export interface PlacementConsoleProjection {
  taskId: string
  constraints: PlacementConstraint
  profiles: readonly PlacementProfile[]
  candidates: readonly PlacementCandidate[]
  selectedCandidate?: PlacementCandidate
  modelSplit: readonly ModelSplitLeg[]
  migrations: readonly PlacementMigration[]
  violations: readonly PlacementViolation[]
  topologyNodeIds: readonly string[]
  timelineEventIds: readonly string[]
  selectedTier?: PlacementTier
  privacyCompliant: boolean
  slaCompliant: boolean
  costCompliant: boolean
  connected: boolean
  committedSequence: number
  revision: number
  fingerprint: string
}

export interface PlacementProjectionOptions {
  constraint?: Partial<PlacementConstraint>
  nodeId?: string
  workerId?: string
}

export function selectPlacementConsole(
  taskId: string,
  options: PlacementProjectionOptions = {},
): ProjectionSelector<PlacementConsoleProjection> {
  const normalized = normalizeOptions(options)
  return projectionSelector(
    `placement-console:${taskId}:${fingerprint([normalized])}`,
    [
      `task:${taskId}`,
      "domain:scheduler",
      "domain:worker",
      "domain:node",
      "domain:recovery",
      "domain:event",
    ],
    (state) => buildPlacementConsoleProjection(state, taskId, normalized),
    (left, right) => left === right || left.fingerprint === right.fingerprint,
  )
}

export function buildPlacementConsoleProjection(
  state: CanonicalProjectionState,
  taskId: string,
  options: PlacementProjectionOptions = {},
): PlacementConsoleProjection {
  const normalized = normalizeOptions(options)
  const schedulers = Object.values(state.schedulers)
    .filter((scheduler) => scheduler.taskId === taskId)
  const workers = Object.values(state.workers)
    .filter((worker) => worker.taskId === taskId)
  const nodes = Object.values(state.nodes)
    .filter((node) => node.taskId === taskId)
  const events = (state.causality.byTask[taskId] ?? [])
    .map((eventId) => state.causality.byEvent[eventId])
    .filter((event): event is CausalEventProjection => Boolean(event))
    .sort((left, right) => left.sequence - right.sequence || left.eventId.localeCompare(right.eventId))
  const constraints = deriveConstraints(schedulers, workers, normalized.constraint)
  const profiles = buildProfiles(schedulers, workers, events)
  const migrations = buildMigrations(events, workers)
  const candidates = buildCandidates(schedulers, profiles, constraints, migrations)
  const selectedCandidate = candidates.find((candidate) => candidate.admission === "selected")
  const modelSplit = buildModelSplit(schedulers, events, profiles, constraints)
  const violations = buildViolations(state, taskId, constraints, profiles, candidates, events)
  const timelineEventIds = unique([
    ...migrations.map((migration) => migration.sourceEventId),
    ...violations.map((violation) => violation.eventId ?? "").filter(Boolean),
    ...modelSplit.flatMap((leg) => leg.sourceEventIds),
  ])
  const topologyNodeIds = unique([
    ...nodes
      .filter((node) =>
        !normalized.nodeId ||
        node.id === normalized.nodeId ||
        node.parentId === normalized.nodeId)
      .map((node) => node.id),
    ...migrations.map((migration) => migration.nodeId ?? "").filter(Boolean),
    ...violations.map((violation) => violation.nodeId ?? "").filter(Boolean),
  ])
  const runtime = state.runtimes[taskId]
  const cursor = state.cursors[taskId]
  const resultFingerprint = fingerprint([
    state.revision,
    constraints,
    profiles.map((profile) => [profile.id, profile.health, profile.online]),
    candidates.map((candidate) => [candidate.id, candidate.admission, candidate.score]),
    migrations.map((migration) => [migration.id, migration.phase]),
    violations.map((violation) => [violation.id, violation.severity]),
  ])
  return Object.freeze({
    taskId,
    constraints,
    profiles: Object.freeze(profiles),
    candidates: Object.freeze(candidates),
    selectedCandidate,
    modelSplit: Object.freeze(modelSplit),
    migrations: Object.freeze(migrations),
    violations: Object.freeze(violations),
    topologyNodeIds: Object.freeze(topologyNodeIds),
    timelineEventIds: Object.freeze(timelineEventIds),
    selectedTier: selectedCandidate?.tier,
    privacyCompliant: !violations.some((violation) =>
      ["privacy", "sensitivity", "residency", "encryption", "isolation"].includes(violation.code)),
    slaCompliant: !violations.some((violation) =>
      ["latency", "capacity", "availability"].includes(violation.code)),
    costCompliant: !violations.some((violation) => violation.code === "cost"),
    connected: runtime?.connected ?? false,
    committedSequence: cursor?.committedSequence ?? runtime?.lastSequence ?? 0,
    revision: state.revision,
    fingerprint: resultFingerprint,
  })
}

function deriveConstraints(
  schedulers: readonly SchedulerProjection[],
  workers: readonly WorkerProjection[],
  override: Partial<PlacementConstraint>,
): PlacementConstraint {
  const selected = [...schedulers].sort((left, right) => right.sequence - left.sequence)[0]
  const source = mergeRecords(selected?.attributes, selected?.metadata)
  const sensitivity = sensitivityLevel(
    override.sensitivity ??
    textFrom(source, ["sensitivity", "privacy_class"], selected?.privacyClass ?? "internal"),
  )
  const requiredCapabilities = unique([
    ...listFrom(source, ["required_capabilities", "capability_refs"]),
    ...(override.requiredCapabilities ?? []),
  ]).sort()
  const forbiddenTiers = unique([
    ...listFrom(source, ["forbidden_tiers"]).map(placementTier),
    ...(override.forbiddenTiers ?? []),
  ])
  const allowedRegions = unique([
    ...listFrom(source, ["allowed_regions", "data_residency_regions"]),
    ...(override.allowedRegions ?? []),
  ]).sort()
  const maximumLatencyMs = override.maximumLatencyMs ??
    optionalInteger(source, ["maximum_latency_ms", "latency_sla_ms"])
  const maximumCostUsd = override.maximumCostUsd ??
    optionalDecimal(source, ["maximum_cost_usd", "cost_budget_usd"])
  const minimumMemoryMb = override.minimumMemoryMb ??
    optionalInteger(source, ["minimum_memory_mb", "memory_mb"])
  const minimumGpuMemoryMb = override.minimumGpuMemoryMb ??
    optionalInteger(source, ["minimum_gpu_memory_mb", "gpu_memory_mb"])
  const requiresIsolation = override.requiresIsolation ??
    truthFrom(source, ["requires_isolation"], sensitivity === "restricted")
  const requiresEncryption = override.requiresEncryption ??
    truthFrom(source, ["requires_encryption"], sensitivity !== "public")
  const requiresTrustedExecution = override.requiresTrustedExecution ??
    truthFrom(source, ["requires_trusted_execution"], sensitivity === "restricted")
  const allowCloud = override.allowCloud ??
    truthFrom(source, ["allow_cloud"], sensitivity !== "restricted")
  const allowEdge = override.allowEdge ?? truthFrom(source, ["allow_edge"], true)
  const allowDevice = override.allowDevice ?? truthFrom(source, ["allow_device"], true)
  return Object.freeze({
    sensitivity,
    dataResidency: override.dataResidency ??
      (textFrom(source, ["data_residency", "required_region"]) || undefined),
    maximumLatencyMs,
    maximumCostUsd,
    minimumMemoryMb,
    minimumGpuMemoryMb,
    requiredCapabilities: Object.freeze(requiredCapabilities),
    forbiddenTiers: Object.freeze(forbiddenTiers),
    allowedRegions: Object.freeze(allowedRegions),
    requiresIsolation,
    requiresEncryption,
    requiresTrustedExecution,
    allowCloud,
    allowEdge,
    allowDevice,
  })
}

function buildProfiles(
  schedulers: readonly SchedulerProjection[],
  workers: readonly WorkerProjection[],
  events: readonly CausalEventProjection[],
): PlacementProfile[] {
  const records = new Map<string, Record<string, unknown>>()
  for (const worker of workers) {
    const attributes = mergeRecords(worker.attributes, worker.metadata)
    const id = worker.placementId ??
      textFrom(attributes, ["placement_id", "device_id", "profile_id"], `worker:${worker.id}`)
    records.set(id, mergeRecords(records.get(id), attributes, {
      id,
      worker_ids: unique([
        ...listFrom(records.get(id) ?? {}, ["worker_ids"]),
        worker.id,
      ]),
      route_ids: unique([
        ...listFrom(records.get(id) ?? {}, ["route_ids"]),
        worker.routeId ?? "",
      ]).filter(Boolean),
      health: worker.health,
      last_event_id: worker.lastEventId,
    }))
  }
  for (const scheduler of schedulers) {
    const attributes = mergeRecords(scheduler.attributes, scheduler.metadata)
    const id = scheduler.placementId ??
      textFrom(attributes, ["placement_id", "device_id", "profile_id"])
    if (id) {
      records.set(id, mergeRecords(records.get(id), attributes, {
        id,
        route_ids: unique([
          ...listFrom(records.get(id) ?? {}, ["route_ids"]),
          scheduler.routeId ?? "",
        ]).filter(Boolean),
        provider_ids: unique([
          ...listFrom(records.get(id) ?? {}, ["provider_ids"]),
          scheduler.providerId ?? "",
        ]).filter(Boolean),
        model_ids: unique([
          ...listFrom(records.get(id) ?? {}, ["model_ids"]),
          scheduler.modelId ?? "",
        ]).filter(Boolean),
      }))
    }
    for (const candidate of values(attributes.placement_candidates ?? attributes.devices)) {
      const selected = candidate && typeof candidate === "object"
        ? candidate as Record<string, unknown>
        : {}
      const candidateId = textFrom(selected, ["id", "placement_id", "device_id", "profile_id"])
      if (!candidateId) continue
      records.set(candidateId, mergeRecords(records.get(candidateId), selected, { id: candidateId }))
    }
  }
  for (const event of events.filter((entry) =>
    /worker|placement|resource|device|edge|cloud/i.test(entry.eventType))) {
    const profileId =
      entityId(event.entityRefs, "placement") ||
      entityId(event.entityRefs, "device")
    if (!profileId) continue
    records.set(profileId, mergeRecords(records.get(profileId), {
      id: profileId,
      last_event_id: event.eventId,
      last_event_type: event.eventType,
      last_summary: event.summary,
    }))
  }
  return sortStable(
    [...records.entries()].map(([id, source]) => buildProfile(id, source)),
    (left, right) =>
      tierRank(left.tier) - tierRank(right.tier) ||
      compareNumber(right.health, left.health) ||
      compareText(left.id, right.id),
  )
}

function buildProfile(id: string, source: Record<string, unknown>): PlacementProfile {
  const capacity = buildCapacity(source)
  const tier = placementTier(textFrom(source, ["tier", "resource_class", "locality", "kind"], id))
  const online = truthFrom(source, ["online", "available", "connected", "enabled"], true)
  const health = healthScore(source, capacity, online)
  const supportedSensitivity = listFrom(source, [
    "supported_sensitivity",
    "privacy_classes",
    "sensitivity_levels",
  ]).map(sensitivityLevel)
  if (!supportedSensitivity.length) {
    if (tier === "device") supportedSensitivity.push("public", "internal", "confidential", "restricted")
    else if (tier === "edge") supportedSensitivity.push("public", "internal", "confidential")
    else if (tier === "cloud") supportedSensitivity.push("public", "internal")
  }
  return Object.freeze({
    id,
    tier,
    label: textFrom(source, ["label", "name", "title"], id),
    workerIds: Object.freeze(listFrom(source, ["worker_ids"])),
    routeIds: Object.freeze(listFrom(source, ["route_ids"])),
    providerIds: Object.freeze(listFrom(source, ["provider_ids"])),
    modelIds: Object.freeze(listFrom(source, ["model_ids"])),
    region: textFrom(source, ["region", "location"]) || undefined,
    zone: textFrom(source, ["zone", "availability_zone"]) || undefined,
    online,
    isolated: truthFrom(source, ["isolated", "sandboxed"], tier !== "cloud"),
    encrypted: truthFrom(source, ["encrypted", "encryption_at_rest"], tier !== "device"),
    trustedExecution: truthFrom(source, ["trusted_execution", "tee"], tier === "device"),
    supportsSecrets: truthFrom(source, ["supports_secrets", "secret_custody"], tier === "device"),
    supportedSensitivity: Object.freeze(unique(supportedSensitivity)),
    capabilities: Object.freeze(unique(listFrom(source, ["capabilities", "capability_refs"])).sort()),
    capacity,
    health,
    costPerHourUsd: decimalFrom(source, ["cost_per_hour_usd", "hourly_cost"], tier === "cloud" ? 1 : 0),
    carbonGramsPerHour: optionalDecimal(source, ["carbon_grams_per_hour"]),
    lastEventId: textFrom(source, ["last_event_id"]) || undefined,
  })
}

function buildCapacity(source: Record<string, unknown>): ResourceCapacity {
  const cpuCores = decimalFrom(source, ["cpu_cores", "cpu"], 1)
  const memoryMb = integerFrom(source, ["memory_mb", "ram_mb"], 0)
  const diskMb = integerFrom(source, ["disk_mb", "storage_mb"], 0)
  const processSlots = integerFrom(source, ["process_slots", "slots"], 1)
  return Object.freeze({
    cpuCores,
    cpuUtilization: normalizeUtilization(decimalFrom(source, ["cpu_utilization", "cpu_usage"])),
    gpuCount: integerFrom(source, ["gpu_count", "gpus"]),
    gpuMemoryMb: integerFrom(source, ["gpu_memory_mb", "vram_mb"]),
    gpuUtilization: normalizeUtilization(decimalFrom(source, ["gpu_utilization", "gpu_usage"])),
    memoryMb,
    memoryUsedMb: integerFrom(source, ["memory_used_mb", "ram_used_mb"]),
    diskMb,
    diskUsedMb: integerFrom(source, ["disk_used_mb", "storage_used_mb"]),
    networkMbps: decimalFrom(source, ["network_mbps", "bandwidth_mbps"]),
    networkLatencyMs: decimalFrom(source, ["network_latency_ms", "latency_ms"]),
    processSlots,
    processSlotsUsed: integerFrom(source, ["process_slots_used", "slots_used"]),
  })
}

function healthScore(
  source: Record<string, unknown>,
  capacity: ResourceCapacity,
  online: boolean,
): number {
  if (!online) return 0
  const explicit = decimalFrom(source, ["health_score", "health"], Number.NaN)
  if (Number.isFinite(explicit)) return bounded(explicit > 1 ? explicit / 100 : explicit, 0, 1)
  const cpu = 1 - capacity.cpuUtilization
  const gpu = capacity.gpuCount ? 1 - capacity.gpuUtilization : 1
  const memory = capacity.memoryMb
    ? 1 - ratio(capacity.memoryUsedMb, capacity.memoryMb)
    : 0.5
  const disk = capacity.diskMb
    ? 1 - ratio(capacity.diskUsedMb, capacity.diskMb)
    : 0.5
  const slots = capacity.processSlots
    ? 1 - ratio(capacity.processSlotsUsed, capacity.processSlots)
    : 0
  return bounded(mean([cpu, gpu, memory, disk, slots]), 0, 1)
}

function buildCandidates(
  schedulers: readonly SchedulerProjection[],
  profiles: readonly PlacementProfile[],
  constraints: PlacementConstraint,
  migrations: readonly PlacementMigration[],
): PlacementCandidate[] {
  const selectedPlacementIds = new Set(
    schedulers
      .filter((scheduler) => scheduler.placementId)
      .sort((left, right) => right.sequence - left.sequence)
      .slice(0, 1)
      .map((scheduler) => scheduler.placementId!),
  )
  const rows = profiles.map((profile) => {
    const scheduler = schedulers.find((entry) => entry.placementId === profile.id)
    const missingCapabilities = constraints.requiredCapabilities
      .filter((capability) => !profile.capabilities.includes(capability))
    const violations = admissionViolations(profile, constraints, missingCapabilities)
    const migrating = migrations.some((migration) =>
      migration.toProfileId === profile.id &&
      ["planned", "started"].includes(migration.phase))
    const selected = selectedPlacementIds.has(profile.id)
    const admission: PlacementAdmission = selected
      ? "selected"
      : migrating
        ? "migrating"
        : violations.length
          ? "rejected"
          : "eligible"
    const latency = scheduler?.latencyEstimateMs ?? profile.capacity.networkLatencyMs
    const cost = scheduler?.costEstimate ?? profile.costPerHourUsd
    const resourceScore = resourceScoreFor(profile, constraints)
    const privacyScore = privacyScoreFor(profile, constraints)
    const latencyScore = latency > 0 ? 1 / (1 + latency / 100) : 0.5
    const costScore = cost > 0 ? 1 / (1 + cost) : 1
    const capabilityScore = ratio(
      constraints.requiredCapabilities.length - missingCapabilities.length,
      Math.max(1, constraints.requiredCapabilities.length),
    )
    const availabilityScore = profile.online ? profile.health : 0
    const score = bounded(
      resourceScore * 0.2 +
      privacyScore * 0.25 +
      latencyScore * 0.15 +
      costScore * 0.1 +
      capabilityScore * 0.15 +
      availabilityScore * 0.15,
      0,
      1,
    )
    return Object.freeze({
      id: `candidate:${profile.id}`,
      profileId: profile.id,
      tier: profile.tier,
      admission,
      rank: 0,
      score,
      resourceScore,
      privacyScore,
      latencyScore,
      costScore,
      capabilityScore,
      availabilityScore,
      estimatedLatencyMs: latency,
      estimatedCostUsd: cost,
      missingCapabilities: Object.freeze(missingCapabilities),
      violations: Object.freeze(violations),
      reasons: Object.freeze(candidateReasons(profile, constraints, violations)),
      sourceSchedulerId: scheduler?.id,
      routeId: scheduler?.routeId,
      providerId: scheduler?.providerId,
      modelId: scheduler?.modelId,
    })
  })
  return sortStable(rows, (left, right) =>
    admissionRank(left.admission) - admissionRank(right.admission) ||
    compareNumber(right.score, left.score) ||
    compareText(left.id, right.id))
    .map((candidate, index) => Object.freeze({
      ...candidate,
      rank: index + 1,
    }))
}

function admissionViolations(
  profile: PlacementProfile,
  constraints: PlacementConstraint,
  missingCapabilities: readonly string[],
): string[] {
  const violations: string[] = []
  if (!profile.online) violations.push("profile offline")
  if (constraints.forbiddenTiers.includes(profile.tier)) violations.push("tier forbidden")
  if (profile.tier === "cloud" && !constraints.allowCloud) violations.push("cloud disallowed")
  if (profile.tier === "edge" && !constraints.allowEdge) violations.push("edge disallowed")
  if (profile.tier === "device" && !constraints.allowDevice) violations.push("device disallowed")
  if (!profile.supportedSensitivity.includes(constraints.sensitivity)) {
    violations.push(`sensitivity ${constraints.sensitivity} unsupported`)
  }
  if (constraints.requiresIsolation && !profile.isolated) violations.push("isolation required")
  if (constraints.requiresEncryption && !profile.encrypted) violations.push("encryption required")
  if (constraints.requiresTrustedExecution && !profile.trustedExecution) {
    violations.push("trusted execution required")
  }
  if (
    constraints.dataResidency &&
    profile.region &&
    profile.region !== constraints.dataResidency
  ) violations.push("data residency mismatch")
  if (
    constraints.allowedRegions.length &&
    (!profile.region || !constraints.allowedRegions.includes(profile.region))
  ) violations.push("region is not allowed")
  if (
    constraints.minimumMemoryMb &&
    profile.capacity.memoryMb < constraints.minimumMemoryMb
  ) violations.push("insufficient memory")
  if (
    constraints.minimumGpuMemoryMb &&
    profile.capacity.gpuMemoryMb < constraints.minimumGpuMemoryMb
  ) violations.push("insufficient GPU memory")
  if (
    constraints.maximumLatencyMs &&
    profile.capacity.networkLatencyMs > constraints.maximumLatencyMs
  ) violations.push("latency SLA exceeded")
  if (
    constraints.maximumCostUsd &&
    profile.costPerHourUsd > constraints.maximumCostUsd
  ) violations.push("cost budget exceeded")
  if (missingCapabilities.length) {
    violations.push(`missing capabilities: ${missingCapabilities.join(", ")}`)
  }
  return violations
}

function candidateReasons(
  profile: PlacementProfile,
  constraints: PlacementConstraint,
  violations: readonly string[],
): string[] {
  if (violations.length) return [...violations]
  const reasons = [
    `${profile.tier} tier allowed`,
    `supports ${constraints.sensitivity} sensitivity`,
  ]
  if (constraints.requiresIsolation) reasons.push("isolation available")
  if (constraints.requiresEncryption) reasons.push("encryption available")
  if (constraints.requiresTrustedExecution) reasons.push("trusted execution available")
  if (constraints.requiredCapabilities.length) reasons.push("required capabilities available")
  return reasons
}

function resourceScoreFor(
  profile: PlacementProfile,
  constraints: PlacementConstraint,
): number {
  const memory = constraints.minimumMemoryMb
    ? ratio(profile.capacity.memoryMb, constraints.minimumMemoryMb * 2)
    : 1 - ratio(profile.capacity.memoryUsedMb, Math.max(1, profile.capacity.memoryMb))
  const gpu = constraints.minimumGpuMemoryMb
    ? ratio(profile.capacity.gpuMemoryMb, constraints.minimumGpuMemoryMb * 2)
    : profile.capacity.gpuCount
      ? 1 - profile.capacity.gpuUtilization
      : 0.5
  const cpu = 1 - profile.capacity.cpuUtilization
  const slots = 1 - ratio(
    profile.capacity.processSlotsUsed,
    Math.max(1, profile.capacity.processSlots),
  )
  return bounded(mean([memory, gpu, cpu, slots]), 0, 1)
}

function privacyScoreFor(
  profile: PlacementProfile,
  constraints: PlacementConstraint,
): number {
  let score = profile.supportedSensitivity.includes(constraints.sensitivity) ? 0.4 : 0
  if (!constraints.requiresIsolation || profile.isolated) score += 0.2
  if (!constraints.requiresEncryption || profile.encrypted) score += 0.2
  if (!constraints.requiresTrustedExecution || profile.trustedExecution) score += 0.2
  return bounded(score, 0, 1)
}

function buildModelSplit(
  schedulers: readonly SchedulerProjection[],
  events: readonly CausalEventProjection[],
  profiles: readonly PlacementProfile[],
  constraints: PlacementConstraint,
): ModelSplitLeg[] {
  const legs: ModelSplitLeg[] = []
  for (const scheduler of schedulers) {
    const attributes = mergeRecords(scheduler.attributes, scheduler.metadata)
    const explicit = values(attributes.model_split ?? attributes.route_legs)
    for (const [index, entry] of explicit.entries()) {
      const selected = entry && typeof entry === "object"
        ? entry as Record<string, unknown>
        : {}
      const profileId = textFrom(selected, ["profile_id", "placement_id"])
      const profile = profiles.find((item) => item.id === profileId)
      legs.push(Object.freeze({
        id: textFrom(selected, ["id", "leg_id"], `${scheduler.id}:leg:${index}`),
        role: textFrom(selected, ["role", "purpose"], `leg-${index + 1}`),
        profileId: profileId || undefined,
        tier: profile?.tier ?? placementTier(textFrom(selected, ["tier"])),
        providerId: textFrom(selected, ["provider_id"]) || scheduler.providerId,
        modelId: textFrom(selected, ["model_id"]) || scheduler.modelId,
        fraction: bounded(decimalFrom(selected, ["fraction", "weight"], 1), 0, 1),
        inputSensitivity: sensitivityLevel(
          textFrom(selected, ["input_sensitivity"], constraints.sensitivity),
        ),
        redacted: truthFrom(selected, ["redacted"], false),
        encrypted: truthFrom(selected, ["encrypted"], profile?.encrypted ?? false),
        routeId: textFrom(selected, ["route_id"]) || scheduler.routeId,
        sourceEventIds: Object.freeze(
          (stateEventIds(events, scheduler.id, scheduler.routeId)).slice(-100),
        ),
      }))
    }
  }
  if (!legs.length) {
    for (const scheduler of schedulers.filter((entry) => entry.providerId || entry.modelId)) {
      const profile = profiles.find((item) => item.id === scheduler.placementId)
      legs.push(Object.freeze({
        id: `${scheduler.id}:primary`,
        role: "primary",
        profileId: scheduler.placementId,
        tier: profile?.tier ?? "unknown",
        providerId: scheduler.providerId,
        modelId: scheduler.modelId,
        fraction: 1,
        inputSensitivity: constraints.sensitivity,
        redacted: false,
        encrypted: profile?.encrypted ?? false,
        routeId: scheduler.routeId,
        sourceEventIds: Object.freeze(stateEventIds(events, scheduler.id, scheduler.routeId)),
      }))
    }
  }
  const total = sum(legs.map((leg) => leg.fraction))
  if (total > 0 && Math.abs(total - 1) > 0.0001) {
    return legs.map((leg) => Object.freeze({
      ...leg,
      fraction: leg.fraction / total,
    }))
  }
  return legs
}

function stateEventIds(
  events: readonly CausalEventProjection[],
  schedulerId: string,
  routeId?: string,
): string[] {
  return events
    .filter((event) =>
      event.entityRefs.includes(`scheduler:${schedulerId}`) ||
      Boolean(routeId && event.entityRefs.includes(`route:${routeId}`)))
    .map((event) => event.eventId)
}

function buildMigrations(
  events: readonly CausalEventProjection[],
  workers: readonly WorkerProjection[],
): PlacementMigration[] {
  return events
    .filter((event) => /migrat|relocat|rerout|placement.*change/i.test(event.eventType))
    .map((event) => {
      const workerId = event.workerId || entityId(event.entityRefs, "worker")
      const worker = workers.find((entry) => entry.id === workerId)
      const fromProfileId =
        entityId(event.entityRefs.filter((ref) => /from/i.test(ref)), "placement") ||
        undefined
      const toProfileId =
        entityId(event.entityRefs.filter((ref) => /to/i.test(ref)), "placement") ||
        worker?.placementId
      return Object.freeze({
        id: `migration:${event.eventId}`,
        fromProfileId,
        toProfileId,
        fromTier: placementTier(entityId(event.entityRefs, "from-tier")),
        toTier: placementTier(entityId(event.entityRefs, "to-tier")),
        workerId: workerId || undefined,
        nodeId: event.nodeId,
        routeId: entityId(event.entityRefs, "route") || worker?.routeId,
        reason: event.summary,
        phase: migrationPhase(event.eventType),
        checkpointId: event.checkpointId,
        sourceEventId: event.eventId,
        correlationId: event.correlationId,
        sequence: event.sequence,
        createdAt: event.createdAt,
      })
    })
    .sort((left, right) => right.sequence - left.sequence)
}

function migrationPhase(eventType: string): PlacementMigration["phase"] {
  const selected = eventType.toLowerCase()
  if (/rollback|rolled_back/.test(selected)) return "rolled-back"
  if (/fail|error|reject/.test(selected)) return "failed"
  if (/commit|complete|applied/.test(selected)) return "committed"
  if (/start|running/.test(selected)) return "started"
  return "planned"
}

function buildViolations(
  state: CanonicalProjectionState,
  taskId: string,
  constraints: PlacementConstraint,
  profiles: readonly PlacementProfile[],
  candidates: readonly PlacementCandidate[],
  events: readonly CausalEventProjection[],
): PlacementViolation[] {
  const violations: PlacementViolation[] = []
  for (const candidate of candidates.filter((entry) => entry.admission === "selected")) {
    for (const violation of candidate.violations) {
      violations.push(Object.freeze({
        id: `selected:${candidate.profileId}:${fingerprint([violation])}`,
        severity: /sensitivity|residency|encryption|trusted/.test(violation)
          ? "critical"
          : "error",
        code: violationCode(violation),
        message: violation,
        profileId: candidate.profileId,
        recoverable: true,
      }))
    }
  }
  for (const event of events.filter((entry) =>
    /privacy.*violation|sla.*violation|placement.*violation|resource.*exhaust|capacity.*fail/i.test(entry.eventType))) {
    violations.push(Object.freeze({
      id: `event:${event.eventId}`,
      severity: /critical|privacy|restricted/i.test(event.eventType + event.summary)
        ? "critical"
        : /fail|violation|exhaust/i.test(event.eventType)
          ? "error"
          : "warning",
      code: violationCode(event.eventType + " " + event.summary),
      message: event.summary,
      profileId: entityId(event.entityRefs, "placement") || undefined,
      workerId: event.workerId,
      nodeId: event.nodeId,
      eventId: event.eventId,
      recoverable: Boolean(event.recoveryId || !event.terminal),
      recoveryId: event.recoveryId,
    }))
  }
  const selected = candidates.find((candidate) => candidate.admission === "selected")
  if (!selected && profiles.length) {
    violations.push(Object.freeze({
      id: `task:${taskId}:no-selected-placement`,
      severity: "critical",
      code: "availability",
      message: "No canonical placement is selected.",
      recoverable: true,
    }))
  }
  if (!profiles.length) {
    violations.push(Object.freeze({
      id: `task:${taskId}:no-placement-profile`,
      severity: "critical",
      code: "availability",
      message: "No device, edge, or cloud placement profile is projected.",
      recoverable: true,
    }))
  }
  return sortStable(violations, (left, right) =>
    severityRank(right.severity) - severityRank(left.severity) ||
    compareText(left.id, right.id))
}

function violationCode(value: string): string {
  const selected = value.toLowerCase()
  if (selected.includes("sensitivity")) return "sensitivity"
  if (selected.includes("residency") || selected.includes("region")) return "residency"
  if (selected.includes("encrypt")) return "encryption"
  if (selected.includes("isolat") || selected.includes("sandbox")) return "isolation"
  if (selected.includes("privacy") || selected.includes("secret")) return "privacy"
  if (selected.includes("latency") || selected.includes("sla")) return "latency"
  if (selected.includes("cost") || selected.includes("budget")) return "cost"
  if (selected.includes("capability")) return "capability"
  if (selected.includes("memory") || selected.includes("gpu") || selected.includes("capacity")) return "capacity"
  return "availability"
}

function placementTier(value: unknown): PlacementTier {
  const selected = text(value).toLowerCase()
  if (/device|local|terminal|desktop|laptop|phone/.test(selected)) return "device"
  if (/edge|isolated|sandbox|near/.test(selected)) return "edge"
  if (/cloud|remote|provider|hosted/.test(selected)) return "cloud"
  return "unknown"
}

function sensitivityLevel(value: unknown): Sensitivity {
  const selected = text(value).toLowerCase()
  if (/restrict|secret|regulated|pii|phi/.test(selected)) return "restricted"
  if (/confidential|sensitive/.test(selected)) return "confidential"
  if (/internal|private/.test(selected)) return "internal"
  return "public"
}

function normalizeUtilization(value: number): number {
  return bounded(value > 1 ? value / 100 : value, 0, 1)
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

function tierRank(tier: PlacementTier): number {
  return ["device", "edge", "cloud", "unknown"].indexOf(tier)
}

function admissionRank(admission: PlacementAdmission): number {
  return ["selected", "migrating", "eligible", "rejected", "failed"].indexOf(admission)
}

function severityRank(severity: PlacementViolation["severity"]): number {
  return ["info", "warning", "error", "critical"].indexOf(severity)
}

function normalizeOptions(options: PlacementProjectionOptions): Required<PlacementProjectionOptions> {
  return {
    constraint: options.constraint ?? {},
    nodeId: text(options.nodeId),
    workerId: text(options.workerId),
  }
}
