import {
  OPERATION_NAMES,
  createIdempotencyKey,
  normalizeIdempotencyKey,
  normalizeIdentity,
  type IdentityBinding,
} from "../../../../packages/core/typed-api-client/src/index.ts"
import type { ZyraApiClient } from "./client.ts"

export type LoopXLifecycle = "enabled" | "degraded" | "disabled"

export interface LoopXTodo {
  readonly todo_id: string
  readonly title?: string
  readonly text?: string
  readonly role?: string
  readonly priority?: string
  readonly status?: string
  readonly done?: boolean
  readonly claimed_by?: string
}

export interface LoopXControlState {
  readonly schema: "zyra.loopx-control-state/v1"
  readonly runtime: {
    readonly version: "0.2.13"
    readonly source_commit: string
    readonly source_tree_commit: string
    readonly source_digest: string
    readonly source_kind: "embedded_source"
    readonly archive_fallback: false
  }
  readonly workspace_id: string
  readonly run_id: string
  readonly task_id: string
  readonly goal_id: string
  readonly lifecycle: LoopXLifecycle
  readonly connected: boolean
  readonly degraded: boolean
  readonly error: Readonly<Record<string, unknown>> | null
  readonly private_state: {
    readonly owner: "LoopX"
    readonly goal: Readonly<Record<string, unknown>>
    readonly todos: readonly LoopXTodo[]
    readonly claims: readonly Readonly<Record<string, unknown>>[]
    readonly quota: Readonly<Record<string, unknown>>
    readonly history: readonly Readonly<Record<string, unknown>>[]
  }
  readonly canonical_state: Readonly<Record<string, unknown>>
  readonly sync: {
    readonly pending: number
    readonly acked: number
    readonly dead_letter: number
    readonly cursor: number
    readonly records: readonly Readonly<Record<string, unknown>>[]
  }
  readonly continuation: {
    readonly allowed: boolean
    readonly interaction_contract: Readonly<Record<string, unknown>>
  }
  readonly last_validated_receipt: Readonly<Record<string, unknown>>
  readonly last_sync_receipt: Readonly<Record<string, unknown>>
}

export type LoopXControlAction =
  | "connect"
  | "disconnect"
  | "claim"
  | "release"
  | "interaction_submit"
  | "sync_retry"

export interface LoopXCommandInput {
  readonly taskId: string
  readonly runId: string
  readonly action: LoopXControlAction
  readonly arguments?: Readonly<Record<string, unknown>>
  readonly idempotencyKey?: string
  readonly signal?: AbortSignal
}

function stateProjection(value: unknown): LoopXControlState {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError("LoopX state response must be an object.")
  }
  const state = value as Record<string, unknown>
  if (
    state.schema !== "zyra.loopx-control-state/v1"
    || !state.runtime
    || !["enabled", "degraded", "disabled"].includes(String(state.lifecycle))
    || !state.private_state
    || !state.canonical_state
    || !state.sync
    || !state.continuation
  ) {
    throw new TypeError("LoopX state response violates v1.")
  }
  return Object.freeze(state) as unknown as LoopXControlState
}

export class LoopXApi {
  readonly #client: ZyraApiClient

  constructor(client: ZyraApiClient) {
    this.#client = client
  }

  async state(
    taskId: string,
    options: { goalId?: string; signal?: AbortSignal } = {},
  ): Promise<LoopXControlState> {
    const selectedTaskId = normalizeIdentity("task", taskId)
    const response = await this.#client.endpoint<Record<string, unknown>>(
      OPERATION_NAMES.taskLoopxState,
      {
        path: { task_id: selectedTaskId },
        query: { goal_id: options.goalId },
        binding: { taskId: selectedTaskId },
        signal: options.signal,
        coordinationKey: `task.loopx.state:${selectedTaskId}`,
        latestWins: true,
      },
    )
    return stateProjection(response.data)
  }

  async command(input: LoopXCommandInput): Promise<{
    readonly ok: boolean
    readonly action: LoopXControlAction
    readonly receipt: Readonly<Record<string, unknown>>
    readonly state: LoopXControlState
  }> {
    const taskId = normalizeIdentity("task", input.taskId)
    const runId = normalizeIdentity("run", input.runId)
    const binding: IdentityBinding = { taskId, runId }
    const bodyWithoutKey = {
      action: input.action,
      ...(input.arguments ?? {}),
    }
    const idempotencyKey = normalizeIdempotencyKey(
      input.idempotencyKey
      ?? createIdempotencyKey(
        OPERATION_NAMES.taskLoopxCommand,
        binding,
        bodyWithoutKey,
      ),
    )
    const body = {
      ...bodyWithoutKey,
      idempotency_key: idempotencyKey,
    }
    const response = await this.#client.endpoint<
      Record<string, unknown>,
      typeof body
    >(OPERATION_NAMES.taskLoopxCommand, {
      path: { task_id: taskId },
      body,
      binding,
      idempotencyKey,
      signal: input.signal,
      coordinationKey: `task.loopx.command:${taskId}:${idempotencyKey}`,
      deduplicate: true,
    })
    return Object.freeze({
      ok: response.data.ok === true,
      action: input.action,
      receipt: Object.freeze({
        ...(
          (response.data.sync_receipt ?? response.data.receipt)
          && typeof (response.data.sync_receipt ?? response.data.receipt) === "object"
          && !Array.isArray(response.data.sync_receipt ?? response.data.receipt)
            ? (response.data.sync_receipt ?? response.data.receipt) as Record<string, unknown>
            : {}
        ),
      }),
      state: stateProjection(response.data.state),
    })
  }
}
