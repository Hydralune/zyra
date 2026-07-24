import { describe, expect, test } from "bun:test"
import {
  RecoveryControlAction,
  RecoveryControlError,
  RecoveryControlRuntime,
  assessSealedControlReceipt,
  buildRecoveryControlCommand,
  normalizeRecoveryControlRequest,
  type RecoveryControlOwnerExpectation,
  type RecoveryControlRequest,
  type RecoveryControlTransport,
  type RecoveryControlTransportInput,
} from "../src/features/timeline/control/index.ts"
import { recoveryControlOwnerExpectation } from "../src/features/timeline/view/recovery-control-panel.tsx"
import type { WorkerCausalTimelineProjection } from "../src/features/timeline/projection/index.ts"
import type { TaskProjection } from "../../../packages/core/typed-api-client/src/index.ts"

const TASK = "task_timeline_control"
const RUN = "run_timeline_control"
const OWNER: RecoveryControlOwnerExpectation = Object.freeze({
  taskId: TASK,
  runId: RUN,
  sessionId: "session_timeline_control",
  workerId: "worker_timeline_control",
  leaseId: "lease_timeline_control",
  attemptId: "attempt_timeline_control",
  nodeId: "node_timeline_control",
  graphId: "graph_timeline_control",
  checkpointId: "checkpoint_timeline_control",
  ownerRevision: 11,
  graphRevision: 7,
  sessionRevision: 5,
})

function request(
  action: RecoveryControlRequest["action"],
  overrides: Partial<RecoveryControlRequest> = {},
): RecoveryControlRequest {
  return {
    action,
    taskId: TASK,
    runId: RUN,
    actorId: "timeline-operator",
    reason: `Exercise ${action} from the timeline.`,
    instruction:
      action === RecoveryControlAction.STEER
        ? "Require a verifier before delivery."
        : undefined,
    sealed: false,
    timeoutMs: 300,
    maximumAttempts: action === RecoveryControlAction.RETRY ? 1 : undefined,
    owner: OWNER,
    ...overrides,
  }
}

function response(
  input: RecoveryControlTransportInput,
  options: {
    denied?: boolean
    owner?: string
    changed?: boolean
    action?: string
  } = {},
): Readonly<Record<string, unknown>> {
  const denied = options.denied ?? false
  const owner = options.owner ?? "RecoveryApplication"
  const changed = options.changed ?? true
  return Object.freeze({
    task: {
      task_id: TASK,
      run_id: RUN,
      metadata: {
        operator_intervention_attempt_count: denied ? 1 : 0,
        human_intervention_count: 0,
        last_sealed_control_denial: denied
          ? {
              request_id: input.requestId,
              manual_command_applied: false,
              human_wait_entered: false,
              no_human_wait: true,
              recovery_phase: "applied",
              recovery_action: "replan",
            }
          : undefined,
      },
    },
    intervention_counted: denied,
    operator_intervention_attempt_count: denied ? 1 : 0,
    human_intervention_count: 0,
    command_result: {
      ok: !denied,
      status: denied ? "denied" : "completed",
      error: denied
        ? {
            code: "permission_denied",
            message: "sealed autonomous policy denied manual mutation",
          }
        : undefined,
      data: {
        action: options.action ?? "retry",
        phase: denied ? "denied" : "applied",
        observed_event_ids: [`event_${input.commandId}`],
        control_event: {
          event_id: `event_${input.commandId}`,
          payload: {
            request_id: input.requestId,
            command_id: input.commandId,
            control_runtime: {
              owner,
              operation: options.action ?? "retry",
              changed,
              after: {
                worker_id: OWNER.workerId,
                lease_id: OWNER.leaseId,
                checkpoint_id: OWNER.checkpointId,
                revision: 12,
              },
            },
          },
        },
      },
    },
  })
}

function deferred<T>(): {
  promise: Promise<T>
  resolve(value: T): void
} {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((next) => {
    resolve = next
  })
  return { promise, resolve }
}

describe("timeline recovery control contracts", () => {
  test("fences worker controls with the physical attempt rather than a recovery attempt", () => {
    const task = {
      taskId: TASK,
      runId: RUN,
      sessionId: "session_timeline_control",
      rootNodeId: "node_timeline_control",
      metadata: {
        worker_pool: {
          worker_id: "worker_timeline_control",
          lease_id: "lease_timeline_control",
          attempt_id: "attempt_physical_worker",
        },
      },
    } as unknown as TaskProjection
    const projection = {
      workerEpochs: [
        {
          workerId: "worker_timeline_control",
          leaseId: "lease_timeline_control",
          nodeId: "node_timeline_control",
          terminal: false,
        },
      ],
      recoveryChains: [
        {
          attempts: [
            {
              id: "recovery_attempt_is_not_a_worker_attempt",
              checkpointId: "checkpoint_timeline_control",
            },
          ],
        },
      ],
    } as unknown as WorkerCausalTimelineProjection

    const owner = recoveryControlOwnerExpectation(task, projection)
    expect(owner.attemptId).toBe("attempt_physical_worker")
    expect(owner.checkpointId).toBe("checkpoint_timeline_control")

    const changedProjection = {
      ...projection,
      workerEpochs: [
        {
          workerId: "worker_replaced_after_task_snapshot",
          leaseId: "lease_replaced_after_task_snapshot",
          terminal: false,
        },
      ],
    } as unknown as WorkerCausalTimelineProjection
    expect(
      recoveryControlOwnerExpectation(task, changedProjection).attemptId,
    ).toBeUndefined()
  })

  test("production task timeline mounts the recovery panel and measured virtual viewport", async () => {
    const source = await Bun.file(
      new URL(
        "../src/features/timeline/view/timeline-workbench.tsx",
        import.meta.url,
      ),
    ).text()
    expect(source).toContain(
      'import { RecoveryControlPanel } from "./recovery-control-panel.tsx"',
    )
    expect(source).toContain("<RecoveryControlPanel")
    expect(source).toContain("new ScaledTimelineRuntime")
    expect(source).toContain("timeline-virtual-list")
    expect(source).toContain("onMeasure={measure}")
  })

  test("maps all five actions to bounded canonical slash commands", () => {
    let sequence = 0
    for (const action of Object.values(RecoveryControlAction)) {
      const normalized = normalizeRecoveryControlRequest(request(action), {
        now: () => 1000,
        requestId: () => `request_control_${++sequence}`,
        commandId: () => `cmd_control_${sequence}`,
        defaultTimeoutMs: 1_000,
        maximumTimeoutMs: 30_000,
      })
      expect(normalized.commandName).toBe(`/${action}`)
      expect(normalized.commandArguments.expected_task_id).toBe(TASK)
      expect(normalized.commandArguments.expected_run_id).toBe(RUN)
      expect(normalized.commandArguments.control_source).toBe("timeline")
      expect(normalized.idempotencyKey).toContain("task.control-command")
      expect(normalized.requestDigest).toStartWith("fnv128:")
      const rebuilt = buildRecoveryControlCommand({
        ...normalized,
        metadata: normalized.metadata ?? {},
      })
      expect(rebuilt.text).toBe(normalized.commandText)
    }
    const retry = normalizeRecoveryControlRequest(
      request(RecoveryControlAction.RETRY),
      {
        now: () => 1000,
        requestId: () => "request_retry_exact",
        commandId: () => "cmd_retry_exact",
        defaultTimeoutMs: 1_000,
        maximumTimeoutMs: 30_000,
      },
    )
    expect(retry.commandArguments.maximum_attempts).toBe(1)
    expect(retry.commandArguments.replay_committed_effects).toBe(false)
    const resume = normalizeRecoveryControlRequest(
      request(RecoveryControlAction.RESUME),
      {
        now: () => 1000,
        requestId: () => "request_resume_exact",
        commandId: () => "cmd_resume_exact",
        defaultTimeoutMs: 1_000,
        maximumTimeoutMs: 30_000,
      },
    )
    expect(resume.commandArguments.exact_resume).toBe(true)
    expect(resume.commandArguments.checkpoint_ref).toBe(
      OWNER.checkpointId,
    )
  })

  test("viewer detach never aborts or replays an in-flight durable request", async () => {
    const pending = deferred<Readonly<Record<string, unknown>>>()
    const calls: RecoveryControlTransportInput[] = []
    const transport: RecoveryControlTransport = {
      submit(input) {
        calls.push(input)
        return pending.promise
      },
    }
    let command = 0
    const runtime = new RecoveryControlRuntime({
      taskId: TASK,
      runId: RUN,
      transport,
      requestId: () => "request_detach_once",
      commandId: () => `cmd_detach_${++command}`,
      defaultTimeoutMs: 5_000,
      maximumTimeoutMs: 5_000,
    })
    const submission = runtime.submit(request(RecoveryControlAction.RETRY))
    expect(calls).toHaveLength(1)
    expect(calls[0]?.signal).toBeUndefined()
    runtime.close("viewer route changed")
    expect(runtime.getSnapshot().detached).toBe(true)
    expect(runtime.getSnapshot().ledger.receipts[0]?.detached).toBe(true)
    pending.resolve(response(calls[0]!, { action: "retry" }))
    const completed = await submission.completion
    expect(completed.phase).toBe("applied")
    expect(completed.detached).toBe(true)
    expect(calls).toHaveLength(1)
    expect(runtime.audit().detachedCompletions).toBe(1)
  })

  test("late canonical response supersedes a local timeout without duplicate submission", async () => {
    const pending = deferred<Readonly<Record<string, unknown>>>()
    const calls: RecoveryControlTransportInput[] = []
    const runtime = new RecoveryControlRuntime({
      taskId: TASK,
      runId: RUN,
      transport: {
        submit(input) {
          calls.push(input)
          return pending.promise
        },
      },
      requestId: () => "request_late_response",
      commandId: () => "cmd_late_response",
      defaultTimeoutMs: 250,
      maximumTimeoutMs: 250,
    })
    const submission = runtime.submit(
      request(RecoveryControlAction.RETRY, { timeoutMs: 250 }),
    )
    await new Promise((resolve) => setTimeout(resolve, 280))
    expect(runtime.getSnapshot().ledger.receipts[0]?.phase).toBe("timed-out")
    pending.resolve(response(calls[0]!, { action: "retry" }))
    const completed = await submission.completion
    expect(completed.phase).toBe("applied")
    expect(completed.timedOut).toBe(true)
    expect(completed.observations.some((item) => item.source === "timeout")).toBe(
      true,
    )
    expect(calls).toHaveLength(1)
  })

  test("sealed response proves denial, one attempt, zero humans and autonomous recovery", async () => {
    let captured!: RecoveryControlTransportInput
    const runtime = new RecoveryControlRuntime({
      taskId: TASK,
      runId: RUN,
      transport: {
        submit(input) {
          captured = input
          return Promise.resolve(response(input, { denied: true, action: "kill" }))
        },
      },
      requestId: () => "request_sealed_kill",
      commandId: () => "cmd_sealed_kill",
    })
    const submission = runtime.submit(
      request(RecoveryControlAction.KILL, {
        sealed: true,
        actorId: "sealed-benchmark-observer",
      }),
    )
    const receipt = await submission.completion
    expect(captured.sealed).toBe(true)
    expect(receipt.phase).toBe("denied")
    const assessment = assessSealedControlReceipt(submission.request, receipt)
    expect(assessment.valid).toBe(true)
    expect(assessment.manualMutationApplied).toBe(false)
    expect(assessment.interventionCounted).toBe(true)
    expect(assessment.operatorInterventionAttemptCount).toBe(1)
    expect(assessment.humanInterventionCount).toBe(0)
    expect(assessment.noHumanWait).toBe(true)
    expect(assessment.automaticRecoveryAction).toBe("replan")
  })

  test("disconnect becomes offline, reconnect is explicit, and no command is replayed", async () => {
    let calls = 0
    const runtime = new RecoveryControlRuntime({
      taskId: TASK,
      runId: RUN,
      transport: {
        submit() {
          calls += 1
          return Promise.reject(new Error("network connection offline"))
        },
      },
      requestId: () => "request_offline_control",
      commandId: () => "cmd_offline_control",
    })
    const receipt = await runtime.submitAndWait(
      request(RecoveryControlAction.RETRY),
    )
    expect(receipt.phase).toBe("failed")
    expect(runtime.getSnapshot().connection).toBe("offline")
    runtime.reconnect()
    expect(runtime.getSnapshot().connection).toBe("reconnecting")
    runtime.setConnection("online", "canonical event stream restored")
    expect(runtime.getSnapshot().connection).toBe("online")
    expect(runtime.getSnapshot().reconnectedAt).toBeDefined()
    expect(calls).toBe(1)
    expect(runtime.takeAnnouncements().length).toBeGreaterThan(0)
    const settledRevision = runtime.getSnapshot().revision
    expect(runtime.takeAnnouncements()).toEqual([])
    expect(runtime.getSnapshot().revision).toBe(settledRevision)
  })

  test("same-owner control race supersedes pending receipt but accepts later canonical proof", async () => {
    const pending = [
      deferred<Readonly<Record<string, unknown>>>(),
      deferred<Readonly<Record<string, unknown>>>(),
    ]
    const calls: RecoveryControlTransportInput[] = []
    let identity = 0
    const runtime = new RecoveryControlRuntime({
      taskId: TASK,
      runId: RUN,
      transport: {
        submit(input) {
          calls.push(input)
          return pending[calls.length - 1]!.promise
        },
      },
      requestId: () => `request_race_${++identity}`,
      commandId: () => `cmd_race_${identity}`,
    })
    const first = runtime.submit(
      request(RecoveryControlAction.RETRY, {
        reason: "first same-owner retry",
      }),
    )
    const second = runtime.submit(
      request(RecoveryControlAction.REASSIGN, {
        reason: "newer same-owner reassignment",
      }),
    )
    pending[1]!.resolve(response(calls[1]!, { action: "reassign" }))
    await second.completion
    const superseded = runtime.getSnapshot().ledger.receipts.find(
      (receipt) => receipt.commandId === first.request.commandId,
    )
    expect(superseded?.phase).toBe("superseded")
    pending[0]!.resolve(response(calls[0]!, { action: "retry" }))
    const lateCanonical = await first.completion
    expect(lateCanonical.phase).toBe("applied")
    expect(runtime.getSnapshot().ledger.pendingCount).toBe(0)
    expect(calls).toHaveLength(2)
  })

  test("disabled binding and stale scope fail closed before transport", () => {
    let calls = 0
    const transport: RecoveryControlTransport = {
      submit() {
        calls += 1
        return Promise.resolve({})
      },
    }
    const disabled = new RecoveryControlRuntime({
      taskId: TASK,
      runId: RUN,
      transport,
      disabled: true,
    })
    expect(() =>
      disabled.submit(request(RecoveryControlAction.RETRY)),
    ).toThrow(RecoveryControlError)
    const scoped = new RecoveryControlRuntime({
      taskId: TASK,
      runId: RUN,
      transport,
    })
    expect(() =>
      scoped.submit(
        request(RecoveryControlAction.RETRY, {
          taskId: "task_other_scope",
        }),
      ),
    ).toThrow(RecoveryControlError)
    expect(calls).toBe(0)
  })
})
