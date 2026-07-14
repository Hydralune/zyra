import {
  CREDENTIAL_SCHEMA,
  GatewayProtocolError,
  type CredentialEnvelope,
  type CredentialRequest,
  type JsonValue,
} from "./contracts.ts";
import {
  randomNonce,
  stableId,
  tokenDigest,
} from "./canonical.ts";

export interface CredentialResolver {
  resolve(request: CredentialRequest): Promise<string> | string;
}

interface StoredCredential {
  readonly envelope: CredentialEnvelope;
  readonly secret: string;
}

export class CallbackCredentialResolver implements CredentialResolver {
  readonly callback: (
    request: CredentialRequest,
  ) => Promise<string> | string;

  constructor(
    callback: (request: CredentialRequest) => Promise<string> | string,
  ) {
    this.callback = callback;
  }

  async resolve(request: CredentialRequest): Promise<string> {
    return await this.callback(request);
  }
}

export class CredentialEnvelopeRelay {
  readonly resolver: CredentialResolver;
  readonly maximumTtlMilliseconds: number;
  readonly clock: () => number;
  private readonly active = new Map<string, StoredCredential>();

  constructor(
    resolver: CredentialResolver,
    options: {
      readonly maximumTtlMilliseconds?: number;
      readonly clock?: () => number;
    } = {},
  ) {
    this.resolver = resolver;
    this.maximumTtlMilliseconds =
      options.maximumTtlMilliseconds ?? 300_000;
    this.clock = options.clock ?? Date.now;
  }

  async issue(request: CredentialRequest): Promise<CredentialEnvelope> {
    if (
      request.ttlMilliseconds <= 0 ||
      request.ttlMilliseconds > this.maximumTtlMilliseconds
    ) {
      throw new GatewayProtocolError(
        "credential_ttl_denied",
        "Credential TTL is outside deployment limits",
      );
    }
    let secret: string;
    try {
      secret = await this.resolver.resolve(request);
    } catch (error) {
      throw new GatewayProtocolError(
        "credential_provider_failed",
        "Credential provider failed: " + errorName(error),
        { retryable: true },
      );
    }
    if (!secret) {
      throw new GatewayProtocolError(
        "credential_missing",
        "Credential provider returned empty material",
      );
    }
    const issuedAt = this.clock();
    const nonce = randomNonce();
    const secretHash = tokenDigest(secret);
    const envelope: CredentialEnvelope = Object.freeze({
      schema: CREDENTIAL_SCHEMA,
      envelopeId: stableId("gateway-credential-envelope", {
        requestId: request.requestId,
        sessionId: request.sessionId,
        commandId: request.commandId,
        audience: request.audience,
        scope: [...request.scope].sort(),
        secretDigest: secretHash,
        nonce,
      }),
      requestId: request.requestId,
      sessionId: request.sessionId,
      commandId: request.commandId,
      audience: request.audience,
      scope: Object.freeze([...new Set(request.scope)].sort()),
      secretDigest: secretHash,
      nonce,
      issuedAt,
      expiresAt: issuedAt + request.ttlMilliseconds,
      consumedAt: null,
      metadata: Object.freeze({
        provider: request.provider,
        credentialName: request.credentialName,
        durableSecretStorage: false,
      }),
    });
    this.active.set(envelope.envelopeId, { envelope, secret });
    return envelope;
  }

  consume(
    envelopeId: string,
    binding: {
      readonly sessionId: string;
      readonly commandId: string;
      readonly audience: string;
      readonly requiredScope?: string;
    },
  ): string {
    const stored = this.active.get(envelopeId);
    if (!stored) {
      throw new GatewayProtocolError(
        "credential_replay",
        "Credential envelope is absent, consumed, or lost across restart",
      );
    }
    const { envelope, secret } = stored;
    if (this.clock() >= envelope.expiresAt) {
      this.active.delete(envelopeId);
      throw new GatewayProtocolError(
        "credential_expired",
        "Credential envelope expired",
      );
    }
    if (
      envelope.sessionId !== binding.sessionId ||
      envelope.commandId !== binding.commandId ||
      envelope.audience !== binding.audience
    ) {
      throw new GatewayProtocolError(
        "credential_binding_mismatch",
        "Credential envelope audience or command binding mismatch",
      );
    }
    if (
      binding.requiredScope &&
      !envelope.scope.includes(binding.requiredScope)
    ) {
      throw new GatewayProtocolError(
        "credential_scope_denied",
        "Credential envelope lacks required scope",
      );
    }
    this.active.delete(envelopeId);
    return secret;
  }

  revoke(envelopeId: string): boolean {
    return this.active.delete(envelopeId);
  }

  cleanupExpired(): number {
    const now = this.clock();
    let removed = 0;
    for (const [key, value] of this.active.entries()) {
      if (value.envelope.expiresAt <= now) {
        this.active.delete(key);
        removed += 1;
      }
    }
    return removed;
  }

  descriptor(): JsonValue {
    return {
      relay: "CredentialEnvelopeRelay",
      sourceMechanism: "oh-my-pi credential-free sandbox",
      maximumTtlMilliseconds: this.maximumTtlMilliseconds,
      activeEnvelopes: this.active.size,
      durableSecretStorage: false,
      restartBehavior: "fail_closed",
      singleUse: true,
    };
  }
}

export function redactSecrets(
  value: JsonValue,
  secrets: readonly string[],
): JsonValue {
  const ordered = [...new Set(secrets.filter((item) => item.length >= 4))]
    .sort((left, right) => right.length - left.length);
  return walk(value, ordered, "", 0);
}

function walk(
  value: JsonValue,
  secrets: readonly string[],
  parentKey: string,
  depth: number,
): JsonValue {
  if (depth > 32) {
    return "[REDACTED]";
  }
  const lowered = parentKey
    .toLocaleLowerCase("en-US")
    .replaceAll("-", "_");
  if (
    ["token", "secret", "password", "credential", "authorization", "cookie"]
      .some((fragment) => lowered.includes(fragment))
  ) {
    return value === null ? null : "[REDACTED]";
  }
  if (typeof value === "string") {
    let result = value;
    for (const secret of secrets) {
      result = result.replaceAll(secret, "[REDACTED]");
    }
    result = result
      .replace(
        /\b(authorization\s*[:=]\s*(?:bearer|basic)?\s*)[^\s,;]+/giu,
        "$1[REDACTED]",
      )
      .replace(
        /\b(api[_-]?key|access[_-]?token|password|secret)(\s*[:=]\s*)[^\s,;]+/giu,
        "$1$2[REDACTED]",
      );
    return result;
  }
  if (Array.isArray(value)) {
    return value.map((item) => walk(item, secrets, "", depth + 1));
  }
  if (value !== null && typeof value === "object") {
    const result: Record<string, JsonValue> = {};
    for (const [key, item] of Object.entries(value)) {
      result[key] = walk(item, secrets, key, depth + 1);
    }
    return result;
  }
  return value;
}

function errorName(value: unknown): string {
  return value instanceof Error ? value.name : typeof value;
}
