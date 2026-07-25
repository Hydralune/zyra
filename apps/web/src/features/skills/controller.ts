import type { CommandReceipt } from "../../../../../packages/commands/src/index.ts"
import type { CanonicalProjectionStore } from "../../state/index.ts"
import { SkillCatalogIndex } from "./catalog.ts"
import {
  admitSkillControl,
  buildSkillInvokeCommand,
  buildSkillUpdateCommand,
  skillOperationIdentity,
  terminalSkillControlPhase,
} from "./control-policy.ts"
import { SkillEffectReconciler } from "./effects.ts"
import { buildSkillCatalogProjection } from "./projection.ts"
import type {
  SkillArguments,
  SkillCommandPort,
  SkillControlOperation,
  SkillControlPhase,
  SkillOperationKind,
  SkillPermissionPort,
  SkillProjection,
  SkillWorkbenchSnapshot,
} from "./contracts.ts"
import {
  skillFingerprint,
  skillIdentity,
  skillText,
  uniqueSkillStrings,
} from "./value.ts"

export class SkillWorkbenchController {
  readonly catalogIndex = new SkillCatalogIndex()
  readonly effects = new SkillEffectReconciler()
  readonly #projections: CanonicalProjectionStore
  readonly #commands: SkillCommandPort
  readonly #permissions?: SkillPermissionPort
  readonly #online: () => boolean
  readonly #now: () => Date
  readonly #listeners = new Set<() => void>()
  readonly #operations = new Map<string, SkillControlOperation>()
  readonly #operationByIdempotency = new Map<string, string>()
  #snapshot: SkillWorkbenchSnapshot
  #unsubscribeProjection?: () => void
  #unsubscribeCommands?: () => void
  #closed = false

  constructor(options: {
    projections: CanonicalProjectionStore
    commands: SkillCommandPort
    permissions?: SkillPermissionPort
    online?: () => boolean
    now?: () => Date
    sealed?: boolean
  }) {
    this.#projections = options.projections
    this.#commands = options.commands
    this.#permissions = options.permissions
    this.#online = options.online ?? (() =>
      typeof navigator === "undefined" ? true : navigator.onLine)
    this.#now = options.now ?? (() => new Date())
    this.#snapshot = Object.freeze({
      connected: this.#online(),
      enabled: true,
      viewerOpen: true,
      sealed:
        options.sealed ??
        options.permissions?.getSnapshot().productMode === "sealed",
      closed: false,
      operations: Object.freeze([]),
      revision: 0,
    })
    this.#attach()
  }

  getSnapshot = (): SkillWorkbenchSnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  bind(
    taskId?: string,
    runId?: string,
    sessionId?: string,
    selectedSkillId?: string,
  ): void {
    this.#assertNotClosed()
    if (!taskId) {
      this.catalogIndex.clear()
      this.#replace({
        taskId: undefined,
        runId: undefined,
        sessionId: undefined,
        selectedSkillId: undefined,
        catalog: undefined,
        activeOperation: undefined,
      })
      return
    }
    const task = skillIdentity(taskId, "skill workbench task identity")
    const canonicalTask = this.#projections.state.tasks[task]
    const run = skillIdentity(
      runId ?? canonicalTask?.runId,
      "skill workbench run identity",
    )
    const session = sessionId
      ? skillIdentity(sessionId, "skill workbench session identity")
      : undefined
    const selected = selectedSkillId ?? (
      this.#snapshot.taskId === task ? this.#snapshot.selectedSkillId : undefined
    )
    const catalog = buildSkillCatalogProjection(this.#projections.state, {
      taskId: task,
      runId: run,
      sessionId: session,
      selectedSkillId: selected,
    })
    this.catalogIndex.replace(catalog)
    this.#replace({
      taskId: task,
      runId: run,
      sessionId: session,
      selectedSkillId: catalog.selectedSkillId,
      connected:
        this.#online() &&
        (this.#projections.state.runtimes[task]?.connected ?? true),
      catalog,
    })
    this.#reconcilePending()
  }

  selectSkill(skillId: string): SkillProjection {
    this.#assertAvailable({ requireSelection: false })
    const id = skillIdentity(skillId, "selected skill identity")
    const skill = this.catalogIndex.get(id)
    if (!skill) throw new Error("Skill is absent from the canonical projection.")
    if (skill.taskId !== this.#snapshot.taskId || skill.runId !== this.#snapshot.runId) {
      throw new Error("Skill selection crosses the bound task or run.")
    }
    this.#replace({ selectedSkillId: id })
    return skill
  }

  selectedSkill(): SkillProjection | undefined {
    const id = this.#snapshot.selectedSkillId
    return id ? this.catalogIndex.get(id) : undefined
  }

  async updateSkill(input: {
    skillId?: string
    expectedHash?: string
    nonce?: string
    idempotencyKey?: string
    actorId?: string
    signal?: AbortSignal
  } = {}): Promise<CommandReceipt> {
    this.#assertNotClosed()
    const skill = this.#requireSkill(input.skillId)
    const admission = admitSkillControl({
      skill,
      kind: "update",
      connected: this.#snapshot.connected && this.#online(),
      enabled: this.#snapshot.enabled,
      viewerOpen: this.#snapshot.viewerOpen,
      sealed: this.#sealed(),
    })
    if (!admission.admitted) {
      await this.#rejectControl({
        kind: "update",
        skill,
        reasons: admission.reasons,
        actorId: input.actorId,
        signal: input.signal,
      })
      throw skillControlError(admission.code, admission.reasons)
    }
    if (
      input.expectedHash &&
      input.expectedHash !== skill.version.contentHash
    ) {
      throw skillControlError("skill_update_hash_stale", [
        "expected content hash differs from the canonical skill projection",
      ])
    }
    const nonce = normalizeNonce(
      input.nonce ??
      createNonce("skill-update", skill, this.#now(), this.#operations.size),
    )
    const idempotencyKey = normalizeIdempotency(
      input.idempotencyKey ??
      createIdempotency("skill-update", skill, nonce),
    )
    const command = buildSkillUpdateCommand({
      skill,
      nonce,
      idempotencyKey,
      expectedHash: input.expectedHash,
    })
    return this.#submit({
      kind: "update",
      skill,
      nonce,
      idempotencyKey,
      command,
    })
  }

  async invokeSkill(input: {
    skillId?: string
    arguments?: SkillArguments
    nonce?: string
    idempotencyKey?: string
    actorId?: string
    signal?: AbortSignal
  } = {}): Promise<CommandReceipt> {
    this.#assertNotClosed()
    const skill = this.#requireSkill(input.skillId)
    const admission = admitSkillControl({
      skill,
      kind: "invoke",
      connected: this.#snapshot.connected && this.#online(),
      enabled: this.#snapshot.enabled,
      viewerOpen: this.#snapshot.viewerOpen,
      sealed: this.#sealed(),
    })
    if (!admission.admitted) {
      await this.#rejectControl({
        kind: "invoke",
        skill,
        reasons: admission.reasons,
        actorId: input.actorId,
        signal: input.signal,
      })
      throw skillControlError(admission.code, admission.reasons)
    }
    const nonce = normalizeNonce(
      input.nonce ??
      createNonce("skill-invoke", skill, this.#now(), this.#operations.size),
    )
    const idempotencyKey = normalizeIdempotency(
      input.idempotencyKey ??
      createIdempotency("skill-invoke", skill, nonce),
    )
    const built = buildSkillInvokeCommand({
      skill,
      arguments: input.arguments,
      nonce,
      idempotencyKey,
    })
    return this.#submit({
      kind: "invoke",
      skill,
      nonce,
      idempotencyKey,
      argumentsDigest: built.argumentsDigest,
      command: built.command,
    })
  }

  disconnected(reason = "Browser projection transport disconnected."): void {
    if (this.#closed) return
    for (const operation of this.#operations.values()) {
      if (terminalSkillControlPhase(operation.phase)) continue
      this.#settle(operation.operationId, {
        phase: "disconnected",
        errorCode: "skill_projection_disconnected",
        errorMessage: reason,
      })
    }
    this.#replace({ connected: false })
  }

  reconnected(): void {
    if (this.#closed || !this.#snapshot.enabled || !this.#snapshot.viewerOpen) return
    this.#attach()
    const taskId = this.#snapshot.taskId
    const runId = this.#snapshot.runId
    if (taskId && runId) {
      this.bind(
        taskId,
        runId,
        this.#snapshot.sessionId,
        this.#snapshot.selectedSkillId,
      )
    } else {
      this.#replace({ connected: this.#online() })
    }
    for (const operation of this.#operations.values()) {
      if (operation.phase !== "disconnected") continue
      this.#settle(operation.operationId, {
        phase: "reconciling",
        errorCode: undefined,
        errorMessage: undefined,
      })
    }
    this.#reconcilePending()
  }

  viewerClosed(reason = "Skill viewer closed."): void {
    if (this.#closed || !this.#snapshot.viewerOpen) return
    this.#detach()
    this.catalogIndex.clear()
    for (const operation of this.#operations.values()) {
      if (terminalSkillControlPhase(operation.phase)) continue
      this.#settle(operation.operationId, {
        phase: "disconnected",
        errorCode: "skill_viewer_detached",
        errorMessage: reason,
      })
    }
    this.#replace({
      viewerOpen: false,
      connected: false,
      catalog: undefined,
    })
  }

  viewerOpened(): void {
    this.#assertNotClosed()
    if (this.#snapshot.viewerOpen) return
    this.#replace({ viewerOpen: true })
    this.reconnected()
  }

  setSealed(sealed: boolean): void {
    this.#assertNotClosed()
    const permissionSealed =
      this.#permissions?.getSnapshot().productMode === "sealed"
    this.#replace({ sealed: sealed || permissionSealed })
  }

  disable(reason = "Skill feature binding is disabled."): void {
    if (this.#closed || !this.#snapshot.enabled) return
    const normalized = skillText(
      reason,
      "Skill feature binding is disabled.",
      4_096,
    )
    this.#detach()
    this.catalogIndex.disable(normalized)
    this.effects.disable(normalized)
    for (const operation of this.#operations.values()) {
      if (terminalSkillControlPhase(operation.phase)) continue
      this.#settle(operation.operationId, {
        phase: "disabled",
        errorCode: "skill_feature_disabled",
        errorMessage: normalized,
      })
    }
    this.#replace({
      enabled: false,
      connected: false,
      disabledReason: normalized,
      catalog: undefined,
    })
  }

  enable(): void {
    this.#assertNotClosed()
    if (this.#snapshot.enabled) return
    this.catalogIndex.enable()
    this.effects.enable()
    this.#replace({
      enabled: true,
      disabledReason: undefined,
      connected: this.#online(),
    })
    if (this.#snapshot.viewerOpen) {
      this.#attach()
      this.reconnected()
    }
  }

  close(reason = "Skill workbench controller closed."): void {
    if (this.#closed) return
    this.#closed = true
    this.#detach()
    if (this.#snapshot.enabled) {
      this.catalogIndex.disable(reason)
      this.effects.disable(reason)
    }
    for (const operation of this.#operations.values()) {
      if (terminalSkillControlPhase(operation.phase)) continue
      this.#operations.set(operation.operationId, Object.freeze({
        ...operation,
        phase: "disabled",
        errorCode: "skill_controller_closed",
        errorMessage: reason,
        updatedAt: this.#now().toISOString(),
      }))
    }
    this.#snapshot = Object.freeze({
      ...this.#snapshot,
      enabled: false,
      connected: false,
      viewerOpen: false,
      closed: true,
      disabledReason: reason,
      catalog: undefined,
      operations: this.#operationList(),
      activeOperation: undefined,
      revision: this.#snapshot.revision + 1,
    })
    this.#listeners.clear()
  }

  audit(): {
    closed: boolean
    enabled: boolean
    connected: boolean
    viewerOpen: boolean
    taskId?: string
    runId?: string
    selectedSkillId?: string
    projectionOwner: "M2-01B CanonicalProjectionStore"
    operationOwner: "M1 SkillTool/permission via M2-04A command"
    operations: number
    pendingOperations: number
    catalog: ReturnType<SkillCatalogIndex["audit"]>
  } {
    return Object.freeze({
      closed: this.#closed,
      enabled: this.#snapshot.enabled,
      connected: this.#snapshot.connected,
      viewerOpen: this.#snapshot.viewerOpen,
      taskId: this.#snapshot.taskId,
      runId: this.#snapshot.runId,
      selectedSkillId: this.#snapshot.selectedSkillId,
      projectionOwner: "M2-01B CanonicalProjectionStore",
      operationOwner: "M1 SkillTool/permission via M2-04A command",
      operations: this.#operations.size,
      pendingOperations: [...this.#operations.values()].filter(
        (operation) => !terminalSkillControlPhase(operation.phase),
      ).length,
      catalog: this.catalogIndex.audit(),
    })
  }

  async #submit(input: {
    kind: SkillOperationKind
    skill: SkillProjection
    nonce: string
    idempotencyKey: string
    argumentsDigest?: string
    command: string
  }): Promise<CommandReceipt> {
    const existingOperationId = this.#operationByIdempotency.get(input.idempotencyKey)
    if (existingOperationId) {
      const existing = this.#operations.get(existingOperationId)
      if (
        !existing ||
        existing.kind !== input.kind ||
        existing.skillId !== input.skill.skillId ||
        existing.expectedHash !== input.skill.version.contentHash ||
        existing.argumentsDigest !== input.argumentsDigest
      ) {
        throw skillControlError("skill_idempotency_conflict", [
          "idempotency key is already bound to another skill operation",
        ])
      }
      if (existing.receipt) return existing.receipt
      throw skillControlError("skill_operation_already_pending", [
        "an identical skill operation is already pending",
      ])
    }
    const operationId = skillOperationIdentity({
      kind: input.kind,
      skill: input.skill,
      nonce: input.nonce,
      idempotencyKey: input.idempotencyKey,
      argumentsDigest: input.argumentsDigest,
    })
    const now = this.#now().toISOString()
    const operation: SkillControlOperation = Object.freeze({
      operationId,
      kind: input.kind,
      phase: "submitting",
      taskId: input.skill.taskId,
      runId: input.skill.runId,
      sessionId: input.skill.sessionId,
      skillId: input.skill.skillId,
      expectedHash: input.skill.version.contentHash,
      expectedRegistryRevision: input.skill.version.registryRevision,
      nonce: input.nonce,
      idempotencyKey: input.idempotencyKey,
      argumentsDigest: input.argumentsDigest,
      startedAt: now,
      updatedAt: now,
      canonicalEventIds: Object.freeze([]),
    })
    this.#operations.set(operationId, operation)
    this.#operationByIdempotency.set(input.idempotencyKey, operationId)
    this.effects.begin({
      operationId,
      kind: input.kind,
      state: this.#projections.state,
      catalog: this.#snapshot.catalog!,
      skillId: input.skill.skillId,
      createdAt: now,
    })
    this.#replace({
      activeOperation: operation,
      operations: this.#operationList(),
    })
    try {
      const receipt = await this.#commands.submit(input.command, {
        mode: "enqueue",
        sealed: false,
      })
      this.#assertReceiptBinding(receipt, operation)
      this.effects.bindReceipt({
        operationId,
        commandId: receipt.commandId,
        eventIds: receipt.eventIds,
      })
      const pendingPermission = this.#permissionForCommand(receipt.commandId)
      const phase = receiptPhase(receipt, Boolean(pendingPermission))
      const updated = this.#settle(operationId, {
        phase,
        receipt,
        commandId: receipt.commandId,
        requestId: receipt.requestId,
        permissionId: pendingPermission,
        errorCode: receipt.error?.code,
        errorMessage: receipt.error?.message,
        canonicalEventIds: receipt.eventIds,
      })
      this.#replace({ lastReceipt: receipt })
      if (phase === "reconciling") this.#reconcile(updated)
      if (phase === "failed") {
        throw skillControlError(
          receipt.error?.code ?? "skill_command_rejected",
          [receipt.error?.message ?? receipt.summary],
        )
      }
      return receipt
    } catch (error) {
      const current = this.#operations.get(operationId)
      if (current && !terminalSkillControlPhase(current.phase)) {
        this.#settle(operationId, {
          phase: "failed",
          errorCode:
            typeof error === "object" &&
            error &&
            "code" in error
              ? String((error as { code?: unknown }).code ?? "skill_command_failed")
              : "skill_command_failed",
          errorMessage: error instanceof Error ? error.message : String(error),
        })
      }
      throw error
    }
  }

  async #rejectControl(input: {
    kind: SkillOperationKind
    skill: SkillProjection
    reasons: readonly string[]
    actorId?: string
    signal?: AbortSignal
  }): Promise<void> {
    if (!this.#sealed()) return
    if (!this.#permissions) {
      throw skillControlError("sealed_intervention_binding_missing", [
        "sealed skill control cannot be recorded because the permission binding is absent",
      ])
    }
    await this.#permissions.recordSealedAction({
      action: "steer",
      actorId: skillText(input.actorId, "zyra-web-operator", 256),
      reason:
        `Rejected sealed skill ${input.kind} for ${input.skill.skillId}: ` +
        input.reasons.join("; "),
      signal: input.signal,
    })
  }

  #requireSkill(skillId?: string): SkillProjection {
    this.#assertAvailable()
    const id = skillId
      ? skillIdentity(skillId, "skill control identity")
      : this.#snapshot.selectedSkillId
    if (!id) throw new Error("Select a skill first.")
    const skill = this.catalogIndex.get(id)
    if (!skill) throw new Error("Skill is absent from the canonical projection.")
    if (
      skill.taskId !== this.#snapshot.taskId ||
      skill.runId !== this.#snapshot.runId ||
      (this.#snapshot.sessionId &&
        skill.sessionId &&
        skill.sessionId !== this.#snapshot.sessionId)
    ) {
      throw new Error("Skill control exact task, run, or session binding failed.")
    }
    return skill
  }

  #assertAvailable(options: { requireSelection?: boolean } = {}): void {
    this.#assertNotClosed()
    if (!this.#snapshot.enabled) {
      throw new Error(this.#snapshot.disabledReason ?? "Skill feature binding is disabled.")
    }
    if (!this.#snapshot.viewerOpen) throw new Error("Skill viewer is closed.")
    if (!this.#snapshot.connected || !this.#online()) {
      throw new Error("Skill controls are unavailable while disconnected.")
    }
    if (!this.#snapshot.taskId || !this.#snapshot.runId || !this.#snapshot.catalog) {
      throw new Error("Skill workbench is not bound to a canonical task and run.")
    }
    if (options.requireSelection !== false && !this.#snapshot.selectedSkillId) {
      throw new Error("Select a skill first.")
    }
  }

  #assertNotClosed(): void {
    if (this.#closed) throw new Error("Skill workbench controller is closed.")
  }

  #sealed(): boolean {
    return (
      this.#snapshot.sealed ||
      this.#permissions?.getSnapshot().productMode === "sealed"
    )
  }

  #assertReceiptBinding(
    receipt: CommandReceipt,
    operation: SkillControlOperation,
  ): void {
    if (receipt.taskId !== operation.taskId || receipt.runId !== operation.runId) {
      throw skillControlError("skill_receipt_identity_mismatch", [
        "command receipt task or run differs from the skill operation",
      ])
    }
    if (
      operation.sessionId &&
      receipt.sessionId &&
      receipt.sessionId !== operation.sessionId
    ) {
      throw skillControlError("skill_receipt_session_mismatch", [
        "command receipt session differs from the skill operation",
      ])
    }
    if (receipt.idempotencyKey !== operation.idempotencyKey) {
      throw skillControlError("skill_receipt_idempotency_mismatch", [
        "command receipt idempotency differs from the submitted operation",
      ])
    }
    if (receipt.interventionCounted || receipt.humanInterventionCount > 0) {
      throw skillControlError("skill_receipt_intervention_violation", [
        "skill command receipt counted a human intervention",
      ])
    }
  }

  #permissionForCommand(commandId: string): string | undefined {
    return Object.values(this.#projections.state.permissions)
      .filter((permission) =>
        permission.controlCommandId === commandId &&
        !permission.resolvedAt &&
        !permission.terminal)
      .sort((left, right) =>
        right.sequence - left.sequence ||
        left.id.localeCompare(right.id))[0]?.id
  }

  #projectionChanged(): void {
    if (
      this.#closed ||
      !this.#snapshot.enabled ||
      !this.#snapshot.viewerOpen ||
      !this.#snapshot.taskId ||
      !this.#snapshot.runId
    ) {
      return
    }
    const catalog = buildSkillCatalogProjection(this.#projections.state, {
      taskId: this.#snapshot.taskId,
      runId: this.#snapshot.runId,
      sessionId: this.#snapshot.sessionId,
      selectedSkillId: this.#snapshot.selectedSkillId,
    })
    this.catalogIndex.replace(catalog)
    this.#replace({
      catalog,
      selectedSkillId: catalog.selectedSkillId,
      connected:
        this.#online() &&
        (this.#projections.state.runtimes[this.#snapshot.taskId]?.connected ?? true),
    })
    this.#reconcilePending()
  }

  #commandChanged(): void {
    if (this.#closed) return
    const receipt = this.#commands.getSnapshot().lastOverlayReceipt
    if (!receipt || !/^\/skills?(?:$|\s)/.test(String(receipt.name))) return
    this.#replace({ lastReceipt: receipt })
    const operation = [...this.#operations.values()].find((item) =>
      item.commandId === receipt.commandId ||
      item.requestId === receipt.requestId)
    if (!operation) return
    try {
      this.#assertReceiptBinding(receipt, operation)
    } catch (error) {
      this.#settle(operation.operationId, {
        phase: "quarantined",
        receipt,
        errorCode: "skill_receipt_quarantined",
        errorMessage: error instanceof Error ? error.message : String(error),
      })
      return
    }
    this.effects.bindReceipt({
      operationId: operation.operationId,
      commandId: receipt.commandId,
      eventIds: receipt.eventIds,
    })
    const permissionId = this.#permissionForCommand(receipt.commandId)
    const phase = receiptPhase(receipt, Boolean(permissionId))
    const updated = this.#settle(operation.operationId, {
      phase,
      receipt,
      permissionId,
      errorCode: receipt.error?.code,
      errorMessage: receipt.error?.message,
      canonicalEventIds: receipt.eventIds,
    })
    if (phase === "reconciling") this.#reconcile(updated)
  }

  #reconcilePending(): void {
    for (const operation of this.#operations.values()) {
      if (
        ["queued", "permission_pending", "reconciling", "disconnected"].includes(
          operation.phase,
        )
      ) {
        this.#reconcile(operation)
      }
    }
  }

  #reconcile(operation: SkillControlOperation): void {
    if (!this.#snapshot.catalog || !this.#snapshot.connected) return
    let effect
    try {
      effect = this.effects.verify({
        operationId: operation.operationId,
        state: this.#projections.state,
        verifiedAt: this.#now().toISOString(),
      })
    } catch (error) {
      this.#settle(operation.operationId, {
        phase: "quarantined",
        errorCode: "skill_effect_reconciliation_failed",
        errorMessage: error instanceof Error ? error.message : String(error),
      })
      return
    }
    const permission = operation.commandId
      ? this.#permissionForCommand(operation.commandId)
      : undefined
    if (effect.satisfied) {
      this.#settle(operation.operationId, {
        phase: "committed",
        effect,
        permissionId: permission,
        errorCode: undefined,
        errorMessage: undefined,
        canonicalEventIds: effect.evidenceEventIds,
      })
      return
    }
    if (effect.quarantined) {
      this.#settle(operation.operationId, {
        phase: "quarantined",
        effect,
        errorCode: "skill_effect_quarantined",
        errorMessage: effect.reasons.join("; "),
        canonicalEventIds: effect.evidenceEventIds,
      })
      return
    }
    this.#settle(operation.operationId, {
      phase: permission ? "permission_pending" : "reconciling",
      effect,
      permissionId: permission,
      canonicalEventIds: effect.evidenceEventIds,
    })
  }

  #settle(
    operationId: string,
    patch: Partial<SkillControlOperation>,
  ): SkillControlOperation {
    const current = this.#operations.get(operationId)
    if (!current) throw new Error(`Unknown skill operation ${operationId}.`)
    if (
      terminalSkillControlPhase(current.phase) &&
      patch.phase &&
      patch.phase !== current.phase
    ) {
      return current
    }
    const updated: SkillControlOperation = Object.freeze({
      ...current,
      ...patch,
      updatedAt: this.#now().toISOString(),
      canonicalEventIds: uniqueSkillStrings([
        ...current.canonicalEventIds,
        ...(patch.canonicalEventIds ?? []),
      ]),
    })
    this.#operations.set(operationId, updated)
    this.#replace({
      activeOperation: updated,
      operations: this.#operationList(),
    })
    return updated
  }

  #operationList(): readonly SkillControlOperation[] {
    return Object.freeze(
      [...this.#operations.values()]
        .sort((left, right) =>
          right.startedAt.localeCompare(left.startedAt) ||
          left.operationId.localeCompare(right.operationId))
        .slice(0, 200),
    )
  }

  #attach(): void {
    if (this.#closed || !this.#snapshot.enabled || !this.#snapshot.viewerOpen) return
    if (!this.#unsubscribeProjection) {
      this.#unsubscribeProjection = this.#projections.subscribe(() => {
        this.#projectionChanged()
      })
    }
    if (!this.#unsubscribeCommands) {
      this.#unsubscribeCommands = this.#commands.subscribe(() => {
        this.#commandChanged()
      })
    }
  }

  #detach(): void {
    this.#unsubscribeProjection?.()
    this.#unsubscribeCommands?.()
    this.#unsubscribeProjection = undefined
    this.#unsubscribeCommands = undefined
  }

  #replace(patch: Partial<SkillWorkbenchSnapshot>): void {
    if (this.#closed && !patch.closed) return
    this.#snapshot = Object.freeze({
      ...this.#snapshot,
      ...patch,
      sealed: patch.sealed ?? this.#sealed(),
      operations: patch.operations ?? this.#operationList(),
      revision: this.#snapshot.revision + 1,
    })
    for (const listener of this.#listeners) {
      try {
        listener()
      } catch {
        // A view subscriber cannot alter SkillTool settlement.
      }
    }
  }
}

function receiptPhase(
  receipt: CommandReceipt,
  permissionPending: boolean,
): SkillControlPhase {
  if (["rejected", "expired", "cancelled"].includes(receipt.phase)) return "failed"
  if (permissionPending) return "permission_pending"
  if (receipt.phase === "queued") return "queued"
  if (receipt.phase === "applied") return "reconciling"
  return "submitting"
}

function createNonce(
  prefix: string,
  skill: SkillProjection,
  now: Date,
  sequence: number,
): string {
  return `${prefix}-${skillFingerprint([
    skill.taskId,
    skill.runId,
    skill.skillId,
    skill.version.contentHash,
    now.toISOString(),
    sequence,
  ]).slice("skill:".length)}`
}

function createIdempotency(
  prefix: string,
  skill: SkillProjection,
  nonce: string,
): string {
  return `${prefix}:${skillFingerprint([
    skill.taskId,
    skill.runId,
    skill.skillId,
    skill.version.registryRevision,
    skill.version.contentHash,
    nonce,
  ]).slice("skill:".length)}`
}

function normalizeNonce(value: string): string {
  const normalized = skillText(value, "", 512)
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_.:/+-]{15,511}$/.test(normalized)) {
    throw new TypeError("Skill control nonce is invalid.")
  }
  return normalized
}

function normalizeIdempotency(value: string): string {
  const normalized = skillText(value, "", 512)
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_.:/+-]{15,511}$/.test(normalized)) {
    throw new TypeError("Skill control idempotency key is invalid.")
  }
  return normalized
}

function skillControlError(code: string, reasons: readonly string[]): Error {
  return Object.assign(new Error(reasons.join("; ") || code), {
    name: "SkillControlError",
    code,
    reasons: Object.freeze([...reasons]),
  })
}
