import type {
  SkillArguments,
  SkillControlOperation,
  SkillOperationKind,
  SkillProjection,
} from "./contracts.ts"
import {
  assertPublicSkillArguments,
  quoteSkillCommand,
  skillFingerprint,
  skillIdentity,
  skillText,
  stableSkillStringify,
} from "./value.ts"

export interface SkillControlAdmissionResult {
  admitted: boolean
  code: string
  reasons: readonly string[]
  expectedHash: string
  expectedRegistryRevision: number
}

export function admitSkillControl(input: {
  skill: SkillProjection
  kind: SkillOperationKind
  connected: boolean
  enabled: boolean
  viewerOpen: boolean
  sealed: boolean
}): SkillControlAdmissionResult {
  const reasons: string[] = []
  if (!input.enabled) reasons.push("skill feature binding is disabled")
  if (!input.viewerOpen) reasons.push("skill viewer is closed")
  if (!input.connected) reasons.push("canonical projection transport is disconnected")
  if (input.sealed) reasons.push("sealed runs reject human skill controls")
  if (input.skill.availability !== "available") {
    reasons.push(`skill availability is ${input.skill.availability}`)
  }
  if (input.skill.disabledReason) reasons.push(input.skill.disabledReason)
  if (!input.skill.provenance.trustworthy) reasons.push("skill provenance is not trustworthy")
  if (!input.skill.dependencies.complete) reasons.push("skill dependency graph is incomplete")
  if (input.skill.supplyChain.browserDisposition === "block") {
    reasons.push("skill supply-chain audit is blocking")
  }
  if (input.skill.supplyChain.allowedByOwner === false) {
    reasons.push("canonical skill owner denied the supply chain")
  }
  if (
    input.kind === "update" &&
    !input.skill.approval.updateEligible
  ) {
    reasons.push(...input.skill.approval.reasons)
  }
  if (
    input.kind === "invoke" &&
    !input.skill.approval.invokeEligible
  ) {
    reasons.push(...input.skill.approval.reasons)
  }
  if (!input.skill.version.contentHash) reasons.push("canonical content hash is absent")
  if (input.skill.version.registryRevision < 0) reasons.push("registry revision is invalid")
  const unique = [...new Set(reasons)].sort()
  return Object.freeze({
    admitted: unique.length === 0,
    code: unique.length
      ? input.sealed
        ? "sealed_skill_control_denied"
        : "skill_control_not_admitted"
      : "skill_control_admitted",
    reasons: Object.freeze(unique),
    expectedHash: input.skill.version.contentHash,
    expectedRegistryRevision: input.skill.version.registryRevision,
  })
}

export function buildSkillUpdateCommand(input: {
  skill: SkillProjection
  nonce: string
  idempotencyKey: string
  expectedHash?: string
}): string {
  const skillId = skillIdentity(input.skill.skillId, "skill update identity")
  const expectedHash = skillText(
    input.expectedHash ?? input.skill.version.contentHash,
    "",
    256,
  )
  if (expectedHash !== input.skill.version.contentHash) {
    throw new Error("Skill update expected hash is stale.")
  }
  return [
    "/skills",
    "update",
    "--skill",
    quoteSkillCommand(skillId),
    "--expected-hash",
    quoteSkillCommand(expectedHash),
    "--expected-revision",
    String(input.skill.version.registryRevision),
    "--dependency-digest",
    quoteSkillCommand(input.skill.dependencies.digest),
    "--supply-digest",
    quoteSkillCommand(input.skill.supplyChain.digest),
    "--approval-id",
    quoteSkillCommand(input.skill.approval.approvalId ?? "none"),
    "--nonce",
    quoteSkillCommand(input.nonce),
    "--idempotency-key",
    quoteSkillCommand(input.idempotencyKey),
  ].join(" ")
}

export function buildSkillInvokeCommand(input: {
  skill: SkillProjection
  arguments?: SkillArguments
  nonce: string
  idempotencyKey: string
}): {
  command: string
  argumentsDigest: string
} {
  const skillId = skillIdentity(input.skill.skillId, "skill invocation identity")
  const argumentsValue = assertPublicSkillArguments(input.arguments ?? {})
  const serialized = stableSkillStringify(argumentsValue)
  const argumentsDigest = skillFingerprint([serialized])
  const command = [
    "/skills",
    "invoke",
    "--skill",
    quoteSkillCommand(skillId),
    "--registry-revision",
    String(input.skill.version.registryRevision),
    "--descriptor-digest",
    quoteSkillCommand(input.skill.version.descriptorDigest),
    "--content-hash",
    quoteSkillCommand(input.skill.version.contentHash),
    "--arguments-json",
    quoteSkillCommand(serialized),
    "--arguments-digest",
    quoteSkillCommand(argumentsDigest),
    "--nonce",
    quoteSkillCommand(input.nonce),
    "--idempotency-key",
    quoteSkillCommand(input.idempotencyKey),
  ].join(" ")
  return Object.freeze({ command, argumentsDigest })
}

export function skillOperationIdentity(input: {
  kind: SkillOperationKind
  skill: SkillProjection
  nonce: string
  idempotencyKey: string
  argumentsDigest?: string
}): string {
  return skillFingerprint([
    input.kind,
    input.skill.taskId,
    input.skill.runId,
    input.skill.sessionId,
    input.skill.skillId,
    input.skill.version.contentHash,
    input.skill.version.registryRevision,
    input.nonce,
    input.idempotencyKey,
    input.argumentsDigest,
  ])
}

export function terminalSkillControlPhase(
  phase: SkillControlOperation["phase"],
): boolean {
  return ["committed", "failed", "quarantined", "disabled"].includes(phase)
}
