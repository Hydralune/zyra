import { describe, expect, test } from "bun:test"
import type { CommandReceipt } from "../../../packages/commands/src/index.ts"
import {
  CanonicalProjectionStore,
  type CanonicalProjectionState,
} from "../src/state/index.ts"
import {
  normalizeEventFrame,
  type IngressBatch,
  type IngressEvent,
  type JsonObject,
} from "../src/events/ingress/index.ts"
import {
  McpCatalogError,
  McpCatalogPager,
  McpConsoleController,
  McpEffectLedger,
  McpReconnectSupervisor,
  admitAuthRefresh,
  admitElicitationResponse,
  assertSecretSafe,
  auditAuthPayload,
  buildMcpConsoleProjection,
  credentialPresenceProjection,
  decodeCursor,
  redactMcpAuthDiagnostic,
  verifyMcpEffect,
  type McpCommandHandoff,
  type McpConsoleProjection,
  type McpServerProjection,
} from "../src/features/mcp/index.ts"

const TASK = "task-mcp"
const RUN = "run-mcp"
const SESSION = "session-mcp"
const NOW = "2026-07-25T12:00:00.000Z"
const DIGEST = `sha256:${"a".repeat(64)}`

function event(
  sequence: number,
  options: {
    eventId?: string
    eventType?: string
    domain?: string
    inline?: JsonObject
    metadata?: JsonObject
    toolCallId?: string
    commandId?: string
    terminal?: boolean
  } = {},
): IngressEvent {
  const eventId = options.eventId ?? `mcp-event-${sequence}`
  const eventType = options.eventType ?? "mcp.state.changed"
  const raw = {
    schema: "zyra.runtime-event/v1",
    eventId,
    eventType,
    eventVersion: 1,
    aggregateId: `task:${TASK}`,
    aggregateSequence: sequence,
    globalSequence: sequence,
    producerSequence: sequence,
    idempotencyKey: `mcp-idempotency-${sequence}`,
    correlationId: "mcp-correlation",
    createdAt: NOW,
    committedAt: new Date(Date.parse(NOW) + sequence * 1000).toISOString(),
    durability: "durable",
    effect: "effective",
    identity: {
      taskId: TASK,
      runId: RUN,
      sessionId: SESSION,
      spanId: `mcp-span-${sequence}`,
      toolCallId: options.toolCallId,
      controlCommandId: options.commandId,
    },
    sender: {
      kind: "runtime",
      id: "claude-runtime-mcp",
      capabilityRefs: ["mcp"],
    },
    intent: "status",
    topKRecipients: [],
    summary: `MCP event ${sequence}`,
    stateDelta: {
      domain: options.domain ?? "task",
      operation: "transition",
      path: ["mcp"],
      beforeDigest: DIGEST,
      afterDigest: `sha256:${String(sequence).padStart(64, "0")}`,
      effective: true,
    },
    evidenceRefs: [],
    artifactRefs: [],
    provenance: {
      sourceRepository: "zyra",
      sourceModule: "mcp-panel-test",
      migrationRole: "zyra_owned",
      producerVersion: "test",
      trust: "internal",
    },
    inline: options.inline ?? { status: "running" },
    metadata: options.metadata ?? {},
    sourceBytes: 200,
    inlineBytes: 100,
    envelopeBytes: 2048,
    contentDigest: DIGEST,
    settlement: "atomic",
    terminal: options.terminal ?? false,
  }
  return normalizeEventFrame(
    JSON.parse(JSON.stringify({
      schema: "zyra.event-ingress-frame/v1",
      kind: "event",
      source: "delta",
      generation: 1,
      taskId: TASK,
      sequence,
      previousSequence: Math.max(0, sequence - 1),
      eventId,
      eventType,
      correlationId: "mcp-correlation",
      observedAtMs: Date.parse(NOW) + sequence * 1000,
      cursor: `mcp-cursor-${sequence}`,
      event: raw,
    })),
    TASK,
    1,
  ).event
}

function batch(events: readonly IngressEvent[], snapshot = false): IngressBatch {
  const sequence = Math.max(...events.map((item) => item.globalSequence))
  return {
    taskId: TASK,
    generation: 1,
    events: Object.freeze([...events]),
    receipts: Object.freeze([]),
    cursor: `mcp-cursor-${sequence}`,
    fromSequence: Math.min(...events.map((item) => item.globalSequence)),
    sequence,
    highWatermark: sequence,
    receivedAt: Date.parse(NOW) + sequence * 1000,
    transport: "long_poll",
    snapshot,
    caughtUp: true,
  }
}

function serverFact(overrides: JsonObject = {}): JsonObject {
  return {
    schema: "zyra.mcp-server/v1",
    server_id: "alpha",
    name: "Alpha tools",
    owner_id: "claude-runtime-mcp",
    owner_revision: 4,
    revision: 7,
    task_id: TASK,
    run_id: RUN,
    session_id: SESSION,
    enabled: true,
    connection_state: "connected",
    connection_epoch: 2,
    last_connected_at: NOW,
    config: {
      source: "project",
      source_id: ".mcp.json",
      config_revision: 3,
      config_digest: "sha256:config",
      transport: "streamable-http",
      endpoint_class: "private",
      headers: {
        Authorization: "Bearer super-secret-token",
        "X-Tenant": "tenant-a",
      },
      environment: {
        MCP_API_KEY: "secret-value",
      },
      args: ["--safe"],
    },
    auth: {
      required: true,
      state: "valid",
      provider: "oauth-provider",
      access_token: "secret-access",
      refresh_token: "secret-refresh",
      client_secret: "secret-client",
      expires_at: "2026-07-25T13:00:00.000Z",
      refresh_eligible: true,
      auth_revision: 5,
      scopes: ["tools.read", "tools.execute", "offline_access"],
    },
    capabilities: {
      revision: 11,
      tool_count: 2,
      resource_count: 1,
      prompt_count: 1,
      elicitation: true,
      tool_list_changed: true,
      changed_event_id: "mcp-event-1",
    },
    tools: [
      {
        id: "alpha:tool:search",
        name: "search",
        title: "Search",
        description: "Search indexed documents",
        revision: 2,
        input_schema: {
          properties: { query: { type: "string" } },
          required: ["query"],
        },
      },
      {
        id: "alpha:tool:write",
        name: "write",
        title: "Write",
        description: "Write a document",
        revision: 1,
        sensitive: true,
      },
    ],
    resources: [
      {
        id: "alpha:resource:docs",
        name: "docs",
        uri: "mcp://alpha/docs",
        title: "Documents",
        mime_type: "application/json",
        revision: 1,
      },
    ],
    prompts: [
      {
        id: "alpha:prompt:review",
        name: "review",
        title: "Review document",
        argument_names: ["document"],
        required_arguments: ["document"],
        revision: 1,
      },
    ],
    elicitations: [
      {
        id: "elicit-1",
        owner_id: "claude-runtime-mcp",
        task_id: TASK,
        run_id: RUN,
        session_id: SESSION,
        request_revision: 2,
        title: "Choose account",
        message: "Select the account used by Alpha.",
        requested_at: NOW,
        expires_at: "2026-07-25T13:00:00.000Z",
        state: "pending",
        response_schema: {
          properties: {
            account: { type: "string" },
            password: { type: "string", sensitive: true },
          },
          required: ["account", "password"],
        },
      },
    ],
    ...overrides,
  }
}

function projectionStore(): CanonicalProjectionStore {
  const store = new CanonicalProjectionStore({
    restore: false,
    autoPersist: false,
  })
  store.apply(
    batch([
      event(1, {
        eventType: "task.started",
        domain: "task",
        inline: {
          task_id: TASK,
          title: "MCP task",
          status: "running",
          mcp_owner_id: "claude-runtime-mcp",
          mcp_servers: [
            serverFact(),
            serverFact({
              server_id: "cross-run",
              run_id: "other-run",
            }),
          ],
        },
      }),
      event(2, {
        eventType: "mcp.tool.registered",
        domain: "tool",
        toolCallId: "tool-call-alpha",
        inline: {
          tool_call_id: "tool-call-alpha",
          tool_name: "mcp__alpha__search",
          title: "Search execution",
          status: "running",
          mcp_server_id: "alpha",
          mcp_owner_id: "claude-runtime-mcp",
        },
      }),
    ], true),
  )
  return store
}

function projection(): {
  store: CanonicalProjectionStore
  value: McpConsoleProjection
  server: McpServerProjection
} {
  const store = projectionStore()
  const value = buildMcpConsoleProjection(store.state, TASK, {
    nowMs: Date.parse(NOW),
  })
  const server = value.byId.alpha
  if (!server) throw new Error("fixture MCP server was not projected")
  return { store, value, server }
}

class RecordingHandoff implements McpCommandHandoff {
  readonly commands: string[] = []
  readonly submissions: Array<{
    value: string
    options?: Parameters<McpCommandHandoff["submit"]>[1]
  }> = []
  readonly listeners = new Set<() => void>()
  last?: CommandReceipt

  async submit(
    value: string,
    options?: Parameters<McpCommandHandoff["submit"]>[1],
  ): Promise<CommandReceipt> {
    this.commands.push(value)
    this.submissions.push({ value, options })
    this.last = receipt(value, this.commands.length)
    for (const listener of this.listeners) listener()
    return this.last
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  getSnapshot() {
    return { coordinator: { lastReceipt: this.last } }
  }
}

function receipt(command: string, offset: number): CommandReceipt {
  const action = command.split(/\s+/)[1] ?? "list"
  return {
    schema: "zyra.command-receipt/v1",
    requestId: `request-${offset}`,
    commandId: `command-${offset}`,
    taskId: TASK,
    runId: RUN,
    sessionId: SESSION,
    name: "/mcp",
    scope: "task",
    mode: "enqueue",
    priority: "next",
    idempotencyKey: `receipt-${offset}`,
    phase: "queued",
    summary: `MCP ${action} queued`,
    displayText: `MCP ${action} queued`,
    data: {},
    eventIds: [],
    replayed: false,
    durable: true,
    executed: false,
    interventionCounted: false,
    humanInterventionCount: 0,
    operatorInterventionAttemptCount: 0,
    createdAt: NOW,
    raw: {},
  }
}

describe("MCP canonical projection and secret boundary", () => {
  test("admits authoritative MCP facts, rejects cross-run facts, and never retains credentials", () => {
    const { value, server } = projection()
    expect(value.servers.map((item) => item.id)).toEqual(["alpha"])
    expect(value.rejected.some((item) => item.code === "fact_run_mismatch")).toBe(true)
    expect(server.config.source).toBe("project")
    expect(server.config.transport).toBe("streamable-http")
    expect(server.config.headerNames).toEqual(["X-Tenant"])
    expect(Object.values(server.config.secretFieldPresence).some(Boolean)).toBe(true)
    expect(server.auth.accessTokenPresent).toBe(true)
    expect(server.auth.refreshTokenPresent).toBe(true)
    expect(server.auth.clientSecretPresent).toBe(true)
    expect(server.auth.expiresInMs).toBe(60 * 60_000)
    expect(server.tools).toHaveLength(2)
    expect(server.resources).toHaveLength(1)
    expect(server.prompts).toHaveLength(1)
    const serialized = JSON.stringify(value)
    expect(serialized).not.toContain("super-secret-token")
    expect(serialized).not.toContain("secret-access")
    expect(serialized).not.toContain("secret-refresh")
    expect(serialized).not.toContain("secret-client")
    assertSecretSafe(value)
  })

  test("audits and redacts auth diagnostics including URLs and nested credential fields", () => {
    const source = {
      oauth: {
        access_token: "secret-access",
        refresh_token: "secret-refresh",
        callback: "https://callback.invalid/?code=secret-code&state=safe",
      },
      headers: {
        authorization: "Bearer secret-bearer",
      },
    }
    const audit = auditAuthPayload(source)
    expect(audit.safe).toBe(false)
    expect(audit.presentFields.length).toBeGreaterThan(2)
    const presence = credentialPresenceProjection(source)
    expect(Object.values(presence).every(Boolean)).toBe(true)
    const redacted = redactMcpAuthDiagnostic(source)
    const serialized = JSON.stringify(redacted)
    expect(serialized).not.toContain("secret-access")
    expect(serialized).not.toContain("secret-refresh")
    expect(serialized).not.toContain("secret-code")
    expect(serialized).not.toContain("secret-bearer")
  })

  test("auth refresh admission binds server state, owner, revision, connectivity, and sealed mode", () => {
    const { server } = projection()
    expect(admitAuthRefresh({
      server,
      connected: true,
      sealed: false,
      expectedOwnerId: server.ownerId,
      expectedAuthRevision: server.auth.authRevision,
    }).allowed).toBe(true)
    expect(admitAuthRefresh({
      server,
      connected: true,
      sealed: true,
    }).code).toBe("sealed")
    expect(admitAuthRefresh({
      server,
      connected: false,
      sealed: false,
    }).code).toBe("projection-disconnected")
    expect(admitAuthRefresh({
      server,
      connected: true,
      sealed: false,
      expectedOwnerId: "another-owner",
    }).code).toBe("owner-mismatch")
  })
})

describe("MCP bounded catalogs and reconnect", () => {
  test("paginates each catalog with revision, task, run, session, server, kind, and query binding", () => {
    const { value } = projection()
    const pager = new McpCatalogPager({
      maximumLimit: 1,
      cursorTtlMs: 60_000,
      now: () => Date.parse(NOW),
    })
    const first = pager.page(value, {
      serverId: "alpha",
      kind: "tool",
      limit: 1,
    })
    expect(first.items).toHaveLength(1)
    expect(first.hasNext).toBe(true)
    const cursor = decodeCursor(first.nextCursor!)
    expect(cursor.serverId).toBe("alpha")
    expect(cursor.capabilityRevision).toBe(11)
    const second = pager.page(value, {
      serverId: "alpha",
      kind: "tool",
      limit: 1,
      cursor: first.nextCursor,
    })
    expect(second.offset).toBe(1)
    expect(second.items[0]?.id).not.toBe(first.items[0]?.id)
    expect(() => pager.page(value, {
      serverId: "alpha",
      kind: "prompt",
      cursor: first.nextCursor,
    })).toThrow(McpCatalogError)
    expect(() => pager.page(value, {
      serverId: "alpha",
      kind: "tool",
      query: "write",
      cursor: first.nextCursor,
    })).toThrow("another search query")
    pager.disable("binding removed")
    expect(() => pager.page(value, {
      serverId: "alpha",
      kind: "tool",
    })).toThrow("binding removed")
  })

  test("invalidates pagination when canonical capability revision changes", () => {
    const { value, server } = projection()
    let now = Date.parse(NOW)
    const pager = new McpCatalogPager({
      maximumLimit: 1,
      cursorTtlMs: 60_000,
      now: () => now,
    })
    const first = pager.page(value, {
      serverId: "alpha",
      kind: "tool",
      limit: 1,
    })
    const changedServer = {
      ...server,
      capabilities: {
        ...server.capabilities,
        revision: server.capabilities.revision + 1,
      },
    } as McpServerProjection
    const changed = {
      ...value,
      authority: {
        ...value.authority,
        projectionRevision: value.authority.projectionRevision + 1,
      },
      byId: { alpha: changedServer },
      servers: [changedServer],
      selected: changedServer,
    } as McpConsoleProjection
    expect(() => pager.page(changed, {
      serverId: "alpha",
      kind: "tool",
      cursor: first.nextCursor,
    })).toThrow("capability revision changed")
    now += 120_000
    expect(() => pager.page(value, {
      serverId: "alpha",
      kind: "tool",
      cursor: first.nextCursor,
    })).toThrow("expired")
  })

  test("uses deterministic bounded reconnect backoff and fails closed for disabled/auth servers", () => {
    const { server } = projection()
    let now = Date.parse(NOW)
    const supervisor = new McpReconnectSupervisor({
      now: () => now,
      policy: {
        initialDelayMs: 1000,
        maximumDelayMs: 8000,
        jitterRatio: 0,
        maximumAttempts: 3,
      },
    })
    const disconnected = {
      ...server,
      state: "disconnected",
      reconnectEligible: true,
    } as McpServerProjection
    const first = supervisor.schedule(disconnected, "socket closed")
    expect(first.delayMs).toBe(1000)
    expect(supervisor.ready(server.id)?.state).toBe("scheduled")
    now += 1000
    expect(supervisor.ready(server.id)?.state).toBe("ready")
    supervisor.settle(server.id, "failed", "owner rejected")
    const second = supervisor.schedule(disconnected, "retry")
    expect(second.delayMs).toBe(2000)
    const disabled = {
      ...disconnected,
      enabled: false,
      state: "disabled",
      disabledReason: "policy disabled",
    } as McpServerProjection
    expect(supervisor.admit(disabled).code).toBe("disabled")
    const auth = {
      ...disconnected,
      state: "needs-auth",
    } as McpServerProjection
    expect(supervisor.admit(auth).code).toBe("auth-required")
    supervisor.disable("module disabled")
    expect(() => supervisor.list()).toThrow("module disabled")
  })
})

describe("MCP elicitation, effect reconciliation, and controller fail-closed behavior", () => {
  test("validates scoped schema-bound elicitation without retaining sensitive display values", () => {
    const { server } = projection()
    const request = server.elicitations[0]!
    const admitted = admitElicitationResponse({
      server,
      request,
      response: {
        account: "work",
        password: "secret-password",
      },
      taskId: TASK,
      runId: RUN,
      sessionId: SESSION,
      sealed: false,
      now: new Date(NOW),
    })
    expect(admitted.allowed).toBe(true)
    expect(admitted.answer?.redactedResponse.password).toBe("[present]")
    expect(JSON.stringify(admitted.answer?.redactedResponse)).not.toContain("secret-password")
    expect(admitElicitationResponse({
      server,
      request,
      response: { account: "work" },
      taskId: TASK,
      runId: RUN,
      sessionId: SESSION,
      sealed: false,
      now: new Date(NOW),
    }).code).toBe("required-field-missing")
    expect(admitElicitationResponse({
      server,
      request,
      response: { account: "work", password: "x", extra: "bad" },
      taskId: TASK,
      runId: RUN,
      sessionId: SESSION,
      sealed: false,
      now: new Date(NOW),
    }).code).toBe("unknown-field")
    expect(admitElicitationResponse({
      server,
      request,
      response: { account: "work", password: "x" },
      taskId: TASK,
      runId: RUN,
      sessionId: SESSION,
      sealed: true,
      now: new Date(NOW),
    }).code).toBe("sealed")
  })

  test("hands elicitation secrets through structured arguments while command memory stays redacted", async () => {
    const store = projectionStore()
    const commands = new RecordingHandoff()
    const controller = new McpConsoleController({
      projections: store,
      commands,
      online: () => true,
      now: () => new Date(NOW),
    })
    controller.bind(TASK, RUN, SESSION)
    const request = controller.getSnapshot().projection?.selected?.elicitations[0]
    expect(request).toBeTruthy()
    await controller.submitElicitation(request!.id, {
      account: "work",
      password: "secret-password",
    })
    const submitted = commands.submissions[0]
    expect(submitted?.value).not.toContain("secret-password")
    expect(submitted?.options?.displayValue).not.toContain("secret-password")
    expect(JSON.stringify(submitted?.options?.argumentOverrides)).toContain(
      "secret-password",
    )
    controller.close()
    store.close()
  })

  test("requires a later canonical semantic effect rather than optimistic command receipt", () => {
    const { store, value, server } = projection()
    const ledger = new McpEffectLedger()
    const baseline = ledger.begin({
      operationId: "operation-disable",
      action: "disable",
      projection: value,
      server,
    })
    const pending = verifyMcpEffect({
      baseline,
      projection: value,
      state: store.state,
    })
    expect(pending.satisfied).toBe(false)
    expect(pending.code).toBe("canonical_evidence_pending")
    const disabledServer = {
      ...server,
      enabled: false,
      state: "disabled",
      disabledReason: "operator disabled",
      revision: server.revision + 1,
      ownerRevision: server.ownerRevision + 1,
    } as McpServerProjection
    const changed = {
      ...value,
      authority: {
        ...value.authority,
        projectionRevision: value.authority.projectionRevision + 1,
      },
      servers: [disabledServer],
      byId: { alpha: disabledServer },
      selected: disabledServer,
    } as McpConsoleProjection
    const committed = verifyMcpEffect({
      baseline,
      projection: changed,
      state: store.state,
    })
    expect(committed.satisfied).toBe(true)
    expect(committed.code).toBe("disable_committed")
  })

  test("controller hands controls to /mcp, records sealed attempts, and disabling binding removes behavior", async () => {
    const store = projectionStore()
    const commands = new RecordingHandoff()
    const sealedAttempts: Array<Record<string, unknown>> = []
    let sealed = false
    const controller = new McpConsoleController({
      projections: store,
      commands,
      online: () => true,
      sealed: () => sealed,
      now: () => new Date(NOW),
      sealedAttempts: {
        record(input) {
          sealedAttempts.push(input)
        },
      },
    })
    const bound = controller.bind(TASK, RUN, SESSION)
    expect(bound?.selectedId).toBe("alpha")
    const receipt = await controller.refreshCatalog("alpha")
    expect(receipt.phase).toBe("queued")
    expect(commands.commands[0]).toBe('/mcp refresh "alpha"')
    expect(controller.getSnapshot().activeOperation?.phase).toBe("queued")

    sealed = true
    controller.setSealed(true)
    await expect(controller.reconnectServer("alpha")).rejects.toThrow("sealed mode")
    expect(sealedAttempts).toHaveLength(1)
    expect(commands.commands).toHaveLength(1)

    controller.setSealed(false)
    sealed = false
    controller.disable("MCP binding removed")
    await expect(controller.refreshCatalog("alpha")).rejects.toThrow("MCP binding removed")
    expect(() => controller.pageCatalog({ kind: "tool" })).toThrow("MCP binding removed")
    controller.enable()
    expect(controller.bind(TASK)?.servers).toHaveLength(1)
    controller.close()
    expect(controller.getSnapshot().closed).toBe(true)
    expect(() => controller.bind(TASK)).toThrow("closed")
    store.close()
  })
})
