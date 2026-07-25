import type { CommandReceipt } from "../../../../../packages/commands/src/index.ts"
import type { CanonicalProjectionStore } from "../../state/index.ts"
import type { CanonicalProjectionState } from "../../state/contracts.ts"
import { admitAuthRefresh } from "./auth.ts"
import { McpCatalogPager, type McpCatalogQuery } from "./catalog.ts"
import type {
  McpCatalogPage,
  McpConsoleProjection,
  McpControlAction,
  McpControllerSnapshot,
  McpControlOperation,
  McpControlPhase,
  McpElicitationProjection,
  McpServerProjection,
} from "./contracts.ts"
import { McpEffectLedger } from "./effects.ts"
import {
  admitElicitationResponse,
  elicitationCommandPayload,
  McpElicitationDrafts,
} from "./elicitation.ts"
import {
  buildMcpConsoleProjection,
  type McpProjectionOptions,
} from "./projection.ts"
import { McpReconnectSupervisor } from "./reconnect.ts"
import {
  fingerprint,
  identity,
  quote,
  safeDescription,
  scalar,
} from "./value.ts"

export interface McpCommandHandoff {
  submit(
    value: string,
    options?: {
      mode?: "enqueue" | "steer" | "interrupt"
      sealed?: boolean
      displayValue?: string
      argumentOverrides?: Readonly<Record<string, unknown>>
    },
  ): Promise<CommandReceipt>
  subscribe?(listener: () => void): () => void
  getSnapshot?(): {
    lastOverlayReceipt?: CommandReceipt
    coordinator?: { lastReceipt?: CommandReceipt }
  }
}

export interface McpSealedAttemptRecorder {
  record(input: {
    action: McpControlAction
    taskId: string
    runId: string
    sessionId: string
    serverId: string
    requestId?: string
    reason: string
  }): Promise<void> | void
}

export interface McpControllerOptions {
  projections: CanonicalProjectionStore
  commands: McpCommandHandoff
  online?: () => boolean
  sealed?: () => boolean
  now?: () => Date
  sealedAttempts?: McpSealedAttemptRecorder
  projectionOptions?: Omit<McpProjectionOptions, "selectedServerId">
}

export class McpConsoleController {
  readonly catalog: McpCatalogPager
  readonly reconnect: McpReconnectSupervisor
  readonly effects = new McpEffectLedger()
  readonly drafts = new McpElicitationDrafts()
  readonly #projections: CanonicalProjectionStore
  readonly #commands: McpCommandHandoff
  readonly #online: () => boolean
  readonly #sealed: () => boolean
  readonly #now: () => Date
  readonly #sealedAttempts?: McpSealedAttemptRecorder
  readonly #projectionOptions: Omit<McpProjectionOptions, "selectedServerId">
  readonly #listeners = new Set<() => void>()
  readonly #operations = new Map<string, McpControlOperation>()
  readonly #idempotency = new Map<string, string>()
  readonly #unsubscribeProjection: () => void
  readonly #unsubscribeCommands: () => void
  #snapshot: McpControllerSnapshot

  constructor(options: McpControllerOptions) {
    this.#projections = options.projections
    this.#commands = options.commands
    this.#online = options.online ?? (() =>
      typeof navigator === "undefined" ? true : navigator.onLine)
    this.#sealed = options.sealed ?? (() => false)
    this.#now = options.now ?? (() => new Date())
    this.#sealedAttempts = options.sealedAttempts
    this.#projectionOptions = options.projectionOptions ?? {}
    this.catalog = new McpCatalogPager({
      now: () => this.#now().getTime(),
    })
    this.reconnect = new McpReconnectSupervisor({
      now: () => this.#now().getTime(),
    })
    this.#snapshot = Object.freeze({
      connected: this.#online(),
      enabled: true,
      sealed: this.#sealed(),
      closed: false,
      operations: Object.freeze([]),
      reconnectPlans: Object.freeze([]),
      revision: 0,
    })
    this.#unsubscribeProjection = this.#projections.subscribe(() => {
      this.#onProjectionChanged()
    })
    this.#unsubscribeCommands = this.#commands.subscribe?.(() => {
      this.#onCommandChanged()
    }) ?? (() => {})
  }

  getSnapshot = (): McpControllerSnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#snapshot.closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  bind(taskId?: string, runId?: string, sessionId?: string): McpConsoleProjection | undefined {
    this.#assertOpen()
    if (!taskId) {
      this.#replace({
        taskId: undefined,
        runId: undefined,
        sessionId: undefined,
        selectedServerId: undefined,
        projection: undefined,
        activeOperation: undefined,
        connected: this.#online(),
        sealed: this.#sealed(),
        error: undefined,
      })
      return undefined
    }
    const projection = buildMcpConsoleProjection(
      this.#projections.state,
      taskId,
      {
        ...this.#projectionOptions,
        selectedServerId: this.#snapshot.selectedServerId,
        nowMs: this.#now().getTime(),
      },
    )
    const boundRun = runId || projection.authority.runId
    const boundSession = sessionId || projection.authority.sessionId
    if (boundRun && projection.authority.runId && boundRun !== projection.authority.runId) {
      throw new Error("Requested MCP run differs from canonical projection.")
    }
    if (
      boundSession &&
      projection.authority.sessionId &&
      boundSession !== projection.authority.sessionId
    ) {
      throw new Error("Requested MCP session differs from canonical projection.")
    }
    for (const server of projection.servers) this.reconnect.observe(server)
    this.#replace({
      taskId,
      runId: boundRun,
      sessionId: boundSession,
      selectedServerId: projection.selectedId,
      projection,
      connected: this.#online() && projection.authority.connected,
      sealed: this.#sealed(),
      reconnectPlans: this.reconnect.list(),
      error: undefined,
    })
    this.#reconcileOperations()
    return projection
  }

  selectServer(serverId: string): McpServerProjection {
    this.#assertAvailable(false)
    const selected = identity("MCP server", serverId)
    const server = this.#snapshot.projection?.byId[selected]
    if (!server) {
      throw new Error("MCP server is absent from the canonical projection.")
    }
    this.#replace({
      selectedServerId: selected,
      projection: this.#rebuildProjection(selected),
    })
    return server
  }

  pageCatalog(input: Omit<McpCatalogQuery, "serverId"> & { serverId?: string }): McpCatalogPage {
    this.#assertAvailable(false)
    const projection = this.#requireProjection()
    const serverId = input.serverId ?? this.#requireServer().id
    return this.catalog.page(projection, {
      ...input,
      serverId,
    })
  }

  updateElicitationDraft(
    requestId: string,
    field: string,
    value: unknown,
  ): Readonly<Record<string, unknown>> {
    this.#assertAvailable(false)
    const request = this.#requireElicitation(requestId)
    return this.drafts.update(request, field, value)
  }

  removeElicitationDraft(
    requestId: string,
    field: string,
  ): Readonly<Record<string, unknown>> {
    this.#assertAvailable(false)
    const request = this.#requireElicitation(requestId)
    return this.drafts.remove(request, field)
  }

  async enableServer(serverId?: string): Promise<CommandReceipt> {
    return this.#submitSimple("enable", serverId)
  }

  async disableServer(
    serverId?: string,
    reason = "operator disabled MCP server",
  ): Promise<CommandReceipt> {
    const normalizedReason = safeDescription(reason, "operator disabled MCP server")
    return this.#submitSimple("disable", serverId, [
      "--response",
      JSON.stringify({ reason: normalizedReason }),
    ])
  }

  async refreshCatalog(serverId?: string): Promise<CommandReceipt> {
    return this.#submitSimple("refresh", serverId)
  }

  async refreshAuth(serverId?: string): Promise<CommandReceipt> {
    this.#assertAvailable(true)
    const server = this.#resolveServer(serverId)
    const admission = admitAuthRefresh({
      server,
      connected: this.#snapshot.connected,
      sealed: this.#snapshot.sealed,
      expectedOwnerId: server.ownerId,
      expectedAuthRevision: server.auth.authRevision,
    })
    if (!admission.allowed) {
      if (admission.code === "sealed") {
        await this.#recordSealed("auth-refresh", server, undefined, admission.reason)
      }
      throw new Error(admission.reason)
    }
    return this.#submit("auth-refresh", server, [
      "/mcp",
      "auth-refresh",
      quote(server.id),
    ])
  }

  async reconnectServer(serverId?: string): Promise<CommandReceipt> {
    this.#assertAvailable(true)
    const server = this.#resolveServer(serverId)
    if (this.#snapshot.sealed) {
      const reason = "Human MCP reconnect is forbidden in sealed mode."
      await this.#recordSealed("reconnect", server, undefined, reason)
      throw new Error(reason)
    }
    const admission = this.reconnect.admit(server)
    if (!admission.allowed) throw new Error(admission.reason)
    return this.#submit("reconnect", server, [
      "/mcp",
      "reconnect",
      quote(server.id),
    ])
  }

  scheduleReconnect(
    serverId?: string,
    reason = "canonical connection unavailable",
  ) {
    this.#assertAvailable(false)
    const server = this.#resolveServer(serverId)
    const plan = this.reconnect.schedule(server, reason)
    this.#replace({ reconnectPlans: this.reconnect.list() })
    return plan
  }

  async runScheduledReconnect(serverId: string): Promise<CommandReceipt> {
    this.#assertAvailable(true)
    const server = this.#resolveServer(serverId)
    const plan = this.reconnect.ready(server.id)
    if (!plan || plan.state !== "ready") {
      throw new Error("MCP reconnect plan is not ready.")
    }
    try {
      const receipt = await this.#submit("reconnect", server, [
        "/mcp",
        "reconnect",
        quote(server.id),
      ])
      this.reconnect.settle(server.id, "submitted")
      this.#replace({ reconnectPlans: this.reconnect.list() })
      return receipt
    } catch (error) {
      this.reconnect.settle(
        server.id,
        "failed",
        error instanceof Error ? error.message : String(error),
      )
      this.#replace({ reconnectPlans: this.reconnect.list() })
      throw error
    }
  }

  async submitElicitation(
    requestId: string,
    response?: Readonly<Record<string, unknown>>,
  ): Promise<CommandReceipt> {
    this.#assertAvailable(true)
    const server = this.#requireServer()
    const request = this.#requireElicitation(requestId, server)
    const supplied = response ?? this.drafts.response(request.id)
    const admission = admitElicitationResponse({
      server,
      request,
      response: supplied,
      taskId: this.#requireTask(),
      runId: this.#requireRun(),
      sessionId: this.#requireSession(),
      sealed: this.#snapshot.sealed,
      now: this.#now(),
    })
    if (!admission.allowed || !admission.answer) {
      if (admission.code === "sealed") {
        await this.#recordSealed("elicit", server, request.id, admission.reason)
      }
      throw new Error(admission.reason)
    }
    const payload = elicitationCommandPayload(admission.answer)
    const displayCommand = [
      "/mcp",
      "elicit",
      quote(server.id),
      "--request",
      quote(request.id),
      "--response",
      payload.display,
    ].join(" ")
    const receipt = await this.#submit(
      "elicit",
      server,
      displayCommand.split(" "),
      {
        elicitationId: request.id,
        responseDigest: payload.digest,
        displayCommand,
        argumentOverrides: {
          response: JSON.parse(payload.raw) as Readonly<Record<string, unknown>>,
        },
      },
    )
    this.drafts.clear(request.id)
    return receipt
  }

  disconnected(reason = "Browser canonical projection disconnected."): void {
    if (this.#snapshot.closed) return
    this.reconnect.disconnect(reason)
    for (const operation of this.#operations.values()) {
      if (!terminalPhase(operation.phase)) {
        this.#settle(operation.id, {
          phase: "disconnected",
          error: reason,
        })
      }
    }
    this.#replace({
      connected: false,
      reconnectPlans: this.reconnect.list(),
      error: reason,
    })
  }

  reconnected(): void {
    this.#assertOpen()
    if (!this.#snapshot.enabled) return
    const taskId = this.#snapshot.taskId
    if (!taskId) {
      this.#replace({
        connected: this.#online(),
        sealed: this.#sealed(),
      })
      return
    }
    const projection = this.#rebuildProjection(this.#snapshot.selectedServerId)
    for (const server of projection.servers) this.reconnect.observe(server)
    this.#replace({
      projection,
      connected: this.#online() && projection.authority.connected,
      sealed: this.#sealed(),
      reconnectPlans: this.reconnect.list(),
      error: undefined,
    })
    this.#reconcileOperations()
  }

  setSealed(value: boolean): void {
    this.#assertOpen()
    this.#replace({ sealed: value })
    if (value) {
      for (const operation of this.#operations.values()) {
        if (!terminalPhase(operation.phase)) {
          this.#settle(operation.id, {
            phase: "sealed-denied",
            error: "MCP control was stopped because sealed mode became active.",
          })
        }
      }
    }
  }

  disable(reason = "MCP console binding is disabled."): void {
    if (this.#snapshot.closed || !this.#snapshot.enabled) return
    const normalized = safeDescription(reason, "MCP console binding is disabled.")
    this.catalog.disable(normalized)
    this.reconnect.disable(normalized)
    this.effects.disable(normalized)
    this.drafts.disable(normalized)
    for (const operation of this.#operations.values()) {
      if (!terminalPhase(operation.phase)) {
        this.#settle(operation.id, {
          phase: "disabled",
          error: normalized,
        })
      }
    }
    this.#replace({
      enabled: false,
      disabledReason: normalized,
      connected: false,
      error: normalized,
    })
  }

  enable(): void {
    this.#assertOpen()
    if (this.#snapshot.enabled) return
    this.catalog.enable()
    this.reconnect.enable()
    this.effects.enable()
    this.drafts.enable()
    this.#replace({
      enabled: true,
      disabledReason: undefined,
      connected: this.#online(),
      error: undefined,
    })
    this.reconnected()
  }

  close(reason = "MCP workbench closed."): void {
    if (this.#snapshot.closed) return
    this.#unsubscribeProjection()
    this.#unsubscribeCommands()
    this.drafts.clear()
    this.reconnect.reset()
    for (const operation of this.#operations.values()) {
      if (!terminalPhase(operation.phase)) {
        this.#operations.set(operation.id, Object.freeze({
          ...operation,
          phase: "disconnected",
          error: reason,
          updatedAt: this.#now().toISOString(),
        }))
      }
    }
    this.#snapshot = Object.freeze({
      ...this.#snapshot,
      connected: false,
      closed: true,
      activeOperation: undefined,
      operations: this.#operationList(),
      reconnectPlans: Object.freeze([]),
      error: reason,
      revision: this.#snapshot.revision + 1,
    })
    this.#listeners.clear()
  }

  async #submitSimple(
    action: Exclude<McpControlAction, "auth-refresh" | "reconnect" | "elicit">,
    serverId?: string,
    tail: readonly string[] = [],
  ): Promise<CommandReceipt> {
    this.#assertAvailable(true)
    const server = this.#resolveServer(serverId)
    if (action === "enable" && server.enabled && server.state !== "disabled") {
      throw new Error("MCP server is already enabled.")
    }
    if (action !== "enable" && (!server.enabled || server.state === "disabled")) {
      if (action !== "disable") {
        throw new Error(server.disabledReason ?? "MCP server is disabled.")
      }
    }
    if (action === "disable" && !server.enabled && server.state === "disabled") {
      throw new Error("MCP server is already disabled.")
    }
    return this.#submit(action, server, [
      "/mcp",
      action,
      quote(server.id),
      ...tail,
    ])
  }

  async #submit(
    action: McpControlAction,
    server: McpServerProjection,
    parts: readonly string[],
    options: {
      elicitationId?: string
      responseDigest?: string
      displayCommand?: string
      argumentOverrides?: Readonly<Record<string, unknown>>
    } = {},
  ): Promise<CommandReceipt> {
    this.#assertAvailable(true)
    if (this.#snapshot.sealed) {
      const reason = `Human MCP ${action} is forbidden in sealed mode.`
      await this.#recordSealed(action, server, options.elicitationId, reason)
      throw new Error(reason)
    }
    this.#assertServerCurrent(server)
    const commandText = parts.join(" ")
    const displayCommand = options.displayCommand ?? commandText
    const operation = this.#begin(
      action,
      server,
      displayCommand,
      options.elicitationId,
      options.responseDigest,
    )
    try {
      const receipt = await this.#commands.submit(commandText, {
        mode: "enqueue",
        sealed: false,
        displayValue: displayCommand,
        argumentOverrides: options.argumentOverrides,
      })
      if (
        receipt.taskId !== operation.taskId ||
        receipt.runId !== operation.runId ||
        (receipt.sessionId && receipt.sessionId !== operation.sessionId)
      ) {
        this.#settle(operation.id, {
          phase: "quarantined",
          receipt,
          error: "MCP command receipt scope differs from operation.",
          requestId: receipt.requestId,
          commandId: receipt.commandId,
          eventIds: receipt.eventIds,
        })
        throw new Error("MCP command receipt was quarantined for scope mismatch.")
      }
      const phase = receiptPhase(receipt)
      const updated = this.#settle(operation.id, {
        phase,
        receipt,
        requestId: receipt.requestId,
        commandId: receipt.commandId,
        eventIds: receipt.eventIds,
        error: receipt.error?.message,
      })
      this.#replace({ lastReceipt: receipt })
      if (phase === "reconciling") this.#reconcile(updated)
      if (phase === "failed") {
        throw new Error(receipt.error?.message ?? receipt.summary)
      }
      return receipt
    } catch (error) {
      const current = this.#operations.get(operation.id)
      if (current && !terminalPhase(current.phase)) {
        this.#settle(operation.id, {
          phase: "failed",
          error: error instanceof Error ? error.message : String(error),
        })
      }
      throw error
    }
  }

  #begin(
    action: McpControlAction,
    server: McpServerProjection,
    commandText: string,
    elicitationId?: string,
    responseDigest?: string,
  ): McpControlOperation {
    const projection = this.#requireProjection()
    const now = this.#now().toISOString()
    const key = fingerprint([
      action,
      projection.authority.taskId,
      projection.authority.runId,
      projection.authority.sessionId,
      server.id,
      server.ownerId,
      server.ownerRevision,
      projection.authority.projectionRevision,
      elicitationId,
      responseDigest,
    ])
    const duplicateId = this.#idempotency.get(key)
    if (duplicateId) {
      const existing = this.#operations.get(duplicateId)
      if (existing && !terminalPhase(existing.phase)) {
        throw new Error("Equivalent MCP control is already pending.")
      }
    }
    const nonce = fingerprint([key, now, this.#operations.size])
    const id = fingerprint(["mcp-operation/v1", key, nonce])
    const baseline = this.effects.begin({
      operationId: id,
      action,
      projection,
      server,
      elicitationId,
    })
    const operation: McpControlOperation = Object.freeze({
      id,
      nonce,
      idempotencyKey: key,
      action,
      phase: "submitting",
      taskId: projection.authority.taskId,
      runId: projection.authority.runId,
      sessionId: projection.authority.sessionId,
      serverId: server.id,
      ownerId: server.ownerId,
      expectedOwnerRevision: server.ownerRevision,
      expectedProjectionRevision: projection.authority.projectionRevision,
      commandText,
      startedAt: now,
      updatedAt: now,
      baseline,
      elicitationId,
      responseDigest,
      eventIds: Object.freeze([]),
    })
    this.#operations.set(id, operation)
    this.#idempotency.set(key, id)
    this.#replace({
      activeOperation: operation,
      operations: this.#operationList(),
    })
    return operation
  }

  #settle(
    operationId: string,
    patch: Partial<McpControlOperation>,
  ): McpControlOperation {
    const current = this.#operations.get(operationId)
    if (!current) throw new Error("MCP control operation does not exist.")
    const updated: McpControlOperation = Object.freeze({
      ...current,
      ...patch,
      updatedAt: this.#now().toISOString(),
      eventIds: Object.freeze([
        ...new Set(patch.eventIds ?? current.eventIds),
      ]),
    })
    this.#operations.set(operationId, updated)
    this.#replace({
      activeOperation:
        this.#snapshot.activeOperation?.id === operationId ||
        !this.#snapshot.activeOperation
          ? updated
          : this.#snapshot.activeOperation,
      operations: this.#operationList(),
    })
    return updated
  }

  #reconcile(operation: McpControlOperation): void {
    if (!["queued", "running", "permission-pending", "reconciling"].includes(operation.phase)) {
      return
    }
    const projection = this.#snapshot.projection
    if (!projection) return
    const verification = this.effects.verify({
      operationId: operation.id,
      projection,
      state: this.#projections.state,
      commandId: operation.commandId,
      receiptEventIds: operation.eventIds,
    })
    if (verification.quarantined) {
      this.#settle(operation.id, {
        phase: "quarantined",
        verification,
        error: verification.message,
        eventIds: verification.evidenceEventIds,
      })
      return
    }
    if (verification.satisfied) {
      this.#settle(operation.id, {
        phase: "committed",
        verification,
        error: undefined,
        eventIds: verification.evidenceEventIds,
      })
      return
    }
    if (verification.terminal) {
      this.#settle(operation.id, {
        phase: "failed",
        verification,
        error: verification.message,
        eventIds: verification.evidenceEventIds,
      })
      return
    }
    this.#settle(operation.id, {
      phase: "reconciling",
      verification,
      eventIds: verification.evidenceEventIds,
    })
  }

  #reconcileOperations(): void {
    for (const operation of [...this.#operations.values()]) {
      if (["queued", "running", "permission-pending", "reconciling"].includes(operation.phase)) {
        this.#reconcile(operation)
      }
    }
  }

  #onProjectionChanged(): void {
    if (this.#snapshot.closed || !this.#snapshot.taskId) return
    const projection = this.#rebuildProjection(this.#snapshot.selectedServerId)
    for (const server of projection.servers) this.reconnect.observe(server)
    this.#replace({
      projection,
      selectedServerId: projection.selectedId,
      connected: this.#online() && projection.authority.connected,
      sealed: this.#sealed(),
      reconnectPlans: this.reconnect.list(),
      error: undefined,
    })
    this.#reconcileOperations()
  }

  #onCommandChanged(): void {
    if (this.#snapshot.closed) return
    const commandSnapshot = this.#commands.getSnapshot?.()
    const receipt =
      commandSnapshot?.lastOverlayReceipt ??
      commandSnapshot?.coordinator?.lastReceipt
    if (!receipt || receipt.name !== "/mcp") return
    this.#replace({ lastReceipt: receipt })
    const operation = [...this.#operations.values()].find(
      (candidate) =>
        candidate.commandId === receipt.commandId ||
        candidate.requestId === receipt.requestId,
    )
    if (!operation) return
    const phase = receiptPhase(receipt)
    const updated = this.#settle(operation.id, {
      phase,
      receipt,
      requestId: receipt.requestId,
      commandId: receipt.commandId,
      eventIds: receipt.eventIds,
      error: receipt.error?.message,
    })
    if (phase === "reconciling") this.#reconcile(updated)
  }

  #rebuildProjection(selectedServerId?: string): McpConsoleProjection {
    return buildMcpConsoleProjection(
      this.#projections.state,
      this.#requireTask(),
      {
        ...this.#projectionOptions,
        selectedServerId,
        nowMs: this.#now().getTime(),
      },
    )
  }

  #resolveServer(serverId?: string): McpServerProjection {
    if (!serverId) return this.#requireServer()
    const id = identity("MCP server", serverId)
    const server = this.#requireProjection().byId[id]
    if (!server) throw new Error("MCP server is absent from canonical projection.")
    return server
  }

  #requireProjection(): McpConsoleProjection {
    const projection = this.#snapshot.projection
    if (!projection) throw new Error("MCP console is not bound to a canonical projection.")
    return projection
  }

  #requireServer(): McpServerProjection {
    const projection = this.#requireProjection()
    const id = this.#snapshot.selectedServerId ?? projection.selectedId
    const server = id ? projection.byId[id] : undefined
    if (!server) throw new Error("Select a canonical MCP server first.")
    return server
  }

  #requireElicitation(
    requestId: string,
    server = this.#requireServer(),
  ): McpElicitationProjection {
    const id = identity("MCP elicitation", requestId)
    const request = server.elicitations.find((candidate) => candidate.id === id)
    if (!request) {
      throw new Error("MCP elicitation is absent from canonical projection.")
    }
    return request
  }

  #requireTask(): string {
    return identity("task", this.#snapshot.taskId)
  }

  #requireRun(): string {
    return identity("run", this.#snapshot.runId)
  }

  #requireSession(): string {
    return identity("session", this.#snapshot.sessionId)
  }

  #assertServerCurrent(server: McpServerProjection): void {
    const current = this.#requireProjection().byId[server.id]
    if (!current) throw new Error("MCP server left canonical projection.")
    if (current.ownerId !== server.ownerId) {
      throw new Error("MCP server canonical owner changed.")
    }
    if (current.ownerRevision !== server.ownerRevision) {
      throw new Error("MCP server owner revision changed.")
    }
    if (current.revision !== server.revision) {
      throw new Error("MCP server projection is stale.")
    }
  }

  #assertOpen(): void {
    if (this.#snapshot.closed) throw new Error("MCP console is closed.")
  }

  #assertAvailable(mutation: boolean): void {
    this.#assertOpen()
    if (!this.#snapshot.enabled) {
      throw new Error(this.#snapshot.disabledReason ?? "MCP console is disabled.")
    }
    if (!this.#snapshot.taskId) throw new Error("Select a task first.")
    if (mutation && (!this.#snapshot.connected || !this.#online())) {
      throw new Error("MCP controls are unavailable while canonical projection is disconnected.")
    }
    if (mutation && !this.#requireProjection().authority.snapshotComplete) {
      throw new Error("MCP controls require a complete canonical projection snapshot.")
    }
  }

  async #recordSealed(
    action: McpControlAction,
    server: McpServerProjection,
    requestId: string | undefined,
    reason: string,
  ): Promise<void> {
    await this.#sealedAttempts?.record({
      action,
      taskId: this.#requireTask(),
      runId: this.#requireRun(),
      sessionId: this.#requireSession(),
      serverId: server.id,
      requestId,
      reason,
    })
  }

  #operationList(): readonly McpControlOperation[] {
    return Object.freeze(
      [...this.#operations.values()]
        .sort(
          (left, right) =>
            right.startedAt.localeCompare(left.startedAt) ||
            left.id.localeCompare(right.id),
        )
        .slice(0, 200),
    )
  }

  #replace(patch: Partial<McpControllerSnapshot>): void {
    this.#snapshot = Object.freeze({
      ...this.#snapshot,
      ...patch,
      operations: patch.operations ?? this.#operationList(),
      reconnectPlans: patch.reconnectPlans ?? this.#snapshot.reconnectPlans,
      revision: this.#snapshot.revision + 1,
    })
    for (const listener of [...this.#listeners]) {
      try {
        listener()
      } catch {
        // View listeners cannot alter canonical MCP control settlement.
      }
    }
  }
}

function receiptPhase(receipt: CommandReceipt): McpControlPhase {
  if (receipt.phase === "queued") return "queued"
  if (receipt.phase === "received" || receipt.phase === "validated") {
    return "permission-pending"
  }
  if (receipt.phase === "running") return "running"
  if (receipt.phase === "applied") return "reconciling"
  return "failed"
}

function terminalPhase(phase: McpControlPhase): boolean {
  return [
    "committed",
    "failed",
    "quarantined",
    "disabled",
    "sealed-denied",
  ].includes(phase)
}
