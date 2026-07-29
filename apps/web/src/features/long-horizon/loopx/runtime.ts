import type {
  LoopXApi,
  LoopXCommandInput,
} from "../../../api/loopx-api.ts"

type LoopXCommandResult = Awaited<ReturnType<LoopXApi["command"]>>

export class LoopXActionCoordinator {
  readonly #inFlight = new Map<string, Promise<LoopXCommandResult>>()

  execute(
    api: Pick<LoopXApi, "command">,
    input: LoopXCommandInput,
    cursor: number,
  ): Promise<LoopXCommandResult> {
    const identity = JSON.stringify([
      input.taskId,
      input.runId,
      input.action,
      cursor,
      input.arguments ?? {},
    ])
    const prior = this.#inFlight.get(identity)
    if (prior) return prior
    const idempotencyKey = (
      input.idempotencyKey
      ?? `loopx-ui-${input.action}-${input.taskId}-${cursor}`
    )
    const promise = api.command({
      ...input,
      arguments: {
        ...(input.arguments ?? {}),
        expected_cursor: cursor,
      },
      idempotencyKey,
    })
      .finally(() => this.#inFlight.delete(identity))
    this.#inFlight.set(identity, promise)
    return promise
  }

  pending(): number {
    return this.#inFlight.size
  }
}
