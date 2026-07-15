import {
  type Clock,
  type IdFactory,
  type JsonRecord,
  RandomIdFactory,
  RuntimeInvariantError,
  SystemClock,
  assertNonEmpty,
  assertNonNegativeInteger,
  compareNumbers,
  compareStrings,
  deepClone,
  digestJson,
  uniqueSorted,
} from "../core/runtime-primitives.js";

export const CREDENTIAL_KINDS = [
  "anonymous",
  "api_key",
  "bearer_token",
  "oauth2",
  "aws_sigv4",
  "custom_header",
] as const;

export type CredentialKind = (typeof CREDENTIAL_KINDS)[number];
export type CredentialStatus =
  | "active"
  | "refreshing"
  | "cooldown"
  | "quarantined"
  | "revoked";

export interface CredentialSecret {
  value: string;
  refreshToken?: string;
  auxiliary?: Record<string, string>;
}

export interface SecretVaultPort {
  put(reference: string, secret: CredentialSecret): Promise<void>;
  get(reference: string): Promise<CredentialSecret | null>;
  delete(reference: string): Promise<void>;
}

export interface CredentialRegistration {
  credentialId?: string;
  providerId: string;
  accountId: string;
  kind: CredentialKind;
  secret: CredentialSecret;
  scopes?: string[];
  allowedModels?: string[];
  headerName?: string;
  priority?: number;
  expiresAt?: number | null;
  refreshAfter?: number | null;
  metadata?: JsonRecord;
}

export interface CredentialRecord {
  credentialId: string;
  providerId: string;
  accountId: string;
  kind: CredentialKind;
  vaultReference: string;
  scopes: string[];
  allowedModels: string[];
  headerName: string | null;
  priority: number;
  status: CredentialStatus;
  createdAt: number;
  updatedAt: number;
  lastUsedAt: number | null;
  expiresAt: number | null;
  refreshAfter: number | null;
  cooldownUntil: number | null;
  consecutiveFailures: number;
  totalSuccesses: number;
  totalFailures: number;
  revision: number;
  metadata: JsonRecord;
}

export interface CredentialLease {
  leaseId: string;
  credentialId: string;
  holderId: string;
  acquiredAt: number;
  expiresAt: number;
  revision: number;
}

export interface ResolvedCredential {
  credentialId: string;
  providerId: string;
  accountId: string;
  kind: CredentialKind;
  headers: Record<string, string>;
  signingMaterial: CredentialSecret | null;
  expiresAt: number | null;
  revision: number;
}

export interface CredentialSelectionRequest {
  providerId: string;
  modelId: string;
  requiredScopes?: string[];
  excludedCredentialIds?: string[];
  minimumValidityMilliseconds?: number;
}

export interface CredentialRefreshResult {
  secret: CredentialSecret;
  expiresAt: number | null;
  refreshAfter: number | null;
  scopes?: string[];
  metadata?: JsonRecord;
}

export type CredentialRefresher = (
  record: Readonly<CredentialRecord>,
  current: Readonly<CredentialSecret>,
) => Promise<CredentialRefreshResult>;

export interface CredentialRuntimeSnapshot {
  version: "zyra.provider-credentials/v1";
  revision: number;
  records: CredentialRecord[];
  leases: CredentialLease[];
  checksum: string;
}

export interface CredentialRuntimeOptions {
  clock?: Clock;
  ids?: IdFactory;
  refreshLeaseMilliseconds?: number;
  cooldownBaseMilliseconds?: number;
  quarantineFailureThreshold?: number;
}

export class InMemorySecretVault implements SecretVaultPort {
  private readonly secrets = new Map<string, CredentialSecret>();

  async put(reference: string, secret: CredentialSecret): Promise<void> {
    assertNonEmpty(reference, "reference");
    validateSecret(secret);
    this.secrets.set(reference, deepClone(secret));
  }

  async get(reference: string): Promise<CredentialSecret | null> {
    const value = this.secrets.get(reference);
    return value === undefined ? null : deepClone(value);
  }

  async delete(reference: string): Promise<void> {
    this.secrets.delete(reference);
  }

  size(): number {
    return this.secrets.size;
  }
}

export class ProviderCredentialRuntime {
  private readonly clock: Clock;
  private readonly ids: IdFactory;
  private readonly refreshLeaseMilliseconds: number;
  private readonly cooldownBaseMilliseconds: number;
  private readonly quarantineFailureThreshold: number;
  private readonly records = new Map<string, CredentialRecord>();
  private readonly leases = new Map<string, CredentialLease>();
  private revision = 0;

  constructor(
    private readonly vault: SecretVaultPort,
    options: CredentialRuntimeOptions = {},
  ) {
    this.clock = options.clock ?? new SystemClock();
    this.ids = options.ids ?? new RandomIdFactory();
    this.refreshLeaseMilliseconds = options.refreshLeaseMilliseconds ?? 30_000;
    this.cooldownBaseMilliseconds = options.cooldownBaseMilliseconds ?? 1_000;
    this.quarantineFailureThreshold = options.quarantineFailureThreshold ?? 5;
    assertNonNegativeInteger(
      this.refreshLeaseMilliseconds,
      "refreshLeaseMilliseconds",
    );
    assertNonNegativeInteger(
      this.cooldownBaseMilliseconds,
      "cooldownBaseMilliseconds",
    );
    assertNonNegativeInteger(
      this.quarantineFailureThreshold,
      "quarantineFailureThreshold",
    );
  }

  async register(input: CredentialRegistration): Promise<CredentialRecord> {
    validateRegistration(input);
    const credentialId = input.credentialId ?? this.ids.next("credential");
    if (this.records.has(credentialId)) {
      throw new RuntimeInvariantError("credential_already_exists", {
        credentialId,
      });
    }
    const now = this.clock.now();
    const vaultReference = `zyra-credential/${credentialId}`;
    await this.vault.put(vaultReference, input.secret);
    const record: CredentialRecord = {
      credentialId,
      providerId: input.providerId,
      accountId: input.accountId,
      kind: input.kind,
      vaultReference,
      scopes: uniqueSorted(input.scopes ?? []),
      allowedModels: uniqueSorted(input.allowedModels ?? []),
      headerName: input.headerName ?? null,
      priority: input.priority ?? 0,
      status: "active",
      createdAt: now,
      updatedAt: now,
      lastUsedAt: null,
      expiresAt: input.expiresAt ?? null,
      refreshAfter: input.refreshAfter ?? null,
      cooldownUntil: null,
      consecutiveFailures: 0,
      totalSuccesses: 0,
      totalFailures: 0,
      revision: 1,
      metadata: deepClone(input.metadata ?? {}),
    };
    this.records.set(credentialId, record);
    this.bumpRevision();
    return deepClone(record);
  }

  async rotate(
    credentialId: string,
    expectedRevision: number,
    secret: CredentialSecret,
    update: {
      expiresAt?: number | null;
      refreshAfter?: number | null;
      scopes?: string[];
      metadata?: JsonRecord;
    } = {},
  ): Promise<CredentialRecord> {
    validateSecret(secret);
    const record = this.requireRecord(credentialId);
    this.assertRevision(record, expectedRevision);
    if (record.status === "revoked") {
      throw new RuntimeInvariantError("credential_revoked", { credentialId });
    }
    await this.vault.put(record.vaultReference, secret);
    const next = this.updateRecord(record, {
      status: "active",
      expiresAt: update.expiresAt ?? record.expiresAt,
      refreshAfter: update.refreshAfter ?? record.refreshAfter,
      scopes: update.scopes === undefined ? record.scopes : uniqueSorted(update.scopes),
      metadata: update.metadata === undefined ? record.metadata : deepClone(update.metadata),
      cooldownUntil: null,
      consecutiveFailures: 0,
    });
    return deepClone(next);
  }

  select(request: CredentialSelectionRequest): CredentialRecord {
    assertNonEmpty(request.providerId, "providerId");
    assertNonEmpty(request.modelId, "modelId");
    const now = this.clock.now();
    const excluded = new Set(request.excludedCredentialIds ?? []);
    const scopes = new Set(request.requiredScopes ?? []);
    const minimumValidity = request.minimumValidityMilliseconds ?? 0;
    assertNonNegativeInteger(minimumValidity, "minimumValidityMilliseconds");
    const candidates = [...this.records.values()].filter((record) => {
      if (
        record.providerId !== request.providerId ||
        record.status === "revoked" ||
        record.status === "quarantined" ||
        record.status === "refreshing" ||
        excluded.has(record.credentialId)
      ) {
        return false;
      }
      if (record.status === "cooldown" && (record.cooldownUntil ?? 0) > now) {
        return false;
      }
      if (
        record.expiresAt !== null &&
        record.expiresAt - now < minimumValidity
      ) {
        return false;
      }
      if (
        record.allowedModels.length > 0 &&
        !record.allowedModels.includes(request.modelId)
      ) {
        return false;
      }
      return [...scopes].every((scope) => record.scopes.includes(scope));
    });
    candidates.sort(credentialComparator);
    const selected = candidates[0];
    if (selected === undefined) {
      throw new RuntimeInvariantError("no_eligible_credential", {
        providerId: request.providerId,
        modelId: request.modelId,
        requiredScopes: [...scopes].sort(compareStrings),
      });
    }
    return deepClone(selected);
  }

  needsRefresh(credentialId: string): boolean {
    const record = this.requireRecord(credentialId);
    const now = this.clock.now();
    return (
      record.status !== "revoked" &&
      record.status !== "quarantined" &&
      ((record.refreshAfter !== null && record.refreshAfter <= now) ||
        (record.expiresAt !== null && record.expiresAt <= now))
    );
  }

  acquireRefreshLease(
    credentialId: string,
    holderId: string,
  ): CredentialLease {
    const record = this.requireRecord(credentialId);
    assertNonEmpty(holderId, "holderId");
    this.expireLeases();
    const existing = this.leases.get(credentialId);
    if (existing !== undefined) {
      if (existing.holderId === holderId) {
        return deepClone(existing);
      }
      throw new RuntimeInvariantError("credential_refresh_lease_held", {
        credentialId,
        holderId: existing.holderId,
        expiresAt: existing.expiresAt,
      });
    }
    if (record.status === "revoked" || record.status === "quarantined") {
      throw new RuntimeInvariantError("credential_not_refreshable", {
        credentialId,
        status: record.status,
      });
    }
    const acquiredAt = this.clock.now();
    const lease: CredentialLease = {
      leaseId: this.ids.next("credential-refresh"),
      credentialId,
      holderId,
      acquiredAt,
      expiresAt: acquiredAt + this.refreshLeaseMilliseconds,
      revision: record.revision,
    };
    this.leases.set(credentialId, lease);
    this.updateRecord(record, { status: "refreshing" });
    return deepClone(lease);
  }

  async refresh(
    leaseId: string,
    refresher: CredentialRefresher,
  ): Promise<CredentialRecord> {
    this.expireLeases();
    const lease = [...this.leases.values()].find(
      (candidate) => candidate.leaseId === leaseId,
    );
    if (lease === undefined) {
      throw new RuntimeInvariantError("unknown_credential_refresh_lease", {
        leaseId,
      });
    }
    const record = this.requireRecord(lease.credentialId);
    this.assertRevision(record, lease.revision + 1);
    const secret = await this.vault.get(record.vaultReference);
    if (secret === null) {
      this.leases.delete(record.credentialId);
      this.updateRecord(record, { status: "quarantined" });
      throw new RuntimeInvariantError("credential_secret_missing", {
        credentialId: record.credentialId,
      });
    }
    try {
      const result = await refresher(deepClone(record), deepClone(secret));
      validateSecret(result.secret);
      await this.vault.put(record.vaultReference, result.secret);
      this.leases.delete(record.credentialId);
      return deepClone(
        this.updateRecord(record, {
          status: "active",
          expiresAt: result.expiresAt,
          refreshAfter: result.refreshAfter,
          scopes:
            result.scopes === undefined
              ? record.scopes
              : uniqueSorted(result.scopes),
          metadata:
            result.metadata === undefined
              ? record.metadata
              : deepClone(result.metadata),
          cooldownUntil: null,
          consecutiveFailures: 0,
        }),
      );
    } catch (error) {
      this.leases.delete(record.credentialId);
      this.recordFailure(record.credentialId, "refresh_failed");
      throw error;
    }
  }

  releaseRefreshLease(leaseId: string): void {
    const lease = [...this.leases.values()].find(
      (candidate) => candidate.leaseId === leaseId,
    );
    if (lease === undefined) {
      return;
    }
    this.leases.delete(lease.credentialId);
    const record = this.records.get(lease.credentialId);
    if (record?.status === "refreshing") {
      this.updateRecord(record, { status: "active" });
    }
  }

  async resolve(credentialId: string): Promise<ResolvedCredential> {
    const record = this.requireUsableRecord(credentialId);
    const secret = await this.vault.get(record.vaultReference);
    if (secret === null) {
      this.updateRecord(record, { status: "quarantined" });
      throw new RuntimeInvariantError("credential_secret_missing", {
        credentialId,
      });
    }
    const headers: Record<string, string> = {};
    let signingMaterial: CredentialSecret | null = null;
    if (record.kind === "anonymous") {
      // Anonymous/local providers participate in credential lifecycle accounting
      // without leaking a synthetic secret into request headers.
    } else if (record.kind === "api_key") {
      headers[record.headerName ?? "x-api-key"] = secret.value;
    } else if (record.kind === "bearer_token" || record.kind === "oauth2") {
      headers.authorization = `Bearer ${secret.value}`;
    } else if (record.kind === "custom_header") {
      headers[record.headerName ?? "x-provider-token"] = secret.value;
    } else {
      signingMaterial = deepClone(secret);
    }
    this.updateRecord(record, { lastUsedAt: this.clock.now() });
    return {
      credentialId,
      providerId: record.providerId,
      accountId: record.accountId,
      kind: record.kind,
      headers,
      signingMaterial,
      expiresAt: record.expiresAt,
      revision: record.revision + 1,
    };
  }

  recordSuccess(credentialId: string): CredentialRecord {
    const record = this.requireRecord(credentialId);
    return deepClone(
      this.updateRecord(record, {
        status: "active",
        cooldownUntil: null,
        consecutiveFailures: 0,
        totalSuccesses: record.totalSuccesses + 1,
      }),
    );
  }

  recordFailure(credentialId: string, reason: string): CredentialRecord {
    const record = this.requireRecord(credentialId);
    assertNonEmpty(reason, "reason");
    const failures = record.consecutiveFailures + 1;
    const quarantined = failures >= this.quarantineFailureThreshold;
    const cooldown =
      this.cooldownBaseMilliseconds * Math.min(64, 2 ** Math.max(0, failures - 1));
    return deepClone(
      this.updateRecord(record, {
        status: quarantined ? "quarantined" : "cooldown",
        cooldownUntil: quarantined ? null : this.clock.now() + cooldown,
        consecutiveFailures: failures,
        totalFailures: record.totalFailures + 1,
        metadata: { ...record.metadata, lastFailureReason: reason },
      }),
    );
  }

  unquarantine(credentialId: string): CredentialRecord {
    const record = this.requireRecord(credentialId);
    if (record.status !== "quarantined") {
      return deepClone(record);
    }
    return deepClone(
      this.updateRecord(record, {
        status: "active",
        cooldownUntil: null,
        consecutiveFailures: 0,
      }),
    );
  }

  async revoke(credentialId: string): Promise<CredentialRecord> {
    const record = this.requireRecord(credentialId);
    await this.vault.delete(record.vaultReference);
    this.leases.delete(credentialId);
    return deepClone(
      this.updateRecord(record, {
        status: "revoked",
        cooldownUntil: null,
        expiresAt: this.clock.now(),
      }),
    );
  }

  get(credentialId: string): CredentialRecord {
    return deepClone(this.requireRecord(credentialId));
  }

  list(providerId?: string): CredentialRecord[] {
    return [...this.records.values()]
      .filter((record) => providerId === undefined || record.providerId === providerId)
      .sort(credentialComparator)
      .map((record) => deepClone(record));
  }

  snapshot(): CredentialRuntimeSnapshot {
    this.expireLeases();
    const body = {
      version: "zyra.provider-credentials/v1" as const,
      revision: this.revision,
      records: this.list(),
      leases: [...this.leases.values()]
        .sort((left, right) => compareStrings(left.credentialId, right.credentialId))
        .map((lease) => deepClone(lease)),
    };
    return { ...body, checksum: digestJson(body) };
  }

  restore(snapshot: CredentialRuntimeSnapshot): void {
    const { checksum, ...body } = snapshot;
    if (snapshot.version !== "zyra.provider-credentials/v1") {
      throw new RuntimeInvariantError("unsupported_credential_snapshot", {
        version: snapshot.version,
      });
    }
    if (digestJson(body) !== checksum) {
      throw new RuntimeInvariantError("credential_snapshot_checksum_mismatch");
    }
    this.records.clear();
    this.leases.clear();
    for (const source of snapshot.records) {
      const record = validateRecord(source);
      if (this.records.has(record.credentialId)) {
        throw new RuntimeInvariantError("duplicate_credential_in_snapshot", {
          credentialId: record.credentialId,
        });
      }
      this.records.set(record.credentialId, record);
    }
    for (const lease of snapshot.leases) {
      if (!this.records.has(lease.credentialId)) {
        throw new RuntimeInvariantError("credential_lease_without_record", {
          credentialId: lease.credentialId,
        });
      }
      this.leases.set(lease.credentialId, deepClone(lease));
    }
    this.revision = snapshot.revision;
    this.expireLeases();
  }

  private requireRecord(credentialId: string): CredentialRecord {
    const record = this.records.get(credentialId);
    if (record === undefined) {
      throw new RuntimeInvariantError("unknown_credential", { credentialId });
    }
    return record;
  }

  private requireUsableRecord(credentialId: string): CredentialRecord {
    const record = this.requireRecord(credentialId);
    const now = this.clock.now();
    if (record.status === "cooldown" && (record.cooldownUntil ?? 0) <= now) {
      return this.updateRecord(record, {
        status: "active",
        cooldownUntil: null,
      });
    }
    if (record.status !== "active") {
      throw new RuntimeInvariantError("credential_not_active", {
        credentialId,
        status: record.status,
      });
    }
    if (record.expiresAt !== null && record.expiresAt <= now) {
      throw new RuntimeInvariantError("credential_expired", {
        credentialId,
        expiresAt: record.expiresAt,
      });
    }
    return record;
  }

  private assertRevision(record: CredentialRecord, expected: number): void {
    if (record.revision !== expected) {
      throw new RuntimeInvariantError("credential_revision_conflict", {
        credentialId: record.credentialId,
        expected,
        actual: record.revision,
      });
    }
  }

  private updateRecord(
    record: CredentialRecord,
    update: Partial<CredentialRecord>,
  ): CredentialRecord {
    const next: CredentialRecord = {
      ...record,
      ...deepClone(update),
      credentialId: record.credentialId,
      vaultReference: record.vaultReference,
      updatedAt: this.clock.now(),
      revision: record.revision + 1,
    };
    this.records.set(record.credentialId, next);
    this.bumpRevision();
    return next;
  }

  private expireLeases(): void {
    const now = this.clock.now();
    for (const [credentialId, lease] of this.leases) {
      if (lease.expiresAt > now) {
        continue;
      }
      this.leases.delete(credentialId);
      const record = this.records.get(credentialId);
      if (record?.status === "refreshing") {
        this.updateRecord(record, { status: "active" });
      }
    }
  }

  private bumpRevision(): void {
    this.revision += 1;
  }
}

function validateRegistration(input: CredentialRegistration): void {
  assertNonEmpty(input.providerId, "providerId");
  assertNonEmpty(input.accountId, "accountId");
  if (!CREDENTIAL_KINDS.includes(input.kind)) {
    throw new RuntimeInvariantError("unsupported_credential_kind", {
      kind: input.kind,
    });
  }
  validateSecret(input.secret);
  if (input.priority !== undefined && !Number.isSafeInteger(input.priority)) {
    throw new RuntimeInvariantError("invalid_credential_priority", {
      priority: input.priority,
    });
  }
  if (input.expiresAt !== undefined && input.expiresAt !== null) {
    assertNonNegativeInteger(input.expiresAt, "expiresAt");
  }
  if (input.refreshAfter !== undefined && input.refreshAfter !== null) {
    assertNonNegativeInteger(input.refreshAfter, "refreshAfter");
  }
  if (input.kind === "custom_header" && input.headerName === undefined) {
    throw new RuntimeInvariantError("custom_header_name_required");
  }
}

function validateSecret(secret: CredentialSecret): void {
  assertNonEmpty(secret.value, "secret.value");
  if (secret.refreshToken !== undefined) {
    assertNonEmpty(secret.refreshToken, "secret.refreshToken");
  }
  for (const [key, value] of Object.entries(secret.auxiliary ?? {})) {
    assertNonEmpty(key, "secret.auxiliary.key");
    assertNonEmpty(value, `secret.auxiliary.${key}`);
  }
}

function validateRecord(source: CredentialRecord): CredentialRecord {
  assertNonEmpty(source.credentialId, "credentialId");
  assertNonEmpty(source.providerId, "providerId");
  assertNonEmpty(source.accountId, "accountId");
  assertNonEmpty(source.vaultReference, "vaultReference");
  assertNonNegativeInteger(source.revision, "revision");
  return deepClone(source);
}

function credentialComparator(
  left: CredentialRecord,
  right: CredentialRecord,
): number {
  return (
    compareNumbers(right.priority, left.priority) ||
    compareNumbers(left.consecutiveFailures, right.consecutiveFailures) ||
    compareNumbers(left.lastUsedAt ?? 0, right.lastUsedAt ?? 0) ||
    compareStrings(left.credentialId, right.credentialId)
  );
}
