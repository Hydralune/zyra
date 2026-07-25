import type {
  CanonicalProjectionState,
  ToolProjection,
} from "../../state/contracts.ts"
import type { JsonObject, JsonValue } from "../../events/ingress/index.ts"
import type {
  SkillProjectionAdmission,
  SkillProjectionCandidate,
  SkillProjectionRejection,
} from "./contracts.ts"
import {
  containsSecretMaterial,
  encodedSkillBytes,
  firstSkillRecord,
  firstSkillText,
  mergeSkillRecords,
  optionalSkillIdentity,
  redactSkillValue,
  skillArray,
  skillIdentity,
  skillRecord,
  skillText,
} from "./value.ts"

const SKILL_TOOL_NAMES = new Set([
  "skill",
  "skilltool",
  "skill_tool",
  "list_skills",
  "read_skill_resource",
  "update_skill",
  "invoke_skill",
  "typescript-skill",
])

const ACCEPTED_OWNERS = [
  /^typescript$/i,
  /^typescript[.:/]/i,
  /^03c(?:\s+|[.:/-])skillcoordinator$/i,
  /^skillcoordinator(?:[.:/-]|$)/i,
]

const MAX_CANDIDATE_BYTES = 2 * 1024 * 1024

export function admitSkillToolProjection(
  state: CanonicalProjectionState,
  tool: ToolProjection,
  input: {
    taskId: string
    runId?: string
    sessionId?: string
    payload?: JsonObject
    candidateIndex?: number
  },
): SkillProjectionAdmission {
  const sourceId = `${tool.id}:${input.candidateIndex ?? 0}`
  const reject = (
    code: string,
    reason: string,
    skillId?: string,
  ): SkillProjectionAdmission => Object.freeze({
    accepted: false,
    rejection: Object.freeze({
      sourceId,
      code,
      reason,
      eventId: tool.lastEventId,
      skillId,
    }),
  })

  if (tool.taskId !== input.taskId) {
    return reject("task_identity_mismatch", "Skill candidate belongs to another task.")
  }
  if (input.runId && tool.runId !== input.runId) {
    return reject("run_identity_mismatch", "Skill candidate belongs to another run.")
  }
  if (input.sessionId && tool.sessionId && tool.sessionId !== input.sessionId) {
    return reject("session_identity_mismatch", "Skill candidate belongs to another session.")
  }
  if (!tool.effective) {
    return reject("non_effective_projection", "Skill candidate is not an effective canonical transition.")
  }
  if (["optimistic", "orphaned", "tombstoned"].includes(tool.status)) {
    return reject(
      "non_canonical_projection_status",
      `Skill candidate has non-canonical projection status ${tool.status}.`,
    )
  }
  if (!state.causality.byEvent[tool.lastEventId]) {
    return reject("missing_canonical_event", "Skill candidate has no M2-01B causal event.")
  }
  const normalizedTool = skillText(tool.toolName, "", 256).toLowerCase()
  const attributes = skillRecord(tool.attributes)
  const metadata = skillRecord(tool.metadata)
  if (
    !SKILL_TOOL_NAMES.has(normalizedTool) &&
    !hasSkillDiscriminator(attributes) &&
    !hasSkillDiscriminator(input.payload)
  ) {
    return reject("not_skill_projection", "Tool projection does not carry a SkillTool discriminator.")
  }
  const payload = mergeSkillRecords(
    attributes,
    skillRecord(input.payload),
  )
  const skillId = safeCandidateSkillId(payload)
  if (!skillId) {
    return reject("missing_skill_identity", "Skill candidate has no valid canonical skill identity.")
  }
  const owner = firstSkillText(
    [payload, metadata],
    [
      "canonical_owner",
      "canonicalOwner",
      "canonical_runtime_owner",
      "canonicalRuntimeOwner",
      "registry_owner",
      "state_owner",
    ],
  )
  if (!owner || !ACCEPTED_OWNERS.some((pattern) => pattern.test(owner))) {
    return reject(
      "skill_owner_rejected",
      "Skill candidate is not owned by the internalized TypeScript SkillTool runtime.",
      skillId,
    )
  }
  if (encodedSkillBytes(payload) > MAX_CANDIDATE_BYTES) {
    return reject(
      "skill_candidate_too_large",
      "Skill candidate exceeds the bounded browser projection budget.",
      skillId,
    )
  }
  const secretPaths = containsSecretMaterial(payload)
  if (secretPaths.length) {
    return reject(
      "secret_material_rejected",
      `Skill candidate contains secret-like material at ${secretPaths.slice(0, 6).join(", ")}.`,
      skillId,
    )
  }
  const safePayload = redactSkillValue(payload)
  const safeMetadata = redactSkillValue(metadata)
  if (
    !safePayload ||
    typeof safePayload !== "object" ||
    Array.isArray(safePayload) ||
    !safeMetadata ||
    typeof safeMetadata !== "object" ||
    Array.isArray(safeMetadata)
  ) {
    return reject("projection_shape_invalid", "Skill projection could not be normalized.", skillId)
  }
  const candidate: SkillProjectionCandidate = Object.freeze({
    sourceId,
    sourceToolCallId: tool.toolCallId ?? tool.id,
    sourceEventId: tool.lastEventId,
    sourceSequence: tool.sequence,
    sourceRevision: tool.revision,
    sourceUpdatedAt: tool.updatedAt,
    taskId: tool.taskId,
    runId: tool.runId,
    sessionId: tool.sessionId,
    workerId: tool.workerId,
    canonicalOwner: owner,
    payload: Object.freeze(safePayload as JsonObject),
    metadata: Object.freeze(safeMetadata as JsonObject),
    artifactIds: Object.freeze([...tool.artifactIds]),
    commandId: tool.controlCommandId,
    permissionId: tool.permissionId,
    errorCode: tool.errorCode,
  })
  return Object.freeze({ accepted: true, candidate })
}

export function collectSkillProjectionAdmissions(
  state: CanonicalProjectionState,
  input: {
    taskId: string
    runId?: string
    sessionId?: string
  },
): {
  candidates: readonly SkillProjectionCandidate[]
  rejections: readonly SkillProjectionRejection[]
} {
  const candidates: SkillProjectionCandidate[] = []
  const rejections: SkillProjectionRejection[] = []
  const tools = Object.values(state.tools)
    .filter((tool) =>
      tool.taskId === input.taskId &&
      (!input.runId || tool.runId === input.runId))
    .sort((left, right) =>
      left.sequence - right.sequence ||
      left.id.localeCompare(right.id))
  for (const tool of tools) {
    const payloads = expandCandidatePayloads(tool)
    if (!payloads.length) {
      const admission = admitSkillToolProjection(state, tool, input)
      if (admission.candidate) candidates.push(admission.candidate)
      if (admission.rejection && admission.rejection.code !== "not_skill_projection") {
        rejections.push(admission.rejection)
      }
      continue
    }
    payloads.forEach((payload, candidateIndex) => {
      const admission = admitSkillToolProjection(state, tool, {
        ...input,
        payload,
        candidateIndex,
      })
      if (admission.candidate) candidates.push(admission.candidate)
      if (admission.rejection) rejections.push(admission.rejection)
    })
  }
  return Object.freeze({
    candidates: Object.freeze(candidates),
    rejections: Object.freeze(rejections),
  })
}

function expandCandidatePayloads(tool: ToolProjection): JsonObject[] {
  const attributes = skillRecord(tool.attributes)
  const nested = [
    attributes.skill,
    attributes.descriptor,
    attributes.skill_descriptor,
    attributes.active_skill,
    attributes.invocation,
    attributes.skill_invocation,
  ]
    .map(skillRecord)
    .filter((entry) => Object.keys(entry).length > 0)
  const catalogValues = [
    attributes.skills,
    attributes.active_skills,
    attributes.descriptors,
    attributes.catalog,
  ]
  const catalog: JsonObject[] = []
  for (const value of catalogValues) {
    if (
      value &&
      typeof value === "object" &&
      !Array.isArray(value) &&
      Array.isArray((value as JsonObject).items)
    ) {
      for (const item of skillArray((value as JsonObject).items)) {
        const record = skillRecord(item)
        if (Object.keys(record).length) catalog.push(record)
      }
      continue
    }
    for (const item of skillArray(value)) {
      const record = skillRecord(item)
      if (Object.keys(record).length) catalog.push(record)
    }
  }
  const output = [...nested, ...catalog]
  if (hasSkillDiscriminator(attributes)) output.push(attributes)
  const seen = new Set<string>()
  return output.filter((payload) => {
    const id = safeCandidateSkillId(payload)
    const identity = `${id}:${JSON.stringify(payload).slice(0, 2_048)}`
    if (seen.has(identity)) return false
    seen.add(identity)
    return true
  })
}

function hasSkillDiscriminator(value: unknown): boolean {
  const record = skillRecord(value)
  return Boolean(
    record.skill_id ||
    record.skillId ||
    record.skill_name ||
    record.skillName ||
    record.descriptor_digest ||
    record.body_digest ||
    record.registry_revision ||
    record.skill_revision ||
    record.invocation_id,
  )
}

function safeCandidateSkillId(payload: JsonObject): string | undefined {
  const nested = firstSkillRecord([payload], [
    "skill",
    "descriptor",
    "skill_descriptor",
  ])
  const raw = firstSkillText(
    [payload, nested],
    ["skill_id", "skillId", "id", "skill_name", "skillName", "name"],
  )
  try {
    return optionalSkillIdentity(raw, "canonical skill identity")
  } catch {
    return undefined
  }
}

export function assertSkillProjectionOwner(value: unknown): string {
  const owner = skillText(value, "", 256)
  if (!owner || !ACCEPTED_OWNERS.some((pattern) => pattern.test(owner))) {
    throw new TypeError("Skill projection canonical owner is not admissible.")
  }
  return owner
}

export function assertExactSkillBinding(input: {
  candidate: SkillProjectionCandidate
  taskId: string
  runId: string
  sessionId?: string
}): void {
  if (input.candidate.taskId !== skillIdentity(input.taskId, "task identity")) {
    throw new Error("Skill projection task binding changed.")
  }
  if (input.candidate.runId !== skillIdentity(input.runId, "run identity")) {
    throw new Error("Skill projection run binding changed.")
  }
  if (
    input.sessionId &&
    input.candidate.sessionId &&
    input.candidate.sessionId !== skillIdentity(input.sessionId, "session identity")
  ) {
    throw new Error("Skill projection session binding changed.")
  }
}
