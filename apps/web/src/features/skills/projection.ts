import type { CanonicalProjectionState } from "../../state/contracts.ts"
import type { JsonObject, JsonValue } from "../../events/ingress/index.ts"
import {
  collectSkillProjectionAdmissions,
} from "./admission.ts"
import {
  parseSkillBody,
  projectSkillResources,
  projectSkillToolScope,
} from "./body.ts"
import { buildSkillDependencyGraph } from "./dependencies.ts"
import {
  compareSkillVersions,
  detectSkillVersionDrift,
  projectSkillProvenance,
  projectSkillVersion,
} from "./provenance.ts"
import {
  auditSkillSupplyChain,
  projectSkillStagedApproval,
} from "./supply-chain.ts"
import type {
  SkillAvailability,
  SkillCatalogProjection,
  SkillInvocationProjection,
  SkillProjection,
  SkillProjectionCandidate,
  SkillVersionProjection,
} from "./contracts.ts"
import {
  classifySkillStatus,
  firstSkillArray,
  firstSkillRecord,
  firstSkillText,
  optionalSkillHash,
  optionalSkillIdentity,
  optionalSkillTimestamp,
  skillBoolean,
  skillFingerprint,
  skillInteger,
  skillNumber,
  skillRecord,
  skillRecords,
  skillText,
  uniqueSkillStrings,
} from "./value.ts"

export function buildSkillCatalogProjection(
  state: CanonicalProjectionState,
  input: {
    taskId: string
    runId?: string
    sessionId?: string
    selectedSkillId?: string
  },
): SkillCatalogProjection {
  const admissions = collectSkillProjectionAdmissions(state, input)
  const grouped = groupCandidates(admissions.candidates)
  const skills: SkillProjection[] = []
  const warnings: string[] = []
  for (const [skillId, candidates] of grouped) {
    try {
      skills.push(projectSkill(state, skillId, candidates))
    } catch (error) {
      warnings.push(
        `skill ${skillId} projection failed closed: ${
          error instanceof Error ? error.message : String(error)
        }`,
      )
    }
  }
  skills.sort((left, right) =>
    Number(right.activeInvocationIds.length > 0) -
      Number(left.activeInvocationIds.length > 0) ||
    Number(!right.ready) - Number(!left.ready) ||
    left.displayName.localeCompare(right.displayName) ||
    left.skillId.localeCompare(right.skillId))
  const selectedSkillId =
    input.selectedSkillId && skills.some((skill) => skill.skillId === input.selectedSkillId)
      ? input.selectedSkillId
      : skills[0]?.skillId
  for (const rejection of admissions.rejections) {
    if (
      [
        "secret_material_rejected",
        "skill_owner_rejected",
        "non_canonical_projection_status",
      ].includes(rejection.code)
    ) {
      warnings.push(rejection.reason)
    }
  }
  const core = {
    taskId: input.taskId,
    runId: input.runId,
    sessionId: input.sessionId,
    revision: state.revision,
    skills,
    selectedSkillId,
    totalSkills: skills.length,
    availableSkills: skills.filter((skill) => skill.availability === "available").length,
    blockedSkills: skills.filter((skill) => !skill.ready).length,
    activeInvocations: skills.reduce(
      (count, skill) => count + skill.activeInvocationIds.length,
      0,
    ),
    warnings: uniqueSkillStrings(warnings),
    rejectedCandidates: admissions.rejections,
  }
  return Object.freeze({
    ...core,
    skills: Object.freeze(skills),
    warnings: Object.freeze(core.warnings),
    rejectedCandidates: Object.freeze([...admissions.rejections]),
    fingerprint: skillFingerprint([
      state.revision,
      input.taskId,
      skills.map((skill) => skill.fingerprint),
      admissions.rejections,
    ]),
  })
}

export function projectSkill(
  state: CanonicalProjectionState,
  skillId: string,
  candidates: readonly SkillProjectionCandidate[],
): SkillProjection {
  if (!candidates.length) throw new Error("Skill projection has no admitted candidates.")
  const ordered = [...candidates].sort(compareCandidate)
  const newest = ordered[0]!
  const records = candidateRecords(ordered)
  const descriptor = selectDescriptorRecord(records)
  const bodyRaw = selectBody(records)
  const expectedBodyDigest = safeOptionalHash(
    firstSkillText(
      [descriptor, ...records],
      ["body_digest", "bodyDigest"],
    ),
  )
  const body = parseSkillBody(bodyRaw, expectedBodyDigest)
  const versionHistory = ordered.map((candidate) =>
    projectSkillVersion(candidateRecords([candidate]), {
      bodyDigest: body.digest,
      eventId: candidate.sourceEventId,
      observedAt: candidate.sourceUpdatedAt,
      sourceRevision: candidate.sourceRevision,
    }))
  const version = [...versionHistory].sort(compareSkillVersions).at(-1) ??
    projectSkillVersion(records, {
      bodyDigest: body.digest,
      eventId: newest.sourceEventId,
      observedAt: newest.sourceUpdatedAt,
      sourceRevision: newest.sourceRevision,
    })
  const canonicalOwner = newest.canonicalOwner
  const provenance = projectSkillProvenance(
    [descriptor, ...records],
    canonicalOwner,
  )
  const resources = projectSkillResources([descriptor, ...records], body)
  const toolScope = projectSkillToolScope([descriptor, ...records])
  const dependencies = buildSkillDependencyGraph(
    skillId,
    [descriptor, ...records],
  )
  const availability = normalizeAvailability(
    firstSkillText(
      [descriptor, ...records],
      ["availability", "status", "skill_status"],
      "available",
    ),
  )
  const supplyChain = auditSkillSupplyChain({
    skillId,
    records: [descriptor, ...records],
    body,
    resources,
    provenance,
    dependencies,
    version,
  })
  const approval = projectSkillStagedApproval({
    records: [descriptor, ...records],
    version,
    dependencies,
    supplyChain,
    availability,
    observedAt: newest.sourceUpdatedAt,
  })
  const invocations = projectSkillInvocations(
    state,
    skillId,
    ordered,
    version,
  )
  const activeInvocationIds = invocations
    .filter((invocation) =>
      ["queued", "running", "background"].includes(invocation.status) &&
      !invocation.quarantined)
    .map((invocation) => invocation.invocationId)
  const warnings: string[] = [
    ...body.suspiciousFragments.map((fragment) => `body audit: ${fragment}`),
    ...resources.flatMap((resource) =>
      resource.warnings.map((warning) => `${resource.path}: ${warning}`)),
    ...toolScope.warnings,
    ...provenance.warnings,
    ...detectSkillVersionDrift(versionHistory),
    ...dependencies.missing.map((id) => `missing dependency ${id}`),
    ...dependencies.unapproved.map((id) => `unapproved dependency ${id}`),
    ...dependencies.versionConflicts.map((id) => `dependency version conflict ${id}`),
    ...dependencies.cycles.map((cycle) => `dependency cycle ${cycle.join(" -> ")}`),
    ...supplyChain.warnings,
    ...approval.reasons,
    ...invocations
      .filter((invocation) => invocation.quarantined)
      .map((invocation) =>
        `invocation ${invocation.invocationId} quarantined: ${invocation.quarantineReason}`),
  ]
  const disabledReason = firstSkillText(
    [descriptor, ...records],
    ["disabled_reason", "disabledReason", "invalid_reason"],
  ) || undefined
  if (disabledReason) warnings.push(disabledReason)
  const name = firstSkillText(
    [descriptor, ...records],
    ["name", "skill_name", "skillName"],
    skillId,
  )
  const displayName = firstSkillText(
    [descriptor, ...records],
    ["display_name", "displayName", "title"],
    name,
  )
  const description = firstSkillText(
    [descriptor, ...records],
    ["description", "summary"],
    body.summary,
    32_768,
  )
  const ready =
    availability === "available" &&
    !disabledReason &&
    provenance.trustworthy &&
    dependencies.complete &&
    supplyChain.browserDisposition !== "block" &&
    supplyChain.allowedByOwner !== false &&
    approval.invokeEligible
  const core = {
    skillId,
    name,
    displayName,
    description,
    taskId: newest.taskId,
    runId: newest.runId,
    sessionId: newest.sessionId,
    availability,
    disabledReason,
    canonicalOwner,
    projectionRevision: Math.max(...ordered.map((candidate) => candidate.sourceRevision)),
    sequence: Math.max(...ordered.map((candidate) => candidate.sourceSequence)),
    lastEventId: newest.sourceEventId,
    lastUpdatedAt: newest.sourceUpdatedAt,
    body,
    resources,
    toolScope,
    provenance,
    version,
    dependencies,
    supplyChain,
    approval,
    invocations,
    activeInvocationIds,
    resultCount: invocations.filter((invocation) => invocation.status === "completed").length,
    errorCount: invocations.filter((invocation) => invocation.status === "failed").length,
    warnings: uniqueSkillStrings(warnings),
    ready,
  }
  return Object.freeze({
    ...core,
    resources: Object.freeze([...resources]),
    invocations: Object.freeze([...invocations]),
    activeInvocationIds: Object.freeze(activeInvocationIds),
    warnings: Object.freeze(core.warnings),
    fingerprint: skillFingerprint([
      skillId,
      version,
      dependencies.digest,
      supplyChain.digest,
      approval,
      invocations.map(invocationFingerprint),
      ready,
    ]),
  })
}

function projectSkillInvocations(
  state: CanonicalProjectionState,
  skillId: string,
  candidates: readonly SkillProjectionCandidate[],
  version: SkillVersionProjection,
): SkillInvocationProjection[] {
  const invocations = new Map<string, SkillInvocationProjection>()
  for (const candidate of [...candidates].sort((left, right) =>
    left.sourceSequence - right.sourceSequence ||
    left.sourceId.localeCompare(right.sourceId))) {
    const payloads = invocationPayloads(candidate.payload)
    for (const payload of payloads) {
      const invocationId = safeInvocationId(payload, candidate)
      if (!invocationId) continue
      const previous = invocations.get(invocationId)
      const status = invocationStatus(payload, candidate)
      const registryRevision = skillInteger(
        payload.registry_revision ??
        payload.registryRevision ??
        payload.skill_revision,
        previous?.registryRevision ?? version.registryRevision,
        0,
        Number.MAX_SAFE_INTEGER,
      )
      const descriptorDigest = safeOptionalHash(
        firstSkillText(
          [payload],
          ["descriptor_digest", "descriptorDigest", "skill_digest"],
        ),
      ) ?? previous?.descriptorDigest
      const eventIds = uniqueSkillStrings([
        ...(previous?.eventIds ?? []),
        candidate.sourceEventId,
        ...(
          state.causality.byToolCall[candidate.sourceToolCallId] ?? []
        ),
      ])
      const result = firstSkillRecord(
        [payload],
        ["result", "output", "invocation_result"],
      )
      const failure = firstSkillRecord(
        [payload],
        ["failure", "error", "invocation_error"],
      )
      const completedAt =
        optionalSkillTimestamp(
          payload.completed_at ??
          payload.completedAt ??
          result.completed_at,
        ) ??
        (["completed", "failed", "cancelled"].includes(status)
          ? candidate.sourceUpdatedAt
          : previous?.completedAt)
      const quarantineReasons: string[] = []
      if (candidate.taskId !== candidates[0]?.taskId) {
        quarantineReasons.push("invocation task binding differs from skill task")
      }
      if (candidate.runId !== candidates[0]?.runId) {
        quarantineReasons.push("invocation run binding differs from skill run")
      }
      if (registryRevision < version.registryRevision) {
        quarantineReasons.push("invocation uses an older registry revision")
      }
      if (descriptorDigest && descriptorDigest !== version.descriptorDigest) {
        quarantineReasons.push("invocation descriptor digest differs from current skill")
      }
      if (
        previous &&
        ["completed", "failed", "cancelled"].includes(previous.status) &&
        previous.status !== status &&
        !["completed", "failed", "cancelled"].includes(status)
      ) {
        quarantineReasons.push("late projection attempts to reopen a terminal invocation")
      }
      const projected: SkillInvocationProjection = Object.freeze({
        invocationId,
        skillId,
        taskId: candidate.taskId,
        runId: candidate.runId,
        sessionId: candidate.sessionId,
        toolCallId: candidate.sourceToolCallId,
        workerId: candidate.workerId,
        commandId: candidate.commandId ?? previous?.commandId,
        permissionId: candidate.permissionId ?? previous?.permissionId,
        registryRevision,
        descriptorDigest,
        status,
        mode: firstSkillText(
          [payload],
          ["mode", "invocation_mode", "execution_mode"],
        ) || previous?.mode,
        startedAt:
          optionalSkillTimestamp(payload.started_at ?? payload.startedAt) ??
          previous?.startedAt ??
          candidate.sourceUpdatedAt,
        completedAt,
        durationMs: optionalNumber(
          payload.duration_ms ?? payload.durationMs,
          previous?.durationMs,
        ),
        toolCalls: optionalInteger(
          payload.tool_calls ?? payload.toolCalls,
          previous?.toolCalls,
        ),
        inputTokens: optionalInteger(
          payload.input_tokens ?? payload.inputTokens,
          previous?.inputTokens,
        ),
        outputTokens: optionalInteger(
          payload.output_tokens ?? payload.outputTokens,
          previous?.outputTokens,
        ),
        costMicros: optionalInteger(
          payload.cost_micros ?? payload.costMicros,
          previous?.costMicros,
        ),
        resultSummary: firstSkillText(
          [result, payload],
          ["summary", "result_summary", "message"],
          previous?.resultSummary,
          32_768,
        ) || undefined,
        resultDigest:
          safeOptionalHash(
            firstSkillText(
              [result, payload],
              ["result_digest", "output_digest", "digest"],
            ),
          ) ?? previous?.resultDigest,
        resultArtifactIds: uniqueSkillStrings([
          ...(previous?.resultArtifactIds ?? []),
          ...candidate.artifactIds,
          ...stringValues(
            result.artifact_ids ??
            payload.result_artifact_ids ??
            payload.artifact_ids,
          ),
        ]),
        errorCode: firstSkillText(
          [failure, payload],
          ["error_code", "code"],
          candidate.errorCode ?? previous?.errorCode,
        ) || undefined,
        errorMessage: firstSkillText(
          [failure, payload],
          ["error_message", "message", "reason"],
          previous?.errorMessage,
          32_768,
        ) || undefined,
        eventIds,
        canonical: true,
        quarantined: quarantineReasons.length > 0,
        quarantineReason: uniqueSkillStrings(quarantineReasons).join("; ") || undefined,
      })
      invocations.set(invocationId, projected)
    }
  }
  return [...invocations.values()].sort((left, right) =>
    right.startedAt.localeCompare(left.startedAt) ||
    left.invocationId.localeCompare(right.invocationId))
}

function invocationPayloads(payload: JsonObject): JsonObject[] {
  const result: JsonObject[] = []
  for (const key of [
    "invocation",
    "skill_invocation",
    "invocation_result",
    "result",
  ]) {
    const record = skillRecord(payload[key])
    if (Object.keys(record).length) result.push({ ...payload, ...record })
  }
  for (const key of ["invocations", "skill_invocations", "results"]) {
    for (const record of skillRecords(payload[key])) result.push(record)
  }
  if (
    payload.invocation_id ||
    payload.invocationId ||
    /invok|execut|result|skill.*(?:fail|complet|start)/i.test(
      skillText(payload.event_type ?? payload.operation ?? payload.phase),
    )
  ) {
    result.push(payload)
  }
  return result
}

function invocationStatus(
  payload: JsonObject,
  candidate: SkillProjectionCandidate,
): SkillInvocationProjection["status"] {
  const value = classifySkillStatus(
    payload.status ??
    payload.phase ??
    payload.lifecycle ??
    payload.invocation_status ??
    candidate.errorCode ??
    "unknown",
  )
  if (/queue|pending|admitted/.test(value)) return "queued"
  if (/start|run|execut|stream/.test(value)) return "running"
  if (/background|detached/.test(value)) return "background"
  if (/complet|succeed|result|applied/.test(value)) return "completed"
  if (/cancel|abort|kill/.test(value)) return "cancelled"
  if (/fail|error|reject|deny/.test(value)) return "failed"
  return "unknown"
}

function safeInvocationId(
  payload: JsonObject,
  candidate: SkillProjectionCandidate,
): string | undefined {
  const raw = firstSkillText(
    [payload],
    ["invocation_id", "invocationId", "execution_id"],
  )
  try {
    return optionalSkillIdentity(raw, "skill invocation identity")
  } catch {
    return raw
      ? `invocation:${skillFingerprint([raw, candidate.sourceToolCallId]).slice("skill:".length)}`
      : undefined
  }
}

function groupCandidates(
  candidates: readonly SkillProjectionCandidate[],
): Map<string, SkillProjectionCandidate[]> {
  const grouped = new Map<string, SkillProjectionCandidate[]>()
  for (const candidate of candidates) {
    const skillId = candidateSkillId(candidate)
    if (!skillId) continue
    const values = grouped.get(skillId) ?? []
    values.push(candidate)
    grouped.set(skillId, values)
  }
  return new Map(
    [...grouped.entries()].sort(([left], [right]) => left.localeCompare(right)),
  )
}

function candidateSkillId(candidate: SkillProjectionCandidate): string | undefined {
  const nested = firstSkillRecord(
    [candidate.payload],
    ["skill", "descriptor", "skill_descriptor"],
  )
  const raw = firstSkillText(
    [candidate.payload, nested],
    ["skill_id", "skillId", "id", "skill_name", "skillName", "name"],
  )
  try {
    return optionalSkillIdentity(raw, "skill identity")
  } catch {
    return undefined
  }
}

function candidateRecords(
  candidates: readonly SkillProjectionCandidate[],
): JsonObject[] {
  const output: JsonObject[] = []
  for (const candidate of candidates) {
    const nested = [
      firstSkillRecord([candidate.payload], ["skill"]),
      firstSkillRecord([candidate.payload], ["descriptor", "skill_descriptor"]),
      candidate.payload,
      candidate.metadata,
    ]
    for (const record of nested) {
      if (Object.keys(record).length) output.push(record)
    }
  }
  return output
}

function selectDescriptorRecord(records: readonly JsonObject[]): JsonObject {
  return records.find((record) =>
    Boolean(
      record.body ||
      record.instructions ||
      record.descriptor_digest ||
      record.tool_scope ||
      record.resources,
    )) ?? records[0] ?? {}
}

function selectBody(records: readonly JsonObject[]): JsonValue | undefined {
  for (const record of records) {
    for (const key of [
      "body",
      "markdown",
      "instructions",
      "rendered_body",
      "content",
    ]) {
      if (typeof record[key] === "string") return record[key]
    }
  }
  return undefined
}

function compareCandidate(
  left: SkillProjectionCandidate,
  right: SkillProjectionCandidate,
): number {
  return (
    right.sourceSequence - left.sourceSequence ||
    right.sourceRevision - left.sourceRevision ||
    right.sourceUpdatedAt.localeCompare(left.sourceUpdatedAt) ||
    left.sourceId.localeCompare(right.sourceId)
  )
}

function normalizeAvailability(value: string): SkillAvailability {
  const normalized = classifySkillStatus(value)
  if (["available", "enabled", "ready", "active"].includes(normalized)) return "available"
  if (["disabled", "inactive"].includes(normalized)) return "disabled"
  if (["shadowed", "overridden"].includes(normalized)) return "shadowed"
  if (["missing", "deleted", "removed"].includes(normalized)) return "missing"
  return "invalid"
}

function safeOptionalHash(value: string): string | undefined {
  try {
    return optionalSkillHash(value)
  } catch {
    return undefined
  }
}

function stringValues(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.map((item) => skillText(item, "", 512)).filter(Boolean)
}

function optionalInteger(
  value: unknown,
  fallback?: number,
): number | undefined {
  if (value === undefined || value === null || value === "") return fallback
  return skillInteger(value, fallback ?? 0, 0, Number.MAX_SAFE_INTEGER)
}

function optionalNumber(
  value: unknown,
  fallback?: number,
): number | undefined {
  if (value === undefined || value === null || value === "") return fallback
  return skillNumber(value, fallback ?? 0, 0, Number.MAX_SAFE_INTEGER)
}

function invocationFingerprint(value: SkillInvocationProjection): unknown {
  return [
    value.invocationId,
    value.status,
    value.registryRevision,
    value.resultDigest,
    value.errorCode,
    value.eventIds,
    value.quarantined,
  ]
}
