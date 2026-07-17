import type { JsonObject } from "../contracts.ts";
import {
  cloneJson,
  constantTimeDigestEquals,
  deterministicId,
  digest,
  wildcardMatches,
} from "../e02/canonical.ts";
import type {
  E02Clock,
  PermissionScope,
  PermissionStandingGrant,
} from "../e02/contracts.ts";
import type { PermissionIdentityRecord } from "./model.ts";

export interface StandingGrantIssueInput {
  sessionId: string;
  workspaceRoot: string;
  scope: PermissionScope;
  issuedForDecisionId: string;
  expiresAt: string;
  maxUses: number;
  metadata?: JsonObject;
}
export interface StandingGrantConsumption extends JsonObject {
  grant: PermissionStandingGrant;
  matched: boolean;
  consumed: boolean;
  reason: string;
  revisionBefore: number;
  revisionAfter: number;
  consumedAt: string | null;
  consumptionDigest: string;
}

export class StandingGrantRuntime {
  private readonly clock: E02Clock;
  private readonly grants = new Map<string, PermissionStandingGrant>();
  private revisionValue = 0;

  constructor(clock: E02Clock = () => new Date().toISOString()) {
    this.clock = clock;
  }

  get revision(): number {
    return this.revisionValue;
  }

  issue(input: StandingGrantIssueInput, expectedRevision = this.revisionValue): PermissionStandingGrant {
    this.assertRevision(expectedRevision);
    if (!input.sessionId || !input.issuedForDecisionId) throw new Error("standing grant session and decision binding are required");
    if (!Number.isSafeInteger(input.maxUses) || input.maxUses < 1) throw new Error("standing grant max uses must be positive");
    if (Number.isNaN(Date.parse(input.expiresAt)) || Date.parse(input.expiresAt) <= Date.parse(this.clock())) {
      throw new Error("standing grant expiry must be in the future");
    }
    const now = this.clock();
    const grantId = deterministicId("permission-grant", {
      sessionId: input.sessionId,
      workspaceRoot: input.workspaceRoot,
      scope: input.scope,
      issuedForDecisionId: input.issuedForDecisionId,
      expiresAt: input.expiresAt,
      maxUses: input.maxUses,
    }, 40);
    const existing = this.grants.get(grantId);
    if (existing) {
      const comparable = { ...existing, revision: 0, useCount: 0, issuedAt: "", metadata: {} };
      const requested = {
        grantId,
        sessionId: input.sessionId,
        workspaceRoot: input.workspaceRoot,
        scope: input.scope,
        issuedForDecisionId: input.issuedForDecisionId,
        issuedAt: "",
        expiresAt: new Date(input.expiresAt).toISOString(),
        revision: 0,
        maxUses: input.maxUses,
        useCount: 0,
        revokedAt: null,
        revocationReason: null,
        metadata: {},
      };
      if (digest(comparable) !== digest(requested)) throw new Error(`standing grant id collision ${grantId}`);
      return cloneJson(existing);
    }
    const grant: PermissionStandingGrant = {
      grantId,
      sessionId: input.sessionId,
      workspaceRoot: input.workspaceRoot,
      scope: cloneJson(input.scope),
      issuedForDecisionId: input.issuedForDecisionId,
      issuedAt: now,
      expiresAt: new Date(input.expiresAt).toISOString(),
      revision: 1,
      maxUses: input.maxUses,
      useCount: 0,
      revokedAt: null,
      revocationReason: null,
      metadata: cloneJson(input.metadata ?? {}),
    };
    this.grants.set(grantId, grant);
    this.revisionValue += 1;
    return cloneJson(grant);
  }

  consume(
    identity: PermissionIdentityRecord,
    expectedRevision = this.revisionValue,
  ): StandingGrantConsumption | null {
    this.assertRevision(expectedRevision);
    const candidates = this.list(true)
      .filter((grant) => this.matches(grant, identity))
      .sort((left, right) => scopeRank(right.scope) - scopeRank(left.scope)
        || Date.parse(left.expiresAt) - Date.parse(right.expiresAt)
        || left.grantId.localeCompare(right.grantId));
    const grant = candidates[0];
    if (!grant) return null;
    const revisionBefore = this.revisionValue;
    const now = this.clock();
    const next: PermissionStandingGrant = {
      ...grant,
      revision: grant.revision + 1,
      useCount: grant.useCount + 1,
      metadata: {
        ...grant.metadata,
        last_consumed_at: now,
        last_request_fingerprint: identity.requestFingerprint,
        last_tool_call_id: identity.context.toolCallId,
      },
    };
    this.grants.set(grant.grantId, next);
    this.revisionValue += 1;
    const base = {
      grant: next,
      matched: true,
      consumed: true,
      reason: "standing grant matched the exact permission scope",
      revisionBefore,
      revisionAfter: this.revisionValue,
      consumedAt: now,
    };
    return { ...base, consumptionDigest: digest(base) };
  }

  revoke(
    grantId: string,
    reason: string,
    expectedGrantRevision: number,
    expectedRuntimeRevision = this.revisionValue,
  ): PermissionStandingGrant {
    this.assertRevision(expectedRuntimeRevision);
    const grant = this.grants.get(grantId);
    if (!grant) throw new Error(`unknown standing grant ${grantId}`);
    if (grant.revision !== expectedGrantRevision) {
      throw new Error(`standing grant revision conflict: expected ${expectedGrantRevision}, observed ${grant.revision}`);
    }
    if (grant.revokedAt) return cloneJson(grant);
    const next: PermissionStandingGrant = {
      ...grant,
      revision: grant.revision + 1,
      revokedAt: this.clock(),
      revocationReason: reason,
    };
    this.grants.set(grantId, next);
    this.revisionValue += 1;
    return cloneJson(next);
  }

  matches(grant: PermissionStandingGrant, identity: PermissionIdentityRecord): boolean {
    const context = identity.context;
    if (grant.revokedAt || grant.useCount >= grant.maxUses) return false;
    if (Date.parse(grant.expiresAt) <= Date.parse(this.clock())) return false;
    if (grant.sessionId !== context.sessionId) return false;
    if (grant.workspaceRoot && grant.workspaceRoot !== context.workspaceRoot) return false;
    const scope = grant.scope;
    return wildcardMatches(scope.toolPattern, context.toolName)
      && wildcardMatches(scope.namespacePattern, context.namespace)
      && wildcardMatches(scope.serverPattern, context.serverId)
      && wildcardMatches(scope.commandPattern, context.commandName)
      && wildcardMatches(scope.resourcePattern, context.resourceUri, true)
      && wildcardMatches(scope.operationPattern, context.operation)
      && wildcardMatches(scope.workspacePattern, context.workspaceRoot, true)
      && wildcardMatches(scope.sessionPattern, context.sessionId)
      && wildcardMatches(scope.argumentPattern, JSON.stringify(context.arguments), true);
  }

  list(includeInactive = false): PermissionStandingGrant[] {
    const now = Date.parse(this.clock());
    return [...this.grants.values()]
      .filter((grant) => includeInactive
        || (!grant.revokedAt && grant.useCount < grant.maxUses && Date.parse(grant.expiresAt) > now))
      .map(cloneJson)
      .sort((left, right) => left.grantId.localeCompare(right.grantId));
  }

  snapshot(): JsonObject {
    const base: JsonObject = {
      version: "zyra.e02-standing-grants/v1",
      revision: this.revisionValue,
      grants: this.list(true),
    };
    return { ...base, snapshot_hash: digest(base) };
  }

  restore(snapshot: JsonObject): void {
    if (snapshot.version !== "zyra.e02-standing-grants/v1") throw new Error("unsupported standing grant snapshot");
    const expectedHash = digest({ version: snapshot.version, revision: snapshot.revision, grants: snapshot.grants });
    if (!constantTimeDigestEquals(expectedHash, String(snapshot.snapshot_hash ?? ""))) throw new Error("standing grant snapshot hash mismatch");
    const revision = Number(snapshot.revision);
    if (!Number.isSafeInteger(revision) || revision < 0) throw new Error("standing grant snapshot revision is invalid");
    const grants = Array.isArray(snapshot.grants) ? snapshot.grants as unknown as PermissionStandingGrant[] : [];
    this.grants.clear();
    for (const grant of grants) {
      if (this.grants.has(grant.grantId)) throw new Error(`duplicate standing grant ${grant.grantId}`);
      this.grants.set(grant.grantId, cloneJson(grant));
    }
    this.revisionValue = revision;
  }

  private assertRevision(expectedRevision: number): void {
    if (expectedRevision !== this.revisionValue) {
      throw new Error(`standing grant runtime revision conflict: expected ${expectedRevision}, observed ${this.revisionValue}`);
    }
  }
}

function scopeRank(scope: PermissionScope): number {
  return Object.values(scope).filter((value) => value !== "*").length;
}
