import type { CredentialRecord, SecretMaterial, SecretResolver } from "./contracts.ts";
import {
  type Clock,
  type IdFactory,
  RandomIdFactory,
  SystemClock,
  assertIdentifier,
  assertNonEmpty,
  assertNonNegativeInteger,
  deepClone,
  digestJson,
  fingerprintSecret,
  uniqueSorted,
} from "./canonical.ts";
import { ProviderControlPlaneError } from "./errors.ts";
import { ProviderControlPlaneStore } from "./store.ts";
import { ProviderCatalog } from "./catalog.ts";

export interface CredentialRegistration {
  readonly credentialId?: string;
  readonly integrationId: string;
  readonly providerId: string;
  readonly accountId: string;
  readonly secretRef: string;
  readonly fingerprint: string;
  readonly priority?: number;
  readonly allowedModels?: readonly string[];
  readonly scopes?: readonly string[];
  readonly expiresAt?: number | null;
  readonly refreshAfter?: number | null;
  readonly metadata?: CredentialRecord["metadata"];
}

export interface CredentialSelection {
  readonly providerId: string;
  readonly modelId: string;
  readonly excludedCredentialIds?: readonly string[];
  readonly requiredScopes?: readonly string[];
  readonly minimumValidityMilliseconds?: number;
}

export interface ResolvedCredential {
  readonly record: CredentialRecord;
  readonly material: SecretMaterial | null;
  readonly headers: Readonly<Record<string, string>>;
}

export class EnvironmentSecretResolver implements SecretResolver {
  async resolve(secretRef: string): Promise<SecretMaterial | null> {
    const prefix = "env://";
    if (!secretRef.startsWith(prefix)) return null;
    const name = secretRef.slice(prefix.length);
    assertIdentifier(name, "secret environment name");
    const value = process.env[name];
    return value ? { value } : null;
  }
}

export class CompositeSecretResolver implements SecretResolver {
  private readonly resolvers: readonly SecretResolver[];

  constructor(resolvers: readonly SecretResolver[]) {
    this.resolvers = resolvers;
  }

  async resolve(secretRef: string, signal?: AbortSignal): Promise<SecretMaterial | null> {
    for (const resolver of this.resolvers) {
      const value = await resolver.resolve(secretRef, signal);
      if (value !== null) return value;
    }
    return null;
  }
}

export class InMemorySecretResolver implements SecretResolver {
  private readonly values = new Map<string, SecretMaterial>();

  put(secretRef: string, material: SecretMaterial): void {
    assertNonEmpty(secretRef, "secretRef");
    assertNonEmpty(material.value, "material.value");
    this.values.set(secretRef, deepClone(material));
  }

  delete(secretRef: string): void {
    this.values.delete(secretRef);
  }

  async resolve(secretRef: string): Promise<SecretMaterial | null> {
    return deepClone(this.values.get(secretRef) ?? null);
  }
}

export class CredentialManager {
  private readonly clock: Clock;
  private readonly ids: IdFactory;
  private readonly store: ProviderControlPlaneStore;
  private readonly catalog: ProviderCatalog;
  private readonly secrets: SecretResolver;

  constructor(
    store: ProviderControlPlaneStore,
    catalog: ProviderCatalog,
    secrets: SecretResolver,
    options: { clock?: Clock; ids?: IdFactory } = {},
  ) {
    this.store = store;
    this.catalog = catalog;
    this.secrets = secrets;
    this.clock = options.clock ?? new SystemClock();
    this.ids = options.ids ?? new RandomIdFactory();
  }

  register(input: CredentialRegistration): CredentialRecord {
    validateRegistration(input);
    const provider = this.catalog.provider(input.providerId);
    const integrationId = provider.integrationId ?? input.integrationId;
    if (integrationId !== input.integrationId) throw new TypeError("credential integration does not match provider integration");
    this.catalog.integration(integrationId);
    const credentialId = input.credentialId ?? this.ids.next("credential");
    if (this.store.getCredential(credentialId) !== null) {
      throw new ProviderControlPlaneError({
        layer: "credential",
        kind: "credential_version_conflict",
        message: `credential already exists: ${credentialId}`,
        credentialId,
      });
    }
    const now = this.clock.now();
    const record: CredentialRecord = {
      credentialId,
      integrationId,
      providerId: input.providerId,
      accountId: input.accountId,
      secretRef: input.secretRef,
      fingerprint: input.fingerprint,
      version: 1,
      status: input.expiresAt !== undefined && input.expiresAt !== null && input.expiresAt <= now ? "expired" : "active",
      priority: input.priority ?? 0,
      allowedModels: uniqueSorted(input.allowedModels ?? []),
      scopes: uniqueSorted(input.scopes ?? []),
      expiresAt: input.expiresAt ?? null,
      refreshAfter: input.refreshAfter ?? null,
      blockedReason: null,
      lastUsedAt: null,
      failureCount: 0,
      successCount: 0,
      createdAt: now,
      updatedAt: now,
      metadata: deepClone(input.metadata ?? {}),
    };
    this.store.putCredential(record);
    return deepClone(record);
  }

  rotate(
    credentialId: string,
    expectedVersion: number,
    update: Pick<CredentialRegistration, "secretRef" | "fingerprint" | "expiresAt" | "refreshAfter" | "scopes" | "metadata">,
  ): CredentialRecord {
    const current = this.require(credentialId);
    if (current.version !== expectedVersion) this.versionConflict(current, expectedVersion);
    if (current.status === "revoked") this.unusable(current, "credential_revoked");
    const now = this.clock.now();
    return this.update(current, {
      secretRef: update.secretRef,
      fingerprint: update.fingerprint,
      expiresAt: update.expiresAt ?? current.expiresAt,
      refreshAfter: update.refreshAfter ?? current.refreshAfter,
      scopes: update.scopes === undefined ? current.scopes : uniqueSorted(update.scopes),
      metadata: update.metadata === undefined ? current.metadata : deepClone(update.metadata),
      status: "active",
      blockedReason: null,
      failureCount: 0,
      updatedAt: now,
    });
  }

  select(input: CredentialSelection): CredentialRecord {
    assertIdentifier(input.providerId, "providerId");
    assertIdentifier(input.modelId, "modelId");
    const now = this.clock.now();
    const minimumValidity = input.minimumValidityMilliseconds ?? 0;
    assertNonNegativeInteger(minimumValidity, "minimumValidityMilliseconds");
    const excluded = new Set(input.excludedCredentialIds ?? []);
    const scopes = new Set(input.requiredScopes ?? []);
    const candidates = this.store.listCredentials(input.providerId).filter((record) => {
      if (record.status !== "active" || excluded.has(record.credentialId)) return false;
      if (record.expiresAt !== null && record.expiresAt - now < minimumValidity) return false;
      if (record.allowedModels.length > 0 && !record.allowedModels.includes(input.modelId)) return false;
      return [...scopes].every((scope) => record.scopes.includes(scope));
    });
    candidates.sort((left, right) =>
      right.priority - left.priority ||
      left.failureCount - right.failureCount ||
      (left.lastUsedAt ?? 0) - (right.lastUsedAt ?? 0) ||
      left.credentialId.localeCompare(right.credentialId),
    );
    const selected = candidates[0];
    if (selected === undefined) {
      throw new ProviderControlPlaneError({
        layer: "credential",
        kind: "credential_missing",
        message: `no eligible credential for ${input.providerId}/${input.modelId}`,
        providerId: input.providerId,
        modelId: input.modelId,
        detail: { excludedCredentialIds: [...excluded], requiredScopes: [...scopes] },
      });
    }
    return deepClone(selected);
  }

  async resolvePinned(credentialId: string, version: number, signal?: AbortSignal): Promise<ResolvedCredential> {
    let record = this.require(credentialId);
    record = this.requireUsable(record);
    if (record.version !== version) this.versionConflict(record, version);
    const integration = this.catalog.integration(record.integrationId);
    const material = integration.kind === "anonymous" ? null : await this.secrets.resolve(record.secretRef, signal);
    if (integration.kind !== "anonymous" && material === null) {
      this.block(record.credentialId, record.version, "secret_ref_unresolved");
      throw new ProviderControlPlaneError({
        layer: "credential",
        kind: "credential_blocked",
        message: "credential secret reference could not be resolved",
        credentialId: record.credentialId,
        providerId: record.providerId,
        recoveryIntent: "rotate_credential",
        detail: { secretRef: record.secretRef },
      });
    }
    if (material !== null && fingerprintSecret(material.value) !== record.fingerprint) {
      this.block(record.credentialId, record.version, "secret_fingerprint_mismatch");
      throw new ProviderControlPlaneError({
        layer: "credential",
        kind: "credential_blocked",
        message: "credential fingerprint mismatch",
        credentialId: record.credentialId,
        providerId: record.providerId,
        recoveryIntent: "surface_to_operator",
      });
    }
    const headers: Record<string, string> = {};
    if (material !== null) {
      if (integration.kind === "api_key") headers[integration.headerName ?? "x-api-key"] = material.value;
      else if (integration.kind === "bearer" || integration.kind === "oauth2") headers.authorization = `${integration.authorizationScheme ?? "Bearer"} ${material.value}`;
      else if (integration.kind === "custom_header") headers[integration.headerName ?? "x-provider-token"] = material.value;
    }
    const updated = this.update(record, { lastUsedAt: this.clock.now() }, false);
    return { record: updated, material, headers };
  }

  async resolveRoutePinned(
    routeId: string,
    credentialId: string,
    version: number,
    signal?: AbortSignal,
  ): Promise<ResolvedCredential> {
    const current = this.requireUsable(this.require(credentialId));
    const snapshot = this.store.getRouteCredentialSnapshot(routeId);
    if (snapshot === null) {
      throw new ProviderControlPlaneError({
        layer: "credential",
        kind: "credential_missing",
        message: `route credential snapshot not found: ${routeId}`,
        routeId,
        credentialId,
        recoveryIntent: "change_provider_route",
      });
    }
    const { checksum, ...snapshotBody } = snapshot;
    if (digestJson(snapshotBody) !== checksum) {
      throw new ProviderControlPlaneError({
        layer: "credential",
        kind: "credential_version_conflict",
        message: `route credential snapshot checksum mismatch: ${routeId}`,
        routeId,
        credentialId,
        recoveryIntent: "surface_to_operator",
      });
    }
    if (
      snapshot.credentialId !== credentialId
      || snapshot.credentialVersion !== version
    ) {
      throw new ProviderControlPlaneError({
        layer: "credential",
        kind: "credential_version_conflict",
        message: "route credential snapshot identity mismatch",
        routeId,
        credentialId,
        detail: {
          expectedVersion: version,
          snapshotVersion: snapshot.credentialVersion,
        },
      });
    }
    const material = snapshot.integrationKind === "anonymous"
      ? null
      : await this.secrets.resolve(snapshot.secretRef, signal);
    if (snapshot.integrationKind !== "anonymous" && material === null) {
      throw new ProviderControlPlaneError({
        layer: "credential",
        kind: "credential_blocked",
        message: "pinned credential secret reference could not be resolved",
        routeId,
        credentialId,
        providerId: snapshot.providerId,
        recoveryIntent: "rotate_credential",
      });
    }
    if (
      material !== null
      && fingerprintSecret(material.value) !== snapshot.credentialFingerprint
    ) {
      throw new ProviderControlPlaneError({
        layer: "credential",
        kind: "credential_blocked",
        message: "pinned credential fingerprint mismatch",
        routeId,
        credentialId,
        providerId: snapshot.providerId,
        recoveryIntent: "surface_to_operator",
      });
    }
    const headers: Record<string, string> = {};
    if (material !== null) {
      if (snapshot.integrationKind === "api_key") {
        headers[snapshot.headerName ?? "x-api-key"] = material.value;
      } else if (snapshot.integrationKind === "bearer" || snapshot.integrationKind === "oauth2") {
        headers.authorization = `${snapshot.authorizationScheme ?? "Bearer"} ${material.value}`;
      } else if (snapshot.integrationKind === "custom_header") {
        headers[snapshot.headerName ?? "x-provider-token"] = material.value;
      }
    }
    // Health/status remains a current overlay so revoke/block/expiry fences
    // old routes before request bytes. Auth material stays pinned to the route.
    return {
      record: {
        ...deepClone(current),
        version: snapshot.credentialVersion,
        fingerprint: snapshot.credentialFingerprint,
        secretRef: snapshot.secretRef,
        integrationId: snapshot.integrationId,
      },
      material,
      headers,
    };
  }

  recordSuccessIfCurrent(credentialId: string, expectedVersion: number): CredentialRecord | null {
    const current = this.require(credentialId);
    if (current.version !== expectedVersion) return null;
    return this.recordSuccess(credentialId, expectedVersion);
  }

  recordSuccess(credentialId: string, expectedVersion: number): CredentialRecord {
    const current = this.require(credentialId);
    if (current.version !== expectedVersion) this.versionConflict(current, expectedVersion);
    return this.update(current, { successCount: current.successCount + 1, failureCount: 0 }, false);
  }

  recordFailure(credentialId: string, expectedVersion: number, reason: string): CredentialRecord {
    const current = this.require(credentialId);
    if (current.version !== expectedVersion) this.versionConflict(current, expectedVersion);
    const failures = current.failureCount + 1;
    return this.update(current, {
      failureCount: failures,
      status: failures >= 5 ? "blocked" : current.status,
      blockedReason: failures >= 5 ? reason : current.blockedReason,
      metadata: { ...current.metadata, lastFailureReason: reason },
    }, failures >= 5);
  }

  block(credentialId: string, expectedVersion: number, reason: string): CredentialRecord {
    const current = this.require(credentialId);
    if (current.version !== expectedVersion) this.versionConflict(current, expectedVersion);
    assertNonEmpty(reason, "reason");
    return this.update(current, { status: "blocked", blockedReason: reason });
  }

  revoke(credentialId: string, expectedVersion: number): CredentialRecord {
    const current = this.require(credentialId);
    if (current.version !== expectedVersion) this.versionConflict(current, expectedVersion);
    return this.update(current, { status: "revoked", blockedReason: "revoked", expiresAt: this.clock.now() });
  }

  get(credentialId: string): CredentialRecord {
    return deepClone(this.require(credentialId));
  }

  list(providerId?: string): CredentialRecord[] {
    return this.store.listCredentials(providerId);
  }

  private update(
    current: CredentialRecord,
    patch: Partial<CredentialRecord>,
    bumpVersion = true,
  ): CredentialRecord {
    const next: CredentialRecord = {
      ...deepClone(current),
      ...deepClone(patch),
      credentialId: current.credentialId,
      providerId: current.providerId,
      integrationId: current.integrationId,
      secretRef: patch.secretRef ?? current.secretRef,
      version: current.version + (bumpVersion ? 1 : 0),
      updatedAt: this.clock.now(),
    };
    this.store.putCredential(next);
    return deepClone(next);
  }

  private require(credentialId: string): CredentialRecord {
    assertIdentifier(credentialId, "credentialId");
    const record = this.store.getCredential(credentialId);
    if (record === null) {
      throw new ProviderControlPlaneError({
        layer: "credential",
        kind: "credential_missing",
        message: `credential not found: ${credentialId}`,
        credentialId,
      });
    }
    return record;
  }

  private requireUsable(record: CredentialRecord): CredentialRecord {
    if (record.status === "revoked") this.unusable(record, "credential_revoked");
    if (record.status === "blocked") this.unusable(record, "credential_blocked");
    if (record.status === "expired" || (record.expiresAt !== null && record.expiresAt <= this.clock.now())) {
      if (record.status !== "expired") record = this.update(record, { status: "expired" });
      this.unusable(record, "credential_expired");
    }
    if (record.status !== "active") this.unusable(record, "credential_blocked");
    return record;
  }

  private unusable(record: CredentialRecord, kind: "credential_revoked" | "credential_expired" | "credential_blocked"): never {
    throw new ProviderControlPlaneError({
      layer: "credential",
      kind,
      message: `credential is not usable: ${record.credentialId} (${record.status})`,
      credentialId: record.credentialId,
      providerId: record.providerId,
      recoveryIntent: kind === "credential_expired" ? "refresh_credential" : "rotate_credential",
    });
  }

  private versionConflict(record: CredentialRecord, expectedVersion: number): never {
    throw new ProviderControlPlaneError({
      layer: "credential",
      kind: "credential_version_conflict",
      message: `credential version conflict: expected ${expectedVersion}, actual ${record.version}`,
      credentialId: record.credentialId,
      providerId: record.providerId,
      detail: { expectedVersion, actualVersion: record.version },
    });
  }
}

function validateRegistration(input: CredentialRegistration): void {
  if (input.credentialId !== undefined) assertIdentifier(input.credentialId, "credentialId");
  assertIdentifier(input.integrationId, "integrationId");
  assertIdentifier(input.providerId, "providerId");
  assertNonEmpty(input.accountId, "accountId");
  assertNonEmpty(input.secretRef, "secretRef");
  if (!/^(env|vault|sealed|memory):\/\/.+/.test(input.secretRef)) throw new TypeError("secretRef must use an approved reference scheme");
  if (!/^sha256:[0-9a-f]{16}$/i.test(input.fingerprint)) throw new TypeError("fingerprint must be a truncated SHA-256 fingerprint");
  if (input.priority !== undefined && !Number.isSafeInteger(input.priority)) throw new TypeError("priority must be a safe integer");
  if (input.expiresAt !== undefined && input.expiresAt !== null) assertNonNegativeInteger(input.expiresAt, "expiresAt");
  if (input.refreshAfter !== undefined && input.refreshAfter !== null) assertNonNegativeInteger(input.refreshAfter, "refreshAfter");
}
