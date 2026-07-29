import { describe, expect, test } from "bun:test"
import type {
  LoopXApi,
  LoopXControlState,
} from "../../../api/loopx-api.ts"
import { loopxWorkbenchView } from "./contracts.ts"
import { LoopXActionCoordinator } from "./runtime.ts"

function state(
  patch: Partial<LoopXControlState> = {},
): LoopXControlState {
  return {
    schema: "zyra.loopx-control-state/v1",
    runtime: {
      version: "0.2.13",
      source_commit: "a2c072d412d90839132e1cf39c23dd431c394175",
      source_tree_commit: "7232dca45ec2ca996edc43b2d3558edc802c844e",
      source_digest: "f8aeac4f805d6b11345680bbf15bef0f1fbbcff595e019421a881b74b14491a6",
      source_kind: "embedded_source",
      archive_fallback: false,
    },
    workspace_id: "workspace-loopx",
    run_id: "run-loopx",
    task_id: "task-loopx",
    goal_id: "goal-loopx",
    lifecycle: "enabled",
    connected: true,
    degraded: false,
    error: null,
    private_state: {
      owner: "LoopX",
      goal: { objective_ref: "zyra://objective" },
      todos: [
        {
          todo_id: "todo_primary",
          title: "Continue bounded delivery",
          claimed_by: "private-controller",
        },
      ],
      claims: [
        { todo_id: "todo_primary", claimant: "private-controller" },
      ],
      quota: { limit_slots: 4, spent_slots: 1 },
      history: [],
    },
    canonical_state: {
      task: { status: "running", owner: "Zyra orchestration/runtime" },
      worker_lease: { status: "active", owner: "WorkerLeaseManager" },
      execution_budget: { tool_calls: 2, owner: "ResourceScheduler" },
      loopx_claim_is_worker_lease: false,
      loopx_quota_is_execution_budget: false,
    },
    sync: {
      pending: 0,
      acked: 2,
      dead_letter: 0,
      cursor: 2,
      records: [],
    },
    continuation: {
      allowed: true,
      interaction_contract: {},
    },
    last_validated_receipt: {},
    last_sync_receipt: {},
    ...patch,
  }
}

describe("LoopX long-horizon workbench", () => {
  test("keeps private control distinct from canonical lease and budget", () => {
    const view = loopxWorkbenchView(state())
    expect(view.lifecycle).toBe("enabled")
    expect(view.runtimeVersion).toBe("0.2.13")
    expect(view.runtimeSource).toBe("embedded_source")
    expect(view.todos[0]?.claimed_by).toBe("private-controller")
    expect(view.workerLeaseStatus).toBe("active")
    expect(view.quota.spent_slots).toBe(1)
    expect(view.executionBudget.tool_calls).toBe(2)
    expect(view.continuationAllowed).toBe(true)
  })

  test("coalesces duplicate submits and preserves one idempotency key", async () => {
    const coordinator = new LoopXActionCoordinator()
    const keys: string[] = []
    let resolve!: (value: Awaited<ReturnType<LoopXApi["command"]>>) => void
    const pending = new Promise<Awaited<ReturnType<LoopXApi["command"]>>>(
      (done) => { resolve = done },
    )
    const api = {
      command(input: Parameters<LoopXApi["command"]>[0]) {
        keys.push(String(input.idempotencyKey))
        return pending
      },
    }
    const input = {
      taskId: "task-loopx",
      runId: "run-loopx",
      action: "interaction_submit" as const,
      arguments: { input_ref: "event:one" },
    }
    const first = coordinator.execute(api, input, 2)
    const duplicate = coordinator.execute(api, input, 2)
    expect(first).toBe(duplicate)
    expect(coordinator.pending()).toBe(1)
    expect(keys).toEqual([
      "loopx-ui-interaction_submit-task-loopx-2",
    ])
    resolve({
      ok: true,
      action: "interaction_submit",
      receipt: { status: "applied" },
      state: state(),
    })
    await first
    expect(coordinator.pending()).toBe(0)
  })

  test("projects degraded and dead-letter recovery state", () => {
    const view = loopxWorkbenchView(
      state({
        lifecycle: "degraded",
        degraded: true,
        sync: {
          pending: 1,
          acked: 1,
          dead_letter: 1,
          cursor: 3,
          records: [],
        },
        continuation: {
          allowed: false,
          interaction_contract: {},
        },
      }),
    )
    expect(view.lifecycle).toBe("degraded")
    expect(view.sync.dead_letter).toBe(1)
    expect(view.continuationAllowed).toBe(false)
  })
})
