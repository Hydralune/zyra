import {
  SKILL_OUTCOME_PROTOCOL,
  canonicalize,
  cloneJson,
  contractError,
  digest,
  normalizeIdentity,
  stableId,
  uniqueStrings,
  type JsonObject,
  type OutcomeEvidenceReference,
  type PolicyDecisionReference,
  type ReuseCondition,
  type RuntimeIdentity,
  type SkillInvocationOutcomeInput,
  type SkillInvocationStatus,
  type SkillVersionReference,
} from "./contracts.ts";

export interface SkillCoordinatorOutcomeEnvelope {
  protocol: "zyra.skill-coordinator-outcome/v1";
  invocation: JsonObject;
  provenance: JsonObject;
  policy: JsonObject;
  composition: JsonObject;
  sourceRecordDigest: string;
  metadata: JsonObject;
}

export interface RuntimeSkillToolResult {
  toolCallId: string;
  toolName: string;
  ok: boolean;
  summary: string;
  output: JsonObject;
  artifacts: JsonObject[];
  error: string | null;
  metadata: JsonObject;
  identity: RuntimeIdentity;
  eventSequence: number;
  occurredAt: string;
}

export class SkillCoordinatorOutcomeAdapter {
  adapt(value: RuntimeSkillToolResult): SkillInvocationOutcomeInput | null {
    if (value.toolName !== "skill") return null;
    const envelopeValue = object(value.output.outcome_reference ?? value.output.skill_outcome_reference);
    if (String(envelopeValue.protocol ?? "") !== "zyra.skill-coordinator-outcome/v1") {
      throw contractError("skill_outcome_reference_missing", "03C skill result is missing its immutable outcome reference", {
        tool_call_id: value.toolCallId,
        output_keys: Object.keys(value.output).sort(),
      });
    }
    const invocation = object(envelopeValue.invocation);
    const provenance = object(envelopeValue.provenance);
    const policyValue = object(envelopeValue.policy);
    const composition = object(envelopeValue.composition);
    const identity = normalizeIdentity(value.identity);
    const invocationId = required(invocation.invocation_id ?? invocation.invocationId, "skill invocation id");
    const status = normalizeStatus(invocation.status, value.ok, value.error);
    const version = versionReference(provenance);
    const policy = policyReference(policyValue);
    const artifacts = value.artifacts.length > 0
      ? value.artifacts.map(cloneJson)
      : objectArray(invocation.artifacts);
    const evidence = buildEvidence({
      result: value,
      invocationId,
      invocation,
      envelope: envelopeValue,
      artifacts,
      policy,
    });
    const reuseConditions = reuseConditionsFrom(envelopeValue, evidence, version, value);
    const failure = object(invocation.failure);
    const successReason = status === "completed"
      ? value.summary.trim() || `${version.skillName} completed`
      : null;
    const failureReason = status === "failed" || status === "cancelled"
      ? value.error?.trim() || String(failure.message ?? "skill invocation failed").trim()
      : null;
    const sourceRecordDigest = digestValue(envelopeValue.source_record_digest ?? envelopeValue.sourceRecordDigest, "skill source record digest");
    const outputDigest = digest(canonicalize(invocation.output ?? null));
    const input: SkillInvocationOutcomeInput = {
      protocol: SKILL_OUTCOME_PROTOCOL,
      identity,
      invocationId,
      toolCallId: value.toolCallId,
      compositionId: required(composition.composition_id ?? composition.compositionId, "skill composition id"),
      status,
      version,
      policy,
      evidence,
      artifacts,
      outputDigest,
      summary: value.summary.trim() || `${version.skillName} ${status}`,
      successReason,
      failureReason,
      reuseConditions,
      inputTokens: integer(invocation.input_tokens ?? invocation.inputTokens, 0),
      outputTokens: integer(invocation.output_tokens ?? invocation.outputTokens, 0),
      toolCalls: integer(invocation.tool_calls ?? invocation.toolCalls, 0),
      costMicros: integer(invocation.cost_micros ?? invocation.costMicros, 0),
      startedAt: timestampValue(invocation.started_at ?? invocation.startedAt, value.occurredAt),
      completedAt: status === "background" ? null : timestampValue(invocation.completed_at ?? invocation.completedAt, value.occurredAt),
      sourceRecordDigest,
      metadata: {
        ...object(envelopeValue.metadata),
        // Evidence emitted by the current 03C resolution. It is never treated
        // as an executable descriptor; resume must revalidate it before reuse.
        current_skill_authority: cloneJson(object(value.output.current_authority)),
        runtime_event_sequence: value.eventSequence,
        runtime_tool_result_ok: value.ok,
        "03c_outcome_reference": true,
        static_skill_document_used: false,
        skill_cache_invocation_allowed: false,
      },
    };
    return input;
  }
}

function versionReference(value: JsonObject): SkillVersionReference {
  const resourcesValue = object(value.resource_digests ?? value.resourceDigests);
  const resourceDigests: Record<string, string> = {};
  for (const [path, valueDigest] of Object.entries(resourcesValue)) {
    const text = String(valueDigest ?? "").replace(/^sha256:/, "").toLowerCase();
    if (!/^[0-9a-f]{64}$/.test(text)) throw contractError("skill_outcome_resource_digest", `03C skill resource ${path} lacks an immutable digest`);
    resourceDigests[path] = text;
  }
  return {
    skillId: required(value.skill_id ?? value.skillId, "skill id"),
    skillName: required(value.skill_name ?? value.skillName, "skill name"),
    registryRevision: positiveInteger(value.registry_revision ?? value.registryRevision, "skill registry revision"),
    descriptorDigest: digestValue(value.descriptor_digest ?? value.descriptorDigest, "skill descriptor digest"),
    bodyDigest: digestValue(value.body_digest ?? value.bodyDigest, "skill body digest"),
    resourceDigests,
    sourceRevision: required(value.source_revision ?? value.sourceRevision, "skill source revision"),
  };
}

function policyReference(value: JsonObject): PolicyDecisionReference {
  const effect = String(value.effect ?? "allow");
  if (effect !== "allow" && effect !== "deny" && effect !== "ask") throw contractError("skill_outcome_policy_effect", `invalid 03C skill policy effect ${effect}`);
  return {
    decisionId: required(value.decision_id ?? value.decisionId, "skill policy decision id"),
    effect,
    policyRevision: required(value.policy_revision ?? value.policyRevision, "skill policy revision"),
    policyDigest: digestValue(value.policy_digest ?? value.policyDigest, "skill policy digest"),
    requestedTools: stringArray(value.requested_tools ?? value.requestedTools),
    effectiveTools: stringArray(value.effective_tools ?? value.effectiveTools),
    deniedTools: stringArray(value.denied_tools ?? value.deniedTools),
    approvalId: optional(value.approval_id ?? value.approvalId),
  };
}

function buildEvidence(input: {
  result: RuntimeSkillToolResult;
  invocationId: string;
  invocation: JsonObject;
  envelope: JsonObject;
  artifacts: JsonObject[];
  policy: PolicyDecisionReference;
}): OutcomeEvidenceReference[] {
  const values: OutcomeEvidenceReference[] = [];
  let sequence = Math.max(0, input.result.eventSequence);
  values.push({
    evidenceId: stableId("skill-evidence", input.invocationId, "tool-result", input.result.toolCallId),
    kind: "tool_result",
    sourceId: input.result.toolCallId,
    sourceDigest: digest({ ok: input.result.ok, summary: input.result.summary, output: input.result.output, error: input.result.error }),
    sequence,
    occurredAt: input.result.occurredAt,
    toolName: "skill",
    toolCallId: input.result.toolCallId,
    artifactId: null,
    trustedRuntime: true,
    metadata: {
      runtime_event_sequence: input.result.eventSequence,
      canonical_owner: "03C SkillCoordinator",
      invocation_id: input.invocationId,
    },
  });
  sequence += 1;
  values.push({
    evidenceId: stableId("skill-evidence", input.invocationId, "permission", input.policy.decisionId),
    kind: "permission",
    sourceId: input.policy.decisionId,
    sourceDigest: input.policy.policyDigest,
    sequence,
    occurredAt: input.result.occurredAt,
    toolName: "skill",
    toolCallId: input.result.toolCallId,
    artifactId: null,
    trustedRuntime: true,
    metadata: {
      effect: input.policy.effect,
      policy_revision: input.policy.policyRevision,
      effective_tools: input.policy.effectiveTools,
      denied_tools: input.policy.deniedTools,
    },
  });
  const stepSummaries = stepSummariesFrom(input.invocation);
  for (let index = 0; index < stepSummaries.length; index += 1) {
    const summary = stepSummaries[index];
    const [toolCallId, ...rest] = summary.split(":");
    sequence += 1;
    values.push({
      evidenceId: stableId("skill-evidence", input.invocationId, "child-tool", index, summary),
      kind: "tool_call",
      sourceId: toolCallId || `${input.invocationId}:child-tool:${index}`,
      sourceDigest: digest(summary),
      sequence,
      occurredAt: input.result.occurredAt,
      toolName: childToolName(rest.join(":")),
      toolCallId: toolCallId || null,
      artifactId: null,
      trustedRuntime: true,
      metadata: { summary, child_execution: true, ordinal: index },
    });
  }
  for (let index = 0; index < input.artifacts.length; index += 1) {
    const artifact = input.artifacts[index];
    const artifactId = required(artifact.artifact_id ?? artifact.artifactId ?? artifact.id, "skill artifact id");
    sequence += 1;
    values.push({
      evidenceId: stableId("skill-evidence", input.invocationId, "artifact", artifactId),
      kind: "artifact",
      sourceId: artifactId,
      sourceDigest: digest(artifact),
      sequence,
      occurredAt: input.result.occurredAt,
      toolName: null,
      toolCallId: null,
      artifactId,
      trustedRuntime: true,
      metadata: { artifact_kind: String(artifact.kind ?? "unknown"), ordinal: index },
    });
  }
  sequence += 1;
  values.push({
    evidenceId: stableId("skill-evidence", input.invocationId, "event", input.result.eventSequence),
    kind: "event",
    sourceId: `runtime-event:${input.result.eventSequence}`,
    sourceDigest: digest({ envelope: input.envelope, sequence: input.result.eventSequence }),
    sequence,
    occurredAt: input.result.occurredAt,
    toolName: "skill",
    toolCallId: input.result.toolCallId,
    artifactId: null,
    trustedRuntime: true,
    metadata: { event_type: "tool_call_completed", event_sequence: input.result.eventSequence },
  });
  return values;
}

function reuseConditionsFrom(
  envelope: JsonObject,
  evidence: OutcomeEvidenceReference[],
  version: SkillVersionReference,
  result: RuntimeSkillToolResult,
): ReuseCondition[] {
  const declared = objectArray(envelope.reuse_conditions ?? envelope.reuseConditions);
  if (declared.length > 0) {
    return declared.map((value, index) => {
      const sourceEvidenceIds = stringArray(value.source_evidence_ids ?? value.sourceEvidenceIds);
      return {
        conditionId: String(value.condition_id ?? value.conditionId ?? stableId("reuse-condition", version.skillId, index, value)),
        kind: normalizeConditionKind(value.kind),
        operator: normalizeConditionOperator(value.operator),
        key: required(value.key, "reuse condition key"),
        value: canonicalize(value.value ?? null),
        required: value.required !== false,
        sourceEvidenceIds: sourceEvidenceIds.length > 0 ? sourceEvidenceIds : [evidence[0].evidenceId],
      };
    });
  }
  const conditions: ReuseCondition[] = [{
    conditionId: stableId("reuse-condition", version.skillId, "version"),
    kind: "custom",
    operator: "equals",
    key: "skill.descriptor_digest",
    value: version.descriptorDigest,
    required: true,
    sourceEvidenceIds: [evidence[0].evidenceId],
  }];
  const workspace = String(result.metadata.workspace_id ?? result.metadata.workspace_root ?? "").trim();
  if (workspace) conditions.push({
    conditionId: stableId("reuse-condition", version.skillId, "workspace", workspace),
    kind: "workspace",
    operator: "equals",
    key: "workspace.id",
    value: workspace,
    required: false,
    sourceEvidenceIds: [evidence[0].evidenceId],
  });
  const childTools = uniqueStrings(evidence.filter((item) => item.kind === "tool_call").map((item) => item.toolName).filter(Boolean));
  for (const tool of childTools) conditions.push({
    conditionId: stableId("reuse-condition", version.skillId, "tool", tool),
    kind: "tool",
    operator: "contains",
    key: "tools.available",
    value: tool,
    required: true,
    sourceEvidenceIds: evidence.filter((item) => item.toolName === tool).map((item) => item.evidenceId),
  });
  return conditions;
}

function normalizeStatus(value: unknown, ok: boolean, error: string | null): SkillInvocationStatus {
  const status = String(value ?? "").trim();
  if (["completed", "failed", "cancelled", "background"].includes(status)) return status as SkillInvocationStatus;
  if (ok) return "completed";
  return error === "user_cancelled" || error === "aborted" ? "cancelled" : "failed";
}

function stepSummariesFrom(invocation: JsonObject): string[] {
  const output = object(invocation.output);
  const direct = output.step_summaries ?? output.stepSummaries;
  return stringArray(direct);
}

function childToolName(summary: string): string | null {
  const match = summary.match(/(?:tool[=:\s]+)([A-Za-z0-9_.:-]+)/i);
  return match?.[1] ?? null;
}

function object(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? cloneJson(value as JsonObject) : {};
}

function objectArray(value: unknown): JsonObject[] {
  return Array.isArray(value) ? value.filter((item) => item && typeof item === "object" && !Array.isArray(item)).map((item) => cloneJson(item as JsonObject)) : [];
}

function stringArray(value: unknown): string[] {
  return uniqueStrings(Array.isArray(value) ? value : []);
}

function required(value: unknown, label: string): string {
  const text = String(value ?? "").trim();
  if (!text) throw contractError("skill_outcome_adapter_required", `${label} is required`);
  return text;
}

function optional(value: unknown): string | null {
  return String(value ?? "").trim() || null;
}

function digestValue(value: unknown, label: string): string {
  const text = required(value, label).replace(/^sha256:/, "").toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(text)) throw contractError("skill_outcome_adapter_digest", `${label} must be sha256`);
  return text;
}

function integer(value: unknown, fallback: number): number {
  const number = Number(value);
  return Number.isSafeInteger(number) && number >= 0 ? number : fallback;
}

function positiveInteger(value: unknown, label: string): number {
  const number = integer(value, 0);
  if (number < 1) throw contractError("skill_outcome_adapter_integer", `${label} must be positive`);
  return number;
}

function timestampValue(value: unknown, fallback: string): string {
  const text = String(value ?? fallback).trim();
  const parsed = Date.parse(text);
  if (!Number.isFinite(parsed)) throw contractError("skill_outcome_adapter_timestamp", "03C skill outcome timestamp is invalid");
  return new Date(parsed).toISOString();
}

function normalizeConditionKind(value: unknown): ReuseCondition["kind"] {
  const text = String(value ?? "custom");
  if (["workspace", "language", "tool", "artifact", "failure", "goal", "provider", "custom"].includes(text)) return text as ReuseCondition["kind"];
  return "custom";
}

function normalizeConditionOperator(value: unknown): ReuseCondition["operator"] {
  const text = String(value ?? "equals");
  if (["equals", "contains", "matches", "present", "absent", "in"].includes(text)) return text as ReuseCondition["operator"];
  return "equals";
}
