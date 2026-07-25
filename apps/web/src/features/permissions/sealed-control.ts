import type { PermissionInterventionRecord } from "./contracts.ts"
import type { PermissionConsoleRuntime } from "./runtime.ts"

export type PermissionSealedManualAction = "steer" | "retry" | "mode_change"

export function classifyPermissionSealedManualAction(input: {
  value: string
  deliveryMode?: "enqueue" | "steer" | "interrupt"
}): PermissionSealedManualAction | undefined {
  if (input.deliveryMode === "steer" || input.deliveryMode === "interrupt") {
    return "steer"
  }
  const normalized = input.value.trim().replace(/\s+/g, " ").toLowerCase()
  if (!normalized.startsWith("/")) return undefined
  if (/^\/retry(?:\s|$)/.test(normalized)) return "retry"
  if (
    /^\/permissions?(?:\s|$)/.test(normalized)
    && /(?:^|\s)(?:--mode(?:=|\s+)|mode(?:=|\s+))(?:ask|allow|deny|sealed|interactive|auto|bypass|yolo)\b/.test(
      normalized,
    )
  ) {
    return "mode_change"
  }
  if (
    /^\/(?:mode|permission-mode)(?:\s|$)/.test(normalized)
    && /\b(?:ask|allow|deny|sealed|interactive|auto|bypass|yolo)\b/.test(
      normalized,
    )
  ) {
    return "mode_change"
  }
  return undefined
}

export function permissionDisplayActor(
  metadata: Readonly<Record<string, unknown>> | undefined,
): string {
  for (const key of [
    "authenticated_actor",
    "operator_id",
    "operator_name",
    "user_id",
    "user_name",
  ]) {
    const value = metadata?.[key]
    if (
      typeof value === "string"
      && value.trim()
      && !/[\u0000\r\n]/.test(value)
      && new TextEncoder().encode(value.trim()).byteLength <= 256
    ) {
      return value.trim()
    }
  }
  return "zyra-web-operator"
}

export async function rejectPermissionSealedManualAction(input: {
  runtime: PermissionConsoleRuntime
  action: PermissionSealedManualAction
  actorId: string
  requestId?: string
  reason: string
  signal?: AbortSignal
}): Promise<PermissionInterventionRecord | undefined> {
  if (input.runtime.getSnapshot().productMode !== "sealed") return undefined
  return input.runtime.recordSealedAction({
    action: input.action,
    actorId: input.actorId,
    requestId: input.requestId,
    reason: input.reason,
    signal: input.signal,
  })
}
