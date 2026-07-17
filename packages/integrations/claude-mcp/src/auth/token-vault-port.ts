import type { JsonObject } from "../contracts.ts";
import {
  cloneJson,
  constantTimeTextEqual,
  deterministicMcpId,
  monotonicNow,
  randomMcpSecret,
  sha256,
} from "../core/canonical.ts";
import { McpRuntimeError } from "../core/failure.ts";

export type McpCredentialKind =
  | "access_token"
  | "refresh_token"
  | "client_secret"
  | "authorization_code"
  | "pkce_verifier"
  | "state_nonce"
  | "dynamic_client_registration";

export interface McpCredentialBinding {
  serverId: string;
  providerId: string;
  subject: string;
  audience: string;
  scopes: string[];
  clientId: string | null;
  kind: McpCredentialKind;
}

export interface McpCredentialMetadata {
  handle: string;
  binding: McpCredentialBinding;
  revision: number;
  valueDigest: string;
  createdAt: string;
  updatedAt: string;
  expiresAt: string | null;
  revokedAt: string | null;
  metadata: JsonObject;
}

export interface McpCredentialWrite {
  binding: McpCredentialBinding;
  value: string;
  expiresAt?: string | null;
  expectedRevision?: number | null;
  metadata?: JsonObject;
}

export interface McpCredentialLease {
  handle: string;
  value: string;
  metadata: McpCredentialMetadata;
  leaseId: string;
  leasedAt: string;
  expiresAt: string;
}

export interface McpTokenVaultSnapshot {
  version: "zyra.mcp-token-vault/v1";
  revision: number;
  metadata: McpCredentialMetadata[];
  valueDigests: Record<string, string>;
  capturedAt: string;
  valuesIncluded: false;
}

export interface McpTokenVaultAdapter {
  store(write: McpCredentialWrite): Promise<McpCredentialMetadata>;
  read(handle: string, binding?: Partial<McpCredentialBinding>): Promise<McpCredentialLease | null>;
  metadata(handle: string): Promise<McpCredentialMetadata | null>;
  find(binding: Partial<McpCredentialBinding>): Promise<McpCredentialMetadata[]>;
  revoke(handle: string, expectedRevision?: number): Promise<McpCredentialMetadata>;
  delete(handle: string, expectedRevision?: number): Promise<boolean>;
  snapshot(): Promise<McpTokenVaultSnapshot>;
}

interface StoredCredential {
  metadata: McpCredentialMetadata;
  value: string;
}

export class McpTokenVaultPort implements McpTokenVaultAdapter {
  private readonly values = new Map<string, StoredCredential>();
  private readonly now: () => Date;
  private readonly leaseDurationMs: number;
  private revision = 0;
  private lastTimestamp: string | null = null;

  constructor(options: { now?: () => Date; leaseDurationMs?: number } = {}) {
    this.now = options.now ?? (() => new Date());
    this.leaseDurationMs = options.leaseDurationMs ?? 30_000;
  }

  async store(write: McpCredentialWrite): Promise<McpCredentialMetadata> {
    const binding = normalizeBinding(write.binding);
    if (!write.value) throw vaultError(binding.serverId, "empty_credential", "credential value cannot be empty");
    const handle = credentialHandle(binding);
    const existing = this.values.get(handle);
    if (write.expectedRevision !== undefined && write.expectedRevision !== null) {
      const actual = existing?.metadata.revision ?? 0;
      if (write.expectedRevision !== actual) {
        throw vaultError(binding.serverId, "credential_revision_conflict", `credential revision ${write.expectedRevision} does not match ${actual}`, {
          handle,
          expected_revision: write.expectedRevision,
          actual_revision: actual,
        });
      }
    }
    const timestamp = this.timestamp();
    const expiresAt = normalizeExpiry(write.expiresAt ?? null, timestamp);
    this.revision += 1;
    const metadata: McpCredentialMetadata = {
      handle,
      binding,
      revision: (existing?.metadata.revision ?? 0) + 1,
      valueDigest: sha256(write.value),
      createdAt: existing?.metadata.createdAt ?? timestamp,
      updatedAt: timestamp,
      expiresAt,
      revokedAt: null,
      metadata: cloneJson(write.metadata ?? {}),
    };
    this.values.set(handle, { metadata, value: write.value });
    return cloneJson(metadata);
  }

  async read(handle: string, binding: Partial<McpCredentialBinding> = {}): Promise<McpCredentialLease | null> {
    const stored = this.values.get(handle);
    if (!stored) return null;
    if (stored.metadata.revokedAt) return null;
    if (stored.metadata.expiresAt && Date.parse(stored.metadata.expiresAt) <= this.now().getTime()) return null;
    if (!bindingMatches(stored.metadata.binding, binding)) return null;
    if (!constantTimeTextEqual(stored.metadata.valueDigest, sha256(stored.value))) {
      throw vaultError(stored.metadata.binding.serverId, "credential_digest_mismatch", `credential ${handle} failed integrity validation`);
    }
    const leasedAt = this.timestamp();
    return {
      handle,
      value: stored.value,
      metadata: cloneJson(stored.metadata),
      leaseId: deterministicMcpId("mcp-credential-lease", {
        handle,
        revision: stored.metadata.revision,
        leased_at: leasedAt,
        nonce: randomMcpSecret(16),
      }),
      leasedAt,
      expiresAt: new Date(Date.parse(leasedAt) + this.leaseDurationMs).toISOString(),
    };
  }

  async metadata(handle: string): Promise<McpCredentialMetadata | null> {
    const value = this.values.get(handle);
    return value ? cloneJson(value.metadata) : null;
  }

  async find(binding: Partial<McpCredentialBinding>): Promise<McpCredentialMetadata[]> {
    return [...this.values.values()]
      .map((stored) => stored.metadata)
      .filter((metadata) => bindingMatches(metadata.binding, binding))
      .sort((left, right) => left.handle.localeCompare(right.handle))
      .map(cloneJson);
  }

  async revoke(handle: string, expectedRevision?: number): Promise<McpCredentialMetadata> {
    const stored = this.values.get(handle);
    if (!stored) throw vaultError("", "credential_not_found", `credential ${handle} does not exist`);
    if (expectedRevision !== undefined && expectedRevision !== stored.metadata.revision) {
      throw vaultError(stored.metadata.binding.serverId, "credential_revision_conflict", `credential revision ${expectedRevision} does not match ${stored.metadata.revision}`);
    }
    if (!stored.metadata.revokedAt) {
      this.revision += 1;
      stored.metadata = {
        ...stored.metadata,
        revision: stored.metadata.revision + 1,
        revokedAt: this.timestamp(),
        updatedAt: this.timestamp(),
      };
    }
    return cloneJson(stored.metadata);
  }

  async delete(handle: string, expectedRevision?: number): Promise<boolean> {
    const stored = this.values.get(handle);
    if (!stored) return false;
    if (expectedRevision !== undefined && expectedRevision !== stored.metadata.revision) {
      throw vaultError(stored.metadata.binding.serverId, "credential_revision_conflict", `credential revision ${expectedRevision} does not match ${stored.metadata.revision}`);
    }
    stored.value = "\0".repeat(stored.value.length);
    this.values.delete(handle);
    this.revision += 1;
    return true;
  }

  async snapshot(): Promise<McpTokenVaultSnapshot> {
    const metadata = [...this.values.values()]
      .map((stored) => cloneJson(stored.metadata))
      .sort((left, right) => left.handle.localeCompare(right.handle));
    const valueDigests: Record<string, string> = {};
    for (const record of metadata) valueDigests[record.handle] = record.valueDigest;
    return {
      version: "zyra.mcp-token-vault/v1",
      revision: this.revision,
      metadata,
      valueDigests,
      capturedAt: this.timestamp(),
      valuesIncluded: false,
    };
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

/**
 * Compatibility name retained for callers that selected the original in-memory
 * implementation before E02 made the canonical custody symbol explicit.
 */
export class InMemoryMcpTokenVault extends McpTokenVaultPort {}

export function credentialHandle(bindingValue: McpCredentialBinding): string {
  const binding = normalizeBinding(bindingValue);
  return deterministicMcpId("mcp-credential", binding, 40);
}

export function normalizeBinding(binding: McpCredentialBinding): McpCredentialBinding {
  if (!binding.serverId) throw vaultError("", "invalid_credential_binding", "credential server id is required");
  if (!binding.providerId) throw vaultError(binding.serverId, "invalid_credential_binding", "credential provider id is required");
  if (!binding.kind) throw vaultError(binding.serverId, "invalid_credential_binding", "credential kind is required");
  return {
    serverId: binding.serverId,
    providerId: binding.providerId,
    subject: binding.subject || "",
    audience: binding.audience || "",
    scopes: [...new Set(binding.scopes ?? [])].sort(),
    clientId: binding.clientId || null,
    kind: binding.kind,
  };
}

function bindingMatches(binding: McpCredentialBinding, partial: Partial<McpCredentialBinding>): boolean {
  if (partial.serverId !== undefined && partial.serverId !== binding.serverId) return false;
  if (partial.providerId !== undefined && partial.providerId !== binding.providerId) return false;
  if (partial.subject !== undefined && partial.subject !== binding.subject) return false;
  if (partial.audience !== undefined && partial.audience !== binding.audience) return false;
  if (partial.clientId !== undefined && partial.clientId !== binding.clientId) return false;
  if (partial.kind !== undefined && partial.kind !== binding.kind) return false;
  if (partial.scopes !== undefined && partial.scopes.some((scope) => !binding.scopes.includes(scope))) return false;
  return true;
}

function normalizeExpiry(value: string | null, timestamp: string): string | null {
  if (value === null) return null;
  if (Number.isNaN(Date.parse(value))) throw vaultError("", "invalid_credential_expiry", "credential expiry is invalid");
  if (Date.parse(value) <= Date.parse(timestamp)) throw vaultError("", "expired_credential_write", "cannot store an already-expired credential");
  return value;
}

function vaultError(serverId: string, code: string, message: string, details: JsonObject = {}): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-vault", { server_id: serverId, code, message }),
    category: "authentication",
    code,
    message,
    serverId,
    retryable: code.includes("conflict"),
    disposition: code.includes("conflict") ? "retry_same_connection" : "reauthorize",
    details,
  });
}
