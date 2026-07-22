import {
  type Clock,
  type IdFactory,
  RandomIdFactory,
  SystemClock,
  assertIdentifier,
  assertNonEmpty,
  canonicalJson,
  canonicalize,
  deepClone,
  digestJson,
} from "./canonical.ts";
import type { JsonRecord } from "./canonical.ts";
import type { CredentialRecord, IntegrationDefinition } from "./contracts.ts";
import { ProviderCatalog } from "./catalog.ts";
import { CredentialManager } from "./credentials.ts";
import { ProviderControlPlaneError } from "./errors.ts";
import { ProviderControlPlaneStore } from "./store.ts";

export type CredentialRefreshState =
  | "running"
  | "applied"
  | "failed"
  | "superseded"
  | "abandoned";

export interface CredentialRefreshRequest {
  readonly refreshId?: string;
  readonly credentialId: string;
  readonly expectedVersion: number;
  readonly reason: "scheduled" | "expired" | "authentication_failed" | "manual";
  readonly requestedBy: string;
  readonly leaseMilliseconds?: number;
  readonly metadata?: Readonly<Record<string, unknown>>;
}

export interface CredentialRefreshMaterial {
  readonly secretRef: string;
  readonly fingerprint: string;
  readonly expiresAt: number | null;
  readonly refreshAfter: number | null;
  readonly scopes?: readonly string[];
  readonly metadata?: Readonly<Record<string, unknown>>;
}

export interface CredentialRefreshContext {
  readonly refreshId: string;
  readonly ownerToken: string;
  readonly credential: CredentialRecord;
  readonly integration: IntegrationDefinition;
  readonly reason: CredentialRefreshRequest["reason"];
  readonly attempt: number;
}

export interface CredentialRefresher {
  refresh(context: CredentialRefreshContext, signal?: AbortSignal): Promise<CredentialRefreshMaterial>;
}

export interface CredentialRefreshReceipt {
  readonly refreshId: string;
  readonly credentialId: string;
  readonly providerId: string;
  readonly integrationId: string;
  readonly expectedVersion: number;
  readonly resultingVersion: number | null;
  readonly state: CredentialRefreshState;
  readonly reason: CredentialRefreshRequest["reason"];
  readonly requestedBy: string;
  readonly ownerToken: string;
  readonly attempt: number;
  readonly leaseExpiresAt: number;
  readonly startedAt: number;
  readonly completedAt: number | null;
  readonly failureKind: string;
  readonly failureMessage: string;
  readonly outputDigest: string;
  readonly metadata: Readonly<Record<string, unknown>>;
}

export interface CredentialRefreshOptions {
  readonly clock?: Clock;
  readonly ids?: IdFactory;
  readonly defaultLeaseMilliseconds?: number;
  readonly minimumValidityMilliseconds?: number;
}

interface JsonRow { readonly json: string }

export class CredentialRefreshRuntime {
  private readonly store: ProviderControlPlaneStore;
  private readonly catalog: ProviderCatalog;
  private readonly credentials: CredentialManager;
  private readonly clock: Clock;
  private readonly ids: IdFactory;
  private readonly defaultLeaseMilliseconds: number;
  private readonly minimumValidityMilliseconds: number;

  constructor(
    store: ProviderControlPlaneStore,
    catalog: ProviderCatalog,
    credentials: CredentialManager,
    options: CredentialRefreshOptions = {},
  ) {
    this.store = store;
    this.catalog = catalog;
    this.credentials = credentials;
    this.clock = options.clock ?? new SystemClock();
    this.ids = options.ids ?? new RandomIdFactory();
    this.defaultLeaseMilliseconds = options.defaultLeaseMilliseconds ?? 60_000;
    this.minimumValidityMilliseconds = options.minimumValidityMilliseconds ?? 300_000;
    for (const [name, value] of Object.entries({
      defaultLeaseMilliseconds: this.defaultLeaseMilliseconds,
      minimumValidityMilliseconds: this.minimumValidityMilliseconds,
    })) {
      if (!Number.isSafeInteger(value) || value <= 0) throw new TypeError(`${name} must be a positive integer`);
    }
    this.initialize();
  }

  due(providerId?: string, now = this.clock.now()): CredentialRecord[] {
    const records = this.credentials.list(providerId);
    return records
      .filter((credential) => {
        if (credential.status === "revoked" || credential.status === "blocked") return false;
        const integration = this.catalog.integration(credential.integrationId);
        if (!integration.supportsRefresh) return false;
        if (credential.refreshAfter !== null && credential.refreshAfter <= now) return true;
        return credential.expiresAt !== null
          && credential.expiresAt - now <= this.minimumValidityMilliseconds;
      })
      .sort((left, right) =>
        (left.refreshAfter ?? left.expiresAt ?? Number.MAX_SAFE_INTEGER)
          - (right.refreshAfter ?? right.expiresAt ?? Number.MAX_SAFE_INTEGER)
        || left.credentialId.localeCompare(right.credentialId));
  }

  claim(request: CredentialRefreshRequest): CredentialRefreshReceipt {
    validateRequest(request);
    const credential = this.credentials.get(request.credentialId);
    const integration = this.catalog.integration(credential.integrationId);
    if (!integration.supportsRefresh) {
      throw new ProviderControlPlaneError({
        layer: "credential",
        kind: "credential_blocked",
        message: `integration does not support credential refresh: ${integration.integrationId}`,
        credentialId: credential.credentialId,
        providerId: credential.providerId,
        recoveryIntent: "rotate_credential",
      });
    }
    if (credential.version !== request.expectedVersion) {
      throw versionConflict(credential, request.expectedVersion);
    }
    if (credential.status === "revoked" || credential.status === "blocked") {
      throw new ProviderControlPlaneError({
        layer: "credential",
        kind: credential.status === "revoked" ? "credential_revoked" : "credential_blocked",
        message: `credential cannot be refreshed in state ${credential.status}`,
        credentialId: credential.credentialId,
        providerId: credential.providerId,
        recoveryIntent: "rotate_credential",
      });
    }
    const refreshId = request.refreshId ?? this.ids.next("credential_refresh");
    assertIdentifier(refreshId, "refreshId");
    const leaseMilliseconds = request.leaseMilliseconds ?? this.defaultLeaseMilliseconds;
    if (!Number.isSafeInteger(leaseMilliseconds) || leaseMilliseconds <= 0) {
      throw new TypeError("leaseMilliseconds must be a positive integer");
    }
    return this.store.transaction(() => {
      const existing = this.read(refreshId);
      if (existing !== null) {
        if (
          existing.credentialId !== credential.credentialId
          || existing.expectedVersion !== request.expectedVersion
          || existing.reason !== request.reason
        ) {
          throw new ProviderControlPlaneError({
            layer: "credential",
            kind: "credential_version_conflict",
            message: `refresh id is already bound to another request: ${refreshId}`,
            credentialId: credential.credentialId,
            providerId: credential.providerId,
            recoveryIntent: "surface_to_operator",
          });
        }
        if (existing.state === "applied" || existing.state === "superseded") return deepClone(existing);
        if (existing.state === "running" && existing.leaseExpiresAt > this.clock.now()) {
          throw new ProviderControlPlaneError({
            layer: "credential",
            kind: "credential_version_conflict",
            message: `credential refresh is already owned: ${refreshId}`,
            credentialId: credential.credentialId,
            providerId: credential.providerId,
            retryable: true,
            recoveryIntent: "retry_same_route",
          });
        }
      }
      const active = this.runningForCredential(credential.credentialId);
      if (active !== null && active.refreshId !== refreshId && active.leaseExpiresAt > this.clock.now()) {
        throw new ProviderControlPlaneError({
          layer: "credential",
          kind: "credential_version_conflict",
          message: `credential refresh lease is held by ${active.refreshId}`,
          credentialId: credential.credentialId,
          providerId: credential.providerId,
          retryable: true,
          recoveryIntent: "retry_same_route",
        });
      }
      const now = this.clock.now();
      const receipt: CredentialRefreshReceipt = {
        refreshId,
        credentialId: credential.credentialId,
        providerId: credential.providerId,
        integrationId: credential.integrationId,
        expectedVersion: credential.version,
        resultingVersion: null,
        state: "running",
        reason: request.reason,
        requestedBy: request.requestedBy,
        ownerToken: this.ids.next("refresh_owner"),
        attempt: (existing?.attempt ?? 0) + 1,
        leaseExpiresAt: now + leaseMilliseconds,
        startedAt: now,
        completedAt: null,
        failureKind: "",
        failureMessage: "",
        outputDigest: "",
        metadata: canonicalize(request.metadata ?? {}) as Readonly<Record<string, unknown>>,
      };
      this.write(receipt);
      return deepClone(receipt);
    });
  }

  heartbeat(refreshId: string, ownerToken: string, leaseMilliseconds = this.defaultLeaseMilliseconds): CredentialRefreshReceipt {
    assertIdentifier(refreshId, "refreshId");
    assertNonEmpty(ownerToken, "ownerToken");
    if (!Number.isSafeInteger(leaseMilliseconds) || leaseMilliseconds <= 0) throw new TypeError("leaseMilliseconds must be positive");
    return this.store.transaction(() => {
      const receipt = this.requireOwned(refreshId, ownerToken);
      const next = { ...receipt, leaseExpiresAt: this.clock.now() + leaseMilliseconds };
      this.write(next);
      return deepClone(next);
    });
  }

  async execute(
    request: CredentialRefreshRequest,
    refresher: CredentialRefresher,
    signal?: AbortSignal,
  ): Promise<CredentialRefreshReceipt> {
    const claim = this.claim(request);
    if (claim.state === "applied" || claim.state === "superseded") return claim;
    const credential = this.credentials.get(claim.credentialId);
    const integration = this.catalog.integration(claim.integrationId);
    try {
      const material = await refresher.refresh({
        refreshId: claim.refreshId,
        ownerToken: claim.ownerToken,
        credential,
        integration,
        reason: claim.reason,
        attempt: claim.attempt,
      }, signal);
      validateMaterial(material);
      return this.apply(claim.refreshId, claim.ownerToken, material);
    } catch (error) {
      if (this.read(claim.refreshId)?.state === "running") {
        this.fail(claim.refreshId, claim.ownerToken, error);
      }
      throw error;
    }
  }

  apply(
    refreshId: string,
    ownerToken: string,
    material: CredentialRefreshMaterial,
  ): CredentialRefreshReceipt {
    validateMaterial(material);
    return this.store.transaction(() => {
      const receipt = this.requireOwned(refreshId, ownerToken);
      const current = this.credentials.get(receipt.credentialId);
      if (current.version !== receipt.expectedVersion) {
        const superseded: CredentialRefreshReceipt = {
          ...receipt,
          state: "superseded",
          resultingVersion: current.version,
          completedAt: this.clock.now(),
          leaseExpiresAt: 0,
          failureKind: "credential_version_conflict",
          failureMessage: `expected ${receipt.expectedVersion}, actual ${current.version}`,
        };
        this.write(superseded);
        return deepClone(superseded);
      }
      const rotated = this.credentials.rotate(receipt.credentialId, receipt.expectedVersion, {
        secretRef: material.secretRef,
        fingerprint: material.fingerprint,
        expiresAt: material.expiresAt,
        refreshAfter: material.refreshAfter,
        scopes: material.scopes,
        metadata: canonicalize(material.metadata ?? current.metadata) as JsonRecord,
      });
      const outputDigest = digestJson({
        credentialId: rotated.credentialId,
        version: rotated.version,
        fingerprint: rotated.fingerprint,
        expiresAt: rotated.expiresAt,
        refreshAfter: rotated.refreshAfter,
      });
      const applied: CredentialRefreshReceipt = {
        ...receipt,
        state: "applied",
        resultingVersion: rotated.version,
        completedAt: this.clock.now(),
        leaseExpiresAt: 0,
        outputDigest,
      };
      this.write(applied);
      return deepClone(applied);
    });
  }

  fail(refreshId: string, ownerToken: string, error: unknown): CredentialRefreshReceipt {
    return this.store.transaction(() => {
      const receipt = this.requireOwned(refreshId, ownerToken);
      const next: CredentialRefreshReceipt = {
        ...receipt,
        state: "failed",
        completedAt: this.clock.now(),
        leaseExpiresAt: 0,
        failureKind: error instanceof ProviderControlPlaneError ? error.kind : "refresh_failed",
        failureMessage: error instanceof Error ? error.message : String(error),
      };
      this.write(next);
      return deepClone(next);
    });
  }

  recoverStale(now = this.clock.now()): CredentialRefreshReceipt[] {
    return this.store.transaction(() => {
      const stale = this.list().filter((receipt) => receipt.state === "running" && receipt.leaseExpiresAt <= now);
      const recovered = stale.map((receipt): CredentialRefreshReceipt => ({
        ...receipt,
        state: "abandoned",
        completedAt: now,
        leaseExpiresAt: 0,
        failureKind: "refresh_lease_expired",
        failureMessage: "credential refresh owner stopped heartbeating",
      }));
      for (const receipt of recovered) this.write(receipt);
      return deepClone(recovered);
    });
  }

  get(refreshId: string): CredentialRefreshReceipt | null {
    assertIdentifier(refreshId, "refreshId");
    return deepClone(this.read(refreshId));
  }

  list(credentialId?: string): CredentialRefreshReceipt[] {
    const rows = credentialId === undefined
      ? this.store.db.prepare("SELECT json FROM provider_credential_refresh_jobs ORDER BY started_at, refresh_id").all()
      : this.store.db.prepare(
          "SELECT json FROM provider_credential_refresh_jobs WHERE credential_id = ? ORDER BY started_at, refresh_id",
        ).all(credentialId);
    return (rows as unknown as JsonRow[]).map((row) => JSON.parse(row.json) as CredentialRefreshReceipt);
  }

  private initialize(): void {
    this.store.db.exec(`
      CREATE TABLE IF NOT EXISTS provider_credential_refresh_jobs (
        refresh_id TEXT PRIMARY KEY,
        credential_id TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        expected_version INTEGER NOT NULL,
        state TEXT NOT NULL,
        owner_token TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        lease_expires_at INTEGER NOT NULL,
        started_at INTEGER NOT NULL,
        completed_at INTEGER,
        json TEXT NOT NULL,
        checksum TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_provider_refresh_credential_state
        ON provider_credential_refresh_jobs(credential_id, state, lease_expires_at);
    `);
  }

  private read(refreshId: string): CredentialRefreshReceipt | null {
    const row = this.store.db.prepare(
      "SELECT json FROM provider_credential_refresh_jobs WHERE refresh_id = ?",
    ).get(refreshId) as JsonRow | undefined;
    return row === undefined ? null : JSON.parse(row.json) as CredentialRefreshReceipt;
  }

  private runningForCredential(credentialId: string): CredentialRefreshReceipt | null {
    const row = this.store.db.prepare(`
      SELECT json FROM provider_credential_refresh_jobs
      WHERE credential_id = ? AND state = 'running'
      ORDER BY started_at DESC LIMIT 1
    `).get(credentialId) as JsonRow | undefined;
    return row === undefined ? null : JSON.parse(row.json) as CredentialRefreshReceipt;
  }

  private requireOwned(refreshId: string, ownerToken: string): CredentialRefreshReceipt {
    const receipt = this.read(refreshId);
    if (receipt === null) throw new TypeError(`credential refresh not found: ${refreshId}`);
    if (receipt.state !== "running") throw new Error(`credential refresh is not running: ${receipt.state}`);
    if (receipt.ownerToken !== ownerToken) throw new Error("credential refresh owner token mismatch");
    if (receipt.leaseExpiresAt <= this.clock.now()) throw new Error("credential refresh lease expired");
    return receipt;
  }

  private write(receipt: CredentialRefreshReceipt): void {
    const json = canonicalJson(receipt);
    this.store.db.prepare(`
      INSERT INTO provider_credential_refresh_jobs(
        refresh_id, credential_id, provider_id, expected_version, state,
        owner_token, attempt, lease_expires_at, started_at, completed_at,
        json, checksum
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(refresh_id) DO UPDATE SET
        state = excluded.state,
        owner_token = excluded.owner_token,
        attempt = excluded.attempt,
        lease_expires_at = excluded.lease_expires_at,
        completed_at = excluded.completed_at,
        json = excluded.json,
        checksum = excluded.checksum
    `).run(
      receipt.refreshId,
      receipt.credentialId,
      receipt.providerId,
      receipt.expectedVersion,
      receipt.state,
      receipt.ownerToken,
      receipt.attempt,
      receipt.leaseExpiresAt,
      receipt.startedAt,
      receipt.completedAt,
      json,
      digestJson(receipt),
    );
  }
}

function validateRequest(request: CredentialRefreshRequest): void {
  assertIdentifier(request.credentialId, "credentialId");
  if (!Number.isSafeInteger(request.expectedVersion) || request.expectedVersion <= 0) {
    throw new TypeError("expectedVersion must be a positive integer");
  }
  assertNonEmpty(request.requestedBy, "requestedBy");
  if (!["scheduled", "expired", "authentication_failed", "manual"].includes(request.reason)) {
    throw new TypeError(`unsupported refresh reason: ${request.reason}`);
  }
}

function validateMaterial(material: CredentialRefreshMaterial): void {
  assertNonEmpty(material.secretRef, "material.secretRef");
  if (!/^(env|vault|sealed|memory):\/\/.+/.test(material.secretRef)) {
    throw new TypeError("refreshed secretRef must use an approved reference scheme");
  }
  if (!/^sha256:[0-9a-f]{16}$/i.test(material.fingerprint)) {
    throw new TypeError("refreshed fingerprint must be a truncated SHA-256 fingerprint");
  }
  for (const [name, value] of Object.entries({
    expiresAt: material.expiresAt,
    refreshAfter: material.refreshAfter,
  })) {
    if (value !== null && (!Number.isSafeInteger(value) || value < 0)) {
      throw new TypeError(`${name} must be null or a non-negative integer`);
    }
  }
  if (
    material.expiresAt !== null
    && material.refreshAfter !== null
    && material.refreshAfter >= material.expiresAt
  ) {
    throw new TypeError("refreshAfter must precede expiresAt");
  }
}

function versionConflict(credential: CredentialRecord, expectedVersion: number): ProviderControlPlaneError {
  return new ProviderControlPlaneError({
    layer: "credential",
    kind: "credential_version_conflict",
    message: `credential version conflict: expected ${expectedVersion}, actual ${credential.version}`,
    credentialId: credential.credentialId,
    providerId: credential.providerId,
    recoveryIntent: "surface_to_operator",
    detail: { expectedVersion, actualVersion: credential.version },
  });
}
