import {
  RecoveryControlPhase,
  type NormalizedRecoveryControlRequest,
  type RecoveryControlReceipt,
} from "./contracts.ts"

export interface SealedControlAssessment {
  sealed: boolean
  valid: boolean
  manualMutationApplied: boolean
  denied: boolean
  interventionCounted: boolean
  humanInterventionCount: number
  operatorInterventionAttemptCount: number
  noHumanWait: boolean
  automaticRecoveryObserved: boolean
  automaticRecoveryAction?: string
  failClosed: boolean
  violations: readonly string[]
  evidence: Readonly<Record<string, string | number | boolean>>
}

function freeze<T>(values: Iterable<T>): readonly T[] {
  return Object.freeze([...values])
}

function record(value: unknown): Readonly<Record<string, unknown>> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? Object.freeze({ ...(value as Record<string, unknown>) })
    : Object.freeze({})
}

function nested(
  value: Readonly<Record<string, unknown>>,
  ...path: readonly string[]
): Readonly<Record<string, unknown>> {
  let current = value
  for (const key of path) current = record(current[key])
  return current
}

function stringValue(
  value: Readonly<Record<string, unknown>>,
  ...keys: readonly string[]
): string | undefined {
  for (const key of keys) {
    const candidate = value[key]
    if (typeof candidate === "string" && candidate.trim()) {
      return candidate.trim()
    }
  }
  return undefined
}

function booleanValue(
  value: Readonly<Record<string, unknown>>,
  ...keys: readonly string[]
): boolean | undefined {
  for (const key of keys) {
    if (typeof value[key] === "boolean") return Boolean(value[key])
  }
  return undefined
}

function numberValue(
  value: Readonly<Record<string, unknown>>,
  ...keys: readonly string[]
): number | undefined {
  for (const key of keys) {
    const candidate = Number(value[key])
    if (Number.isFinite(candidate)) return candidate
  }
  return undefined
}

function sealedRecovery(
  receipt: RecoveryControlReceipt,
): {
  observed: boolean
  action?: string
  phase?: string
  failedClosed: boolean
} {
  const response = record(receipt.response)
  const commandResult = record(response.command_result)
  const data = record(commandResult.data)
  const task = record(response.task)
  const metadata = record(task.metadata)
  const denial = record(metadata.last_sealed_control_denial)
  const recovery = record(denial.recovery)
  const plan = record(recovery.plan)
  const decision = record(plan.decision)
  const selected = record(decision.selected)
  const action =
    stringValue(denial, "recovery_action") ??
    stringValue(selected, "action") ??
    stringValue(data, "recovery_action")
  const phase =
    stringValue(denial, "recovery_phase") ??
    stringValue(data, "recovery_phase")
  return {
    observed:
      Boolean(action) ||
      phase === "applied" ||
      phase === "failed_closed" ||
      receipt.observations.some(
        (observation) =>
          observation.source === "sealed-policy" &&
          observation.terminal,
      ),
    action,
    phase,
    failedClosed:
      phase === "failed_closed" ||
      booleanValue(denial, "manual_command_applied") === false,
  }
}

export function assessSealedControlReceipt(
  request: NormalizedRecoveryControlRequest,
  receipt: RecoveryControlReceipt,
): SealedControlAssessment {
  if (!request.sealed) {
    return Object.freeze({
      sealed: false,
      valid: true,
      manualMutationApplied:
        receipt.phase === RecoveryControlPhase.APPLIED,
      denied: receipt.denied,
      interventionCounted: receipt.interventionCounted,
      humanInterventionCount: receipt.humanInterventionCount ?? 0,
      operatorInterventionAttemptCount:
        receipt.operatorInterventionAttemptCount ?? 0,
      noHumanWait: receipt.noHumanWait,
      automaticRecoveryObserved: false,
      failClosed: false,
      violations: Object.freeze([]),
      evidence: Object.freeze({}),
    })
  }
  const response = record(receipt.response)
  const task = record(response.task)
  const metadata = record(task.metadata)
  const commandResult = record(response.command_result)
  const data = record(commandResult.data)
  const humanInterventionCount =
    receipt.humanInterventionCount ??
    numberValue(response, "human_intervention_count") ??
    numberValue(commandResult, "human_intervention_count") ??
    numberValue(data, "human_intervention_count") ??
    numberValue(metadata, "human_intervention_count") ??
    0
  const operatorInterventionAttemptCount =
    receipt.operatorInterventionAttemptCount ??
    numberValue(response, "operator_intervention_attempt_count") ??
    numberValue(commandResult, "operator_intervention_attempt_count") ??
    numberValue(data, "operator_intervention_attempt_count") ??
    numberValue(metadata, "operator_intervention_attempt_count") ??
    0
  const recovery = sealedRecovery(receipt)
  const manualMutationApplied =
    receipt.phase === RecoveryControlPhase.APPLIED ||
    receipt.ownerEvidence.some(
      (evidence) =>
        evidence.changed === true &&
        !/RecoveryDecisionRuntime|RecoveryApplication|GraphStateCustody/.test(
          evidence.owner,
        ),
    )
  const denied =
    receipt.phase === RecoveryControlPhase.DENIED && receipt.denied
  const interventionCounted =
    receipt.interventionCounted ||
    booleanValue(response, "intervention_counted") === true
  const noHumanWait =
    receipt.noHumanWait &&
    !receipt.observations.some(
      (observation) =>
        observation.phase === RecoveryControlPhase.PENDING &&
        /permission|approval|human/i.test(observation.summary),
    )
  const violations: string[] = []
  if (!denied) {
    violations.push("sealed manual command was not canonically denied")
  }
  if (manualMutationApplied) {
    violations.push("sealed manual command produced a direct state mutation")
  }
  if (!interventionCounted) {
    violations.push("sealed denial did not count an operator intervention attempt")
  }
  if (humanInterventionCount !== 0) {
    violations.push("sealed run human_intervention_count is not zero")
  }
  if (operatorInterventionAttemptCount < 1) {
    violations.push("sealed run has no operator intervention ledger entry")
  }
  if (!noHumanWait) {
    violations.push("sealed denial entered a human approval wait")
  }
  if (!recovery.observed) {
    violations.push("sealed denial has no automatic recovery/fail-closed evidence")
  }
  return Object.freeze({
    sealed: true,
    valid: violations.length === 0,
    manualMutationApplied,
    denied,
    interventionCounted,
    humanInterventionCount,
    operatorInterventionAttemptCount,
    noHumanWait,
    automaticRecoveryObserved: recovery.observed,
    automaticRecoveryAction: recovery.action,
    failClosed: recovery.failedClosed || recovery.phase === "failed_closed",
    violations: freeze(violations),
    evidence: Object.freeze({
      command: request.commandName,
      requestId: request.requestId,
      commandId: request.commandId,
      recoveryPhase: recovery.phase ?? "",
      recoveryAction: recovery.action ?? "",
      denialReason:
        stringValue(
          nested(metadata, "last_sealed_control_denial"),
          "reason",
        ) ?? "",
    }),
  })
}

export function sealedReceiptReason(
  receipt: RecoveryControlReceipt,
): string | undefined {
  if (!receipt.sealed) return undefined
  const response = record(receipt.response)
  const task = record(response.task)
  const metadata = record(task.metadata)
  const denial = record(metadata.last_sealed_control_denial)
  return (
    stringValue(denial, "reason", "recovery_error") ??
    receipt.errorMessage ??
    receipt.summary
  )
}

export function sealedReceiptRecoveryAction(
  receipt: RecoveryControlReceipt,
): string | undefined {
  return sealedRecovery(receipt).action
}

export function sealedControlCanRenderApproval(
  receipt: RecoveryControlReceipt,
): boolean {
  if (!receipt.sealed) return receipt.phase === RecoveryControlPhase.PENDING
  return false
}

export function assertSealedReceiptInvariant(
  request: NormalizedRecoveryControlRequest,
  receipt: RecoveryControlReceipt,
): void {
  const assessment = assessSealedControlReceipt(request, receipt)
  if (request.sealed && !assessment.valid) {
    throw new Error(
      `Sealed control invariant failed: ${assessment.violations.join("; ")}`,
    )
  }
}
