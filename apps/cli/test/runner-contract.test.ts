import { describe, expect, test } from "bun:test"
import { Readable, Writable } from "node:stream"
import type {
  TaskMutationProjection,
  TaskProjection,
} from "@zyra/typed-api-client"
import { ApiVersionMismatchError, RequestCancelledError } from "@zyra/typed-api-client"
import { CliApi } from "../src/api.ts"
import { CliExitCode } from "../src/contracts.ts"
import { CliOutput } from "../src/output.ts"
import { classifyTaskOutcome, executeRun, taskHasSettledRunResult } from "../src/runner.ts"

class Capture extends Writable {
  text = ""
  override _write(chunk: Buffer | string, _encoding: BufferEncoding, callback: (error?: Error | null) => void): void {
    this.text += chunk.toString()
    callback()
  }
}

function task(status: string): TaskProjection {
  return {
    taskId: "task_contract_001",
    runId: "run_contract_001",
    rootNodeId: "node_root_001",
    userGoal: "Verify a real result.",
    status,
    createdAt: "2026-08-04T00:00:00.000Z",
    updatedAt: "2026-08-04T00:00:01.000Z",
    planNodes: [],
    artifacts: [],
    metadata: {
      final_answer: "verified",
      delivery: {
        schema: "zyra.task-workspace-delivery/v1",
        workspace_id: "ws_contract_001",
        created_paths: ["smoke.txt"],
        modified_paths: [],
        deleted_paths: [],
        changed_paths: ["smoke.txt"],
        physical_location_redacted: true,
      },
    },
    binding: { taskId: "task_contract_001", runId: "run_contract_001" },
    terminal: status === "completed" || status === "failed" || status === "cancelled",
    active: status === "pending" || status === "running" || status === "blocked",
  }
}

function mutation(selected: TaskProjection): TaskMutationProjection {
  return {
    task: selected,
    events: [],
    receipt: { receipt_id: `receipt_${selected.status}` },
    controls: {},
    raw: {},
  }
}

describe("FE-S01 run result and fail-closed contracts", () => {
  test("distinguishes success, execution failure, cancellation, and verifier failure", () => {
    const passed = {
      final: { schema: "zyra.production-independent-final-verifier/v2", passed: true },
      gate: { schema: "zyra.production-adaptive-depth-completion-gate/v1", hard_conditions_passed: true },
    }
    expect(classifyTaskOutcome(task("completed"), passed).exitCode).toBe(CliExitCode.SUCCESS)
    expect(classifyTaskOutcome(task("failed"), passed).exitCode).toBe(CliExitCode.TASK_FAILED)
    expect(classifyTaskOutcome(task("cancelled"), passed).exitCode).toBe(CliExitCode.CANCELLED)
    expect(classifyTaskOutcome(task("completed"), { ...passed, final: { passed: false } }).exitCode).toBe(CliExitCode.VERIFIER_FAILED)
    expect(classifyTaskOutcome(task("completed"), {}).status).toBe("verifier_evidence_missing")
    expect(classifyTaskOutcome(task("completed"), passed).result?.workspace_delivery).toEqual({
      schema: "zyra.task-workspace-delivery/v1",
      workspace_id: "ws_contract_001",
      created_paths: ["smoke.txt"],
      modified_paths: [],
      deleted_paths: [],
      changed_paths: ["smoke.txt"],
      file_api_resource: "workspaces/ws_contract_001/files",
    })
  })

  test("treats verifier-backed blocked state as settled for the current run only", () => {
    expect(taskHasSettledRunResult(task("blocked"), {})).toBe(false)
    expect(taskHasSettledRunResult(task("blocked"), { finalPassed: false })).toBe(true)
    expect(taskHasSettledRunResult(task("blocked"), { finalPassed: true })).toBe(false)
    expect(taskHasSettledRunResult(task("blocked"), {
      finalPassed: true,
      completionGatePresent: true,
    })).toBe(true)
    expect(taskHasSettledRunResult(task("running"), { finalPassed: false })).toBe(false)
    expect(taskHasSettledRunResult(task("completed"), {})).toBe(true)
  })

  test("returns a verifier-backed blocked result without waiting for stuck mutation transport", async () => {
    const pending = task("pending")
    const blocked = task("blocked")
    const fake = {
      async createPendingTask() { return mutation(pending) },
      async openIngress() {
        return { cursor: "opaque.cursor", generation: 1, frames: [], hasMore: false, caughtUp: true, nextSequence: 0 }
      },
      async runTask(_task: TaskProjection, signal?: AbortSignal) {
        return await new Promise<TaskMutationProjection>((_resolve, reject) => {
          signal?.addEventListener(
            "abort",
            () => reject(new RequestCancelledError("mutation response transport remained open")),
            { once: true },
          )
        })
      },
      async nextIngress() {
        return {
          cursor: "opaque.cursor.final",
          generation: 1,
          frames: [{
            schema: "zyra.event-ingress-frame/v1",
            kind: "event",
            source: "runtime-event-spine",
            generation: 1,
            taskId: blocked.taskId,
            sequence: 1,
            previousSequence: 0,
            eventId: "event_blocked_final_verifier",
            eventType: "runtime.audit.finding",
            cursor: "opaque.cursor.final",
            event: {
              runId: blocked.runId,
              inline: {
                schema: "zyra.production-independent-final-verifier/v2",
                passed: false,
              },
            },
            raw: {},
          }],
          hasMore: false,
          caughtUp: true,
          nextSequence: 2,
        }
      },
      async task() { return blocked },
      async events() { return [] },
      async cancelTask() { return mutation(task("cancelled")) },
    } as unknown as CliApi
    const stdout = new Capture()
    const outcome = await executeRun({
      command: {
        kind: "run",
        goal: "Return the settled verifier result.",
        baseUrl: "http://127.0.0.1:8000",
        autoStart: false,
        startupTimeoutMs: 1_000,
        timeoutMs: 10_000,
        sealed: true,
      },
      api: fake,
      output: new CliOutput({
        stdout,
        stderr: new Capture(),
        requestId: "request_contract_blocked_settlement",
        command: "run",
      }),
      stdin: Readable.from([]),
      signal: new AbortController().signal,
    })

    expect(outcome.exitCode).toBe(CliExitCode.VERIFIER_FAILED)
    expect(outcome.status).toBe("verifier_failed")
    expect(stdout.text).toContain("canonical_task_result_settled_before_mutation_transport")
  })

  test("does not infer success after event ingress disconnect and missing verifier", async () => {
    const pending = task("pending")
    const completed = task("completed")
    const fake = {
      async createPendingTask() { return mutation(pending) },
      async openIngress() {
        return { cursor: "opaque.cursor", generation: 1, frames: [], hasMore: false, caughtUp: true, nextSequence: 0 }
      },
      async runTask() { return mutation(completed) },
      async nextIngress() { throw new Error("stream disconnected") },
      async task() { return completed },
      async events() { return [] },
      async cancelTask() { return mutation(task("cancelled")) },
    } as unknown as CliApi
    const stdout = new Capture()
    const stderr = new Capture()
    const output = new CliOutput({
      stdout,
      stderr,
      requestId: "request_contract_001",
      command: "run",
    })
    const outcome = await executeRun({
      command: {
        kind: "run",
        goal: "Verify a real result.",
        baseUrl: "http://127.0.0.1:8000",
        autoStart: false,
        startupTimeoutMs: 1_000,
        timeoutMs: 10_000,
        sealed: false,
      },
      api: fake,
      output,
      stdin: Readable.from([]),
      signal: new AbortController().signal,
    })
    expect(outcome.exitCode).toBe(CliExitCode.VERIFIER_FAILED)
    expect(outcome.status).toBe("verifier_evidence_missing")
    for (const line of stdout.text.trim().split("\n")) expect(() => JSON.parse(line)).not.toThrow()
  })

  test("reconciles a detached long-running mutation until the canonical task is terminal", async () => {
    const pending = task("pending")
    const completed = task("completed")
    let taskReads = 0
    const fake = {
      async createPendingTask() { return mutation(pending) },
      async openIngress() {
        return { cursor: "opaque.cursor", generation: 1, frames: [], hasMore: false, caughtUp: true, nextSequence: 0 }
      },
      async runTask() {
        throw new RequestCancelledError("HTTP response headers detached while the mutation remained in flight")
      },
      async nextIngress() {
        return { cursor: "opaque.cursor.next", generation: 1, frames: [], hasMore: false, caughtUp: true, nextSequence: 0 }
      },
      async task() {
        taskReads += 1
        return taskReads < 2 ? pending : completed
      },
      async events() {
        return [
          {
            eventId: "event_final_verifier",
            eventType: "final_verifier",
            taskId: completed.taskId,
            runId: completed.runId,
            createdAt: "2026-08-04T00:00:02.000Z",
            payload: { schema: "zyra.production-independent-final-verifier/v2", passed: true },
          },
          {
            eventId: "event_completion_gate",
            eventType: "completion_gate",
            taskId: completed.taskId,
            runId: completed.runId,
            createdAt: "2026-08-04T00:00:03.000Z",
            payload: { schema: "zyra.production-adaptive-depth-completion-gate/v1", hard_conditions_passed: true },
          },
        ]
      },
      async cancelTask() { return mutation(task("cancelled")) },
    } as unknown as CliApi
    const stdout = new Capture()
    const output = new CliOutput({
      stdout,
      stderr: new Capture(),
      requestId: "request_contract_reconcile",
      command: "run",
    })

    const outcome = await executeRun({
      command: {
        kind: "run",
        goal: "Verify a real long-running result.",
        baseUrl: "http://127.0.0.1:8000",
        autoStart: false,
        startupTimeoutMs: 1_000,
        timeoutMs: 10_000,
        sealed: false,
      },
      api: fake,
      output,
      stdin: Readable.from([]),
      signal: new AbortController().signal,
    })

    expect(taskReads).toBe(3)
    expect(outcome.exitCode).toBe(CliExitCode.SUCCESS)
    expect(outcome.status).toBe("completed")
    expect(stdout.text).toContain("zyra.cli-run-reconciliation.v1")
    expect(stdout.text).toContain('"phase":"terminal"')
  })

  test("does not wait for a stuck ingress poll after the run request settles", async () => {
    const pending = task("pending")
    const blocked = task("blocked")
    let ingressCalls = 0
    const fake = {
      async createPendingTask() { return mutation(pending) },
      async openIngress() {
        return { cursor: "opaque.cursor", generation: 1, frames: [], hasMore: false, caughtUp: true, nextSequence: 0 }
      },
      async runTask() { throw new Error("structured task execution failure") },
      async nextIngress() {
        ingressCalls += 1
        if (ingressCalls === 1) return new Promise(() => undefined)
        return { cursor: "opaque.cursor.next", generation: 1, frames: [], hasMore: false, caughtUp: true, nextSequence: 0 }
      },
      async task() { return blocked },
      async events() { return [] },
      async cancelTask() { return mutation(task("cancelled")) },
    } as unknown as CliApi
    const output = new CliOutput({
      stdout: new Capture(),
      stderr: new Capture(),
      requestId: "request_contract_stuck_ingress",
      command: "run",
    })

    await expect(executeRun({
      command: {
        kind: "run",
        goal: "Verify a real result.",
        baseUrl: "http://127.0.0.1:8000",
        autoStart: false,
        startupTimeoutMs: 1_000,
        timeoutMs: 10_000,
        sealed: false,
      },
      api: fake,
      output,
      stdin: Readable.from([]),
      signal: new AbortController().signal,
    })).rejects.toThrow("structured task execution failure")
    expect(ingressCalls).toBeGreaterThanOrEqual(2)
  })

  test("unknown event ingress schema fails closed", async () => {
    const fetchMock = (async (_input: URL | RequestInfo, init?: RequestInit) => {
      const requestId = new Headers(init?.headers).get("X-Request-Id")!
      return new Response(JSON.stringify({
        schema: "zyra.event-ingress-capabilities/v2",
        protocol: "zyra.event-ingress/v1",
        taskId: "task_contract_001",
      }), {
        status: 200,
        headers: {
          "Content-Type": "application/json",
          "X-Zyra-Api-Version": "1.0",
          "X-Request-Id": requestId,
        },
      })
    }) as typeof fetch
    const api = new CliApi({
      baseUrl: "http://127.0.0.1:8000",
      timeoutMs: 5_000,
      fetch: fetchMock,
    })
    try {
      await expect(api.openIngress("task_contract_001")).rejects.toMatchObject({
        code: "contract_schema_unknown",
      })
    } finally {
      api.close()
    }
  })

  test("unknown API versions fail closed and disabled transport has no fallback", async () => {
    const fetchMock = (async (_input: URL | RequestInfo, init?: RequestInit) => {
      const requestId = new Headers(init?.headers).get("X-Request-Id")!
      return new Response(JSON.stringify({
        schema: "zyra.scenario-registry/v1",
        revision: 1,
        definitions: [],
        profiles: [],
        policies: [],
      }), {
        status: 200,
        headers: {
          "Content-Type": "application/json",
          "X-Zyra-Api-Version": "2.0",
          "X-Request-Id": requestId,
        },
      })
    }) as typeof fetch
    const incompatible = new CliApi({
      baseUrl: "http://127.0.0.1:8000",
      timeoutMs: 5_000,
      fetch: fetchMock,
    })
    try {
      await expect(incompatible.scenarioRegistry()).rejects.toBeInstanceOf(ApiVersionMismatchError)
    } finally {
      incompatible.close()
    }

    const disabled = new CliApi({
      baseUrl: "http://127.0.0.1:8000",
      timeoutMs: 5_000,
      fetch: fetchMock,
    })
    disabled.client.disableTransport("FE-S01 disable evidence")
    try {
      await expect(disabled.scenarioRegistry()).rejects.toBeTruthy()
    } finally {
      disabled.close()
    }
  })
})
