import { describe, expect, test } from "bun:test"
import { Writable } from "node:stream"
import type { TaskProjection } from "@zyra/typed-api-client"
import {
  HttpResponseError,
  TransportDisconnectedError,
} from "@zyra/typed-api-client"
import type { CommandTransportRequest } from "@zyra/commands"
import { CliApi, type IngressFrame } from "../src/api.ts"
import { observeTask } from "../src/commands/interactive.ts"
import { resolveSinglePermissionShortcut } from "../src/commands/product.ts"
import {
  CliControlSession,
  formatCommandQueue,
  parseControlIntent,
} from "../src/control/commands.ts"
import { CliPermissionSession, createPermissionProof } from "../src/control/permission.ts"
import { SessionProjection } from "../src/session/projection.ts"

class Capture extends Writable {
  text = ""
  override _write(chunk: Buffer | string, _encoding: BufferEncoding, callback: (error?: Error | null) => void): void {
    this.text += chunk.toString()
    callback()
  }
}

function task(overrides: Partial<TaskProjection> = {}): TaskProjection {
  return {
    taskId: "task_control",
    runId: "run_control",
    sessionId: "session_control",
    rootNodeId: "node_control",
    userGoal: "Exercise FE-S03 controls.",
    status: "running",
    createdAt: "2026-08-04T00:00:00.000Z",
    updatedAt: "2026-08-04T00:00:00.000Z",
    planNodes: [],
    artifacts: [],
    metadata: {},
    binding: { taskId: "task_control", runId: "run_control", sessionId: "session_control" },
    terminal: false,
    active: true,
    ...overrides,
  }
}

function permissionRequest(requestId = "permission-concurrent"): Record<string, unknown> {
  return {
    response_challenge: {
      version: "zyra.permission-response/v2",
      nonce: `nonce-${requestId}`,
      canonical_owner: "typescript.PermissionCoordinator",
    },
    envelope_id: `envelope-${requestId}`,
    request_id: requestId,
    run_id: "run_control",
    task_id: "task_control",
    session_id: "session_control",
    session_revision: 4,
    worker_request_id: `worker-${requestId}`,
    tool_call_id: `tool-${requestId}`,
    request_fingerprint: "c".repeat(64),
    arguments_digest: "d".repeat(64),
    policy_revision: 5,
    mode_revision: 6,
    expires_at: "2099-01-01T00:00:00.000Z",
    status: "delivered",
    prompt: "写入受保护文件",
    reason: "保存用户要求的修改",
    tool_name: "write_file",
    operation: "execute",
    supported_decision_scopes: ["once", "session", "workspace"],
  }
}

function receipt(request: CommandTransportRequest, revision: number, replayed = false): Readonly<Record<string, unknown>> {
  return {
    control_request: {
      request_id: request.requestId,
      command_id: request.commandId,
      canonical_name: request.text.split(/\s+/, 1)[0],
      created_at: "2026-08-04T00:00:00.000Z",
    },
    command_result: {
      request_id: request.requestId,
      command_id: request.commandId,
      name: request.text.split(/\s+/, 1)[0],
      status: "succeeded",
      revision_before: revision,
      revision_after: revision + (request.text.startsWith("/change") ? 1 : 0),
      result: { display_text: "committed", data: {} },
      metadata: { durable: true, executed: true, replayed },
    },
    receipt_replayed: replayed,
  }
}

function eventFrame(sequence: number, eventType: string): IngressFrame {
  return {
    schema: "zyra.event-ingress-frame/v1",
    kind: "event",
    source: "runtime-event-spine",
    generation: 1,
    taskId: "task_control",
    sequence,
    previousSequence: sequence - 1,
    eventId: `event_${sequence}`,
    eventType,
    cursor: `cursor_${sequence}`,
    event: {
      schema: "zyra.runtime-event/v1",
      eventType,
      identity: { taskId: "task_control", runId: "run_control" },
      inline: {},
      artifactRefs: [],
    },
    raw: {},
  }
}

describe("FE-S03 command routing and revision safety", () => {
  test("maps now/next/later and active task shortcuts without creating a local queue", () => {
    expect(parseControlIntent("/now /change stop the current branch")).toMatchObject({ mode: "interrupt", priority: "now" })
    expect(parseControlIntent("/next /change use the verified branch")).toMatchObject({ mode: "enqueue", priority: "next" })
    expect(parseControlIntent("/later /verify")).toMatchObject({ mode: "enqueue", priority: "later" })
    expect(parseControlIntent("redirect this task")).toMatchObject({ kind: "submit", text: "/change redirect this task" })
    expect(parseControlIntent("/cancel reason")).toEqual({ kind: "task-cancel", reason: "reason" })
    expect(parseControlIntent("/retry request_1")).toEqual({ kind: "command-retry", requestId: "request_1" })
    expect(parseControlIntent("/compact preserve verification context")).toMatchObject({
      kind: "submit",
      text: "/compact preserve verification context",
      mode: "enqueue",
      priority: "next",
    })
  })

  test("probes canonical revision before a mutation and surfaces stale conflicts without auto retry", async () => {
    const submitted: CommandTransportRequest[] = []
    let conflict = false
    const api = {
      async submitControlCommand(request: CommandTransportRequest) {
        submitted.push(request)
        if (conflict && request.text.startsWith("/change")) {
          throw new HttpResponseError(409, "session revision changed", {
            code: "command_conflict",
            body: {
              command_result: {
                error: { code: "revision_conflict", details: { expected: request.expectedRevision, actual: 8 } },
              },
            },
          })
        }
        return receipt(request, 7)
      },
    } as unknown as CliApi
    const controls = new CliControlSession({ api, task: task() })
    const accepted = await controls.submit("/change preserve the verified route", { mode: "enqueue" })
    expect(submitted.map((item) => item.text)).toEqual(["/status", "/change preserve the verified route"])
    expect(submitted[1]?.expectedRevision).toBe(7)
    expect(accepted.revisionAfter).toBe(8)

    conflict = true
    await expect(controls.submit("/change overwrite stale state", { mode: "enqueue" })).rejects.toMatchObject({
      code: "command_revision_conflict",
      details: { expected: 8, actual: 8, automatic_retry: false },
    })
    expect(submitted.filter((item) => item.text.includes("overwrite stale state"))).toHaveLength(1)
  })

  test("reconciles an ambiguous mutation through its durable receipt without replaying it", async () => {
    const submitted: CommandTransportRequest[] = []
    const reconciled: CommandTransportRequest[] = []
    const api = {
      async submitControlCommand(request: CommandTransportRequest) {
        submitted.push(request)
        throw new TransportDisconnectedError(
          "connection closed after command dispatch",
        )
      },
      async controlCommandReceipt(request: CommandTransportRequest) {
        reconciled.push(request)
        return receipt(request, 7, true)
      },
    } as unknown as CliApi
    const controls = new CliControlSession({ api, task: task(), revision: 7 })

    const accepted = await controls.submit(
      "/change preserve the committed result",
      { mode: "enqueue" },
    )

    expect(submitted).toHaveLength(1)
    expect(reconciled).toHaveLength(1)
    expect(reconciled[0]?.requestId).toBe(submitted[0]?.requestId)
    expect(reconciled[0]?.idempotencyKey).toBe(submitted[0]?.idempotencyKey)
    expect(accepted).toMatchObject({
      phase: "applied",
      replayed: true,
      revisionBefore: 7,
      revisionAfter: 8,
    })
    expect(controls.revision).toBe(8)
  })

  test("reports an actionable unknown outcome when the receipt cannot be queried", async () => {
    let mutationCount = 0
    const api = {
      async submitControlCommand() {
        mutationCount += 1
        throw new TransportDisconnectedError("connection closed")
      },
      async controlCommandReceipt() {
        throw new Error("receipt endpoint is unavailable")
      },
    } as unknown as CliApi
    const controls = new CliControlSession({ api, task: task(), revision: 7 })

    await expect(controls.submit("/change do not duplicate me", {
      mode: "enqueue",
    })).rejects.toMatchObject({
      code: "command_outcome_unknown",
      details: {
        automatic_retry: false,
        mutation_replayed: false,
        transport_category: "disconnect",
      },
    })
    expect(mutationCount).toBe(1)
  })

  test("reconciles task cancel, task continue, and command cancel from canonical reads", async () => {
    let taskReads = 0
    const api = {
      async cancelTask() {
        throw new TransportDisconnectedError("cancel response lost")
      },
      async runTask() {
        throw new TransportDisconnectedError("continue response lost")
      },
      async task() {
        taskReads += 1
        if (taskReads === 1) {
          return task({
            status: "cancelled",
            terminal: true,
            active: false,
          })
        }
        if (taskReads === 2) {
          return task({ status: "paused", active: false })
        }
        return task({ status: "running", active: true })
      },
      async cancelControlCommand() {
        throw new TransportDisconnectedError("command cancel response lost")
      },
      async commandQueue() {
        return {
          schema: "zyra.command-queue/v1",
          taskId: "task_control",
          sessionId: "session_control",
          sequence: 4,
          revision: 2,
          restored: true,
          items: [{
            queueId: "queue_cancelled",
            requestId: "request_cancelled",
            commandId: "command_cancelled",
            sessionId: "session_control",
            phase: "cancelled",
            priority: "next",
            sequence: 4,
            position: 0,
            createdAt: "2026-08-04T00:00:00.000Z",
            updatedAt: "2026-08-04T00:00:01.000Z",
            cancellable: false,
            retryable: true,
            terminal: true,
            source: "backend-queue",
          }],
          pending: [],
          running: [],
          settled: [],
        }
      },
    } as unknown as CliApi

    const cancelControls = new CliControlSession({ api, task: task() })
    expect(await cancelControls.cancelTask("cancel safely")).toMatchObject({
      status: "cancelled",
      terminal: true,
    })

    const continueControls = new CliControlSession({
      api,
      task: task({ status: "paused", active: false }),
    })
    expect(await continueControls.continueTask()).toMatchObject({
      status: "running",
      active: true,
    })

    expect(await continueControls.cancelCommand("request_cancelled")).toMatchObject({
      status: "cancelled",
      reconciled: true,
      mutation_replayed: false,
    })
  })

  test("renders only the canonical queue order supplied by the server projection", () => {
    const rendered = formatCommandQueue({
      schema: "zyra.command-queue/v1",
      taskId: "task_control",
      sessionId: "session_control",
      sequence: 12,
      revision: 0,
      restored: true,
      items: [
        { queueId: "q1", requestId: "r1", sessionId: "session_control", phase: "queued", priority: "now", sequence: 3, position: 0, createdAt: "2026-08-04T00:00:00Z", updatedAt: "2026-08-04T00:00:00Z", cancellable: true, retryable: false, terminal: false, source: "backend-queue", text: "/change now" },
        { queueId: "q2", requestId: "r2", sessionId: "session_control", phase: "queued", priority: "later", sequence: 4, position: 1, createdAt: "2026-08-04T00:00:00Z", updatedAt: "2026-08-04T00:00:00Z", cancellable: true, retryable: false, terminal: false, source: "backend-queue", text: "/verify" },
      ],
      pending: [],
      running: [],
      settled: [],
    })
    expect(rendered.indexOf("r1")).toBeLessThan(rendered.indexOf("r2"))
    expect(rendered).toContain("canonical owner: PromptQueueRuntime")
  })
})

describe("FE-S03 permission proof and projection", () => {
  test("binds allow/deny proof to every canonical challenge field", () => {
    const proof = createPermissionProof({
      response_challenge: { version: "zyra.permission-response/v1", nonce: "nonce", canonical_owner: "typescript.PermissionCoordinator" },
      envelope_id: "env",
      request_id: "req",
      run_id: "run",
      task_id: "task",
      session_id: "session",
      session_revision: 1,
      worker_request_id: "worker",
      tool_call_id: "tool",
      request_fingerprint: "a".repeat(64),
      arguments_digest: "b".repeat(64),
      policy_revision: 2,
      mode_revision: 3,
      expires_at: "2099-01-01T00:00:00.000Z",
    }, { responseId: "resp", effect: "deny", now: new Date("2026-08-04T00:00:00.000Z") })
    expect(proof.proof).toBe("f864029f7d13bbd21267e578f76d9eb07fce0a1dffb38c675baa7d500993e3af")
    expect(proof).toMatchObject({ request_id: "req", response_id: "resp", effect: "deny", session_revision: 1 })
  })

  test("deduplicates permission pending state by request identity and removes exact decisions", () => {
    const projection = new SessionProjection({ taskId: "task_control", generation: 1 })
    const pending = eventFrame(1, "runtime.permission.pending")
    pending.event = {
      ...pending.event,
      inline: { request_id: "permission_1", reason: "external write", risk_level: "high", expires_at: "2099-01-01T00:00:00Z" },
    }
    projection.apply(pending)
    expect(projection.apply({ ...pending })).toBeUndefined()
    expect(projection.snapshot().pendingPermissionRequests).toMatchObject([{ requestId: "permission_1", risk: "high" }])
    const denied = eventFrame(2, "runtime.permission.denied")
    denied.event = { ...denied.event, inline: { request_id: "permission_1" } }
    projection.apply(denied)
    expect(projection.snapshot().pendingPermissions).toBe(0)
  })

  test("advertises only backend scopes and binds a workspace decision into the submitted proof", async () => {
    const request = {
      response_challenge: {
        version: "zyra.permission-response/v2",
        nonce: "scope-nonce",
        canonical_owner: "typescript.PermissionCoordinator",
      },
      envelope_id: "scope-env",
      request_id: "scope-request",
      run_id: "run_control",
      task_id: "task_control",
      session_id: "session_control",
      session_revision: 4,
      worker_request_id: "scope-worker",
      tool_call_id: "scope-tool-call",
      request_fingerprint: "c".repeat(64),
      arguments_digest: "d".repeat(64),
      policy_revision: 5,
      mode_revision: 6,
      expires_at: "2099-01-01T00:00:00.000Z",
      status: "delivered",
      tool_name: "open_url",
      operation: "execute",
      supported_decision_scopes: ["once", "session", "workspace"],
    }
    let submitted: Record<string, unknown> | undefined
    const api = {
      async openPermissionSession() {
        return {
          taskId: "task_control",
          runId: "run_control",
          sessionId: "session_control",
          custodyToken: "custody-token",
          created: true,
          verified: true,
        }
      },
      async permissionRequests() {
        return { requests: { items: [request] } }
      },
      async resolvePermission(input: Record<string, unknown>) {
        submitted = input
        return {
          receipt: { accepted: true, effect: "allow", decision_scope: "workspace", request_id: "scope-request" },
          scope_rule: { installed: true, persistent: true },
        }
      },
    } as unknown as CliApi
    const session = new CliPermissionSession({ api, task: task() })
    expect(await session.open()).toBe(true)
    expect(session.custodyToken).toBe("custody-token")
    const pending = await session.pending()
    expect(pending[0]?.supportedDecisionScopes).toEqual(["once", "session", "workspace"])
    await session.resolve({
      requestId: "scope-request",
      effect: "allow",
      decisionScope: "workspace",
    })
    expect(submitted).toMatchObject({ effect: "allow", decisionScope: "workspace" })
    expect(submitted?.consoleResponse).toMatchObject({
      request_id: "scope-request",
      effect: "allow",
      decision_scope: "workspace",
    })
  })

  test("binds the A/D shortcut to one exact pending request and rejects ambiguity", async () => {
    let resolved: Record<string, unknown> | undefined
    let refreshes = 0
    const request = permissionRequest("permission-shortcut")
    const api = {
      async openPermissionSession() {
        return { taskId: "task_control", runId: "run_control", sessionId: "session_control", custodyToken: "custody-token", created: true, verified: true }
      },
      async permissionRequests() { return { requests: { items: [request] } } },
      async resolvePermission(input: Record<string, unknown>) {
        resolved = input
        return { receipt: { accepted: true, effect: "allow", decision_scope: "once", request_id: "permission-shortcut" } }
      },
    } as unknown as CliApi
    const session = new CliPermissionSession({ api, task: task() })
    expect(await session.open()).toBe(true)
    expect(await resolveSinglePermissionShortcut({
      permissions: session,
      signal: new AbortController().signal,
      refreshPermissions: async () => { refreshes += 1 },
    }, "allow")).toBe("权限已允许 · 仅本次")
    expect(resolved).toMatchObject({ requestId: "permission-shortcut", effect: "allow", decisionScope: "once" })
    expect(refreshes).toBe(1)

    const ambiguousApi = {
      ...api,
      async permissionRequests() {
        return { requests: { items: [permissionRequest("permission-a"), permissionRequest("permission-b")] } }
      },
    } as unknown as CliApi
    const ambiguous = new CliPermissionSession({ api: ambiguousApi, task: task() })
    expect(await ambiguous.open()).toBe(true)
    await expect(resolveSinglePermissionShortcut({
      permissions: ambiguous,
      signal: new AbortController().signal,
      refreshPermissions: async () => undefined,
    }, "deny")).rejects.toMatchObject({ code: "permission_shortcut_ambiguous" })
  })

  test("allows exactly one winner for concurrent opposite decisions without replay", async () => {
    const request = permissionRequest()
    let reads = 0
    let releaseReads: (() => void) | undefined
    const bothRead = new Promise<void>((resolve) => { releaseReads = resolve })
    let acceptedEffect: "allow" | "deny" | undefined
    let mutations = 0
    const api = {
      async openPermissionSession() {
        return { taskId: "task_control", runId: "run_control", sessionId: "session_control", custodyToken: "custody-token", created: true, verified: true }
      },
      async permissionRequests() {
        reads += 1
        if (reads === 2) releaseReads?.()
        await bothRead
        return { requests: { items: [request] } }
      },
      async resolvePermission(input: Record<string, unknown>) {
        mutations += 1
        acceptedEffect ??= input.effect as "allow" | "deny"
        return { receipt: { accepted: true, effect: acceptedEffect, decision_scope: "once", request_id: "permission-concurrent" } }
      },
    } as unknown as CliApi
    const first = new CliPermissionSession({ api, task: task() })
    const second = new CliPermissionSession({ api, task: task() })
    expect(await first.open()).toBe(true)
    expect(await second.open()).toBe(true)
    const outcomes = await Promise.allSettled([
      first.resolve({ requestId: "permission-concurrent", effect: "allow" }),
      second.resolve({ requestId: "permission-concurrent", effect: "deny" }),
    ])
    expect(outcomes.filter((item) => item.status === "fulfilled")).toHaveLength(1)
    expect(outcomes.filter((item) => item.status === "rejected")).toHaveLength(1)
    expect(outcomes.find((item) => item.status === "rejected")).toMatchObject({ reason: { code: "permission_decision_conflict" } })
    expect(mutations).toBe(2)
  })

  test("updates canonical permission mode with revision fencing and reconciles a lost acknowledgement", async () => {
    let state = {
      mode: "default",
      revision: 0,
      interactive: true,
      headless: false,
      sealedAutonomous: false,
      bypassAvailable: false,
      autoClassifierEnabled: false,
      changedAt: "2026-08-04T00:00:00.000Z",
      changedBy: "bootstrap",
      reason: "initial permission mode",
    }
    let loseAcknowledgement = false
    const updates: Array<Record<string, unknown>> = []
    const api = {
      async openPermissionSession() {
        return {
          taskId: "task_control",
          runId: "run_control",
          sessionId: "session_control",
          custodyToken: "custody-token",
          created: true,
          verified: true,
        }
      },
      async permissionMode() {
        return { mode: { ...state } }
      },
      async updatePermissionMode(input: Record<string, unknown>) {
        updates.push(input)
        const before = state.revision
        const next = String(input.mode)
        state = {
          ...state,
          mode: next,
          revision: before + 1,
          changedBy: "zyra-cli",
          reason: String(input.reason),
        }
        if (loseAcknowledgement) throw new TransportDisconnectedError("mode response lost")
        return {
          receipt: {
            transition: {
              from: "default",
              to: next,
              revisionBefore: before,
              revisionAfter: state.revision,
              changed: true,
            },
          },
        }
      },
    } as unknown as CliApi
    const session = new CliPermissionSession({ api, task: task() })
    expect(await session.open()).toBe(true)
    expect(await session.mode()).toMatchObject({ mode: "default", revision: 0 })
    const updated = await session.setMode({ mode: "acceptEdits", expectedRevision: 0 })
    expect(updated).toMatchObject({ reconciled: false, revisionBefore: 0, revisionAfter: 1 })
    expect(updated.state).toMatchObject({ mode: "acceptEdits", revision: 1 })

    loseAcknowledgement = true
    const reconciled = await session.setMode({ mode: "plan", expectedRevision: 1 })
    expect(reconciled).toMatchObject({ reconciled: true, revisionBefore: 1, revisionAfter: 2 })
    expect(reconciled.state).toMatchObject({ mode: "plan", revision: 2 })
    expect(updates).toHaveLength(2)
  })

  test("does not retry a conflicting permission mode mutation", async () => {
    let updates = 0
    const api = {
      async openPermissionSession() {
        return {
          taskId: "task_control",
          runId: "run_control",
          sessionId: "session_control",
          custodyToken: "custody-token",
          created: true,
          verified: true,
        }
      },
      async updatePermissionMode() {
        updates += 1
        throw new HttpResponseError(409, "permission mode revision conflict")
      },
      async permissionMode() {
        return {
          mode: {
            mode: "dontAsk",
            revision: 4,
            interactive: true,
            headless: false,
            sealedAutonomous: false,
            bypassAvailable: false,
            autoClassifierEnabled: false,
          },
        }
      },
    } as unknown as CliApi
    const session = new CliPermissionSession({ api, task: task() })
    expect(await session.open()).toBe(true)
    await expect(session.setMode({ mode: "plan", expectedRevision: 3 })).rejects.toMatchObject({
      code: "permission_mode_conflict",
      details: {
        requested_mode: "plan",
        actual_mode: "dontAsk",
        actual_revision: 4,
        automatic_retry: false,
      },
    })
    expect(updates).toBe(1)
  })

  test("rejects expired requests and fails closed when permission custody is disabled", async () => {
    expect(() => createPermissionProof({
      response_challenge: { version: "zyra.permission-response/v1", nonce: "nonce", canonical_owner: "typescript.PermissionCoordinator" },
      envelope_id: "env",
      request_id: "req",
      run_id: "run",
      task_id: "task",
      session_id: "session",
      session_revision: 1,
      worker_request_id: "worker",
      tool_call_id: "tool",
      request_fingerprint: "a".repeat(64),
      arguments_digest: "b".repeat(64),
      policy_revision: 2,
      mode_revision: 3,
      expires_at: "2026-08-03T00:00:00.000Z",
    }, { responseId: "resp", effect: "allow", now: new Date("2026-08-04T00:00:00.000Z") }))
      .toThrow("expired")

    const api = {
      async openPermissionSession() {
        throw new Error("permission adapter disabled")
      },
    } as unknown as CliApi
    const session = new CliPermissionSession({ api, task: task() })
    expect(await session.open()).toBe(false)
    await expect(session.pending()).rejects.toMatchObject({
      code: "permission_custody_unavailable",
    })
  })
})

describe("FE-S03 cursor recovery", () => {
  test("reopens SSE from the last server cursor after disconnect and preserves the projection", async () => {
    const output = new Capture()
    let streamCalls = 0
    let snapshotCalls = 0
    const api = {
      async ingressCapabilities() {
        return { taskId: "task_control", generation: 1, subscriptionCursor: "cursor_0", subscriptionSequence: 0, sseAvailable: true, raw: {} }
      },
      async *snapshotIngress() {
        snapshotCalls += 1
        yield { cursor: "cursor_0", generation: 1, frames: [], hasMore: false, caughtUp: true, nextSequence: 0 }
      },
      async runTask() { return { task: task({ status: "completed", terminal: true, active: false }), events: [], receipt: {}, controls: {}, raw: {} } },
      async *streamIngress() {
        streamCalls += 1
        if (streamCalls === 1) {
          yield { kind: "event", taskId: "task_control", generation: 1, sequence: 1, frame: eventFrame(1, "runtime.text.delta") }
          return
        }
        yield { kind: "event", taskId: "task_control", generation: 1, sequence: 2, frame: eventFrame(2, "runtime.task.completed") }
        yield { kind: "close", taskId: "task_control", generation: 1, sequence: 2, cursor: "cursor_2" }
      },
      async task() { return task({ status: "completed", terminal: true, active: false }) },
    } as unknown as CliApi
    const result = await observeTask({
      api,
      task: task({ status: "pending", active: false }),
      cwd: "G:\\agent-zoo",
      output,
      signal: new AbortController().signal,
      resume: false,
    })
    expect(result.exitCode).toBe(0)
    expect(streamCalls).toBe(2)
    expect(snapshotCalls).toBe(1)
    expect(output.text).toContain("cursor resume")
    expect(output.text.match(/000001/g)).toHaveLength(1)
    expect(output.text).toContain("000002")
  })

  test("replaces from a canonical snapshot on a gap without duplicating rendered events", async () => {
    const output = new Capture()
    let streamCalls = 0
    let snapshotCalls = 0
    const api = {
      async ingressCapabilities() {
        return { taskId: "task_control", generation: 1, subscriptionCursor: "cursor_0", subscriptionSequence: 0, sseAvailable: true, raw: {} }
      },
      async *snapshotIngress() {
        snapshotCalls += 1
        const frames = snapshotCalls === 1
          ? []
          : [eventFrame(1, "runtime.text.delta"), eventFrame(2, "runtime.tool.succeeded")]
        yield { cursor: snapshotCalls === 1 ? "cursor_0" : "cursor_2", generation: 1, frames, hasMore: false, caughtUp: true, nextSequence: frames.length }
      },
      async runTask() { return { task: task({ status: "completed", terminal: true, active: false }), events: [], receipt: {}, controls: {}, raw: {} } },
      async *streamIngress() {
        streamCalls += 1
        if (streamCalls === 1) {
          yield { kind: "event", taskId: "task_control", generation: 1, sequence: 1, frame: eventFrame(1, "runtime.text.delta") }
          yield { kind: "event", taskId: "task_control", generation: 1, sequence: 3, frame: eventFrame(3, "runtime.task.completed") }
          return
        }
        yield { kind: "event", taskId: "task_control", generation: 1, sequence: 3, frame: eventFrame(3, "runtime.task.completed") }
        yield { kind: "close", taskId: "task_control", generation: 1, sequence: 3, cursor: "cursor_3" }
      },
      async task() { return task({ status: "completed", terminal: true, active: false }) },
    } as unknown as CliApi
    const result = await observeTask({
      api,
      task: task({ status: "pending", active: false }),
      cwd: "G:\\agent-zoo",
      output,
      signal: new AbortController().signal,
      resume: false,
    })
    expect(result.exitCode).toBe(0)
    expect(snapshotCalls).toBe(2)
    expect(streamCalls).toBe(2)
    expect(output.text).toContain("snapshot replacement")
    expect(output.text.match(/000001/g)).toHaveLength(1)
    expect(output.text.match(/000002/g)).toHaveLength(1)
    expect(output.text.match(/000003/g)).toHaveLength(1)
  })
})
