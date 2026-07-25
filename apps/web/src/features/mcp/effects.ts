import type { CanonicalProjectionState } from "../../state/contracts.ts"
import type {
  McpConsoleProjection,
  McpControlAction,
  McpEffectBaseline,
  McpEffectVerification,
  McpServerProjection,
} from "./contracts.ts"
import { fingerprint, unique } from "./value.ts"

interface TrackedEffect {
  operationId: string
  baseline: McpEffectBaseline
}

export class McpEffectLedger {
  readonly #tracked = new Map<string, TrackedEffect>()
  readonly #verified = new Map<string, McpEffectVerification>()
  #enabled = true
  #disabledReason = "MCP effect ledger is disabled."

  begin(input: {
    operationId: string
    action: McpControlAction
    projection: McpConsoleProjection
    server: McpServerProjection
    elicitationId?: string
  }): McpEffectBaseline {
    this.#assertEnabled()
    const baseline = captureMcpEffectBaseline(input)
    const existing = this.#tracked.get(input.operationId)
    if (existing && existing.baseline.id !== baseline.id) {
      throw new Error("MCP operation identity was reused with another canonical baseline.")
    }
    if (!existing) {
      this.#tracked.set(input.operationId, Object.freeze({
        operationId: input.operationId,
        baseline,
      }))
    }
    return existing?.baseline ?? baseline
  }

  verify(input: {
    operationId: string
    projection: McpConsoleProjection
    state: CanonicalProjectionState
    commandId?: string
    receiptEventIds?: readonly string[]
  }): McpEffectVerification {
    this.#assertEnabled()
    const tracked = this.#tracked.get(input.operationId)
    if (!tracked) throw new Error("MCP effect baseline does not exist.")
    const verification = verifyMcpEffect({
      baseline: tracked.baseline,
      projection: input.projection,
      state: input.state,
      commandId: input.commandId,
      receiptEventIds: input.receiptEventIds,
    })
    this.#verified.set(input.operationId, verification)
    return verification
  }

  get(operationId: string): McpEffectVerification | undefined {
    this.#assertEnabled()
    return this.#verified.get(operationId)
  }

  remove(operationId: string): boolean {
    this.#assertEnabled()
    this.#verified.delete(operationId)
    return this.#tracked.delete(operationId)
  }

  clear(): void {
    this.#assertEnabled()
    this.#tracked.clear()
    this.#verified.clear()
  }

  disable(reason = "MCP effect ledger is disabled."): void {
    this.#enabled = false
    this.#disabledReason = reason
  }

  enable(): void {
    this.#enabled = true
  }

  #assertEnabled(): void {
    if (!this.#enabled) throw new Error(this.#disabledReason)
  }
}

export function captureMcpEffectBaseline(input: {
  action: McpControlAction
  projection: McpConsoleProjection
  server: McpServerProjection
  elicitationId?: string
}): McpEffectBaseline {
  const elicitation = input.elicitationId
    ? input.server.elicitations.find((item) => item.id === input.elicitationId)
    : undefined
  const catalogFingerprint = fingerprint([
    input.server.tools.map((item) => [item.id, item.revision]),
    input.server.resources.map((item) => [item.id, item.revision]),
    input.server.prompts.map((item) => [item.id, item.revision]),
  ])
  const value = {
    action: input.action,
    taskId: input.projection.authority.taskId,
    runId: input.projection.authority.runId,
    sessionId: input.projection.authority.sessionId,
    serverId: input.server.id,
    ownerId: input.server.ownerId,
    projectionRevision: input.projection.authority.projectionRevision,
    ownerRevision: input.server.ownerRevision,
    serverRevision: input.server.revision,
    configRevision: input.server.config.configRevision,
    authRevision: input.server.auth.authRevision,
    capabilityRevision: input.server.capabilities.revision,
    connectionEpoch: input.server.connectionEpoch,
    state: input.server.state,
    enabled: input.server.enabled,
    expiresAt: input.server.auth.expiresAt,
    catalogFingerprint,
    elicitationId: elicitation?.id,
    elicitationState: elicitation?.state,
  }
  return Object.freeze({
    id: fingerprint(["mcp-effect/v1", value]),
    ...value,
  })
}

export function verifyMcpEffect(input: {
  baseline: McpEffectBaseline
  projection: McpConsoleProjection
  state: CanonicalProjectionState
  commandId?: string
  receiptEventIds?: readonly string[]
}): McpEffectVerification {
  const baseline = input.baseline
  const changed: string[] = []
  const evidence = canonicalEvidence(
    input.state,
    baseline.serverId,
    input.commandId,
    input.receiptEventIds,
  )
  if (input.projection.authority.taskId !== baseline.taskId) {
    return result(
      false,
      true,
      true,
      "task_changed",
      "Selected MCP projection belongs to another task.",
      input.projection,
      changed,
      evidence,
    )
  }
  if (
    input.projection.authority.runId !== baseline.runId ||
    input.projection.authority.sessionId !== baseline.sessionId
  ) {
    return result(
      false,
      true,
      true,
      "scope_changed",
      "MCP task run or session changed while control was pending.",
      input.projection,
      changed,
      evidence,
    )
  }
  const server = input.projection.byId[baseline.serverId]
  if (!server) {
    if (baseline.action === "disable" && projectionAdvanced(input.projection, baseline)) {
      return result(
        true,
        true,
        false,
        "server_removed_after_disable",
        "Canonical MCP projection removed the disabled server.",
        input.projection,
        ["server"],
        evidence,
      )
    }
    return result(
      false,
      false,
      false,
      "server_missing",
      "MCP server is absent; wait for a complete canonical projection.",
      input.projection,
      changed,
      evidence,
    )
  }
  if (server.ownerId !== baseline.ownerId) {
    return result(
      false,
      true,
      true,
      "owner_changed",
      "Canonical MCP owner changed while control was pending.",
      input.projection,
      changed,
      evidence,
    )
  }
  if (server.ownerRevision < baseline.ownerRevision) {
    return result(
      false,
      true,
      true,
      "owner_revision_regressed",
      "Canonical MCP owner revision regressed.",
      input.projection,
      changed,
      evidence,
    )
  }
  if (server.ownerRevision > baseline.ownerRevision) changed.push("ownerRevision")
  if (server.revision > baseline.serverRevision) changed.push("serverRevision")
  if (server.config.configRevision > baseline.configRevision) changed.push("configRevision")
  if (server.auth.authRevision > baseline.authRevision) changed.push("authRevision")
  if (server.capabilities.revision > baseline.capabilityRevision) changed.push("capabilityRevision")
  if (server.connectionEpoch > baseline.connectionEpoch) changed.push("connectionEpoch")
  if (server.state !== baseline.state) changed.push("state")
  if (server.enabled !== baseline.enabled) changed.push("enabled")
  if (server.auth.expiresAt !== baseline.expiresAt) changed.push("expiresAt")
  const catalogFingerprint = fingerprint([
    server.tools.map((item) => [item.id, item.revision]),
    server.resources.map((item) => [item.id, item.revision]),
    server.prompts.map((item) => [item.id, item.revision]),
  ])
  if (catalogFingerprint !== baseline.catalogFingerprint) changed.push("catalog")
  const advanced = projectionAdvanced(input.projection, baseline)
  const evidenceObserved =
    (input.receiptEventIds?.length ?? 0) === 0 ||
    (input.receiptEventIds ?? []).every((eventId) =>
      Boolean(input.state.causality.byEvent[eventId]))
  const commandObserved =
    !input.commandId ||
    Boolean(input.state.commands[input.commandId]) ||
    Boolean(input.state.causality.byControlCommand[input.commandId]?.length)
  if (!advanced || !evidenceObserved || !commandObserved) {
    return result(
      false,
      false,
      false,
      "canonical_evidence_pending",
      "Waiting for a later canonical MCP projection and command evidence.",
      input.projection,
      changed,
      evidence,
    )
  }
  if (baseline.action === "enable") {
    const satisfied =
      server.enabled &&
      server.state !== "disabled" &&
      (changed.includes("enabled") ||
        changed.includes("serverRevision") ||
        changed.includes("ownerRevision"))
    return effectResult(
      satisfied,
      "enable",
      "Canonical MCP server is enabled.",
      "Canonical MCP enable effect is not yet visible.",
      input.projection,
      changed,
      evidence,
    )
  }
  if (baseline.action === "disable") {
    const satisfied =
      !server.enabled &&
      server.state === "disabled" &&
      Boolean(server.disabledReason) &&
      (changed.includes("enabled") ||
        changed.includes("state") ||
        changed.includes("serverRevision"))
    return effectResult(
      satisfied,
      "disable",
      "Canonical MCP server is disabled with an owner reason.",
      "Canonical MCP disable effect is not yet visible.",
      input.projection,
      changed,
      evidence,
    )
  }
  if (baseline.action === "reconnect") {
    const satisfied =
      server.state === "connected" &&
      (changed.includes("connectionEpoch") ||
        changed.includes("state") ||
        changed.includes("serverRevision"))
    return effectResult(
      satisfied,
      "reconnect",
      "Canonical MCP connection advanced and is connected.",
      "Canonical MCP reconnect effect is not yet visible.",
      input.projection,
      changed,
      evidence,
    )
  }
  if (baseline.action === "refresh") {
    const satisfied =
      changed.includes("capabilityRevision") ||
      changed.includes("catalog") ||
      (changed.includes("serverRevision") &&
        server.capabilities.changedEventId !== undefined)
    return effectResult(
      satisfied,
      "refresh",
      "Canonical MCP capability catalog advanced.",
      "Canonical MCP catalog refresh effect is not yet visible.",
      input.projection,
      changed,
      evidence,
    )
  }
  if (baseline.action === "auth-refresh") {
    const satisfied =
      server.auth.state === "valid" &&
      !server.auth.refreshInFlight &&
      (changed.includes("authRevision") ||
        changed.includes("expiresAt") ||
        baseline.state === "needs-auth" && server.state !== "needs-auth")
    const terminalFailure =
      server.auth.state === "failed" &&
      changed.includes("authRevision")
    if (terminalFailure) {
      return result(
        false,
        true,
        false,
        "auth_refresh_failed",
        server.auth.lastFailureMessage ?? "Canonical MCP authentication refresh failed.",
        input.projection,
        changed,
        evidence,
      )
    }
    return effectResult(
      satisfied,
      "auth_refresh",
      "Canonical MCP authentication revision is valid.",
      "Canonical MCP authentication refresh effect is not yet visible.",
      input.projection,
      changed,
      evidence,
    )
  }
  const elicitation = baseline.elicitationId
    ? server.elicitations.find((item) => item.id === baseline.elicitationId)
    : undefined
  if (!elicitation) {
    return result(
      false,
      false,
      false,
      "elicitation_missing",
      "MCP elicitation request is absent from canonical projection.",
      input.projection,
      changed,
      evidence,
    )
  }
  if (elicitation.requestRevision < (baseline.ownerRevision || 0)) {
    return result(
      false,
      true,
      true,
      "elicitation_revision_regressed",
      "MCP elicitation revision regressed.",
      input.projection,
      changed,
      evidence,
    )
  }
  const accepted = elicitation.state === "accepted"
  const failed = ["declined", "cancelled", "expired", "failed"].includes(elicitation.state)
  if (failed) {
    return result(
      false,
      true,
      false,
      `elicitation_${elicitation.state}`,
      `Canonical MCP elicitation settled as ${elicitation.state}.`,
      input.projection,
      [...changed, "elicitationState"],
      evidence,
    )
  }
  return result(
    accepted,
    accepted,
    false,
    accepted ? "elicitation_accepted" : "elicitation_pending",
    accepted
      ? "Canonical MCP owner accepted the elicitation response."
      : "Canonical MCP elicitation settlement is pending.",
    input.projection,
    elicitation.state !== baseline.elicitationState
      ? [...changed, "elicitationState"]
      : changed,
    evidence,
  )
}

function projectionAdvanced(
  projection: McpConsoleProjection,
  baseline: McpEffectBaseline,
): boolean {
  return (
    projection.authority.projectionRevision > baseline.projectionRevision &&
    projection.authority.snapshotComplete
  )
}

function canonicalEvidence(
  state: CanonicalProjectionState,
  serverId: string,
  commandId?: string,
  receiptEventIds: readonly string[] = [],
): string[] {
  const ids = new Set(receiptEventIds)
  if (commandId) {
    for (const eventId of state.causality.byControlCommand[commandId] ?? []) {
      ids.add(eventId)
    }
  }
  for (const eventId of state.causality.eventOrder) {
    const event = state.causality.byEvent[eventId]
    if (!event || !event.effective) continue
    if (
      event.entityRefs.some((reference) =>
        reference === `mcp:${serverId}` ||
        reference === `mcp-server:${serverId}` ||
        reference.endsWith(`:${serverId}`))
    ) {
      ids.add(eventId)
    }
  }
  return [...ids]
    .filter((eventId) => Boolean(state.causality.byEvent[eventId]))
    .sort((left, right) =>
      (state.causality.byEvent[left]?.sequence ?? 0) -
        (state.causality.byEvent[right]?.sequence ?? 0) ||
      left.localeCompare(right))
}

function effectResult(
  satisfied: boolean,
  code: string,
  success: string,
  pending: string,
  projection: McpConsoleProjection,
  changed: readonly string[],
  evidence: readonly string[],
): McpEffectVerification {
  return result(
    satisfied,
    satisfied,
    false,
    satisfied ? `${code}_committed` : `${code}_pending`,
    satisfied ? success : pending,
    projection,
    changed,
    evidence,
  )
}

function result(
  satisfied: boolean,
  terminal: boolean,
  quarantined: boolean,
  code: string,
  message: string,
  projection: McpConsoleProjection,
  changed: readonly string[],
  evidence: readonly string[],
): McpEffectVerification {
  return Object.freeze({
    satisfied,
    terminal,
    quarantined,
    code,
    message,
    observedRevision: projection.authority.projectionRevision,
    changed: Object.freeze(unique(changed)),
    evidenceEventIds: Object.freeze(unique(evidence)),
  })
}
