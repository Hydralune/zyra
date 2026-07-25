import type {
  PlacementCandidate,
  PlacementConsoleProjection,
  PlacementConstraint,
  PlacementMigration,
  PlacementProfile,
  PlacementTier,
  Sensitivity,
} from "./projection.ts"
import {
  bounded,
  compareNumber,
  compareText,
  fingerprint,
  mean,
  ratio,
  sortStable,
  sum,
  text,
  unique,
} from "../session/value.ts"

export interface WorkloadDemand {
  id: string
  cpuCores: number
  gpuCount: number
  gpuMemoryMb: number
  memoryMb: number
  diskMb: number
  networkMbps: number
  processSlots: number
  expectedDurationMs: number
  sensitivity: Sensitivity
  capabilities: readonly string[]
  preferredTiers: readonly PlacementTier[]
  parallelism: number
  checkpointable: boolean
  migratable: boolean
  containsSecrets: boolean
}

export interface AdmissionDecision {
  id: string
  demand: WorkloadDemand
  profile: PlacementProfile
  allowed: boolean
  reasons: readonly string[]
  warnings: readonly string[]
  cpuHeadroom: number
  gpuHeadroom: number
  gpuMemoryHeadroomMb: number
  memoryHeadroomMb: number
  diskHeadroomMb: number
  networkHeadroomMbps: number
  processSlotHeadroom: number
  privacyScore: number
  resourceScore: number
  localityScore: number
  costEstimateUsd: number
  latencyEstimateMs: number
  score: number
}

export interface PlacementScenario {
  id: string
  name: string
  demand: WorkloadDemand
  constraint: PlacementConstraint
  decisions: readonly AdmissionDecision[]
  selected?: AdmissionDecision
  rejected: readonly AdmissionDecision[]
  costEstimateUsd: number
  latencyEstimateMs: number
  privacyScore: number
  resourceScore: number
  feasible: boolean
  warnings: readonly string[]
}

export interface MigrationPlan {
  id: string
  demand: WorkloadDemand
  fromProfile: PlacementProfile
  toProfile: PlacementProfile
  allowed: boolean
  reasons: readonly string[]
  requiresCheckpoint: boolean
  checkpointId?: string
  estimatedDowntimeMs: number
  transferBytes: number
  transferTimeMs: number
  costDeltaUsd: number
  latencyDeltaMs: number
  privacyDelta: number
  steps: readonly string[]
}

export class PlacementAdmissionEngine {
  readonly #scenarios = new Map<string, PlacementScenario>()
  readonly #migrations = new Map<string, MigrationPlan>()
  #enabled = true
  #disabledReason = "Placement admission engine is disabled."

  evaluate(
    projection: PlacementConsoleProjection,
    demandInput: Partial<WorkloadDemand>,
    name = "current",
  ): PlacementScenario {
    this.#assertEnabled()
    const demand = normalizeDemand(demandInput)
    const decisions = projection.profiles.map((profile) =>
      evaluateProfile(profile, demand, projection.constraints))
    const ordered = sortStable(decisions, (left, right) =>
      Number(right.allowed) - Number(left.allowed) ||
      compareNumber(right.score, left.score) ||
      compareNumber(left.costEstimateUsd, right.costEstimateUsd) ||
      compareText(left.profile.id, right.profile.id))
    const selected = ordered.find((decision) => decision.allowed)
    const rejected = ordered.filter((decision) => !decision.allowed)
    const warnings: string[] = []
    if (!selected) warnings.push("no profile can admit the workload")
    if (selected && selected.profile.tier === "cloud" && demand.sensitivity === "restricted") {
      warnings.push("restricted workload selected cloud placement")
    }
    if (selected && selected.warnings.length) warnings.push(...selected.warnings)
    if (ordered.filter((decision) => decision.allowed).length === 1) {
      warnings.push("placement has no eligible fallback")
    }
    const scenario: PlacementScenario = Object.freeze({
      id: fingerprint([
        name,
        demand,
        projection.revision,
        ordered.map((decision) => [decision.profile.id, decision.allowed, decision.score]),
      ]),
      name: text(name, "current"),
      demand,
      constraint: projection.constraints,
      decisions: Object.freeze(ordered),
      selected,
      rejected: Object.freeze(rejected),
      costEstimateUsd: selected?.costEstimateUsd ?? 0,
      latencyEstimateMs: selected?.latencyEstimateMs ?? 0,
      privacyScore: selected?.privacyScore ?? 0,
      resourceScore: selected?.resourceScore ?? 0,
      feasible: Boolean(selected),
      warnings: Object.freeze(unique(warnings)),
    })
    this.#scenarios.set(scenario.id, scenario)
    return scenario
  }

  compare(leftId: string, rightId: string): Readonly<Record<string, unknown>> {
    this.#assertEnabled()
    const left = this.#scenarios.get(leftId)
    const right = this.#scenarios.get(rightId)
    if (!left || !right) throw new Error("Both placement scenarios must exist.")
    return Object.freeze({
      left_id: left.id,
      right_id: right.id,
      feasible_changed: left.feasible !== right.feasible,
      selected_profile_changed: left.selected?.profile.id !== right.selected?.profile.id,
      selected_tier_changed: left.selected?.profile.tier !== right.selected?.profile.tier,
      cost_delta_usd: right.costEstimateUsd - left.costEstimateUsd,
      latency_delta_ms: right.latencyEstimateMs - left.latencyEstimateMs,
      privacy_delta: right.privacyScore - left.privacyScore,
      resource_delta: right.resourceScore - left.resourceScore,
      newly_eligible_profile_ids: Object.freeze(
        right.decisions
          .filter((decision) => decision.allowed)
          .map((decision) => decision.profile.id)
          .filter((id) => !left.decisions.some((decision) =>
            decision.allowed && decision.profile.id === id)),
      ),
      newly_rejected_profile_ids: Object.freeze(
        left.decisions
          .filter((decision) => decision.allowed)
          .map((decision) => decision.profile.id)
          .filter((id) => !right.decisions.some((decision) =>
            decision.allowed && decision.profile.id === id)),
      ),
    })
  }

  migrate(input: {
    scenarioId: string
    fromProfileId: string
    toProfileId: string
    checkpointId?: string
    stateBytes?: number
  }): MigrationPlan {
    this.#assertEnabled()
    const scenario = this.#scenarios.get(input.scenarioId)
    if (!scenario) throw new Error("Placement scenario does not exist.")
    const fromDecision = scenario.decisions.find((decision) =>
      decision.profile.id === input.fromProfileId)
    const toDecision = scenario.decisions.find((decision) =>
      decision.profile.id === input.toProfileId)
    if (!fromDecision || !toDecision) throw new Error("Migration profile is absent from scenario.")
    const reasons: string[] = []
    if (!scenario.demand.migratable) reasons.push("workload is not migratable")
    if (!toDecision.allowed) reasons.push(...toDecision.reasons)
    if (fromDecision.profile.id === toDecision.profile.id) reasons.push("source and target are identical")
    const requiresCheckpoint = scenario.demand.checkpointable &&
      fromDecision.profile.tier !== toDecision.profile.tier
    if (requiresCheckpoint && !input.checkpointId) reasons.push("cross-tier migration requires checkpoint")
    const transferBytes = Math.max(
      0,
      input.stateBytes ??
      (scenario.demand.memoryMb + scenario.demand.gpuMemoryMb) * 1024 * 1024,
    )
    const bandwidth = Math.max(
      1,
      Math.min(
        fromDecision.profile.capacity.networkMbps || Number.MAX_SAFE_INTEGER,
        toDecision.profile.capacity.networkMbps || Number.MAX_SAFE_INTEGER,
      ),
    )
    const transferTimeMs = transferBytes * 8 / (bandwidth * 1_000_000) * 1000
    const estimatedDowntimeMs =
      transferTimeMs +
      (requiresCheckpoint ? 1000 : 0) +
      toDecision.latencyEstimateMs
    const steps: string[] = []
    steps.push("freeze new work admission")
    if (requiresCheckpoint) steps.push("commit exact canonical checkpoint")
    steps.push("fence external side effects")
    steps.push("transfer encrypted state and artifact references")
    steps.push("verify target capability and privacy constraints")
    steps.push("acquire target worker lease")
    steps.push("resume from exact checkpoint or handoff boundary")
    steps.push("verify canonical route and timeline projection")
    steps.push("release source lease")
    const plan: MigrationPlan = Object.freeze({
      id: fingerprint([
        scenario.id,
        input.fromProfileId,
        input.toProfileId,
        input.checkpointId,
        transferBytes,
      ]),
      demand: scenario.demand,
      fromProfile: fromDecision.profile,
      toProfile: toDecision.profile,
      allowed: reasons.length === 0,
      reasons: Object.freeze(unique(reasons)),
      requiresCheckpoint,
      checkpointId: input.checkpointId,
      estimatedDowntimeMs,
      transferBytes,
      transferTimeMs,
      costDeltaUsd: toDecision.costEstimateUsd - fromDecision.costEstimateUsd,
      latencyDeltaMs: toDecision.latencyEstimateMs - fromDecision.latencyEstimateMs,
      privacyDelta: toDecision.privacyScore - fromDecision.privacyScore,
      steps: Object.freeze(steps),
    })
    this.#migrations.set(plan.id, plan)
    return plan
  }

  scenario(id: string): PlacementScenario | undefined {
    this.#assertEnabled()
    return this.#scenarios.get(id)
  }

  scenarios(): readonly PlacementScenario[] {
    this.#assertEnabled()
    return Object.freeze([...this.#scenarios.values()])
  }

  migration(id: string): MigrationPlan | undefined {
    this.#assertEnabled()
    return this.#migrations.get(id)
  }

  migrations(): readonly MigrationPlan[] {
    this.#assertEnabled()
    return Object.freeze([...this.#migrations.values()])
  }

  audit(projection: PlacementConsoleProjection): Readonly<Record<string, unknown>> {
    this.#assertEnabled()
    return Object.freeze({
      scenario_count: this.#scenarios.size,
      migration_count: this.#migrations.size,
      infeasible_scenario_ids: Object.freeze(
        this.scenarios().filter((scenario) => !scenario.feasible).map((scenario) => scenario.id),
      ),
      rejected_migration_ids: Object.freeze(
        this.migrations().filter((migration) => !migration.allowed).map((migration) => migration.id),
      ),
      selected_tier: projection.selectedTier,
      privacy_compliant: projection.privacyCompliant,
      sla_compliant: projection.slaCompliant,
      cost_compliant: projection.costCompliant,
      profile_count: projection.profiles.length,
      candidate_count: projection.candidates.length,
      violation_count: projection.violations.length,
    })
  }

  disable(reason = "Placement admission engine is disabled."): void {
    this.#enabled = false
    this.#disabledReason = text(reason, "Placement admission engine is disabled.")
  }

  enable(): void {
    this.#enabled = true
  }

  clear(): void {
    this.#assertEnabled()
    this.#scenarios.clear()
    this.#migrations.clear()
  }

  #assertEnabled(): void {
    if (!this.#enabled) throw new Error(this.#disabledReason)
  }
}

export function reconcileMigrationHistory(
  plans: readonly MigrationPlan[],
  canonical: readonly PlacementMigration[],
): Readonly<Record<string, unknown>> {
  const committed = canonical.filter((migration) => migration.phase === "committed")
  const failed = canonical.filter((migration) =>
    migration.phase === "failed" || migration.phase === "rolled-back")
  const matchedPlans = plans.filter((plan) =>
    canonical.some((migration) =>
      migration.fromProfileId === plan.fromProfile.id &&
      migration.toProfileId === plan.toProfile.id &&
      (!plan.checkpointId || migration.checkpointId === plan.checkpointId)))
  const unmatchedPlans = plans.filter((plan) => !matchedPlans.includes(plan))
  const unmatchedEvents = canonical.filter((migration) =>
    !plans.some((plan) =>
      migration.fromProfileId === plan.fromProfile.id &&
      migration.toProfileId === plan.toProfile.id))
  return Object.freeze({
    plan_count: plans.length,
    canonical_migration_count: canonical.length,
    matched_plan_ids: Object.freeze(matchedPlans.map((plan) => plan.id)),
    unmatched_plan_ids: Object.freeze(unmatchedPlans.map((plan) => plan.id)),
    unmatched_event_ids: Object.freeze(unmatchedEvents.map((migration) => migration.sourceEventId)),
    committed_event_ids: Object.freeze(committed.map((migration) => migration.sourceEventId)),
    failed_event_ids: Object.freeze(failed.map((migration) => migration.sourceEventId)),
    all_plans_observed: unmatchedPlans.length === 0,
    all_events_planned: unmatchedEvents.length === 0,
  })
}

function normalizeDemand(input: Partial<WorkloadDemand>): WorkloadDemand {
  return Object.freeze({
    id: text(input.id, "workload"),
    cpuCores: bounded(input.cpuCores ?? 1, 0, 100000),
    gpuCount: bounded(Math.floor(input.gpuCount ?? 0), 0, 10000),
    gpuMemoryMb: bounded(Math.floor(input.gpuMemoryMb ?? 0), 0, 100000000),
    memoryMb: bounded(Math.floor(input.memoryMb ?? 1024), 0, 100000000),
    diskMb: bounded(Math.floor(input.diskMb ?? 0), 0, 100000000),
    networkMbps: bounded(input.networkMbps ?? 0, 0, 1000000),
    processSlots: bounded(Math.floor(input.processSlots ?? 1), 0, 100000),
    expectedDurationMs: bounded(Math.floor(input.expectedDurationMs ?? 60000), 1, 31_536_000_000),
    sensitivity: input.sensitivity ?? "internal",
    capabilities: Object.freeze(unique((input.capabilities ?? []).map((item) => text(item)).filter(Boolean))),
    preferredTiers: Object.freeze(unique(input.preferredTiers ?? [])),
    parallelism: bounded(Math.floor(input.parallelism ?? 1), 1, 100000),
    checkpointable: input.checkpointable !== false,
    migratable: input.migratable !== false,
    containsSecrets: input.containsSecrets === true,
  })
}

function evaluateProfile(
  profile: PlacementProfile,
  demand: WorkloadDemand,
  constraint: PlacementConstraint,
): AdmissionDecision {
  const reasons: string[] = []
  const warnings: string[] = []
  const cpuAvailable = profile.capacity.cpuCores * (1 - profile.capacity.cpuUtilization)
  const gpuAvailable = profile.capacity.gpuCount * (1 - profile.capacity.gpuUtilization)
  const gpuMemoryHeadroomMb = profile.capacity.gpuMemoryMb -
    Math.round(profile.capacity.gpuMemoryMb * profile.capacity.gpuUtilization) -
    demand.gpuMemoryMb
  const memoryHeadroomMb = profile.capacity.memoryMb -
    profile.capacity.memoryUsedMb -
    demand.memoryMb
  const diskHeadroomMb = profile.capacity.diskMb -
    profile.capacity.diskUsedMb -
    demand.diskMb
  const networkHeadroomMbps = profile.capacity.networkMbps - demand.networkMbps
  const processSlotHeadroom = profile.capacity.processSlots -
    profile.capacity.processSlotsUsed -
    demand.processSlots
  const cpuHeadroom = cpuAvailable - demand.cpuCores
  const gpuHeadroom = gpuAvailable - demand.gpuCount
  if (!profile.online) reasons.push("profile offline")
  if (cpuHeadroom < 0) reasons.push("insufficient CPU")
  if (gpuHeadroom < 0) reasons.push("insufficient GPU count")
  if (gpuMemoryHeadroomMb < 0) reasons.push("insufficient GPU memory")
  if (memoryHeadroomMb < 0) reasons.push("insufficient memory")
  if (profile.capacity.diskMb && diskHeadroomMb < 0) reasons.push("insufficient disk")
  if (profile.capacity.networkMbps && networkHeadroomMbps < 0) reasons.push("insufficient network")
  if (processSlotHeadroom < 0) reasons.push("insufficient process slots")
  const missingCapabilities = demand.capabilities.filter((capability) =>
    !profile.capabilities.includes(capability))
  if (missingCapabilities.length) reasons.push(`missing capabilities: ${missingCapabilities.join(", ")}`)
  if (!profile.supportedSensitivity.includes(demand.sensitivity)) reasons.push("sensitivity unsupported")
  if (constraint.requiresIsolation && !profile.isolated) reasons.push("isolation required")
  if (constraint.requiresEncryption && !profile.encrypted) reasons.push("encryption required")
  if (constraint.requiresTrustedExecution && !profile.trustedExecution) reasons.push("trusted execution required")
  if (demand.containsSecrets && !profile.supportsSecrets) reasons.push("secret custody unavailable")
  if (constraint.forbiddenTiers.includes(profile.tier)) reasons.push("tier forbidden")
  if (profile.tier === "cloud" && !constraint.allowCloud) reasons.push("cloud disallowed")
  if (profile.tier === "edge" && !constraint.allowEdge) reasons.push("edge disallowed")
  if (profile.tier === "device" && !constraint.allowDevice) reasons.push("device disallowed")
  if (constraint.dataResidency && profile.region !== constraint.dataResidency) reasons.push("residency mismatch")
  if (constraint.allowedRegions.length && (!profile.region || !constraint.allowedRegions.includes(profile.region))) {
    reasons.push("region not allowed")
  }
  if (constraint.maximumLatencyMs && profile.capacity.networkLatencyMs > constraint.maximumLatencyMs) {
    reasons.push("latency SLA exceeded")
  }
  if (demand.preferredTiers.length && !demand.preferredTiers.includes(profile.tier)) {
    warnings.push("tier is not preferred")
  }
  if (profile.health < 0.25) warnings.push("profile health is low")
  if (memoryHeadroomMb >= 0 && memoryHeadroomMb < demand.memoryMb * 0.25) warnings.push("memory headroom is narrow")
  const expectedHours = demand.expectedDurationMs / 3_600_000
  const costEstimateUsd = profile.costPerHourUsd * expectedHours * demand.parallelism
  if (constraint.maximumCostUsd && costEstimateUsd > constraint.maximumCostUsd) reasons.push("cost budget exceeded")
  const privacyScore = privacyScoreFor(profile, demand, constraint)
  const resourceScore = resourceScoreFor(profile, demand, {
    cpuHeadroom,
    gpuHeadroom,
    gpuMemoryHeadroomMb,
    memoryHeadroomMb,
    diskHeadroomMb,
    networkHeadroomMbps,
    processSlotHeadroom,
  })
  const localityScore = demand.preferredTiers.length
    ? demand.preferredTiers.includes(profile.tier) ? 1 : 0
    : profile.tier === "device" ? 1 : profile.tier === "edge" ? 0.75 : 0.5
  const score = bounded(
    resourceScore * 0.35 +
    privacyScore * 0.35 +
    profile.health * 0.15 +
    localityScore * 0.1 +
    (1 / (1 + costEstimateUsd)) * 0.05,
    0,
    1,
  )
  return Object.freeze({
    id: fingerprint([demand.id, profile.id, reasons, score]),
    demand,
    profile,
    allowed: reasons.length === 0,
    reasons: Object.freeze(unique(reasons)),
    warnings: Object.freeze(unique(warnings)),
    cpuHeadroom,
    gpuHeadroom,
    gpuMemoryHeadroomMb,
    memoryHeadroomMb,
    diskHeadroomMb,
    networkHeadroomMbps,
    processSlotHeadroom,
    privacyScore,
    resourceScore,
    localityScore,
    costEstimateUsd,
    latencyEstimateMs: profile.capacity.networkLatencyMs,
    score,
  })
}

function privacyScoreFor(
  profile: PlacementProfile,
  demand: WorkloadDemand,
  constraint: PlacementConstraint,
): number {
  const checks = [
    profile.supportedSensitivity.includes(demand.sensitivity),
    !constraint.requiresIsolation || profile.isolated,
    !constraint.requiresEncryption || profile.encrypted,
    !constraint.requiresTrustedExecution || profile.trustedExecution,
    !demand.containsSecrets || profile.supportsSecrets,
    !constraint.dataResidency || profile.region === constraint.dataResidency,
  ]
  return ratio(checks.filter(Boolean).length, checks.length)
}

function resourceScoreFor(
  profile: PlacementProfile,
  demand: WorkloadDemand,
  headroom: {
    cpuHeadroom: number
    gpuHeadroom: number
    gpuMemoryHeadroomMb: number
    memoryHeadroomMb: number
    diskHeadroomMb: number
    networkHeadroomMbps: number
    processSlotHeadroom: number
  },
): number {
  const scores = [
    bounded((headroom.cpuHeadroom + demand.cpuCores) / Math.max(1, demand.cpuCores * 2), 0, 1),
    demand.gpuCount
      ? bounded((headroom.gpuHeadroom + demand.gpuCount) / Math.max(1, demand.gpuCount * 2), 0, 1)
      : 1,
    demand.gpuMemoryMb
      ? bounded((headroom.gpuMemoryHeadroomMb + demand.gpuMemoryMb) / Math.max(1, demand.gpuMemoryMb * 2), 0, 1)
      : 1,
    bounded((headroom.memoryHeadroomMb + demand.memoryMb) / Math.max(1, demand.memoryMb * 2), 0, 1),
    demand.diskMb
      ? bounded((headroom.diskHeadroomMb + demand.diskMb) / Math.max(1, demand.diskMb * 2), 0, 1)
      : 1,
    demand.networkMbps
      ? bounded((headroom.networkHeadroomMbps + demand.networkMbps) / Math.max(1, demand.networkMbps * 2), 0, 1)
      : 1,
    bounded((headroom.processSlotHeadroom + demand.processSlots) / Math.max(1, demand.processSlots * 2), 0, 1),
    profile.health,
  ]
  return bounded(mean(scores), 0, 1)
}
