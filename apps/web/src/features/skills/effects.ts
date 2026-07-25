import type { CanonicalProjectionState } from "../../state/contracts.ts"
import type {
  SkillCatalogProjection,
  SkillControlEffectBaseline,
  SkillControlEffectVerification,
  SkillOperationKind,
} from "./contracts.ts"
import { buildSkillCatalogProjection } from "./projection.ts"
import {
  skillFingerprint,
  skillText,
  uniqueSkillStrings,
} from "./value.ts"

interface TrackedSkillEffect {
  baseline: SkillControlEffectBaseline
  commandId?: string
  receiptEventIds: readonly string[]
  createdAt: string
}

export class SkillEffectReconciler {
  readonly #tracked = new Map<string, TrackedSkillEffect>()
  readonly #verified = new Map<string, SkillControlEffectVerification>()
  #enabled = true
  #disabledReason = "Skill effect reconciler is disabled."

  begin(input: {
    operationId: string
    kind: SkillOperationKind
    state: CanonicalProjectionState
    catalog: SkillCatalogProjection
    skillId: string
    createdAt?: string
  }): SkillControlEffectBaseline {
    this.#assertEnabled()
    const skill = input.catalog.skills.find((item) => item.skillId === input.skillId)
    if (!skill) throw new Error("Cannot capture an effect baseline for an absent skill.")
    const baseline: SkillControlEffectBaseline = Object.freeze({
      operationId: input.operationId,
      kind: input.kind,
      taskId: skill.taskId,
      runId: skill.runId,
      sessionId: skill.sessionId,
      skillId: skill.skillId,
      projectionRevision: input.state.revision,
      skillProjectionRevision: skill.projectionRevision,
      contentHash: skill.version.contentHash,
      registryRevision: skill.version.registryRevision,
      dependencyDigest: skill.dependencies.digest,
      supplyDigest: skill.supplyChain.digest,
      invocationIds: Object.freeze(
        skill.invocations.map((invocation) => invocation.invocationId).sort(),
      ),
      eventCount: input.state.causality.eventOrder.length,
      fingerprint: skillFingerprint([
        input.operationId,
        input.kind,
        input.state.revision,
        skill.fingerprint,
      ]),
    })
    const previous = this.#tracked.get(input.operationId)
    if (previous && previous.baseline.fingerprint !== baseline.fingerprint) {
      throw new Error("Skill operation identity was reused with another canonical baseline.")
    }
    this.#tracked.set(input.operationId, previous ?? Object.freeze({
      baseline,
      receiptEventIds: Object.freeze([]),
      createdAt: input.createdAt ?? new Date().toISOString(),
    }))
    return previous?.baseline ?? baseline
  }

  bindReceipt(input: {
    operationId: string
    commandId?: string
    eventIds?: readonly string[]
  }): void {
    this.#assertEnabled()
    const tracked = this.#tracked.get(input.operationId)
    if (!tracked) throw new Error("Skill effect baseline does not exist.")
    if (
      tracked.commandId &&
      input.commandId &&
      tracked.commandId !== input.commandId
    ) {
      throw new Error("Skill operation receipt changed its command identity.")
    }
    this.#tracked.set(input.operationId, Object.freeze({
      ...tracked,
      commandId: input.commandId ?? tracked.commandId,
      receiptEventIds: uniqueSkillStrings([
        ...tracked.receiptEventIds,
        ...(input.eventIds ?? []),
      ]),
    }))
  }

  verify(input: {
    operationId: string
    state: CanonicalProjectionState
    verifiedAt?: string
  }): SkillControlEffectVerification {
    this.#assertEnabled()
    const tracked = this.#tracked.get(input.operationId)
    if (!tracked) throw new Error("Skill effect baseline does not exist.")
    const catalog = buildSkillCatalogProjection(input.state, {
      taskId: tracked.baseline.taskId,
      runId: tracked.baseline.runId,
      sessionId: tracked.baseline.sessionId,
      selectedSkillId: tracked.baseline.skillId,
    })
    const current = catalog.skills.find(
      (skill) => skill.skillId === tracked.baseline.skillId,
    )
    const changedFields: string[] = []
    const reasons: string[] = []
    const evidenceInvocationIds: string[] = []
    if (!current) {
      reasons.push("skill is absent from the later canonical projection")
    } else {
      if (current.version.contentHash !== tracked.baseline.contentHash) {
        changedFields.push("skill.content_hash")
      }
      if (current.version.registryRevision !== tracked.baseline.registryRevision) {
        changedFields.push("skill.registry_revision")
      }
      if (current.projectionRevision > tracked.baseline.skillProjectionRevision) {
        changedFields.push("skill.projection_revision")
      }
      if (current.dependencies.digest !== tracked.baseline.dependencyDigest) {
        changedFields.push("skill.dependency_digest")
      }
      if (current.supplyChain.digest !== tracked.baseline.supplyDigest) {
        changedFields.push("skill.supply_digest")
      }
      for (const invocation of current.invocations) {
        if (!tracked.baseline.invocationIds.includes(invocation.invocationId)) {
          evidenceInvocationIds.push(invocation.invocationId)
        }
      }
    }
    const evidenceEventIds = effectEvidenceEvents(input.state, tracked)
    const commandObserved =
      !tracked.commandId ||
      Boolean(input.state.commands[tracked.commandId]) ||
      Boolean(input.state.causality.byControlCommand[tracked.commandId]?.length)
    const revisionAdvanced =
      input.state.revision > tracked.baseline.projectionRevision
    const updateSatisfied =
      tracked.baseline.kind === "update" &&
      Boolean(current) &&
      changedFields.includes("skill.content_hash") &&
      changedFields.includes("skill.registry_revision") &&
      current!.version.registryRevision > tracked.baseline.registryRevision &&
      current!.version.contentHash !== tracked.baseline.contentHash
    const invocationSatisfied =
      tracked.baseline.kind === "invoke" &&
      Boolean(current) &&
      evidenceInvocationIds.some((invocationId) => {
        const invocation = current!.invocations.find(
          (item) => item.invocationId === invocationId,
        )
        return Boolean(invocation && !invocation.quarantined)
      })
    const quarantined =
      tracked.baseline.kind === "invoke" &&
      evidenceInvocationIds.length > 0 &&
      evidenceInvocationIds.every((invocationId) =>
        current?.invocations.find((item) => item.invocationId === invocationId)
          ?.quarantined === true)
    const semanticSatisfied =
      tracked.baseline.kind === "update"
        ? updateSatisfied
        : invocationSatisfied
    const satisfied =
      Boolean(current) &&
      revisionAdvanced &&
      commandObserved &&
      semanticSatisfied &&
      !quarantined
    if (!revisionAdvanced) reasons.push("canonical projection revision has not advanced")
    if (!commandObserved) reasons.push("control command is absent from canonical projection")
    if (tracked.baseline.kind === "update" && !updateSatisfied) {
      reasons.push("later skill version has not advanced to a different content hash")
    }
    if (tracked.baseline.kind === "invoke" && !invocationSatisfied) {
      reasons.push("later canonical projection has no newly bound skill invocation")
    }
    if (quarantined) reasons.push("new invocation evidence failed exact skill binding")
    if (satisfied) {
      reasons.push(
        tracked.baseline.kind === "update"
          ? "canonical skill update effect observed"
          : "canonical skill invocation effect observed",
      )
    }
    const verification: SkillControlEffectVerification = Object.freeze({
      operationId: input.operationId,
      kind: tracked.baseline.kind,
      satisfied,
      pending: !satisfied && !quarantined,
      quarantined,
      observedProjectionRevision: input.state.revision,
      changedFields: Object.freeze(uniqueSkillStrings(changedFields)),
      evidenceEventIds: Object.freeze(evidenceEventIds),
      evidenceInvocationIds: Object.freeze(evidenceInvocationIds.sort()),
      reasons: Object.freeze(uniqueSkillStrings(reasons)),
      verifiedAt: input.verifiedAt ?? new Date().toISOString(),
    })
    this.#verified.set(input.operationId, verification)
    return verification
  }

  get(operationId: string): SkillControlEffectVerification | undefined {
    this.#assertEnabled()
    return this.#verified.get(operationId)
  }

  list(): readonly SkillControlEffectVerification[] {
    this.#assertEnabled()
    return Object.freeze(
      [...this.#verified.values()].sort((left, right) =>
        right.verifiedAt.localeCompare(left.verifiedAt) ||
        left.operationId.localeCompare(right.operationId)),
    )
  }

  abandon(operationId: string): boolean {
    this.#assertEnabled()
    this.#verified.delete(operationId)
    return this.#tracked.delete(operationId)
  }

  clear(): void {
    this.#assertEnabled()
    this.#tracked.clear()
    this.#verified.clear()
  }

  disable(reason = "Skill effect reconciler is disabled."): void {
    this.#enabled = false
    this.#disabledReason = skillText(
      reason,
      "Skill effect reconciler is disabled.",
      4_096,
    )
    this.#tracked.clear()
    this.#verified.clear()
  }

  enable(): void {
    this.#enabled = true
  }

  #assertEnabled(): void {
    if (!this.#enabled) throw new Error(this.#disabledReason)
  }
}

function effectEvidenceEvents(
  state: CanonicalProjectionState,
  tracked: TrackedSkillEffect,
): string[] {
  const ids = new Set(tracked.receiptEventIds)
  if (tracked.commandId) {
    for (const eventId of state.causality.byControlCommand[tracked.commandId] ?? []) {
      ids.add(eventId)
    }
  }
  for (const eventId of state.causality.eventOrder.slice(tracked.baseline.eventCount)) {
    const event = state.causality.byEvent[eventId]
    if (!event || event.taskId !== tracked.baseline.taskId) continue
    if (event.runId !== tracked.baseline.runId) continue
    const normalized = event.eventType.toLowerCase()
    const kindMatches =
      tracked.baseline.kind === "update"
        ? /skill.*(?:update|reload|revision|change)|(?:update|reload).*skill/.test(normalized)
        : /skill.*(?:invok|execut|start|result|fail|complete)|(?:invok|execut).*skill/.test(normalized)
    if (!kindMatches) continue
    const skillRef = event.entityRefs.some((ref) =>
      ref.includes(tracked.baseline.skillId))
    const toolRef = event.toolCallId
      ? Boolean(state.tools[event.toolCallId]?.attributes.skill_id === tracked.baseline.skillId)
      : false
    if (skillRef || toolRef || normalized.includes("skill")) ids.add(eventId)
  }
  return [...ids]
    .filter((eventId) => {
      const event = state.causality.byEvent[eventId]
      return Boolean(
        event &&
        event.taskId === tracked.baseline.taskId &&
        event.runId === tracked.baseline.runId,
      )
    })
    .sort((left, right) =>
      (state.causality.byEvent[left]?.sequence ?? 0) -
        (state.causality.byEvent[right]?.sequence ?? 0) ||
      left.localeCompare(right))
}
