import type { TaskApi } from "../../../api/task-api.ts"
import type {
  RecoveryControlTransport,
  RecoveryControlTransportInput,
} from "./contracts.ts"

export function taskApiRecoveryControlTransport(
  taskApi: Pick<TaskApi, "controlCommand">,
): RecoveryControlTransport {
  return Object.freeze({
    submit(
      input: RecoveryControlTransportInput,
    ): Promise<Readonly<Record<string, unknown>>> {
      return taskApi.controlCommand({
        taskId: input.taskId,
        runId: input.runId,
        text: input.text,
        arguments: input.arguments,
        requestId: input.requestId,
        commandId: input.commandId,
        actorId: input.actorId,
        sessionId: input.sessionId,
        expectedRevision: input.expectedRevision,
        sealed: input.sealed,
        idempotencyKey: input.idempotencyKey,
        timeoutMs: input.timeoutMs,
        signal: input.signal,
      })
    },
  })
}

export function recoveryControlTransportDisabled(
  reason = "Timeline recovery control binding is disabled.",
): RecoveryControlTransport {
  return Object.freeze({
    async submit(): Promise<Readonly<Record<string, unknown>>> {
      throw Object.assign(new Error(reason), {
        code: "timeline_control_binding_disabled",
      })
    },
  })
}
