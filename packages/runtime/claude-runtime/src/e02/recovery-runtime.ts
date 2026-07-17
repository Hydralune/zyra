import type { JsonObject, JsonValue } from "../contracts.ts";
import {
  canonicalize,
  cloneJson,
  constantTimeDigestEquals,
  deterministicId,
  digest,
  hashChain,
  normalizeIdentifier,
} from "./canonical.ts";
import type { E02RuntimeIdentity } from "./contracts.ts";

export type E02RecoverySource = "transition" | "execution" | "checkpoint" | "control";
export type E02RecoverySeverity = "low" | "medium" | "high" | "critical";
export type E02RecoveryIncidentPhase =
  | "open"
  | "diagnosed"
  | "plan_ready"
  | "executing"
  | "resolved"
  | "escalated";
export type E02RecoveryStepKind =
  | "inspect_receipt"
  | "verify_provider_state"
  | "confirm_effect"
  | "confirm_no_effect"
  | "retry_idempotent_delivery"
  | "request_operator_evidence";

export interface E02RecoveryEvidence extends JsonObject {
  evidenceId: string;
  incidentId: string;
  source: E02RecoverySource;
  kind: string;
  payload: JsonObject;
  payloadDigest: string;
  observedAt: string;
  observer: string;
  metadata: JsonObject;
  evidenceDigest: string;
}

export interface E02RecoveryDiagnosis extends JsonObject {
  diagnosisId: string;
  incidentId: string;
  severity: E02RecoverySeverity;
  effectState: "absent" | "recorded" | "indeterminate";
  replaySafe: boolean;
  requiresExternalEvidence: boolean;
  reasons: string[];
  evidenceIds: string[];
  diagnosedAt: string;
  diagnosisDigest: string;
}

export interface E02RecoveryStep extends JsonObject {
  stepId: string;
  incidentId: string;
  index: number;
  kind: E02RecoveryStepKind;
  status: "pending" | "running" | "committed" | "failed" | "skipped";
  requiresHumanAuthority: boolean;
  reexecutesExternalEffect: false;
  expectedEvidenceKind: string | null;
  startedAt: string | null;
  completedAt: string | null;
  result: JsonObject | null;
  error: JsonObject | null;
  stepDigest: string;
}

export interface E02RecoveryPlan extends JsonObject {
  planId: string;
  incidentId: string;
  diagnosisId: string;
  strategy: string;
  steps: E02RecoveryStep[];
  createdAt: string;
  completedAt: string | null;
  planDigest: string;
}

export interface E02RecoveryClaim extends JsonObject {
  claimId: string;
  incidentId: string;
  owner: string;
  runtimeEpoch: number;
  claimedAt: string;
  expiresAt: string;
  releasedAt: string | null;
  releaseReason: string | null;
  claimDigest: string;
}

export interface E02RecoveryIncident extends JsonObject {
  incidentId: string;
  source: E02RecoverySource;
  sourceId: string;
  sourcePhase: string;
  sourceDigest: string;
  phase: E02RecoveryIncidentPhase;
  severity: E02RecoverySeverity;
  summary: string;
  openedAt: string;
  updatedAt: string;
  resolvedAt: string | null;
  resolution: JsonObject | null;
  diagnosisId: string | null;
  planId: string | null;
  activeClaimId: string | null;
  evidenceIds: string[];
  priorIncidentHash: string;
  incidentHash: string;
  metadata: JsonObject;
}

export interface E02RecoverySourceRecord {
  source: E02RecoverySource;
  sourceId: string;
  sourcePhase: string;
  summary: string;
  payload: JsonObject;
  severity?: E02RecoverySeverity;
  metadata?: JsonObject;
}

export interface E02RecoveryRuntimeSnapshot {
  version: "zyra.e02-recovery/v1";
  runtime: E02RuntimeIdentity;
  sequence: number;
  previousIncidentHash: string;
  incidents: E02RecoveryIncident[];
  evidence: E02RecoveryEvidence[];
  diagnoses: E02RecoveryDiagnosis[];
  plans: E02RecoveryPlan[];
  claims: E02RecoveryClaim[];
  snapshotHash: string;
}

export class E02RecoveryRuntime {
  readonly runtime: E02RuntimeIdentity;
  private readonly now: () => Date;
  private sequence = 0;
  private previousIncidentHash = digest({ genesis: "zyra.e02-recovery/v1" });
  private readonly incidents = new Map<string, E02RecoveryIncident>();
  private readonly evidence = new Map<string, E02RecoveryEvidence>();
  private readonly diagnoses = new Map<string, E02RecoveryDiagnosis>();
  private readonly plans = new Map<string, E02RecoveryPlan>();
  private readonly claims = new Map<string, E02RecoveryClaim>();

  constructor(options: {
    runtime: E02RuntimeIdentity;
    now?: () => Date;
    snapshot?: E02RecoveryRuntimeSnapshot | null;
  }) {
    this.runtime = cloneJson(options.runtime);
    this.now = options.now ?? (() => new Date());
    if (options.snapshot) this.restore(options.snapshot);
  }

  observe(value: E02RecoverySourceRecord): E02RecoveryIncident {
    const source = value.source;
    const sourceId = value.sourceId.trim();
    const sourcePhase = normalizeIdentifier(value.sourcePhase);
    if (!sourceId || !sourcePhase || !value.summary.trim()) {
      throw recoveryError("e02_recovery_source_invalid", "recovery source requires id, phase, and summary");
    }
    const payload = canonicalize(value.payload) as JsonObject;
    const sourceDigest = digest({ source, source_id: sourceId, source_phase: sourcePhase, payload });
    const incidentId = deterministicId("e02-recovery-incident", {
      runtime_id: this.runtime.runtimeId,
      source,
      source_id: sourceId,
    }, 48);
    const prior = this.incidents.get(incidentId);
    if (prior) {
      if (prior.phase === "resolved") return cloneJson(prior);
      if (!constantTimeDigestEquals(prior.sourceDigest, sourceDigest)) {
        prior.sourceDigest = sourceDigest;
        prior.sourcePhase = sourcePhase;
        prior.summary = value.summary.trim();
        prior.updatedAt = this.timestamp();
        prior.metadata = { ...prior.metadata, ...canonicalize(value.metadata ?? {}) as JsonObject };
        prior.incidentHash = incidentDigest(prior);
      }
      this.addEvidence(incidentId, {
        source,
        kind: "source_observation",
        payload,
        observer: "E02RecoveryRuntime.observe",
        metadata: value.metadata ?? {},
      });
      return cloneJson(prior);
    }
    const openedAt = this.timestamp();
    const base = {
      source,
      sourceId,
      sourcePhase,
      sourceDigest,
      phase: "open" as const,
      severity: value.severity ?? inferSeverity(source, sourcePhase, payload),
      summary: value.summary.trim(),
      openedAt,
      updatedAt: openedAt,
      resolvedAt: null,
      resolution: null,
      diagnosisId: null,
      planId: null,
      activeClaimId: null,
      evidenceIds: [] as string[],
      priorIncidentHash: this.previousIncidentHash,
      metadata: canonicalize(value.metadata ?? {}) as JsonObject,
    };
    const incidentHash = hashChain(this.previousIncidentHash, { incidentId, ...base });
    const incident: E02RecoveryIncident = { incidentId, ...base, incidentHash };
    this.sequence += 1;
    this.previousIncidentHash = incidentHash;
    this.incidents.set(incidentId, incident);
    this.addEvidence(incidentId, {
      source,
      kind: "source_observation",
      payload,
      observer: "E02RecoveryRuntime.observe",
      metadata: value.metadata ?? {},
    });
    return cloneJson(incident);
  }

  observeAll(values: E02RecoverySourceRecord[]): E02RecoveryIncident[] {
    return values.map((value) => this.observe(value));
  }

  addEvidence(incidentId: string, input: {
    source: E02RecoverySource;
    kind: string;
    payload: JsonObject;
    observer: string;
    metadata?: JsonObject;
  }): E02RecoveryEvidence {
    const incident = this.requireIncident(incidentId);
    if (!input.kind.trim() || !input.observer.trim()) {
      throw recoveryError("e02_recovery_evidence_invalid", "recovery evidence requires kind and observer");
    }
    const payload = canonicalize(input.payload) as JsonObject;
    const payloadDigest = digest(payload);
    const observedAt = this.timestamp();
    const base = {
      incidentId,
      source: input.source,
      kind: normalizeIdentifier(input.kind),
      payload,
      payloadDigest,
      observedAt,
      observer: input.observer.trim(),
      metadata: canonicalize(input.metadata ?? {}) as JsonObject,
    };
    const evidenceId = deterministicId("e02-recovery-evidence", {
      incident_id: incidentId,
      source: input.source,
      kind: base.kind,
      payload_digest: payloadDigest,
      observer: base.observer,
    }, 48);
    const prior = this.evidence.get(evidenceId);
    if (prior) {
      if (!constantTimeDigestEquals(prior.payloadDigest, payloadDigest)) {
        throw recoveryError("e02_recovery_evidence_collision", `evidence ${evidenceId} collided`);
      }
      return cloneJson(prior);
    }
    const evidence: E02RecoveryEvidence = {
      evidenceId,
      ...base,
      evidenceDigest: digest({ evidenceId, ...base }),
    };
    this.evidence.set(evidenceId, evidence);
    if (!incident.evidenceIds.includes(evidenceId)) incident.evidenceIds.push(evidenceId);
    incident.updatedAt = observedAt;
    incident.incidentHash = incidentDigest(incident);
    return cloneJson(evidence);
  }

  diagnose(incidentId: string): E02RecoveryDiagnosis {
    const incident = this.requireIncident(incidentId);
    if (incident.phase === "resolved") {
      throw recoveryError("e02_recovery_incident_resolved", `incident ${incidentId} is already resolved`);
    }
    if (incident.diagnosisId) {
      const prior = this.diagnoses.get(incident.diagnosisId);
      if (prior) return cloneJson(prior);
    }
    const evidence = incident.evidenceIds.map((id) => this.evidence.get(id)).filter(Boolean) as E02RecoveryEvidence[];
    const effectState = inferEffectState(incident, evidence);
    const replaySafe = inferReplaySafety(incident, evidence, effectState);
    const requiresExternalEvidence = effectState === "indeterminate" && !replaySafe;
    const reasons = diagnosisReasons(incident, evidence, effectState, replaySafe);
    const diagnosedAt = this.timestamp();
    const base = {
      incidentId,
      severity: incident.severity,
      effectState,
      replaySafe,
      requiresExternalEvidence,
      reasons,
      evidenceIds: evidence.map((value) => value.evidenceId).sort(),
      diagnosedAt,
    };
    const diagnosisId = deterministicId("e02-recovery-diagnosis", base, 48);
    const diagnosis: E02RecoveryDiagnosis = {
      diagnosisId,
      ...base,
      diagnosisDigest: digest({ diagnosisId, ...base }),
    };
    this.diagnoses.set(diagnosisId, diagnosis);
    incident.diagnosisId = diagnosisId;
    incident.phase = "diagnosed";
    incident.updatedAt = diagnosedAt;
    incident.incidentHash = incidentDigest(incident);
    return cloneJson(diagnosis);
  }

  buildPlan(incidentId: string): E02RecoveryPlan {
    const incident = this.requireIncident(incidentId);
    const diagnosis = incident.diagnosisId
      ? this.requireDiagnosis(incident.diagnosisId)
      : this.diagnose(incidentId);
    if (incident.planId) {
      const prior = this.plans.get(incident.planId);
      if (prior) return cloneJson(prior);
    }
    const kinds = recoveryStepKinds(incident, diagnosis);
    const steps = kinds.map((kind, index) => recoveryStep(incidentId, index, kind));
    const strategy = diagnosis.effectState === "recorded"
      ? "confirm recorded effect from durable receipt"
      : diagnosis.effectState === "absent" && diagnosis.replaySafe
        ? "confirm no effect and permit upstream idempotent retry"
        : "obtain provider evidence before any reconciliation";
    const createdAt = this.timestamp();
    const planBase = {
      incidentId,
      diagnosisId: diagnosis.diagnosisId,
      strategy,
      steps,
      createdAt,
      completedAt: null,
    };
    const planId = deterministicId("e02-recovery-plan", {
      incident_id: incidentId,
      diagnosis_id: diagnosis.diagnosisId,
      strategy,
      step_kinds: kinds,
    }, 48);
    const plan: E02RecoveryPlan = {
      planId,
      ...planBase,
      planDigest: digest({ planId, ...planBase }),
    };
    this.plans.set(planId, plan);
    incident.planId = planId;
    incident.phase = "plan_ready";
    incident.updatedAt = createdAt;
    incident.incidentHash = incidentDigest(incident);
    return cloneJson(plan);
  }

  claim(incidentId: string, ownerValue: string, ttlMsValue = 300_000): E02RecoveryClaim {
    const incident = this.requireIncident(incidentId);
    if (incident.phase === "resolved" || incident.phase === "escalated") {
      throw recoveryError("e02_recovery_claim_terminal", `incident ${incidentId} is ${incident.phase}`);
    }
    const owner = normalizeIdentifier(ownerValue);
    if (!owner) throw recoveryError("e02_recovery_claim_owner_missing", "recovery claim requires owner");
    const active = incident.activeClaimId ? this.claims.get(incident.activeClaimId) : null;
    if (active && !active.releasedAt && Date.parse(active.expiresAt) > this.now().getTime()) {
      if (active.owner === owner) return cloneJson(active);
      throw recoveryError("e02_recovery_claim_conflict", `incident ${incidentId} is claimed by ${active.owner}`);
    }
    if (active && !active.releasedAt) this.releaseClaim(active.claimId, "expired_before_reclaim");
    const claimedAt = this.timestamp();
    const ttlMs = Math.max(1_000, Math.min(3_600_000, Math.trunc(ttlMsValue)));
    const expiresAt = new Date(Date.parse(claimedAt) + ttlMs).toISOString();
    const base = {
      incidentId,
      owner,
      runtimeEpoch: this.runtime.epoch,
      claimedAt,
      expiresAt,
      releasedAt: null,
      releaseReason: null,
    };
    const claimId = deterministicId("e02-recovery-claim", base, 48);
    const claim: E02RecoveryClaim = { claimId, ...base, claimDigest: digest({ claimId, ...base }) };
    this.claims.set(claimId, claim);
    incident.activeClaimId = claimId;
    incident.updatedAt = claimedAt;
    incident.incidentHash = incidentDigest(incident);
    return cloneJson(claim);
  }

  releaseClaim(claimId: string, reasonValue: string): E02RecoveryClaim {
    const claim = this.requireClaim(claimId);
    const reason = reasonValue.trim();
    if (!reason) throw recoveryError("e02_recovery_claim_release_reason_missing", "releasing claim requires reason");
    if (!claim.releasedAt) {
      claim.releasedAt = this.timestamp();
      claim.releaseReason = reason;
      claim.claimDigest = claimDigest(claim);
      const incident = this.requireIncident(claim.incidentId);
      if (incident.activeClaimId === claimId) incident.activeClaimId = null;
      incident.updatedAt = claim.releasedAt;
      incident.incidentHash = incidentDigest(incident);
    }
    return cloneJson(claim);
  }

  startStep(incidentId: string, stepId: string, claimId: string): E02RecoveryStep {
    const incident = this.requireIncident(incidentId);
    const plan = incident.planId ? this.requirePlan(incident.planId) : this.buildPlan(incidentId);
    this.assertActiveClaim(incident, claimId);
    const step = plan.steps.find((value) => value.stepId === stepId);
    if (!step) throw recoveryError("e02_recovery_step_not_found", `step ${stepId} was not found`);
    if (step.status === "running") return cloneJson(step);
    if (step.status !== "pending") {
      throw recoveryError("e02_recovery_step_terminal", `step ${stepId} is ${step.status}`);
    }
    const priorIncomplete = plan.steps.some((value) => value.index < step.index && value.status !== "committed" && value.status !== "skipped");
    if (priorIncomplete) {
      throw recoveryError("e02_recovery_step_order", `step ${stepId} cannot start before prior steps finish`);
    }
    step.status = "running";
    step.startedAt = this.timestamp();
    step.stepDigest = stepDigest(step);
    plan.planDigest = planDigest(plan);
    incident.phase = "executing";
    incident.updatedAt = step.startedAt;
    incident.incidentHash = incidentDigest(incident);
    return cloneJson(step);
  }

  completeStep(
    incidentId: string,
    stepId: string,
    claimId: string,
    resultValue: JsonObject,
  ): E02RecoveryStep {
    const incident = this.requireIncident(incidentId);
    this.assertActiveClaim(incident, claimId);
    const plan = this.requirePlan(incident.planId ?? "");
    const step = plan.steps.find((value) => value.stepId === stepId);
    if (!step) throw recoveryError("e02_recovery_step_not_found", `step ${stepId} was not found`);
    if (step.status === "committed") return cloneJson(step);
    if (step.status !== "running") throw recoveryError("e02_recovery_step_not_running", `step ${stepId} is ${step.status}`);
    const result = canonicalize(resultValue) as JsonObject;
    if (step.expectedEvidenceKind) {
      const evidenceKind = stringValue(result.evidence_kind);
      if (evidenceKind !== step.expectedEvidenceKind) {
        throw recoveryError("e02_recovery_step_evidence_missing", `step ${stepId} requires ${step.expectedEvidenceKind}`);
      }
    }
    step.status = "committed";
    step.completedAt = this.timestamp();
    step.result = result;
    step.error = null;
    step.stepDigest = stepDigest(step);
    if (result.evidence && typeof result.evidence === "object" && !Array.isArray(result.evidence)) {
      this.addEvidence(incidentId, {
        source: incident.source,
        kind: step.expectedEvidenceKind ?? "recovery_step_result",
        payload: result.evidence,
        observer: `recovery-step:${step.stepId}`,
        metadata: { claim_id: claimId },
      });
    }
    if (plan.steps.every((value) => value.status === "committed" || value.status === "skipped")) {
      plan.completedAt = step.completedAt;
    }
    plan.planDigest = planDigest(plan);
    incident.updatedAt = step.completedAt;
    incident.incidentHash = incidentDigest(incident);
    return cloneJson(step);
  }

  failStep(incidentId: string, stepId: string, claimId: string, error: unknown): E02RecoveryStep {
    const incident = this.requireIncident(incidentId);
    this.assertActiveClaim(incident, claimId);
    const plan = this.requirePlan(incident.planId ?? "");
    const step = plan.steps.find((value) => value.stepId === stepId);
    if (!step) throw recoveryError("e02_recovery_step_not_found", `step ${stepId} was not found`);
    if (step.status !== "running") throw recoveryError("e02_recovery_step_not_running", `step ${stepId} is ${step.status}`);
    step.status = "failed";
    step.completedAt = this.timestamp();
    step.error = errorObject(error);
    step.stepDigest = stepDigest(step);
    plan.planDigest = planDigest(plan);
    incident.severity = elevateSeverity(incident.severity);
    incident.phase = "escalated";
    incident.updatedAt = step.completedAt;
    incident.incidentHash = incidentDigest(incident);
    this.releaseClaim(claimId, "step_failed");
    return cloneJson(step);
  }

  resolve(incidentId: string, claimId: string, resolutionValue: JsonObject): E02RecoveryIncident {
    const incident = this.requireIncident(incidentId);
    this.assertActiveClaim(incident, claimId);
    const plan = this.requirePlan(incident.planId ?? "");
    if (!plan.completedAt || plan.steps.some((value) => value.status !== "committed" && value.status !== "skipped")) {
      throw recoveryError("e02_recovery_plan_incomplete", `incident ${incidentId} recovery plan is incomplete`);
    }
    const resolution = canonicalize(resolutionValue) as JsonObject;
    const outcome = stringValue(resolution.outcome);
    if (outcome !== "confirmed_effect" && outcome !== "confirmed_no_effect" && outcome !== "delivery_retried") {
      throw recoveryError("e02_recovery_resolution_invalid", `invalid recovery outcome ${outcome}`);
    }
    incident.phase = "resolved";
    incident.resolvedAt = this.timestamp();
    incident.updatedAt = incident.resolvedAt;
    incident.resolution = resolution;
    incident.incidentHash = incidentDigest(incident);
    this.releaseClaim(claimId, "incident_resolved");
    return cloneJson(incident);
  }

  incident(incidentId: string): E02RecoveryIncident | null {
    const value = this.incidents.get(incidentId);
    return value ? cloneJson(value) : null;
  }

  list(input: {
    source?: E02RecoverySource;
    phase?: E02RecoveryIncidentPhase;
    minimumSeverity?: E02RecoverySeverity;
    limit?: number;
  } = {}): E02RecoveryIncident[] {
    const limit = Math.max(1, Math.min(10_000, Math.trunc(input.limit ?? 100)));
    const severity = input.minimumSeverity ? severityRank(input.minimumSeverity) : 0;
    return [...this.incidents.values()]
      .filter((value) => !input.source || value.source === input.source)
      .filter((value) => !input.phase || value.phase === input.phase)
      .filter((value) => severityRank(value.severity) >= severity)
      .sort((left, right) => severityRank(right.severity) - severityRank(left.severity)
        || right.updatedAt.localeCompare(left.updatedAt))
      .slice(0, limit)
      .map(cloneJson);
  }

  unresolved(): E02RecoveryIncident[] {
    return [...this.incidents.values()]
      .filter((value) => value.phase !== "resolved")
      .sort((left, right) => severityRank(right.severity) - severityRank(left.severity)
        || left.openedAt.localeCompare(right.openedAt))
      .map(cloneJson);
  }

  plan(incidentId: string): E02RecoveryPlan | null {
    const incident = this.requireIncident(incidentId);
    if (!incident.planId) return null;
    return cloneJson(this.requirePlan(incident.planId));
  }

  evidenceFor(incidentId: string): E02RecoveryEvidence[] {
    const incident = this.requireIncident(incidentId);
    return incident.evidenceIds
      .map((id) => this.evidence.get(id))
      .filter(Boolean)
      .map((value) => cloneJson(value!));
  }

  verifyIncident(incidentId: string): JsonObject {
    const incident = this.requireIncident(incidentId);
    const errors: JsonObject[] = [];
    if (!incident.incidentHash) errors.push({ code: "incident_hash_missing" });
    if (incident.evidenceIds.some((id) => !this.evidence.has(id))) errors.push({ code: "evidence_missing" });
    if (incident.diagnosisId && !this.diagnoses.has(incident.diagnosisId)) errors.push({ code: "diagnosis_missing" });
    if (incident.planId && !this.plans.has(incident.planId)) errors.push({ code: "plan_missing" });
    if (incident.activeClaimId && !this.claims.has(incident.activeClaimId)) errors.push({ code: "claim_missing" });
    if (incident.phase === "resolved" && (!incident.resolvedAt || !incident.resolution)) {
      errors.push({ code: "resolution_incomplete" });
    }
    return { ok: errors.length === 0, incident_id: incidentId, phase: incident.phase, errors };
  }

  recommendNext(incidentId: string): JsonObject {
    const incident = this.requireIncident(incidentId);
    if (incident.phase === "resolved") {
      return {
        incident_id: incidentId,
        action: "none",
        reason: "incident is resolved",
        automatic_effect_replay: false,
      };
    }
    if (incident.phase === "open") {
      return {
        incident_id: incidentId,
        action: "diagnose",
        reason: "no diagnosis exists",
        requires_claim: false,
        automatic_effect_replay: false,
      };
    }
    if (incident.phase === "diagnosed") {
      return {
        incident_id: incidentId,
        action: "build_plan",
        reason: "diagnosis exists without an executable reconciliation plan",
        requires_claim: false,
        automatic_effect_replay: false,
      };
    }
    if (incident.phase === "plan_ready") {
      const plan = this.requirePlan(incident.planId ?? "");
      const next = plan.steps.find((step) => step.status === "pending");
      return {
        incident_id: incidentId,
        action: "claim_and_start_step",
        step_id: next?.stepId ?? null,
        step_kind: next?.kind ?? null,
        requires_claim: true,
        requires_human_authority: next?.requiresHumanAuthority ?? false,
        automatic_effect_replay: false,
      };
    }
    if (incident.phase === "executing") {
      const plan = this.requirePlan(incident.planId ?? "");
      const running = plan.steps.find((step) => step.status === "running");
      const pending = plan.steps.find((step) => step.status === "pending");
      return {
        incident_id: incidentId,
        action: running ? "complete_running_step" : pending ? "start_next_step" : "resolve",
        step_id: running?.stepId ?? pending?.stepId ?? null,
        step_kind: running?.kind ?? pending?.kind ?? null,
        requires_claim: true,
        automatic_effect_replay: false,
      };
    }
    return {
      incident_id: incidentId,
      action: "request_operator_review",
      reason: "incident was escalated after an unsuccessful recovery step",
      requires_claim: false,
      automatic_effect_replay: false,
    };
  }

  timeline(incidentId: string): JsonObject[] {
    const incident = this.requireIncident(incidentId);
    const values: JsonObject[] = [{
      at: incident.openedAt,
      kind: "incident_opened",
      phase: "open",
      source: incident.source,
      source_id: incident.sourceId,
      summary: incident.summary,
    }];
    for (const evidence of this.evidenceFor(incidentId)) {
      values.push({
        at: evidence.observedAt,
        kind: "evidence_observed",
        evidence_id: evidence.evidenceId,
        evidence_kind: evidence.kind,
        observer: evidence.observer,
        payload_digest: evidence.payloadDigest,
      });
    }
    if (incident.diagnosisId) {
      const diagnosis = this.requireDiagnosis(incident.diagnosisId);
      values.push({
        at: diagnosis.diagnosedAt,
        kind: "incident_diagnosed",
        diagnosis_id: diagnosis.diagnosisId,
        effect_state: diagnosis.effectState,
        replay_safe: diagnosis.replaySafe,
        severity: diagnosis.severity,
      });
    }
    if (incident.planId) {
      const plan = this.requirePlan(incident.planId);
      values.push({
        at: plan.createdAt,
        kind: "recovery_plan_created",
        plan_id: plan.planId,
        strategy: plan.strategy,
        step_count: plan.steps.length,
      });
      for (const step of plan.steps) {
        if (step.startedAt) {
          values.push({
            at: step.startedAt,
            kind: "recovery_step_started",
            step_id: step.stepId,
            step_kind: step.kind,
            index: step.index,
          });
        }
        if (step.completedAt) {
          values.push({
            at: step.completedAt,
            kind: "recovery_step_completed",
            step_id: step.stepId,
            step_kind: step.kind,
            status: step.status,
            result_digest: digest(step.result),
            error: step.error,
          });
        }
      }
    }
    for (const claim of this.claims.values()) {
      if (claim.incidentId !== incidentId) continue;
      values.push({
        at: claim.claimedAt,
        kind: "incident_claimed",
        claim_id: claim.claimId,
        owner: claim.owner,
        expires_at: claim.expiresAt,
      });
      if (claim.releasedAt) {
        values.push({
          at: claim.releasedAt,
          kind: "incident_claim_released",
          claim_id: claim.claimId,
          reason: claim.releaseReason,
        });
      }
    }
    if (incident.resolvedAt) {
      values.push({
        at: incident.resolvedAt,
        kind: "incident_resolved",
        resolution: incident.resolution,
      });
    }
    return values.sort((left, right) => stringValue(left.at).localeCompare(stringValue(right.at))
      || stringValue(left.kind).localeCompare(stringValue(right.kind)));
  }

  verifyGraph(): JsonObject {
    const errors: JsonObject[] = [];
    for (const incident of this.incidents.values()) {
      const verification = this.verifyIncident(incident.incidentId);
      if (verification.ok !== true) {
        errors.push({
          code: "incident_invalid",
          incident_id: incident.incidentId,
          verification,
        });
      }
      if (incident.diagnosisId) {
        const diagnosis = this.diagnoses.get(incident.diagnosisId);
        if (diagnosis && diagnosis.incidentId !== incident.incidentId) {
          errors.push({ code: "diagnosis_binding_mismatch", incident_id: incident.incidentId });
        }
      }
      if (incident.planId) {
        const plan = this.plans.get(incident.planId);
        if (plan && plan.incidentId !== incident.incidentId) {
          errors.push({ code: "plan_binding_mismatch", incident_id: incident.incidentId });
        }
        if (plan) {
          for (const [index, step] of plan.steps.entries()) {
            if (step.incidentId !== incident.incidentId || step.index !== index) {
              errors.push({
                code: "step_binding_mismatch",
                incident_id: incident.incidentId,
                step_id: step.stepId,
              });
            }
            if (step.reexecutesExternalEffect !== false) {
              errors.push({
                code: "external_effect_replay_enabled",
                incident_id: incident.incidentId,
                step_id: step.stepId,
              });
            }
          }
        }
      }
    }
    for (const evidence of this.evidence.values()) {
      if (!this.incidents.has(evidence.incidentId)) {
        errors.push({ code: "orphan_evidence", evidence_id: evidence.evidenceId });
      }
      const { evidenceDigest, ...base } = evidence;
      if (!constantTimeDigestEquals(digest(base), evidenceDigest)) {
        errors.push({ code: "evidence_digest_mismatch", evidence_id: evidence.evidenceId });
      }
    }
    return {
      ok: errors.length === 0,
      incident_count: this.incidents.size,
      evidence_count: this.evidence.size,
      diagnosis_count: this.diagnoses.size,
      plan_count: this.plans.size,
      claim_count: this.claims.size,
      errors,
    };
  }

  statistics(): JsonObject {
    const sourceCounts = new Map<E02RecoverySource, number>();
    const severityCounts = new Map<E02RecoverySeverity, number>();
    let totalResolutionMs = 0;
    let resolvedWithDuration = 0;
    for (const incident of this.incidents.values()) {
      sourceCounts.set(incident.source, (sourceCounts.get(incident.source) ?? 0) + 1);
      severityCounts.set(incident.severity, (severityCounts.get(incident.severity) ?? 0) + 1);
      if (incident.resolvedAt) {
        const duration = Date.parse(incident.resolvedAt) - Date.parse(incident.openedAt);
        if (Number.isFinite(duration) && duration >= 0) {
          totalResolutionMs += duration;
          resolvedWithDuration += 1;
        }
      }
    }
    const plans = [...this.plans.values()];
    const steps = plans.flatMap((plan) => plan.steps);
    return {
      source_counts: Object.fromEntries(sourceCounts),
      severity_counts: Object.fromEntries(severityCounts),
      resolved_count: [...this.incidents.values()].filter((value) => value.phase === "resolved").length,
      escalated_count: [...this.incidents.values()].filter((value) => value.phase === "escalated").length,
      average_resolution_ms: resolvedWithDuration > 0 ? Math.round(totalResolutionMs / resolvedWithDuration) : null,
      plan_count: plans.length,
      step_count: steps.length,
      committed_step_count: steps.filter((value) => value.status === "committed").length,
      failed_step_count: steps.filter((value) => value.status === "failed").length,
      steps_reexecuting_external_effects: steps.filter((value) => value.reexecutesExternalEffect !== false).length,
    };
  }

  health(): JsonObject {
    const unresolved = this.unresolved();
    const phaseCounts = new Map<E02RecoveryIncidentPhase, number>();
    for (const incident of this.incidents.values()) {
      phaseCounts.set(incident.phase, (phaseCounts.get(incident.phase) ?? 0) + 1);
    }
    return {
      canonical_owner: "typescript",
      runtime_epoch: this.runtime.epoch,
      sequence: this.sequence,
      incident_count: this.incidents.size,
      unresolved_count: unresolved.length,
      critical_unresolved_count: unresolved.filter((value) => value.severity === "critical").length,
      evidence_count: this.evidence.size,
      diagnosis_count: this.diagnoses.size,
      plan_count: this.plans.size,
      active_claim_count: [...this.claims.values()].filter((value) => !value.releasedAt && Date.parse(value.expiresAt) > this.now().getTime()).length,
      phase_counts: Object.fromEntries(phaseCounts),
      graph_ok: this.verifyGraph().ok,
      previous_incident_hash: this.previousIncidentHash,
      automatic_effect_replay: false,
      scheduler_route_owner: false,
      python_recovery_fallback: false,
    };
  }

  snapshot(): E02RecoveryRuntimeSnapshot {
    const withoutHash = {
      version: "zyra.e02-recovery/v1" as const,
      runtime: cloneJson(this.runtime),
      sequence: this.sequence,
      previousIncidentHash: this.previousIncidentHash,
      incidents: [...this.incidents.values()].sort((left, right) => left.openedAt.localeCompare(right.openedAt)).map(cloneJson),
      evidence: [...this.evidence.values()].sort((left, right) => left.observedAt.localeCompare(right.observedAt)).map(cloneJson),
      diagnoses: [...this.diagnoses.values()].sort((left, right) => left.diagnosedAt.localeCompare(right.diagnosedAt)).map(cloneJson),
      plans: [...this.plans.values()].sort((left, right) => left.createdAt.localeCompare(right.createdAt)).map(cloneJson),
      claims: [...this.claims.values()].sort((left, right) => left.claimedAt.localeCompare(right.claimedAt)).map(cloneJson),
    };
    return { ...withoutHash, snapshotHash: digest(withoutHash) };
  }

  private assertActiveClaim(incident: E02RecoveryIncident, claimId: string): void {
    const claim = this.requireClaim(claimId);
    if (claim.incidentId !== incident.incidentId || incident.activeClaimId !== claimId) {
      throw recoveryError("e02_recovery_claim_binding", `claim ${claimId} does not own incident ${incident.incidentId}`);
    }
    if (claim.releasedAt) throw recoveryError("e02_recovery_claim_released", `claim ${claimId} was released`);
    if (claim.runtimeEpoch !== this.runtime.epoch) throw recoveryError("e02_recovery_claim_epoch", `claim ${claimId} belongs to another epoch`);
    if (Date.parse(claim.expiresAt) <= this.now().getTime()) {
      this.releaseClaim(claimId, "expired");
      throw recoveryError("e02_recovery_claim_expired", `claim ${claimId} expired`);
    }
  }

  private requireIncident(incidentId: string): E02RecoveryIncident {
    const value = this.incidents.get(incidentId);
    if (!value) throw recoveryError("e02_recovery_incident_not_found", `incident ${incidentId} was not found`);
    return value;
  }

  private requireDiagnosis(diagnosisId: string): E02RecoveryDiagnosis {
    const value = this.diagnoses.get(diagnosisId);
    if (!value) throw recoveryError("e02_recovery_diagnosis_not_found", `diagnosis ${diagnosisId} was not found`);
    return value;
  }

  private requirePlan(planId: string): E02RecoveryPlan {
    const value = this.plans.get(planId);
    if (!value) throw recoveryError("e02_recovery_plan_not_found", `recovery plan ${planId} was not found`);
    return value;
  }

  private requireClaim(claimId: string): E02RecoveryClaim {
    const value = this.claims.get(claimId);
    if (!value) throw recoveryError("e02_recovery_claim_not_found", `recovery claim ${claimId} was not found`);
    return value;
  }

  private restore(snapshotValue: E02RecoveryRuntimeSnapshot): void {
    const snapshot = cloneJson(snapshotValue);
    if (snapshot.version !== "zyra.e02-recovery/v1") {
      throw recoveryError("e02_recovery_snapshot_version", `unsupported recovery snapshot ${snapshot.version}`);
    }
    const { snapshotHash, ...withoutHash } = snapshot;
    if (!constantTimeDigestEquals(digest(withoutHash), snapshotHash)) {
      throw recoveryError("e02_recovery_snapshot_digest", "recovery snapshot digest mismatch");
    }
    this.assertRuntimeBinding(snapshot.runtime);
    if (this.runtime.epoch <= snapshot.runtime.epoch) {
      throw recoveryError("e02_recovery_snapshot_epoch", "recovery restore target epoch must advance");
    }
    for (const value of snapshot.evidence) this.evidence.set(value.evidenceId, cloneJson(value));
    for (const value of snapshot.diagnoses) this.diagnoses.set(value.diagnosisId, cloneJson(value));
    for (const value of snapshot.plans) {
      const plan = cloneJson(value);
      for (const step of plan.steps) {
        if (step.status === "running") {
          step.status = "failed";
          step.completedAt = this.timestamp();
          step.error = { code: "restore_epoch_fence", message: "recovery step fenced on restore" };
          step.stepDigest = stepDigest(step);
        }
      }
      plan.planDigest = planDigest(plan);
      this.plans.set(plan.planId, plan);
    }
    for (const value of snapshot.claims) {
      const claim = cloneJson(value);
      if (!claim.releasedAt) {
        claim.releasedAt = this.timestamp();
        claim.releaseReason = "restore_epoch_fence";
        claim.claimDigest = claimDigest(claim);
      }
      this.claims.set(claim.claimId, claim);
    }
    for (const value of snapshot.incidents) {
      const incident = cloneJson(value);
      if (this.incidents.has(incident.incidentId)) throw recoveryError("e02_recovery_snapshot_duplicate", `duplicate incident ${incident.incidentId}`);
      incident.activeClaimId = null;
      if (incident.phase === "executing") {
        incident.phase = "escalated";
        incident.severity = elevateSeverity(incident.severity);
        incident.updatedAt = this.timestamp();
        incident.metadata = { ...incident.metadata, restore_epoch_fence: true };
        incident.incidentHash = incidentDigest(incident);
      }
      this.incidents.set(incident.incidentId, incident);
    }
    this.sequence = snapshot.sequence;
    this.previousIncidentHash = snapshot.previousIncidentHash;
  }

  private assertRuntimeBinding(runtime: E02RuntimeIdentity): void {
    if (
      runtime.runtimeId !== this.runtime.runtimeId
      || runtime.runId !== this.runtime.runId
      || runtime.taskId !== this.runtime.taskId
      || runtime.sessionId !== this.runtime.sessionId
      || runtime.workerRequestId !== this.runtime.workerRequestId
    ) {
      throw recoveryError("e02_recovery_snapshot_binding", "recovery snapshot belongs to another runtime binding");
    }
  }

  private timestamp(): string {
    return this.now().toISOString();
  }
}

function recoveryStep(incidentId: string, index: number, kind: E02RecoveryStepKind): E02RecoveryStep {
  const base = {
    incidentId,
    index,
    kind,
    status: "pending" as const,
    requiresHumanAuthority: kind === "confirm_effect" || kind === "confirm_no_effect" || kind === "request_operator_evidence",
    reexecutesExternalEffect: false as const,
    expectedEvidenceKind: kind === "verify_provider_state" ? "provider_state" : null,
    startedAt: null,
    completedAt: null,
    result: null,
    error: null,
  };
  const stepId = deterministicId("e02-recovery-step", { incident_id: incidentId, index, kind }, 48);
  return { stepId, ...base, stepDigest: digest({ stepId, ...base }) };
}

function recoveryStepKinds(incident: E02RecoveryIncident, diagnosis: E02RecoveryDiagnosis): E02RecoveryStepKind[] {
  const values: E02RecoveryStepKind[] = ["inspect_receipt"];
  if (diagnosis.effectState === "indeterminate") values.push("verify_provider_state");
  if (diagnosis.requiresExternalEvidence) values.push("request_operator_evidence");
  if (diagnosis.effectState === "recorded") values.push("confirm_effect");
  else if (diagnosis.effectState === "absent") values.push("confirm_no_effect");
  else values.push("confirm_effect", "confirm_no_effect");
  if (incident.source === "checkpoint" && diagnosis.replaySafe) values.push("retry_idempotent_delivery");
  return values;
}

function inferEffectState(incident: E02RecoveryIncident, evidence: E02RecoveryEvidence[]): E02RecoveryDiagnosis["effectState"] {
  for (const item of evidence) {
    const payload = item.payload;
    const candidates = [payload.effectReceipt, payload.effect_receipt, payload.receipt];
    for (const candidate of candidates) {
      if (candidate !== null && typeof candidate === "object" && !Array.isArray(candidate)) {
        if (
          candidate.ok === true
          || typeof candidate.receipt_id === "string"
          || typeof candidate.providerReceiptId === "string"
          || typeof candidate.provider_receipt_id === "string"
        ) return "recorded";
      }
    }
    if (payload.confirm_no_effect === true || payload.effect_started === false) return "absent";
  }
  if (incident.sourcePhase.includes("prepared") || incident.sourcePhase.includes("authorized")) return "absent";
  return "indeterminate";
}

function inferReplaySafety(
  incident: E02RecoveryIncident,
  evidence: E02RecoveryEvidence[],
  effectState: E02RecoveryDiagnosis["effectState"],
): boolean {
  if (effectState === "recorded") return false;
  if (incident.source === "checkpoint") return true;
  const text = JSON.stringify(evidence.map((value) => value.payload)).toLowerCase();
  return text.includes('"idempotent":true') || text.includes('"nonidempotent":false') || effectState === "absent";
}

function diagnosisReasons(
  incident: E02RecoveryIncident,
  evidence: E02RecoveryEvidence[],
  effectState: E02RecoveryDiagnosis["effectState"],
  replaySafe: boolean,
): string[] {
  const reasons = [`source=${incident.source}`, `phase=${incident.sourcePhase}`, `effect_state=${effectState}`];
  reasons.push(replaySafe ? "idempotent replay may be considered after reconciliation" : "external effect must not be replayed");
  if (evidence.length === 0) reasons.push("no durable evidence was attached");
  if (incident.severity === "critical") reasons.push("critical incident requires explicit authority");
  return reasons;
}

function inferSeverity(source: E02RecoverySource, phase: string, payload: JsonObject): E02RecoverySeverity {
  if (source === "execution" || phase.includes("effect")) return "critical";
  if (source === "control") return "high";
  if (source === "checkpoint") return payload.non_idempotent === true ? "critical" : "medium";
  return "high";
}

function severityRank(value: E02RecoverySeverity): number {
  if (value === "critical") return 4;
  if (value === "high") return 3;
  if (value === "medium") return 2;
  return 1;
}

function elevateSeverity(value: E02RecoverySeverity): E02RecoverySeverity {
  if (value === "low") return "medium";
  if (value === "medium") return "high";
  return "critical";
}

function incidentDigest(value: E02RecoveryIncident): string {
  const { incidentHash: _ignored, ...base } = value;
  return digest(base);
}

function stepDigest(value: E02RecoveryStep): string {
  const { stepDigest: _ignored, ...base } = value;
  return digest(base);
}

function planDigest(value: E02RecoveryPlan): string {
  const { planDigest: _ignored, ...base } = value;
  return digest(base);
}

function claimDigest(value: E02RecoveryClaim): string {
  const { claimDigest: _ignored, ...base } = value;
  return digest(base);
}

function stringValue(value: JsonValue | undefined): string {
  return typeof value === "string" ? value : "";
}

function errorObject(error: unknown): JsonObject {
  if (error instanceof Error) {
    const value = error as Error & { code?: unknown; details?: unknown };
    return {
      name: value.name,
      message: value.message,
      code: typeof value.code === "string" ? value.code : "recovery_step_failed",
      details: canonicalize(value.details ?? {}) as JsonValue,
    };
  }
  return { name: "Error", message: String(error), code: "recovery_step_failed", details: {} };
}

function recoveryError(code: string, message: string, details: JsonObject = {}): Error {
  return Object.assign(new Error(message), {
    name: "E02RecoveryRuntimeError",
    code,
    details: cloneJson(details),
  });
}
