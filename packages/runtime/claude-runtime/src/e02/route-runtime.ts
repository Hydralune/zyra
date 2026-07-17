import type { JsonObject, ToolSpecContract } from "../contracts.ts";
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

export type E02RouteDomain = "mcp" | "skill" | "plugin" | "command" | "agent" | "control";

export interface E02RouteCandidate extends JsonObject {
  routeId: string;
  toolName: string;
  canonicalName: string;
  domain: E02RouteDomain;
  owner: string;
  sourceId: string;
  sourceRevision: string;
  priority: number;
  enabled: boolean;
  spec: ToolSpecContract & JsonObject;
  specDigest: string;
  registeredAt: string;
  metadata: JsonObject;
}

export interface E02RouteDecision extends JsonObject {
  toolName: string;
  selectedRouteId: string | null;
  selectedOwner: string | null;
  selectedDomain: E02RouteDomain | null;
  candidateRouteIds: string[];
  shadowedRouteIds: string[];
  conflict: boolean;
  reason: string;
  revision: number;
  decisionDigest: string;
}

export interface E02RouteRevision extends JsonObject {
  revisionId: string;
  revision: number;
  priorRevision: number;
  added: string[];
  updated: string[];
  removed: string[];
  unchanged: string[];
  conflicts: string[];
  sourceDigest: string;
  committedAt: string;
  previousRevisionHash: string;
  revisionHash: string;
  metadata: JsonObject;
}

export interface E02RouteLease extends JsonObject {
  leaseId: string;
  routeId: string;
  toolName: string;
  owner: string;
  routeRevision: number;
  sessionId: string;
  workerRequestId: string;
  toolCallId: string;
  argumentsDigest: string;
  issuedAt: string;
  expiresAt: string;
  consumedAt: string | null;
  revokedAt: string | null;
  revocationReason: string | null;
  leaseDigest: string;
}

export interface E02RouteSnapshot {
  version: "zyra.e02-route-runtime/v1";
  runtime: E02RuntimeIdentity;
  revision: number;
  previousRevisionHash: string;
  candidates: E02RouteCandidate[];
  decisions: E02RouteDecision[];
  revisions: E02RouteRevision[];
  leases: E02RouteLease[];
  snapshotHash: string;
}

export class E02RouteRuntime {
  readonly runtime: E02RuntimeIdentity;
  private readonly now: () => Date;
  private revision = 0;
  private previousRevisionHash = digest({ genesis: "zyra.e02-route-runtime/v1" });
  private readonly candidates = new Map<string, E02RouteCandidate>();
  private readonly decisions = new Map<string, E02RouteDecision>();
  private readonly revisions: E02RouteRevision[] = [];
  private readonly leases = new Map<string, E02RouteLease>();

  constructor(options: {
    runtime: E02RuntimeIdentity;
    now?: () => Date;
    snapshot?: E02RouteSnapshot | null;
  }) {
    this.runtime = cloneJson(options.runtime);
    this.now = options.now ?? (() => new Date());
    if (options.snapshot) this.restore(options.snapshot);
  }

  reconcile(
    values: Array<{
      spec: ToolSpecContract;
      domain: E02RouteDomain;
      owner: string;
      sourceId?: string;
      sourceRevision?: string;
      priority?: number;
      enabled?: boolean;
      metadata?: JsonObject;
    }>,
    metadataValue: JsonObject = {},
  ): E02RouteRevision | null {
    const next = new Map<string, E02RouteCandidate>();
    for (const value of values) {
      const candidate = this.buildCandidate(value);
      const prior = next.get(candidate.routeId);
      if (prior && !constantTimeDigestEquals(prior.specDigest, candidate.specDigest)) {
        throw routeError(
          "e02_route_identity_collision",
          `route ${candidate.routeId} has multiple different specifications`,
        );
      }
      next.set(candidate.routeId, candidate);
    }
    const added: string[] = [];
    const updated: string[] = [];
    const removed: string[] = [];
    const unchanged: string[] = [];
    for (const [routeId, candidate] of next) {
      const prior = this.candidates.get(routeId);
      if (!prior) added.push(routeId);
      else if (!constantTimeDigestEquals(routeCandidateDigest(prior), routeCandidateDigest(candidate))) updated.push(routeId);
      else unchanged.push(routeId);
    }
    for (const routeId of this.candidates.keys()) {
      if (!next.has(routeId)) removed.push(routeId);
    }
    const sourceDigest = digest([...next.values()]
      .sort((left, right) => left.routeId.localeCompare(right.routeId))
      .map((candidate) => ({ route_id: candidate.routeId, digest: routeCandidateDigest(candidate) })));
    const priorSourceDigest = digest([...this.candidates.values()]
      .sort((left, right) => left.routeId.localeCompare(right.routeId))
      .map((candidate) => ({ route_id: candidate.routeId, digest: routeCandidateDigest(candidate) })));
    if (constantTimeDigestEquals(sourceDigest, priorSourceDigest)) return null;
    const nextDecisions = this.resolveAll(next, this.revision + 1);
    const conflicts = [...nextDecisions.values()]
      .filter((decision) => decision.conflict)
      .map((decision) => decision.toolName)
      .sort();
    if (conflicts.length) {
      throw routeError(
        "e02_route_unresolved_conflict",
        `capability route conflict for ${conflicts.join(", ")}`,
        { conflicts },
      );
    }
    this.revokeRemovedLeases(next, removed);
    this.candidates.clear();
    for (const [routeId, candidate] of next) this.candidates.set(routeId, candidate);
    this.decisions.clear();
    for (const [name, decision] of nextDecisions) this.decisions.set(name, decision);
    const priorRevision = this.revision;
    const revision = priorRevision + 1;
    const committedAt = this.timestamp();
    const revisionBase = {
      revision,
      priorRevision,
      added: added.sort(),
      updated: updated.sort(),
      removed: removed.sort(),
      unchanged: unchanged.sort(),
      conflicts,
      sourceDigest,
      committedAt,
      previousRevisionHash: this.previousRevisionHash,
      metadata: canonicalize(metadataValue) as JsonObject,
    };
    const revisionId = deterministicId("e02-route-revision", revisionBase, 48);
    const revisionHash = hashChain(this.previousRevisionHash, { revisionId, ...revisionBase });
    const receipt: E02RouteRevision = { revisionId, ...revisionBase, revisionHash };
    this.revision = revision;
    this.previousRevisionHash = revisionHash;
    this.revisions.push(receipt);
    return cloneJson(receipt);
  }

  resolve(toolNameValue: string): E02RouteDecision {
    const toolName = toolNameValue.trim();
    const decision = this.decisions.get(toolName);
    if (!decision || !decision.selectedRouteId || !decision.selectedOwner) {
      throw routeError("e02_route_not_found", `no enabled TypeScript route owns ${toolName}`);
    }
    const selected = this.candidates.get(decision.selectedRouteId);
    if (!selected || !selected.enabled) {
      throw routeError("e02_route_stale", `selected route for ${toolName} is stale`);
    }
    return cloneJson(decision);
  }

  owns(toolName: string): boolean {
    const decision = this.decisions.get(toolName);
    return Boolean(decision?.selectedRouteId && decision.selectedOwner && !decision.conflict);
  }

  owner(toolName: string): string {
    return this.resolve(toolName).selectedOwner!;
  }

  domain(toolName: string): E02RouteDomain {
    return this.resolve(toolName).selectedDomain!;
  }

  spec(toolName: string): ToolSpecContract {
    const decision = this.resolve(toolName);
    const selected = this.candidates.get(decision.selectedRouteId!);
    if (!selected) throw routeError("e02_route_stale", `route candidate for ${toolName} disappeared`);
    return cloneJson(selected.spec) as ToolSpecContract;
  }

  toolSpecs(): ToolSpecContract[] {
    return [...this.decisions.values()]
      .filter((decision) => decision.selectedRouteId && !decision.conflict)
      .sort((left, right) => left.toolName.localeCompare(right.toolName))
      .map((decision) => this.spec(decision.toolName));
  }

  issueLease(input: {
    toolName: string;
    toolCallId: string;
    argumentsDigest: string;
    ttlMs?: number;
  }): E02RouteLease {
    const decision = this.resolve(input.toolName);
    const selected = this.candidates.get(decision.selectedRouteId!)!;
    if (!input.toolCallId || !input.argumentsDigest) {
      throw routeError("e02_route_lease_binding_missing", "route lease requires tool call and argument digests");
    }
    const issuedAt = this.timestamp();
    const ttlMs = Math.max(1_000, Math.min(300_000, input.ttlMs ?? 30_000));
    const expiresAt = new Date(Date.parse(issuedAt) + ttlMs).toISOString();
    const base = {
      routeId: selected.routeId,
      toolName: selected.toolName,
      owner: selected.owner,
      routeRevision: this.revision,
      sessionId: this.runtime.sessionId,
      workerRequestId: this.runtime.workerRequestId,
      toolCallId: input.toolCallId,
      argumentsDigest: input.argumentsDigest,
      issuedAt,
      expiresAt,
      consumedAt: null,
      revokedAt: null,
      revocationReason: null,
    };
    const leaseId = deterministicId("e02-route-lease", base, 48);
    const lease: E02RouteLease = { leaseId, ...base, leaseDigest: digest({ leaseId, ...base }) };
    const prior = this.leases.get(leaseId);
    if (prior && !constantTimeDigestEquals(prior.leaseDigest, lease.leaseDigest)) {
      throw routeError("e02_route_lease_collision", `route lease ${leaseId} collided`);
    }
    if (prior?.consumedAt || prior?.revokedAt) {
      throw routeError("e02_route_lease_terminal", `route lease ${leaseId} is already terminal`);
    }
    this.leases.set(leaseId, lease);
    return cloneJson(lease);
  }

  consumeLease(
    leaseId: string,
    binding: { toolName: string; toolCallId: string; argumentsDigest: string },
  ): E02RouteLease {
    const lease = this.requireLease(leaseId);
    if (lease.consumedAt) throw routeError("e02_route_lease_consumed", `route lease ${leaseId} was consumed`);
    if (lease.revokedAt) throw routeError("e02_route_lease_revoked", `route lease ${leaseId} was revoked`);
    if (Date.parse(lease.expiresAt) <= this.now().getTime()) {
      this.revokeLease(leaseId, "expired");
      throw routeError("e02_route_lease_expired", `route lease ${leaseId} expired`);
    }
    if (
      lease.toolName !== binding.toolName
      || lease.toolCallId !== binding.toolCallId
      || !constantTimeDigestEquals(lease.argumentsDigest, binding.argumentsDigest)
      || lease.sessionId !== this.runtime.sessionId
      || lease.workerRequestId !== this.runtime.workerRequestId
    ) {
      throw routeError("e02_route_lease_binding_mismatch", `route lease ${leaseId} binding mismatch`);
    }
    const current = this.resolve(binding.toolName);
    if (current.selectedRouteId !== lease.routeId || current.selectedOwner !== lease.owner) {
      this.revokeLease(leaseId, "route_replaced");
      throw routeError("e02_route_lease_stale", `route lease ${leaseId} no longer targets the selected owner`);
    }
    lease.consumedAt = this.timestamp();
    lease.leaseDigest = routeLeaseDigest(lease);
    return cloneJson(lease);
  }

  revokeLease(leaseId: string, reason: string): E02RouteLease {
    const lease = this.requireLease(leaseId);
    if (lease.consumedAt) throw routeError("e02_route_lease_consumed", `cannot revoke consumed route lease ${leaseId}`);
    if (!lease.revokedAt) {
      lease.revokedAt = this.timestamp();
      lease.revocationReason = reason || "revoked";
      lease.leaseDigest = routeLeaseDigest(lease);
    }
    return cloneJson(lease);
  }

  candidatesFor(toolName: string): E02RouteCandidate[] {
    return [...this.candidates.values()]
      .filter((candidate) => candidate.toolName === toolName)
      .sort(compareCandidates)
      .map(cloneJson);
  }

  decisionHistory(): E02RouteRevision[] {
    return this.revisions.map(cloneJson);
  }

  activeLeases(): E02RouteLease[] {
    const now = this.now().getTime();
    return [...this.leases.values()]
      .filter((lease) => !lease.consumedAt && !lease.revokedAt && Date.parse(lease.expiresAt) > now)
      .map(cloneJson);
  }

  health(): JsonObject {
    return {
      canonical_owner: "typescript",
      revision: this.revision,
      route_count: this.decisions.size,
      candidate_count: this.candidates.size,
      active_lease_count: this.activeLeases().length,
      conflict_count: [...this.decisions.values()].filter((decision) => decision.conflict).length,
      previous_revision_hash: this.previousRevisionHash,
      python_route_fallback: false,
    };
  }

  snapshot(): E02RouteSnapshot {
    const withoutHash = {
      version: "zyra.e02-route-runtime/v1" as const,
      runtime: cloneJson(this.runtime),
      revision: this.revision,
      previousRevisionHash: this.previousRevisionHash,
      candidates: [...this.candidates.values()].sort((left, right) => left.routeId.localeCompare(right.routeId)).map(cloneJson),
      decisions: [...this.decisions.values()].sort((left, right) => left.toolName.localeCompare(right.toolName)).map(cloneJson),
      revisions: this.revisions.map(cloneJson),
      leases: [...this.leases.values()].sort((left, right) => left.issuedAt.localeCompare(right.issuedAt)).map(cloneJson),
    };
    return { ...withoutHash, snapshotHash: digest(withoutHash) };
  }

  private buildCandidate(value: {
    spec: ToolSpecContract;
    domain: E02RouteDomain;
    owner: string;
    sourceId?: string;
    sourceRevision?: string;
    priority?: number;
    enabled?: boolean;
    metadata?: JsonObject;
  }): E02RouteCandidate {
    if (!value.spec.name.trim() || !value.owner.trim()) {
      throw routeError("e02_route_candidate_invalid", "route candidate requires tool name and owner");
    }
    const sourceId = value.sourceId?.trim() || value.spec.source || value.owner;
    const sourceRevision = value.sourceRevision?.trim()
      || value.spec.metadata.version
      || value.spec.metadata.revision
      || "1";
    const canonicalName = normalizeIdentifier(value.spec.name);
    const spec = canonicalize(value.spec as unknown as JsonObject) as ToolSpecContract & JsonObject;
    const specDigest = digest(spec);
    const routeId = deterministicId("e02-capability-route", {
      canonical_name: canonicalName,
      domain: value.domain,
      owner: value.owner,
      source_id: sourceId,
    }, 40);
    return {
      routeId,
      toolName: value.spec.name,
      canonicalName,
      domain: value.domain,
      owner: value.owner,
      sourceId,
      sourceRevision,
      priority: Number.isSafeInteger(value.priority) ? value.priority! : domainPriority(value.domain),
      enabled: value.enabled !== false,
      spec,
      specDigest,
      registeredAt: this.timestamp(),
      metadata: canonicalize(value.metadata ?? {}) as JsonObject,
    };
  }

  private resolveAll(
    candidates: Map<string, E02RouteCandidate>,
    revision: number,
  ): Map<string, E02RouteDecision> {
    const byName = new Map<string, E02RouteCandidate[]>();
    for (const candidate of candidates.values()) {
      const values = byName.get(candidate.toolName) ?? [];
      values.push(candidate);
      byName.set(candidate.toolName, values);
    }
    const decisions = new Map<string, E02RouteDecision>();
    for (const [toolName, values] of byName) {
      const enabled = values.filter((candidate) => candidate.enabled).sort(compareCandidates);
      const selected = enabled[0] ?? null;
      const tied = selected
        ? enabled.filter((candidate) => candidate.priority === selected.priority && candidate.owner !== selected.owner)
        : [];
      const conflict = tied.length > 0;
      const base = {
        toolName,
        selectedRouteId: conflict ? null : selected?.routeId ?? null,
        selectedOwner: conflict ? null : selected?.owner ?? null,
        selectedDomain: conflict ? null : selected?.domain ?? null,
        candidateRouteIds: values.map((candidate) => candidate.routeId).sort(),
        shadowedRouteIds: conflict || !selected
          ? []
          : enabled.slice(1).map((candidate) => candidate.routeId).sort(),
        conflict,
        reason: conflict
          ? "equal-priority candidates have different canonical owners"
          : selected
            ? "highest-priority enabled TypeScript candidate selected"
            : "all candidates are disabled",
        revision,
      };
      decisions.set(toolName, { ...base, decisionDigest: digest(base) });
    }
    return decisions;
  }

  private revokeRemovedLeases(next: Map<string, E02RouteCandidate>, removed: string[]): void {
    const removedSet = new Set(removed);
    for (const lease of this.leases.values()) {
      if (lease.consumedAt || lease.revokedAt) continue;
      if (removedSet.has(lease.routeId) || !next.has(lease.routeId)) {
        lease.revokedAt = this.timestamp();
        lease.revocationReason = "route_removed";
        lease.leaseDigest = routeLeaseDigest(lease);
      }
    }
  }

  private requireLease(leaseId: string): E02RouteLease {
    const lease = this.leases.get(leaseId);
    if (!lease) throw routeError("e02_route_lease_unknown", `unknown route lease ${leaseId}`);
    return lease;
  }

  private restore(snapshotValue: E02RouteSnapshot): void {
    const snapshot = cloneJson(snapshotValue);
    if (snapshot.version !== "zyra.e02-route-runtime/v1") {
      throw routeError("e02_route_snapshot_version", `unsupported route snapshot ${snapshot.version}`);
    }
    const { snapshotHash, ...withoutHash } = snapshot;
    if (!constantTimeDigestEquals(digest(withoutHash), snapshotHash)) {
      throw routeError("e02_route_snapshot_digest", "route snapshot digest mismatch");
    }
    if (
      snapshot.runtime.runtimeId !== this.runtime.runtimeId
      || snapshot.runtime.runId !== this.runtime.runId
      || snapshot.runtime.taskId !== this.runtime.taskId
      || snapshot.runtime.sessionId !== this.runtime.sessionId
      || snapshot.runtime.workerRequestId !== this.runtime.workerRequestId
    ) {
      throw routeError("e02_route_snapshot_binding", "route snapshot binding mismatch");
    }
    if (this.runtime.epoch <= snapshot.runtime.epoch) {
      throw routeError("e02_route_snapshot_epoch", "route restore epoch must advance");
    }
    for (const candidate of snapshot.candidates) {
      if (this.candidates.has(candidate.routeId) || !constantTimeDigestEquals(candidate.specDigest, digest(candidate.spec))) {
        throw routeError("e02_route_snapshot_candidate", `invalid route candidate ${candidate.routeId}`);
      }
      this.candidates.set(candidate.routeId, cloneJson(candidate));
    }
    for (const decision of snapshot.decisions) {
      const { decisionDigest, ...base } = decision;
      if (this.decisions.has(decision.toolName) || !constantTimeDigestEquals(digest(base), decisionDigest)) {
        throw routeError("e02_route_snapshot_decision", `invalid route decision ${decision.toolName}`);
      }
      this.decisions.set(decision.toolName, cloneJson(decision));
    }
    let previousHash = digest({ genesis: "zyra.e02-route-runtime/v1" });
    let previousRevision = 0;
    for (const revision of snapshot.revisions) {
      if (revision.priorRevision !== previousRevision || revision.previousRevisionHash !== previousHash) {
        throw routeError("e02_route_snapshot_chain", "route revision chain is not contiguous");
      }
      const { revisionHash, ...base } = revision;
      if (!constantTimeDigestEquals(hashChain(previousHash, base), revisionHash)) {
        throw routeError("e02_route_snapshot_revision", `route revision ${revision.revisionId} digest mismatch`);
      }
      this.revisions.push(cloneJson(revision));
      previousRevision = revision.revision;
      previousHash = revision.revisionHash;
    }
    for (const lease of snapshot.leases) {
      if (this.leases.has(lease.leaseId) || !constantTimeDigestEquals(routeLeaseDigest(lease), lease.leaseDigest)) {
        throw routeError("e02_route_snapshot_lease", `invalid route lease ${lease.leaseId}`);
      }
      const restored = cloneJson(lease);
      if (!restored.consumedAt && !restored.revokedAt) {
        restored.revokedAt = this.timestamp();
        restored.revocationReason = "restore_epoch_fence";
        restored.leaseDigest = routeLeaseDigest(restored);
      }
      this.leases.set(restored.leaseId, restored);
    }
    this.revision = snapshot.revision;
    this.previousRevisionHash = snapshot.previousRevisionHash;
  }

  private timestamp(): string {
    return this.now().toISOString();
  }
}

function compareCandidates(left: E02RouteCandidate, right: E02RouteCandidate): number {
  return right.priority - left.priority
    || left.owner.localeCompare(right.owner)
    || left.sourceId.localeCompare(right.sourceId)
    || left.routeId.localeCompare(right.routeId);
}

function domainPriority(domain: E02RouteDomain): number {
  if (domain === "control") return 1_000;
  if (domain === "agent") return 900;
  if (domain === "command") return 800;
  if (domain === "plugin") return 700;
  if (domain === "skill") return 600;
  return 500;
}

function routeCandidateDigest(candidate: E02RouteCandidate): string {
  return digest({
    route_id: candidate.routeId,
    tool_name: candidate.toolName,
    canonical_name: candidate.canonicalName,
    domain: candidate.domain,
    owner: candidate.owner,
    source_id: candidate.sourceId,
    source_revision: candidate.sourceRevision,
    priority: candidate.priority,
    enabled: candidate.enabled,
    spec_digest: candidate.specDigest,
    metadata: candidate.metadata,
  });
}

function routeLeaseDigest(lease: E02RouteLease): string {
  const { leaseDigest: _ignored, ...withoutDigest } = lease;
  return digest(withoutDigest);
}

function routeError(code: string, message: string, details: JsonObject = {}): Error {
  return Object.assign(new Error(message), {
    name: "E02RouteError",
    code,
    details: cloneJson(details),
  });
}
