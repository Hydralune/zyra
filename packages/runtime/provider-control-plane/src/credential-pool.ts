import {
  type Clock,
  SystemClock,
  canonicalJson,
  deepClone,
  digestJson,
} from "./canonical.ts";
import type { CredentialRecord, ProviderRouteLease } from "./contracts.ts";
import { ProviderControlPlaneStore } from "./store.ts";

export interface CredentialPoolSnapshot {
  readonly credentialId: string;
  readonly providerId: string;
  readonly generation: number;
  readonly authenticationFailures: number;
  readonly usageFailures: number;
  readonly transientFailures: number;
  readonly successCount: number;
  readonly cooldownUntil: number;
  readonly lastFailureKind: string;
  readonly lastRouteId: string;
  readonly lastAttemptAt: number;
  readonly lastSuccessAt: number;
  readonly updatedAt: number;
}

interface JsonRow { readonly json: string }

export interface CredentialPoolOptions {
  readonly clock?: Clock;
  readonly authenticationCooldownMilliseconds?: number;
  readonly usageCooldownMilliseconds?: number;
  readonly transientCooldownMilliseconds?: number;
}

export class ProviderCredentialPoolRuntime {
  private readonly store: ProviderControlPlaneStore;
  private readonly clock: Clock;
  private readonly authenticationCooldown: number;
  private readonly usageCooldown: number;
  private readonly transientCooldown: number;

  constructor(store: ProviderControlPlaneStore, options: CredentialPoolOptions = {}) {
    this.store = store;
    this.clock = options.clock ?? new SystemClock();
    this.authenticationCooldown = options.authenticationCooldownMilliseconds ?? 300_000;
    this.usageCooldown = options.usageCooldownMilliseconds ?? 900_000;
    this.transientCooldown = options.transientCooldownMilliseconds ?? 5_000;
    for (const [name, value] of Object.entries({
      authenticationCooldown: this.authenticationCooldown,
      usageCooldown: this.usageCooldown,
      transientCooldown: this.transientCooldown,
    })) {
      if (!Number.isSafeInteger(value) || value < 0) throw new TypeError(`${name} must be a non-negative integer`);
    }
    this.initialize();
  }

  snapshot(credential: CredentialRecord): CredentialPoolSnapshot {
    return this.read(credential.credentialId) ?? initialSnapshot(credential, this.clock.now());
  }

  isEligible(credential: CredentialRecord, now = this.clock.now()): boolean {
    if (credential.status !== "active") return false;
    if (credential.expiresAt !== null && credential.expiresAt <= now) return false;
    return this.snapshot(credential).cooldownUntil <= now;
  }

  excludedCredentialIds(providerId: string): string[] {
    const now = this.clock.now();
    const excluded: string[] = [];
    for (const credential of this.store.listCredentials(providerId)) {
      if (!this.isEligible(credential, now)) excluded.push(credential.credentialId);
    }
    return excluded.sort();
  }

  orderedEligible(providerId: string, modelId: string): CredentialRecord[] {
    const now = this.clock.now();
    return this.store.listCredentials(providerId)
      .filter((credential) => {
        if (!this.isEligible(credential, now)) return false;
        return credential.allowedModels.length === 0 || credential.allowedModels.includes(modelId);
      })
      .sort((left, right) => {
        const leftPool = this.snapshot(left);
        const rightPool = this.snapshot(right);
        return right.priority - left.priority
          || leftPool.authenticationFailures - rightPool.authenticationFailures
          || leftPool.usageFailures - rightPool.usageFailures
          || left.failureCount - right.failureCount
          || leftPool.lastAttemptAt - rightPool.lastAttemptAt
          || left.credentialId.localeCompare(right.credentialId);
      });
  }

  recordAttempt(lease: ProviderRouteLease): CredentialPoolSnapshot {
    const credential = this.store.getCredential(lease.credentialId);
    if (credential === null) throw new TypeError(`credential not found: ${lease.credentialId}`);
    return this.update(credential, (current, now) => ({
      ...current,
      generation: current.generation + 1,
      lastRouteId: lease.routeId,
      lastAttemptAt: now,
      updatedAt: now,
    }));
  }

  recordSuccess(lease: ProviderRouteLease): CredentialPoolSnapshot {
    const credential = this.store.getCredential(lease.credentialId);
    if (credential === null) throw new TypeError(`credential not found: ${lease.credentialId}`);
    return this.update(credential, (current, now) => ({
      ...current,
      generation: current.generation + 1,
      authenticationFailures: 0,
      usageFailures: 0,
      transientFailures: 0,
      successCount: current.successCount + 1,
      cooldownUntil: 0,
      lastFailureKind: "",
      lastRouteId: lease.routeId,
      lastSuccessAt: now,
      updatedAt: now,
    }));
  }

  recordFailure(
    lease: ProviderRouteLease,
    failureKind: string,
    retryAfterMilliseconds: number | null = null,
  ): CredentialPoolSnapshot {
    const credential = this.store.getCredential(lease.credentialId);
    if (credential === null) throw new TypeError(`credential not found: ${lease.credentialId}`);
    return this.update(credential, (current, now) => {
      const authentication = ["authentication_failed", "credential_blocked", "credential_expired"].includes(failureKind);
      const usage = failureKind === "usage_limited";
      const transient = ["rate_limited", "provider_timeout", "provider_unavailable", "stream_timeout"].includes(failureKind);
      const policyCooldown = authentication
        ? this.authenticationCooldown
        : usage
          ? this.usageCooldown
          : transient
            ? this.transientCooldown
            : 0;
      const cooldown = retryAfterMilliseconds === null
        ? policyCooldown
        : Math.max(policyCooldown, retryAfterMilliseconds);
      return {
        ...current,
        generation: current.generation + 1,
        authenticationFailures: current.authenticationFailures + (authentication ? 1 : 0),
        usageFailures: current.usageFailures + (usage ? 1 : 0),
        transientFailures: current.transientFailures + (transient ? 1 : 0),
        cooldownUntil: Math.max(current.cooldownUntil, now + cooldown),
        lastFailureKind: failureKind,
        lastRouteId: lease.routeId,
        lastAttemptAt: now,
        updatedAt: now,
      };
    });
  }

  clearCooldown(credentialId: string): CredentialPoolSnapshot {
    const credential = this.store.getCredential(credentialId);
    if (credential === null) throw new TypeError(`credential not found: ${credentialId}`);
    return this.update(credential, (current, now) => ({
      ...current,
      generation: current.generation + 1,
      cooldownUntil: 0,
      lastFailureKind: "",
      updatedAt: now,
    }));
  }

  list(providerId?: string): CredentialPoolSnapshot[] {
    const rows = providerId === undefined
      ? this.store.db.prepare("SELECT json FROM provider_credential_pool ORDER BY provider_id, credential_id").all()
      : this.store.db.prepare("SELECT json FROM provider_credential_pool WHERE provider_id = ? ORDER BY credential_id").all(providerId);
    return (rows as unknown as JsonRow[]).map((row) => deepClone(JSON.parse(row.json) as CredentialPoolSnapshot));
  }

  private update(
    credential: CredentialRecord,
    mutate: (current: CredentialPoolSnapshot, now: number) => CredentialPoolSnapshot,
  ): CredentialPoolSnapshot {
    return this.store.transaction(() => {
      const current = this.read(credential.credentialId) ?? initialSnapshot(credential, this.clock.now());
      if (current.providerId !== credential.providerId) throw new Error("credential pool provider identity drift");
      const next = mutate(current, this.clock.now());
      this.write(next);
      return deepClone(next);
    });
  }

  private initialize(): void {
    this.store.db.exec(`
      CREATE TABLE IF NOT EXISTS provider_credential_pool (
        credential_id TEXT PRIMARY KEY,
        provider_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        cooldown_until INTEGER NOT NULL,
        last_failure_kind TEXT NOT NULL,
        updated_at INTEGER NOT NULL,
        json TEXT NOT NULL,
        checksum TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_provider_credential_pool_eligibility
        ON provider_credential_pool(provider_id, cooldown_until);
    `);
  }

  private read(credentialId: string): CredentialPoolSnapshot | null {
    const row = this.store.db.prepare(
      "SELECT json FROM provider_credential_pool WHERE credential_id = ?",
    ).get(credentialId) as JsonRow | undefined;
    return row === undefined ? null : deepClone(JSON.parse(row.json) as CredentialPoolSnapshot);
  }

  private write(value: CredentialPoolSnapshot): void {
    const json = canonicalJson(value);
    this.store.db.prepare(`
      INSERT INTO provider_credential_pool(
        credential_id, provider_id, generation, cooldown_until,
        last_failure_kind, updated_at, json, checksum
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(credential_id) DO UPDATE SET
        generation = excluded.generation,
        cooldown_until = excluded.cooldown_until,
        last_failure_kind = excluded.last_failure_kind,
        updated_at = excluded.updated_at,
        json = excluded.json,
        checksum = excluded.checksum
    `).run(
      value.credentialId,
      value.providerId,
      value.generation,
      value.cooldownUntil,
      value.lastFailureKind,
      value.updatedAt,
      json,
      digestJson(value),
    );
  }
}

function initialSnapshot(credential: CredentialRecord, now: number): CredentialPoolSnapshot {
  return {
    credentialId: credential.credentialId,
    providerId: credential.providerId,
    generation: 0,
    authenticationFailures: 0,
    usageFailures: 0,
    transientFailures: 0,
    successCount: 0,
    cooldownUntil: 0,
    lastFailureKind: "",
    lastRouteId: "",
    lastAttemptAt: 0,
    lastSuccessAt: 0,
    updatedAt: now,
  };
}
