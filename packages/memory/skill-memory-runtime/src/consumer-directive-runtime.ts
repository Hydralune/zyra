import {
  cloneJson,
  contractError,
  digest,
  nowIso,
  stableId,
  uniqueStrings,
  type JsonObject,
  type JsonValue,
  type MemorySignal,
  type RuntimeIdentity,
} from "./contracts.ts";

export const MEMORY_CONSUMER_DIRECTIVE_PROTOCOL = "zyra.memory-consumer-directive/v1" as const;

export type MemoryConsumerKind = "routing" | "recovery" | "context" | "control" | "audit";
export type MemoryDirectiveAction =
  | "observe"
  | "retry"
  | "reroute"
  | "replan"
  | "restore_context"
  | "invalidate_skill_reference"
  | "require_current_authority"
  | "quarantine_procedure"
  | "no_action";
export type MemoryDirectivePriority = "low" | "normal" | "high" | "critical";
export type MemoryDirectiveState = "pending" | "claimed" | "acknowledged" | "released" | "expired";

export interface MemoryConsumerDirective {
  directiveId: string;
  protocol: typeof MEMORY_CONSUMER_DIRECTIVE_PROTOCOL;
  identity: RuntimeIdentity;
  sourceSignalId: string;
  sourceSignalKind: string;
  consumer: MemoryConsumerKind;
  action: MemoryDirectiveAction;
  priority: MemoryDirectivePriority;
  state: MemoryDirectiveState;
  aggregateId: string;
  causationId: string;
  correlationId: string;
  reason: string;
  retryClass: string | null;
  routeHints: string[];
  forbiddenRoutes: string[];
  requiredEvidenceIds: string[];
  requiredArtifactIds: string[];
  requiredProcedureIds: string[];
  requiredSkillIds: string[];
  contextEpoch: number | null;
  compactBoundaryId: string | null;
  currentSkillAuthorityRequired: boolean;
  executableDecision: false;
  payload: JsonObject;
  createdAt: string;
  expiresAt: string | null;
  claimedBy: string | null;
  claimToken: string | null;
  claimEpoch: number;
  claimedAt: string | null;
  acknowledgedAt: string | null;
  releasedAt: string | null;
  terminalEventIds: string[];
  directiveDigest: string;
}

export interface MemoryDirectiveClaim {
  directiveId: string;
  consumer: MemoryConsumerKind;
  workerId: string;
  claimToken: string;
  claimEpoch: number;
  claimedAt: string;
  expiresAt: string;
  claimDigest: string;
}

export interface MemoryDirectiveRule {
  ruleId: string;
  signalKinds: string[];
  consumers: MemoryConsumerKind[];
  action: MemoryDirectiveAction;
  priority: MemoryDirectivePriority;
  retryClass: string | null;
  routeHints: string[];
  forbiddenRoutes: string[];
  requirePayloadKeys: string[];
  rejectPayloadValues: Record<string, JsonValue[]>;
  currentSkillAuthorityRequired: boolean;
  ttlMilliseconds: number | null;
  enabled: boolean;
  metadata: JsonObject;
}

export interface MemoryConsumerDirectiveSnapshot {
  version: "zyra.memory-consumer-directive-runtime/v1";
  identity: RuntimeIdentity;
  revision: number;
  rules: MemoryDirectiveRule[];
  directives: MemoryConsumerDirective[];
  claims: MemoryDirectiveClaim[];
  consumedSignalIds: string[];
  capturedAt: string;
  checksum: string;
}

/**
 * Produces deterministic, durable handoff contracts for routing/recovery and
 * M2 controls. These directives are not scheduler or recovery decisions. A
 * later owner must claim and interpret them; this runtime never invokes a
 * skill, changes a route, or performs a retry itself.
 */
export class MemorySignalConsumerRuntime {
  readonly identity: RuntimeIdentity;
  private readonly rules = new Map<string, MemoryDirectiveRule>();
  private readonly directives = new Map<string, MemoryConsumerDirective>();
  private readonly claims = new Map<string, MemoryDirectiveClaim>();
  private readonly consumedSignalIds = new Set<string>();
  private readonly now: () => Date;
  private revision = 0;

  constructor(options: {
    identity: RuntimeIdentity;
    rules?: MemoryDirectiveRule[];
    now?: () => Date;
    snapshot?: MemoryConsumerDirectiveSnapshot | null;
  }) {
    this.identity = cloneJson(options.identity);
    this.now = options.now ?? (() => new Date());
    for (const rule of options.rules ?? defaultMemoryDirectiveRules()) this.installRule(rule);
    if (options.snapshot) this.restore(options.snapshot);
  }

  installRule(ruleValue: MemoryDirectiveRule): MemoryDirectiveRule {
    const rule = normalizeRule(ruleValue);
    const existing = this.rules.get(rule.ruleId);
    if (existing && digest(existing) !== digest(rule)) {
      throw contractError("memory_directive_rule_conflict", `memory directive rule conflicts: ${rule.ruleId}`);
    }
    this.rules.set(rule.ruleId, rule);
    return cloneJson(existing ?? rule);
  }

  consume(signalValue: MemorySignal): MemoryConsumerDirective[] {
    this.assertEnabled();
    const signal = validateSignal(signalValue, this.identity);
    const existing = [...this.directives.values()].filter((item) => item.sourceSignalId === signal.signalId);
    if (this.consumedSignalIds.has(signal.signalId)) return existing.map(cloneJson);
    const matched = [...this.rules.values()]
      .filter((rule) => rule.enabled)
      .filter((rule) => rule.signalKinds.includes("*") || rule.signalKinds.includes(signal.kind))
      .filter((rule) => rule.requirePayloadKeys.every((key) => signal.payload[key] !== undefined))
      .filter((rule) => !Object.entries(rule.rejectPayloadValues).some(([key, values]) =>
        values.some((value) => digest(value) === digest(signal.payload[key]))
      ))
      .sort((left, right) => priorityRank(right.priority) - priorityRank(left.priority) || left.ruleId.localeCompare(right.ruleId));
    const output: MemoryConsumerDirective[] = [];
    for (const rule of matched) {
      for (const consumer of rule.consumers) {
        const directive = this.buildDirective(signal, rule, consumer);
        const prior = this.directives.get(directive.directiveId);
        if (prior && prior.directiveDigest !== directive.directiveDigest) {
          throw contractError("memory_directive_identity_conflict", `memory directive conflicts: ${directive.directiveId}`);
        }
        if (!prior) {
          this.directives.set(directive.directiveId, directive);
          this.revision += 1;
        }
        output.push(cloneJson(prior ?? directive));
      }
    }
    if (output.length === 0) {
      const rule = normalizeRule({
        ruleId: "memory-signal-observe-default",
        signalKinds: [signal.kind],
        consumers: ["audit"],
        action: "observe",
        priority: "low",
        retryClass: null,
        routeHints: [],
        forbiddenRoutes: [],
        requirePayloadKeys: [],
        rejectPayloadValues: {},
        currentSkillAuthorityRequired: false,
        ttlMilliseconds: null,
        enabled: true,
        metadata: { generated_default_observation: true },
      });
      const directive = this.buildDirective(signal, rule, "audit");
      this.directives.set(directive.directiveId, directive);
      this.revision += 1;
      output.push(cloneJson(directive));
    }
    this.consumedSignalIds.add(signal.signalId);
    this.revision += 1;
    return output;
  }

  consumeMany(signals: MemorySignal[]): MemoryConsumerDirective[] {
    const output: MemoryConsumerDirective[] = [];
    for (const signal of [...signals].sort((left, right) => left.sequence - right.sequence || left.signalId.localeCompare(right.signalId))) {
      output.push(...this.consume(signal));
    }
    return output;
  }

  claim(input: {
    directiveId: string;
    consumer: MemoryConsumerKind;
    workerId: string;
    leaseMilliseconds?: number;
  }): MemoryDirectiveClaim {
    this.assertEnabled();
    const directive = this.directives.get(input.directiveId.trim());
    if (!directive) throw contractError("memory_directive_missing", `memory directive not found: ${input.directiveId}`);
    if (directive.consumer !== input.consumer) {
      throw contractError("memory_directive_consumer_mismatch", "memory directive belongs to another consumer");
    }
    if (["acknowledged", "expired"].includes(directive.state)) {
      throw contractError("memory_directive_terminal", `memory directive is ${directive.state}`);
    }
    const now = this.now().getTime();
    if (directive.expiresAt && Date.parse(directive.expiresAt) <= now) {
      this.updateDirective(directive, {
        state: "expired",
        releasedAt: nowIso(this.now),
        reason: `${directive.reason}; directive expired before claim`,
      });
      throw contractError("memory_directive_expired", `memory directive expired: ${directive.directiveId}`);
    }
    const existing = this.claims.get(directive.directiveId);
    if (existing && Date.parse(existing.expiresAt) > now && existing.workerId !== input.workerId.trim()) {
      throw contractError("memory_directive_claim_held", "memory directive claim is held by another worker");
    }
    const workerId = input.workerId.trim();
    if (!workerId) throw contractError("memory_directive_worker_id", "memory directive claim requires worker id");
    const claimedAt = nowIso(this.now);
    const lease = Math.max(1_000, Math.min(input.leaseMilliseconds ?? 30_000, 60 * 60 * 1_000));
    const expiresAt = new Date(Date.parse(claimedAt) + lease).toISOString();
    const claimEpoch = existing ? existing.claimEpoch + 1 : directive.claimEpoch + 1;
    const claimToken = stableId("memory-directive-claim-token", directive.directiveId, workerId, claimEpoch, claimedAt);
    const unsigned = {
      directiveId: directive.directiveId,
      consumer: directive.consumer,
      workerId,
      claimToken,
      claimEpoch,
      claimedAt,
      expiresAt,
    } satisfies Omit<MemoryDirectiveClaim, "claimDigest">;
    const claim: MemoryDirectiveClaim = { ...unsigned, claimDigest: digest(unsigned) };
    this.claims.set(directive.directiveId, claim);
    this.updateDirective(directive, {
      state: "claimed",
      claimedBy: workerId,
      claimToken,
      claimEpoch,
      claimedAt,
      releasedAt: null,
    });
    return cloneJson(claim);
  }

  acknowledge(input: {
    claim: MemoryDirectiveClaim;
    terminalEventIds: string[];
    result?: JsonObject;
  }): MemoryConsumerDirective {
    this.assertEnabled();
    const directive = this.assertClaim(input.claim);
    const updated = this.updateDirective(directive, {
      state: "acknowledged",
      acknowledgedAt: nowIso(this.now),
      terminalEventIds: uniqueStrings(input.terminalEventIds),
      payload: {
        ...directive.payload,
        consumer_result: cloneJson(input.result ?? {}),
        consumer_owner: input.claim.workerId,
        action_executed_by_06c: false,
      },
    });
    this.claims.delete(directive.directiveId);
    return updated;
  }

  release(input: {
    claim: MemoryDirectiveClaim;
    reason: string;
    terminalEventIds?: string[];
  }): MemoryConsumerDirective {
    this.assertEnabled();
    const directive = this.assertClaim(input.claim);
    const updated = this.updateDirective(directive, {
      state: "released",
      releasedAt: nowIso(this.now),
      reason: `${directive.reason}; released: ${input.reason.trim() || "consumer release"}`,
      terminalEventIds: uniqueStrings(input.terminalEventIds),
    });
    this.claims.delete(directive.directiveId);
    return updated;
  }

  sweepExpired(): MemoryConsumerDirective[] {
    const now = this.now().getTime();
    const changed: MemoryConsumerDirective[] = [];
    for (const directive of this.directives.values()) {
      const claim = this.claims.get(directive.directiveId);
      if (claim && Date.parse(claim.expiresAt) <= now) {
        this.claims.delete(directive.directiveId);
        changed.push(this.updateDirective(directive, {
          state: "released",
          releasedAt: nowIso(this.now),
          reason: `${directive.reason}; consumer lease expired`,
          claimedBy: null,
          claimToken: null,
        }));
        continue;
      }
      if (directive.expiresAt && Date.parse(directive.expiresAt) <= now && directive.state !== "acknowledged") {
        this.claims.delete(directive.directiveId);
        changed.push(this.updateDirective(directive, {
          state: "expired",
          releasedAt: nowIso(this.now),
          reason: `${directive.reason}; directive ttl expired`,
          claimedBy: null,
          claimToken: null,
        }));
      }
    }
    return changed;
  }

  list(options: {
    consumer?: MemoryConsumerKind;
    action?: MemoryDirectiveAction;
    state?: MemoryDirectiveState;
    minimumPriority?: MemoryDirectivePriority;
    after?: string;
    limit?: number;
  } = {}): MemoryConsumerDirective[] {
    const minimum = options.minimumPriority ? priorityRank(options.minimumPriority) : 0;
    return [...this.directives.values()]
      .filter((item) => !options.consumer || item.consumer === options.consumer)
      .filter((item) => !options.action || item.action === options.action)
      .filter((item) => !options.state || item.state === options.state)
      .filter((item) => priorityRank(item.priority) >= minimum)
      .filter((item) => !options.after || item.createdAt > options.after)
      .sort((left, right) => priorityRank(right.priority) - priorityRank(left.priority) || left.createdAt.localeCompare(right.createdAt) || left.directiveId.localeCompare(right.directiveId))
      .slice(0, Math.max(0, Math.min(options.limit ?? 1_000, 100_000)))
      .map(cloneJson);
  }

  get(directiveId: string): MemoryConsumerDirective | null {
    const directive = this.directives.get(directiveId.trim());
    return directive ? cloneJson(directive) : null;
  }

  health(): JsonObject {
    const directives = [...this.directives.values()];
    return {
      protocol: MEMORY_CONSUMER_DIRECTIVE_PROTOCOL,
      revision: this.revision,
      rule_count: this.rules.size,
      directive_count: directives.length,
      pending_count: directives.filter((item) => item.state === "pending" || item.state === "released").length,
      claimed_count: directives.filter((item) => item.state === "claimed").length,
      acknowledged_count: directives.filter((item) => item.state === "acknowledged").length,
      critical_count: directives.filter((item) => item.priority === "critical").length,
      retry_count: directives.filter((item) => item.action === "retry").length,
      reroute_count: directives.filter((item) => item.action === "reroute").length,
      replan_count: directives.filter((item) => item.action === "replan").length,
      active_claim_count: this.claims.size,
      consumed_signal_count: this.consumedSignalIds.size,
      action_execution_owner: "downstream M1-07C/M1-08",
      executes_actions: false,
      owns_scheduler: false,
      owns_recovery_planner: false,
    };
  }

  snapshot(): MemoryConsumerDirectiveSnapshot {
    const unsigned: Omit<MemoryConsumerDirectiveSnapshot, "checksum"> = {
      version: "zyra.memory-consumer-directive-runtime/v1",
      identity: cloneJson(this.identity),
      revision: this.revision,
      rules: [...this.rules.values()].sort((left, right) => left.ruleId.localeCompare(right.ruleId)).map(cloneJson),
      directives: this.list({ limit: 100_000 }).sort((left, right) => left.directiveId.localeCompare(right.directiveId)),
      claims: [...this.claims.values()].sort((left, right) => left.directiveId.localeCompare(right.directiveId)).map(cloneJson),
      consumedSignalIds: [...this.consumedSignalIds].sort(),
      capturedAt: nowIso(this.now),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshotValue: MemoryConsumerDirectiveSnapshot): void {
    if (snapshotValue.version !== "zyra.memory-consumer-directive-runtime/v1") {
      throw contractError("memory_directive_snapshot_version", "unsupported memory consumer directive snapshot version");
    }
    const { checksum, ...unsigned } = snapshotValue;
    if (digest(unsigned) !== checksum) {
      throw contractError("memory_directive_snapshot_checksum", "memory consumer directive snapshot checksum mismatch");
    }
    if (snapshotValue.identity.taskId !== this.identity.taskId || snapshotValue.identity.sessionId !== this.identity.sessionId) {
      throw contractError("memory_directive_snapshot_binding", "memory consumer directive snapshot belongs to another task/session");
    }
    const rules = new Map<string, MemoryDirectiveRule>();
    for (const ruleValue of snapshotValue.rules) {
      const rule = normalizeRule(ruleValue);
      rules.set(rule.ruleId, rule);
    }
    const directives = new Map<string, MemoryConsumerDirective>();
    for (const directive of snapshotValue.directives) {
      const { directiveDigest, ...directiveUnsigned } = directive;
      if (digest(directiveUnsigned) !== directiveDigest) {
        throw contractError("memory_directive_digest", `memory directive digest mismatch: ${directive.directiveId}`);
      }
      if (directive.executableDecision) {
        throw contractError("memory_directive_executable", `memory directive cannot be executable: ${directive.directiveId}`);
      }
      directives.set(directive.directiveId, cloneJson(directive));
    }
    const claims = new Map<string, MemoryDirectiveClaim>();
    for (const claim of snapshotValue.claims) {
      const { claimDigest, ...claimUnsigned } = claim;
      if (digest(claimUnsigned) !== claimDigest) {
        throw contractError("memory_directive_claim_digest", `memory directive claim digest mismatch: ${claim.directiveId}`);
      }
      const directive = directives.get(claim.directiveId);
      if (!directive || directive.state !== "claimed" || directive.claimToken !== claim.claimToken) {
        throw contractError("memory_directive_claim_binding", `memory directive claim is not bound: ${claim.directiveId}`);
      }
      claims.set(claim.directiveId, cloneJson(claim));
    }
    this.rules.clear();
    this.directives.clear();
    this.claims.clear();
    this.consumedSignalIds.clear();
    for (const [key, value] of rules) this.rules.set(key, value);
    for (const [key, value] of directives) this.directives.set(key, value);
    for (const [key, value] of claims) this.claims.set(key, value);
    for (const signalId of uniqueStrings(snapshotValue.consumedSignalIds)) this.consumedSignalIds.add(signalId);
    this.revision = snapshotValue.revision;
  }

  private buildDirective(
    signal: MemorySignal,
    rule: MemoryDirectiveRule,
    consumer: MemoryConsumerKind,
  ): MemoryConsumerDirective {
    const createdAt = nowIso(this.now);
    const expiresAt = rule.ttlMilliseconds === null
      ? null
      : new Date(Date.parse(createdAt) + rule.ttlMilliseconds).toISOString();
    const requiredEvidenceIds = payloadStrings(signal.payload, ["evidence_ids", "required_evidence_ids"]);
    const requiredArtifactIds = payloadStrings(signal.payload, ["artifact_ids", "required_artifact_ids"]);
    const requiredProcedureIds = payloadStrings(signal.payload, ["procedure_ids", "procedure_id"]);
    const requiredSkillIds = payloadStrings(signal.payload, ["skill_ids", "skill_id"]);
    const contextEpoch = payloadInteger(signal.payload, ["context_epoch_after", "context_epoch"]);
    const compactBoundaryId = payloadString(signal.payload, ["boundary_id", "compact_boundary_id"]);
    const unsigned = {
      directiveId: stableId(
        "memory-consumer-directive",
        signal.signalId,
        rule.ruleId,
        consumer,
        rule.action,
      ),
      protocol: MEMORY_CONSUMER_DIRECTIVE_PROTOCOL,
      identity: cloneJson(signal.identity),
      sourceSignalId: signal.signalId,
      sourceSignalKind: signal.kind,
      consumer,
      action: rule.action,
      priority: maxPriority(rule.priority, signal.priority),
      state: "pending" as const,
      aggregateId: signal.aggregateId,
      causationId: signal.causationId,
      correlationId: signal.correlationId,
      reason: `${rule.ruleId}:${signal.kind}`,
      retryClass: rule.retryClass,
      routeHints: cloneJson(rule.routeHints),
      forbiddenRoutes: cloneJson(rule.forbiddenRoutes),
      requiredEvidenceIds,
      requiredArtifactIds,
      requiredProcedureIds,
      requiredSkillIds,
      contextEpoch,
      compactBoundaryId,
      currentSkillAuthorityRequired: rule.currentSkillAuthorityRequired || requiredSkillIds.length > 0,
      executableDecision: false,
      payload: {
        source_signal_payload: cloneJson(signal.payload),
        source_signal_digest: signal.signalDigest,
        rule_id: rule.ruleId,
        rule_metadata: cloneJson(rule.metadata),
        downstream_action_owner: consumer === "recovery" ? "M1-07C RecoveryPlanner" : consumer === "routing" ? "M1-08 RoutingRuntime" : "downstream consumer",
        action_executed_by_06c: false,
      },
      createdAt,
      expiresAt,
      claimedBy: null,
      claimToken: null,
      claimEpoch: 0,
      claimedAt: null,
      acknowledgedAt: null,
      releasedAt: null,
      terminalEventIds: [],
    } satisfies Omit<MemoryConsumerDirective, "directiveDigest">;
    return { ...unsigned, directiveDigest: digest(unsigned) };
  }

  private assertClaim(claimValue: MemoryDirectiveClaim): MemoryConsumerDirective {
    const claim = this.claims.get(claimValue.directiveId);
    if (!claim) throw contractError("memory_directive_claim_missing", `memory directive claim not found: ${claimValue.directiveId}`);
    if (claim.claimDigest !== claimValue.claimDigest || digest({
      directiveId: claimValue.directiveId,
      consumer: claimValue.consumer,
      workerId: claimValue.workerId,
      claimToken: claimValue.claimToken,
      claimEpoch: claimValue.claimEpoch,
      claimedAt: claimValue.claimedAt,
      expiresAt: claimValue.expiresAt,
    }) !== claimValue.claimDigest) {
      throw contractError("memory_directive_claim_invalid", "memory directive claim digest mismatch");
    }
    if (Date.parse(claim.expiresAt) <= this.now().getTime()) {
      throw contractError("memory_directive_claim_expired", "memory directive claim expired");
    }
    const directive = this.directives.get(claim.directiveId);
    if (!directive || directive.state !== "claimed") {
      throw contractError("memory_directive_claim_state", "memory directive is not in claimed state");
    }
    if (
      directive.claimToken !== claim.claimToken
      || directive.claimEpoch !== claim.claimEpoch
      || directive.claimedBy !== claim.workerId
    ) {
      throw contractError("memory_directive_claim_fence", "memory directive claim fence is stale");
    }
    return directive;
  }

  private updateDirective(
    directive: MemoryConsumerDirective,
    changes: Partial<MemoryConsumerDirective>,
  ): MemoryConsumerDirective {
    const changed: MemoryConsumerDirective = {
      ...directive,
      ...changes,
      directiveDigest: "",
    };
    const { directiveDigest: _ignored, ...unsigned } = changed;
    changed.directiveDigest = digest(unsigned);
    this.directives.set(changed.directiveId, changed);
    this.revision += 1;
    return cloneJson(changed);
  }

  private assertEnabled(): void {
    if (process.env.ZYRA_DISABLE_MEMORY_SIGNAL_CONSUMERS === "1") {
      throw contractError("memory_signal_consumer_runtime_disabled", "06C memory signal consumer runtime is disabled");
    }
  }
}

export function defaultMemoryDirectiveRules(): MemoryDirectiveRule[] {
  const rules: MemoryDirectiveRule[] = [
    {
      ruleId: "skill-outcome-success-routing",
      signalKinds: ["skill_memory_updated"],
      consumers: ["routing", "context"],
      action: "observe",
      priority: "normal",
      retryClass: null,
      routeHints: ["prefer_evidence_backed_skill_outcome"],
      forbiddenRoutes: ["cached_skill_body_execution"],
      requirePayloadKeys: ["skill_id", "memory_id"],
      rejectPayloadValues: { status: ["failed", "cancelled"] },
      currentSkillAuthorityRequired: true,
      ttlMilliseconds: 24 * 60 * 60 * 1_000,
      enabled: true,
      metadata: { owner_boundary: "06C_output_only" },
    },
    {
      ruleId: "skill-outcome-failure-recovery",
      signalKinds: ["skill_memory_rejected"],
      consumers: ["recovery", "routing"],
      action: "replan",
      priority: "high",
      retryClass: "skill_outcome_invalid",
      routeHints: ["resolve_current_skill", "select_alternate_capability"],
      forbiddenRoutes: ["reuse_rejected_outcome", "invoke_cached_skill_body"],
      requirePayloadKeys: ["reason"],
      rejectPayloadValues: {},
      currentSkillAuthorityRequired: true,
      ttlMilliseconds: 60 * 60 * 1_000,
      enabled: true,
      metadata: { downstream_owner: "M1-07C" },
    },
    {
      ruleId: "procedure-mined-routing",
      signalKinds: ["procedure_mined"],
      consumers: ["routing", "recovery", "context"],
      action: "observe",
      priority: "normal",
      retryClass: null,
      routeHints: ["consider_validated_procedure"],
      forbiddenRoutes: ["activate_unvalidated_procedure"],
      requirePayloadKeys: ["procedure_id", "validation_status"],
      rejectPayloadValues: { validation_status: ["candidate", "rejected", "superseded"] },
      currentSkillAuthorityRequired: true,
      ttlMilliseconds: 7 * 24 * 60 * 60 * 1_000,
      enabled: true,
      metadata: { procedure_owner: "ReusableProcedureStore" },
    },
    {
      ruleId: "procedure-rejected-replan",
      signalKinds: ["procedure_rejected"],
      consumers: ["recovery", "audit"],
      action: "quarantine_procedure",
      priority: "high",
      retryClass: "procedure_provenance_or_validation_failed",
      routeHints: ["replan_without_procedure"],
      forbiddenRoutes: ["select_rejected_procedure"],
      requirePayloadKeys: ["procedure_id"],
      rejectPayloadValues: {},
      currentSkillAuthorityRequired: false,
      ttlMilliseconds: 24 * 60 * 60 * 1_000,
      enabled: true,
      metadata: { model_can_override: false },
    },
    {
      ruleId: "compact-restored-context",
      signalKinds: ["compact_restored", "context_epoch_advanced"],
      consumers: ["context", "control", "audit"],
      action: "restore_context",
      priority: "high",
      retryClass: null,
      routeHints: ["use_restored_context_epoch"],
      forbiddenRoutes: ["use_precompact_unbounded_context"],
      requirePayloadKeys: ["context_epoch_after"],
      rejectPayloadValues: {},
      currentSkillAuthorityRequired: true,
      ttlMilliseconds: 24 * 60 * 60 * 1_000,
      enabled: true,
      metadata: { compact_owner: "02D ContextCompactionRuntime" },
    },
    {
      ruleId: "compact-deferred-retry",
      signalKinds: ["compact_deferred"],
      consumers: ["recovery", "control"],
      action: "retry",
      priority: "high",
      retryClass: "compact_safe_cut_unavailable",
      routeHints: ["wait_for_tool_atomic_group", "retry_text_ref_compact"],
      forbiddenRoutes: ["split_tool_call_result", "silent_history_drop"],
      requirePayloadKeys: ["reason"],
      rejectPayloadValues: {},
      currentSkillAuthorityRequired: false,
      ttlMilliseconds: 60 * 60 * 1_000,
      enabled: true,
      metadata: { fallback_required: true },
    },
    {
      ruleId: "compact-restore-rejected-reroute",
      signalKinds: ["compact_restore_rejected"],
      consumers: ["recovery", "routing", "control"],
      action: "reroute",
      priority: "critical",
      retryClass: "compact_restore_unrecoverable",
      routeHints: ["use_text_reference_baseline", "restart_from_canonical_checkpoint"],
      forbiddenRoutes: ["continue_with_missing_context", "snapcompact_only"],
      requirePayloadKeys: ["reason"],
      rejectPayloadValues: {},
      currentSkillAuthorityRequired: true,
      ttlMilliseconds: 60 * 60 * 1_000,
      enabled: true,
      metadata: { sealed_policy_safe: true },
    },
  ];
  return rules.map(normalizeRule);
}

function normalizeRule(value: MemoryDirectiveRule): MemoryDirectiveRule {
  if (!value.ruleId?.trim()) throw contractError("memory_directive_rule_id", "memory directive rule id is required");
  if (!value.signalKinds?.length) throw contractError("memory_directive_rule_signal", "memory directive rule requires signal kinds");
  if (!value.consumers?.length) throw contractError("memory_directive_rule_consumer", "memory directive rule requires consumers");
  const rejectPayloadValues: Record<string, JsonValue[]> = {};
  for (const [key, values] of Object.entries(value.rejectPayloadValues ?? {})) {
    rejectPayloadValues[key.trim()] = cloneJson(values);
  }
  const ttl = value.ttlMilliseconds;
  if (ttl !== null && (!Number.isSafeInteger(ttl) || ttl < 1_000)) {
    throw contractError("memory_directive_rule_ttl", "memory directive rule ttl must be null or at least one second");
  }
  return {
    ruleId: value.ruleId.trim(),
    signalKinds: uniqueStrings(value.signalKinds),
    consumers: uniqueStrings(value.consumers) as MemoryConsumerKind[],
    action: value.action,
    priority: value.priority,
    retryClass: value.retryClass?.trim() || null,
    routeHints: uniqueStrings(value.routeHints),
    forbiddenRoutes: uniqueStrings(value.forbiddenRoutes),
    requirePayloadKeys: uniqueStrings(value.requirePayloadKeys),
    rejectPayloadValues,
    currentSkillAuthorityRequired: Boolean(value.currentSkillAuthorityRequired),
    ttlMilliseconds: ttl,
    enabled: Boolean(value.enabled),
    metadata: cloneJson(value.metadata ?? {}),
  };
}

function validateSignal(value: MemorySignal, expected: RuntimeIdentity): MemorySignal {
  if (value.identity.taskId !== expected.taskId || value.identity.sessionId !== expected.sessionId) {
    throw contractError("memory_directive_signal_binding", "memory signal belongs to another task/session");
  }
  if (digest(value.payload) !== value.payloadDigest) {
    throw contractError("memory_directive_signal_payload_digest", `memory signal payload digest mismatch: ${value.signalId}`);
  }
  const { signalDigest, ...unsigned } = value;
  const expectedDigest = digest(unsigned);
  if (signalDigest !== expectedDigest) {
    throw contractError("memory_directive_signal_digest", `memory signal digest mismatch: ${value.signalId}`);
  }
  return cloneJson(value);
}

function priorityRank(value: MemoryDirectivePriority | MemorySignal["priority"]): number {
  return { low: 1, normal: 2, high: 3, critical: 4 }[value] ?? 0;
}

function maxPriority(
  left: MemoryDirectivePriority,
  right: MemorySignal["priority"],
): MemoryDirectivePriority {
  return priorityRank(left) >= priorityRank(right) ? left : right;
}

function payloadStrings(payload: JsonObject, keys: string[]): string[] {
  const values: unknown[] = [];
  for (const key of keys) {
    const value = payload[key];
    if (Array.isArray(value)) values.push(...value);
    else if (value !== null && value !== undefined) values.push(value);
  }
  return uniqueStrings(values);
}

function payloadString(payload: JsonObject, keys: string[]): string | null {
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}

function payloadInteger(payload: JsonObject, keys: string[]): number | null {
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) return value;
  }
  return null;
}
