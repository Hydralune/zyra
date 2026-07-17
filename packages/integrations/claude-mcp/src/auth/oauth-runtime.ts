import type { JsonObject } from "../contracts.ts";
import {
  canonicalJson,
  canonicalObject,
  cloneJson,
  constantTimeTextEqual,
  deterministicMcpId,
  httpUrl,
  monotonicNow,
  randomMcpSecret,
  requiredText,
  sha256,
  sha256Text,
} from "../core/canonical.ts";
import {
  McpRuntimeError,
  normalizeFailure,
  type McpFailureRecord,
} from "../core/failure.ts";
import type {
  McpCredentialBinding,
  McpCredentialMetadata,
  McpTokenVaultAdapter,
} from "./token-vault-port.ts";
import { credentialHandle } from "./token-vault-port.ts";

export type McpOAuthClientAuthentication = "none" | "client_secret_post" | "client_secret_basic";

export interface McpOAuthProviderConfig {
  providerId: string;
  serverId: string;
  issuer: string;
  authorizationEndpoint: string;
  tokenEndpoint: string;
  registrationEndpoint: string | null;
  revocationEndpoint: string | null;
  clientId: string | null;
  clientSecretHandle: string | null;
  clientAuthentication: McpOAuthClientAuthentication;
  redirectUri: string;
  scopes: string[];
  resource: string | null;
  audience: string | null;
  allowDynamicRegistration: boolean;
  tokenEndpointHeaders: Record<string, string>;
  authorizationParameters: Record<string, string>;
  clockSkewSeconds: number;
  metadata: JsonObject;
}

export interface McpOAuthChallengeInput {
  serverId: string;
  providerId: string;
  sessionId: string;
  requestId: string;
  challenge: string;
  returnTo: string | null;
  requestedScopes?: string[];
  metadata?: JsonObject;
}

export interface McpOAuthChallenge {
  challengeId: string;
  serverId: string;
  providerId: string;
  sessionId: string;
  requestId: string;
  state: string;
  authorizationUrl: string;
  redirectUri: string;
  scopes: string[];
  resource: string | null;
  codeChallenge: string;
  codeChallengeMethod: "S256";
  createdAt: string;
  expiresAt: string;
  consumedAt: string | null;
  returnTo: string | null;
  metadata: JsonObject;
}

export interface McpOAuthCallbackInput {
  state: string;
  code: string | null;
  error: string | null;
  errorDescription: string | null;
  sessionId: string;
  requestId: string;
}

export interface McpOAuthTokenSet {
  accessTokenHandle: string;
  refreshTokenHandle: string | null;
  tokenType: string;
  scopes: string[];
  expiresAt: string | null;
  providerId: string;
  serverId: string;
  subject: string;
  audience: string;
  revision: number;
  issuedAt: string;
  metadata: JsonObject;
}

export interface McpOAuthTokenResponse {
  access_token: string;
  token_type?: string;
  expires_in?: number;
  refresh_token?: string;
  scope?: string;
  id_token?: string;
  resource?: string;
  [key: string]: unknown;
}

export interface McpOAuthHttpResponse {
  status: number;
  headers: Record<string, string>;
  body: JsonObject;
}

export type McpOAuthHttpClient = (input: {
  url: string;
  method: "POST";
  headers: Record<string, string>;
  body: URLSearchParams;
  signal?: AbortSignal;
}) => Promise<McpOAuthHttpResponse>;

export interface McpOAuthPoisonRecord {
  poisonId: string;
  providerId: string;
  clientId: string | null;
  reason: string;
  failure: McpFailureRecord | null;
  poisonedAt: string;
  expiresAt: string | null;
  revision: number;
}

export interface McpOAuthSnapshot {
  version: "zyra.mcp-oauth-runtime/v1";
  revision: number;
  providers: McpOAuthProviderConfig[];
  challenges: McpOAuthChallenge[];
  tokenSets: McpOAuthTokenSet[];
  poisonRecords: McpOAuthPoisonRecord[];
  refreshGenerations: Record<string, number>;
  digest: string;
  capturedAt: string;
}

interface ChallengeSecret {
  verifier: string;
  state: string;
  stateDigest: string;
}

export interface McpOAuthRuntimeOptions {
  vault: McpTokenVaultAdapter;
  httpClient?: McpOAuthHttpClient;
  now?: () => Date;
  challengeTtlMs?: number;
  snapshot?: McpOAuthSnapshot | null;
}

export class McpOAuthRuntime {
  private readonly vault: McpTokenVaultAdapter;
  private readonly httpClient: McpOAuthHttpClient;
  private readonly now: () => Date;
  private readonly challengeTtlMs: number;
  private readonly providers = new Map<string, McpOAuthProviderConfig>();
  private readonly challenges = new Map<string, McpOAuthChallenge>();
  private readonly challengeSecrets = new Map<string, ChallengeSecret>();
  private readonly tokenSets = new Map<string, McpOAuthTokenSet>();
  private readonly poisonRecords = new Map<string, McpOAuthPoisonRecord>();
  private readonly refreshPromises = new Map<string, Promise<McpOAuthTokenSet>>();
  private readonly refreshGenerations = new Map<string, number>();
  private revision = 0;
  private lastTimestamp: string | null = null;

  constructor(options: McpOAuthRuntimeOptions) {
    this.vault = options.vault;
    this.httpClient = options.httpClient ?? defaultHttpClient;
    this.now = options.now ?? (() => new Date());
    this.challengeTtlMs = options.challengeTtlMs ?? 10 * 60_000;
    if (options.snapshot) this.restore(options.snapshot);
  }

  register(configValue: McpOAuthProviderConfig): McpOAuthProviderConfig {
    const config = normalizeProvider(configValue);
    const existing = this.providers.get(config.providerId);
    if (existing && existing.serverId !== config.serverId) {
      throw oauthError(config.serverId, "provider_identity_conflict", `OAuth provider ${config.providerId} is already bound to ${existing.serverId}`);
    }
    this.providers.set(config.providerId, config);
    this.revision += 1;
    return cloneJson(config);
  }

  challenge(input: McpOAuthChallengeInput): McpOAuthChallenge {
    const provider = this.requireProvider(input.providerId, input.serverId);
    this.assertClientHealthy(provider);
    const state = randomMcpSecret(32);
    const verifier = randomMcpSecret(64);
    const codeChallenge = Buffer.from(sha256BytesText(verifier), "hex").toString("base64url");
    const scopes = [...new Set([...(provider.scopes ?? []), ...(input.requestedScopes ?? [])])].sort();
    const createdAt = this.timestamp();
    const challengeId = deterministicMcpId("mcp-oauth-challenge", {
      server_id: input.serverId,
      provider_id: input.providerId,
      session_id: input.sessionId,
      request_id: input.requestId,
      state_digest: sha256Text(state),
    }, 40);
    const url = new URL(provider.authorizationEndpoint);
    url.searchParams.set("response_type", "code");
    if (provider.clientId) url.searchParams.set("client_id", provider.clientId);
    url.searchParams.set("redirect_uri", provider.redirectUri);
    url.searchParams.set("state", state);
    url.searchParams.set("code_challenge", codeChallenge);
    url.searchParams.set("code_challenge_method", "S256");
    if (scopes.length) url.searchParams.set("scope", scopes.join(" "));
    if (provider.resource) url.searchParams.set("resource", provider.resource);
    if (provider.audience) url.searchParams.set("audience", provider.audience);
    for (const [key, value] of Object.entries(provider.authorizationParameters)) url.searchParams.set(key, value);
    const challenge: McpOAuthChallenge = {
      challengeId,
      serverId: input.serverId,
      providerId: input.providerId,
      sessionId: input.sessionId,
      requestId: input.requestId,
      state: `[sha256:${sha256Text(state)}]`,
      authorizationUrl: url.toString(),
      redirectUri: provider.redirectUri,
      scopes,
      resource: provider.resource,
      codeChallenge,
      codeChallengeMethod: "S256",
      createdAt,
      expiresAt: new Date(Date.parse(createdAt) + this.challengeTtlMs).toISOString(),
      consumedAt: null,
      returnTo: input.returnTo,
      metadata: cloneJson(input.metadata ?? {}),
    };
    this.challenges.set(challengeId, challenge);
    this.challengeSecrets.set(challengeId, {
      verifier,
      state,
      stateDigest: sha256Text(state),
    });
    this.revision += 1;
    return cloneJson(challenge);
  }

  async callback(input: McpOAuthCallbackInput, signal?: AbortSignal): Promise<McpOAuthTokenSet> {
    const stateDigest = sha256Text(input.state);
    const entry = [...this.challengeSecrets.entries()].find(([, secret]) => constantTimeTextEqual(secret.stateDigest, stateDigest));
    if (!entry) throw oauthError("", "oauth_state_unknown", "OAuth callback state does not match an active challenge");
    const [challengeId, secret] = entry;
    const challenge = this.challenges.get(challengeId);
    if (!challenge) throw oauthError("", "oauth_challenge_missing", "OAuth callback challenge metadata is missing");
    if (challenge.sessionId !== input.sessionId || challenge.requestId !== input.requestId) {
      throw oauthError(challenge.serverId, "oauth_callback_binding_mismatch", "OAuth callback does not match challenge session/request binding");
    }
    if (challenge.consumedAt) throw oauthError(challenge.serverId, "oauth_callback_replayed", "OAuth challenge has already been consumed");
    if (Date.parse(challenge.expiresAt) <= this.now().getTime()) throw oauthError(challenge.serverId, "oauth_challenge_expired", "OAuth challenge has expired");
    if (input.error) {
      challenge.consumedAt = this.timestamp();
      this.challengeSecrets.delete(challengeId);
      this.revision += 1;
      throw oauthError(challenge.serverId, `oauth_${input.error}`, input.errorDescription || `OAuth authorization failed: ${input.error}`);
    }
    const code = requiredText(input.code, "authorization code", 16_384);
    const provider = this.requireProvider(challenge.providerId, challenge.serverId);
    this.assertClientHealthy(provider);
    const body = new URLSearchParams();
    body.set("grant_type", "authorization_code");
    body.set("code", code);
    body.set("redirect_uri", provider.redirectUri);
    body.set("code_verifier", secret.verifier);
    if (provider.clientId) body.set("client_id", provider.clientId);
    if (provider.resource) body.set("resource", provider.resource);
    const headers = await this.clientAuthentication(provider, body);
    let response: McpOAuthHttpResponse;
    try {
      response = await this.httpClient({
        url: provider.tokenEndpoint,
        method: "POST",
        headers: { ...provider.tokenEndpointHeaders, ...headers },
        body,
        signal,
      });
    } catch (error) {
      throw oauthHttpFailure(provider, "authorization_code_exchange_failed", error);
    }
    if (response.status < 200 || response.status >= 300) {
      const codeValue = typeof response.body.error === "string" ? response.body.error : `http_${response.status}`;
      if (codeValue === "invalid_client") this.poisonClient(provider.providerId, "invalid_client during authorization code exchange");
      throw oauthError(provider.serverId, `oauth_token_${codeValue}`, tokenErrorMessage(response));
    }
    const tokenSet = await this.persistTokenResponse(provider, challenge.scopes, response.body as unknown as McpOAuthTokenResponse);
    challenge.consumedAt = this.timestamp();
    this.challengeSecrets.delete(challengeId);
    secret.verifier = "\0".repeat(secret.verifier.length);
    secret.state = "\0".repeat(secret.state.length);
    this.revision += 1;
    return tokenSet;
  }

  async refresh(providerId: string, reason = "expired", signal?: AbortSignal): Promise<McpOAuthTokenSet> {
    const existing = this.refreshPromises.get(providerId);
    if (existing) return existing;
    const promise = this.performRefresh(providerId, reason, signal);
    this.refreshPromises.set(providerId, promise);
    try {
      return await promise;
    } finally {
      this.refreshPromises.delete(providerId);
    }
  }

  poisonClient(providerId: string, reason: string, failure: McpFailureRecord | null = null): McpOAuthPoisonRecord {
    const provider = this.providers.get(providerId);
    if (!provider) throw oauthError("", "provider_not_found", `OAuth provider ${providerId} was not found`);
    const timestamp = this.timestamp();
    const prior = this.poisonRecords.get(providerId);
    const record: McpOAuthPoisonRecord = {
      poisonId: deterministicMcpId("mcp-oauth-poison", {
        provider_id: providerId,
        client_id: provider.clientId,
        reason,
        revision: (prior?.revision ?? 0) + 1,
      }),
      providerId,
      clientId: provider.clientId,
      reason,
      failure: failure ? cloneJson(failure) : null,
      poisonedAt: timestamp,
      expiresAt: null,
      revision: (prior?.revision ?? 0) + 1,
    };
    this.poisonRecords.set(providerId, record);
    this.tokenSets.delete(providerId);
    this.revision += 1;
    return cloneJson(record);
  }

  clearPoison(providerId: string, newClientId: string | null): void {
    const provider = this.providers.get(providerId);
    if (!provider) throw oauthError("", "provider_not_found", `OAuth provider ${providerId} was not found`);
    const poison = this.poisonRecords.get(providerId);
    if (!poison) return;
    if (newClientId === poison.clientId) throw oauthError(provider.serverId, "poisoned_client_unchanged", "cannot clear OAuth poison without rotating client identity");
    provider.clientId = newClientId;
    this.poisonRecords.delete(providerId);
    this.revision += 1;
  }

  async authorization(providerId: string, minimumValidityMs = 30_000, signal?: AbortSignal): Promise<string | null> {
    const provider = this.providers.get(providerId);
    if (!provider) return null;
    this.assertClientHealthy(provider);
    let tokenSet = this.tokenSets.get(providerId);
    if (!tokenSet) return null;
    if (tokenSet.expiresAt && Date.parse(tokenSet.expiresAt) - this.now().getTime() <= minimumValidityMs) {
      if (!tokenSet.refreshTokenHandle) return null;
      tokenSet = await this.refresh(providerId, "minimum_validity", signal);
    }
    const lease = await this.vault.read(tokenSet.accessTokenHandle, {
      serverId: provider.serverId,
      providerId,
      kind: "access_token",
    });
    return lease ? `${tokenSet.tokenType} ${lease.value}` : null;
  }

  get(providerId: string): McpOAuthTokenSet | null {
    const value = this.tokenSets.get(providerId);
    return value ? cloneJson(value) : null;
  }

  snapshot(): McpOAuthSnapshot {
    const withoutDigest = {
      version: "zyra.mcp-oauth-runtime/v1" as const,
      revision: this.revision,
      providers: [...this.providers.values()].sort((left, right) => left.providerId.localeCompare(right.providerId)).map(cloneJson),
      challenges: [...this.challenges.values()].sort((left, right) => left.challengeId.localeCompare(right.challengeId)).map(cloneJson),
      tokenSets: [...this.tokenSets.values()].sort((left, right) => left.providerId.localeCompare(right.providerId)).map(cloneJson),
      poisonRecords: [...this.poisonRecords.values()].sort((left, right) => left.providerId.localeCompare(right.providerId)).map(cloneJson),
      refreshGenerations: Object.fromEntries([...this.refreshGenerations.entries()].sort(([left], [right]) => left.localeCompare(right))),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: sha256(withoutDigest) };
  }

  restore(snapshot: McpOAuthSnapshot): void {
    if (snapshot.version !== "zyra.mcp-oauth-runtime/v1") throw oauthError("", "unsupported_oauth_snapshot", "unsupported OAuth snapshot version");
    const { digest, ...withoutDigest } = snapshot;
    if (sha256(withoutDigest) !== digest) throw oauthError("", "oauth_snapshot_digest_mismatch", "OAuth snapshot digest mismatch");
    this.providers.clear();
    this.challenges.clear();
    this.challengeSecrets.clear();
    this.tokenSets.clear();
    this.poisonRecords.clear();
    this.refreshGenerations.clear();
    this.revision = snapshot.revision;
    for (const provider of snapshot.providers) this.providers.set(provider.providerId, normalizeProvider(provider));
    for (const challenge of snapshot.challenges) {
      if (!challenge.consumedAt && Date.parse(challenge.expiresAt) > this.now().getTime()) {
        // Secrets are intentionally not in snapshots, so callbacks for restored challenges fail closed.
        this.challenges.set(challenge.challengeId, {
          ...cloneJson(challenge),
          metadata: { ...challenge.metadata, secret_restore_available: false },
        });
      }
    }
    for (const tokenSet of snapshot.tokenSets) this.tokenSets.set(tokenSet.providerId, cloneJson(tokenSet));
    for (const poison of snapshot.poisonRecords) this.poisonRecords.set(poison.providerId, cloneJson(poison));
    for (const [providerId, generation] of Object.entries(snapshot.refreshGenerations)) this.refreshGenerations.set(providerId, generation);
  }

  private async performRefresh(providerId: string, reason: string, signal?: AbortSignal): Promise<McpOAuthTokenSet> {
    const provider = this.providers.get(providerId);
    if (!provider) throw oauthError("", "provider_not_found", `OAuth provider ${providerId} was not found`);
    this.assertClientHealthy(provider);
    const current = this.tokenSets.get(providerId);
    if (!current?.refreshTokenHandle) throw oauthError(provider.serverId, "refresh_token_missing", "OAuth refresh token is not available");
    const lease = await this.vault.read(current.refreshTokenHandle, {
      serverId: provider.serverId,
      providerId,
      kind: "refresh_token",
    });
    if (!lease) throw oauthError(provider.serverId, "refresh_token_unavailable", "OAuth refresh token could not be leased");
    const generation = (this.refreshGenerations.get(providerId) ?? 0) + 1;
    this.refreshGenerations.set(providerId, generation);
    const body = new URLSearchParams();
    body.set("grant_type", "refresh_token");
    body.set("refresh_token", lease.value);
    if (provider.clientId) body.set("client_id", provider.clientId);
    if (current.scopes.length) body.set("scope", current.scopes.join(" "));
    if (provider.resource) body.set("resource", provider.resource);
    const headers = await this.clientAuthentication(provider, body);
    let response: McpOAuthHttpResponse;
    try {
      response = await this.httpClient({
        url: provider.tokenEndpoint,
        method: "POST",
        headers: { ...provider.tokenEndpointHeaders, ...headers },
        body,
        signal,
      });
    } catch (error) {
      throw oauthHttpFailure(provider, "refresh_request_failed", error);
    }
    if (response.status < 200 || response.status >= 300) {
      const code = typeof response.body.error === "string" ? response.body.error : `http_${response.status}`;
      if (code === "invalid_client") this.poisonClient(providerId, `invalid_client during refresh generation ${generation}`);
      if (code === "invalid_grant") {
        await this.vault.revoke(current.refreshTokenHandle);
        this.tokenSets.delete(providerId);
      }
      throw oauthError(provider.serverId, `oauth_refresh_${code}`, tokenErrorMessage(response), {
        refresh_generation: generation,
        refresh_reason: reason,
      });
    }
    const tokenSet = await this.persistTokenResponse(provider, current.scopes, response.body as unknown as McpOAuthTokenResponse, current);
    tokenSet.metadata = {
      ...tokenSet.metadata,
      refresh_generation: generation,
      refresh_reason: reason,
    };
    this.tokenSets.set(providerId, tokenSet);
    this.revision += 1;
    return cloneJson(tokenSet);
  }

  private async persistTokenResponse(
    provider: McpOAuthProviderConfig,
    requestedScopes: string[],
    response: McpOAuthTokenResponse,
    prior: McpOAuthTokenSet | null = null,
  ): Promise<McpOAuthTokenSet> {
    const accessToken = requiredText(response.access_token, "access_token", 128 * 1024);
    const issuedAt = this.timestamp();
    const expiresIn = typeof response.expires_in === "number" && Number.isFinite(response.expires_in)
      ? Math.max(0, Math.floor(response.expires_in))
      : null;
    const expiresAt = expiresIn === null
      ? null
      : new Date(Date.parse(issuedAt) + Math.max(0, expiresIn - provider.clockSkewSeconds) * 1_000).toISOString();
    const scopes = typeof response.scope === "string"
      ? [...new Set(response.scope.split(/\s+/).filter(Boolean))].sort()
      : [...new Set(requestedScopes)].sort();
    const subject = extractJwtClaim(accessToken, "sub") ?? "";
    const audience = response.resource
      ?? extractJwtAudience(accessToken)
      ?? provider.audience
      ?? provider.resource
      ?? "";
    const accessBinding: McpCredentialBinding = {
      serverId: provider.serverId,
      providerId: provider.providerId,
      subject,
      audience,
      scopes,
      clientId: provider.clientId,
      kind: "access_token",
    };
    const accessMetadata = await this.vault.store({
      binding: accessBinding,
      value: accessToken,
      expiresAt,
      metadata: { token_type: response.token_type ?? "Bearer" },
    });
    let refreshTokenHandle = prior?.refreshTokenHandle ?? null;
    if (response.refresh_token) {
      const refreshMetadata = await this.vault.store({
        binding: { ...accessBinding, kind: "refresh_token" },
        value: response.refresh_token,
        expiresAt: null,
        metadata: { rotated_at: issuedAt },
      });
      if (prior?.refreshTokenHandle && prior.refreshTokenHandle !== refreshMetadata.handle) {
        await this.vault.revoke(prior.refreshTokenHandle);
      }
      refreshTokenHandle = refreshMetadata.handle;
    }
    const tokenSet: McpOAuthTokenSet = {
      accessTokenHandle: accessMetadata.handle,
      refreshTokenHandle,
      tokenType: normalizeTokenType(response.token_type),
      scopes,
      expiresAt,
      providerId: provider.providerId,
      serverId: provider.serverId,
      subject,
      audience,
      revision: (prior?.revision ?? 0) + 1,
      issuedAt,
      metadata: {
        access_token_digest: accessMetadata.valueDigest,
        id_token_present: Boolean(response.id_token),
      },
    };
    this.tokenSets.set(provider.providerId, tokenSet);
    this.revision += 1;
    return cloneJson(tokenSet);
  }

  private async clientAuthentication(
    provider: McpOAuthProviderConfig,
    body: URLSearchParams,
  ): Promise<Record<string, string>> {
    const headers: Record<string, string> = { "content-type": "application/x-www-form-urlencoded", accept: "application/json" };
    if (provider.clientAuthentication === "none") return headers;
    if (!provider.clientId || !provider.clientSecretHandle) throw oauthError(provider.serverId, "client_credentials_missing", "OAuth confidential client credentials are missing");
    const secret = await this.vault.read(provider.clientSecretHandle, {
      serverId: provider.serverId,
      providerId: provider.providerId,
      kind: "client_secret",
    });
    if (!secret) throw oauthError(provider.serverId, "client_secret_unavailable", "OAuth client secret could not be leased");
    if (provider.clientAuthentication === "client_secret_post") {
      body.set("client_secret", secret.value);
    } else {
      headers.authorization = `Basic ${Buffer.from(`${provider.clientId}:${secret.value}`, "utf8").toString("base64")}`;
    }
    return headers;
  }

  private requireProvider(providerId: string, serverId: string): McpOAuthProviderConfig {
    const provider = this.providers.get(providerId);
    if (!provider) throw oauthError(serverId, "provider_not_found", `OAuth provider ${providerId} was not found`);
    if (provider.serverId !== serverId) throw oauthError(serverId, "provider_server_mismatch", `OAuth provider ${providerId} is not bound to ${serverId}`);
    return provider;
  }

  private assertClientHealthy(provider: McpOAuthProviderConfig): void {
    const poison = this.poisonRecords.get(provider.providerId);
    if (!poison) return;
    if (poison.expiresAt && Date.parse(poison.expiresAt) <= this.now().getTime()) {
      this.poisonRecords.delete(provider.providerId);
      return;
    }
    throw oauthError(provider.serverId, "oauth_client_poisoned", `OAuth client ${provider.clientId ?? "<dynamic>"} is poisoned: ${poison.reason}`, {
      poison_id: poison.poisonId,
      poisoned_at: poison.poisonedAt,
    });
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function normalizeProvider(value: McpOAuthProviderConfig): McpOAuthProviderConfig {
  if (!value.providerId || !value.serverId) throw oauthError(value.serverId || "", "invalid_provider", "OAuth provider id and server id are required");
  if (value.clientAuthentication !== "none" && value.clientAuthentication !== "client_secret_post" && value.clientAuthentication !== "client_secret_basic") {
    throw oauthError(value.serverId, "invalid_client_authentication", `unsupported client authentication ${value.clientAuthentication}`);
  }
  return cloneJson({
    ...value,
    issuer: httpUrl(value.issuer, "OAuth issuer"),
    authorizationEndpoint: httpUrl(value.authorizationEndpoint, "authorization endpoint"),
    tokenEndpoint: httpUrl(value.tokenEndpoint, "token endpoint"),
    registrationEndpoint: value.registrationEndpoint ? httpUrl(value.registrationEndpoint, "registration endpoint") : null,
    revocationEndpoint: value.revocationEndpoint ? httpUrl(value.revocationEndpoint, "revocation endpoint") : null,
    redirectUri: httpUrl(value.redirectUri, "redirect URI"),
    scopes: [...new Set(value.scopes ?? [])].sort(),
    tokenEndpointHeaders: value.tokenEndpointHeaders ?? {},
    authorizationParameters: value.authorizationParameters ?? {},
    clockSkewSeconds: Math.max(0, Math.min(value.clockSkewSeconds ?? 30, 600)),
    metadata: value.metadata ?? {},
  });
}

async function defaultHttpClient(input: Parameters<McpOAuthHttpClient>[0]): Promise<McpOAuthHttpResponse> {
  const response = await fetch(input.url, {
    method: input.method,
    headers: input.headers,
    body: input.body,
    signal: input.signal,
    redirect: "error",
  });
  const text = await response.text();
  let body: JsonObject = {};
  if (text.trim()) {
    try {
      body = canonicalObject(JSON.parse(text), "OAuth token response");
    } catch {
      body = { error: "invalid_json_response", error_description: text.slice(0, 4_096) };
    }
  }
  const headers: Record<string, string> = {};
  response.headers.forEach((headerValue, name) => {
    headers[name.toLowerCase()] = headerValue;
  });
  return { status: response.status, headers, body };
}

function tokenErrorMessage(response: McpOAuthHttpResponse): string {
  const description = typeof response.body.error_description === "string" ? response.body.error_description : "";
  const code = typeof response.body.error === "string" ? response.body.error : `HTTP ${response.status}`;
  return description ? `${code}: ${description}` : code;
}

function normalizeTokenType(value: unknown): string {
  const type = typeof value === "string" && value.trim() ? value.trim() : "Bearer";
  if (!/^[A-Za-z][A-Za-z0-9._~-]*$/.test(type)) throw oauthError("", "invalid_token_type", "OAuth token_type is invalid");
  return type;
}

function extractJwtClaim(token: string, claim: string): string | null {
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    const payload = JSON.parse(Buffer.from(parts[1], "base64url").toString("utf8")) as Record<string, unknown>;
    return typeof payload[claim] === "string" ? payload[claim] as string : null;
  } catch {
    return null;
  }
}

function extractJwtAudience(token: string): string | null {
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    const payload = JSON.parse(Buffer.from(parts[1], "base64url").toString("utf8")) as Record<string, unknown>;
    if (typeof payload.aud === "string") return payload.aud;
    if (Array.isArray(payload.aud) && typeof payload.aud[0] === "string") return payload.aud[0];
    return null;
  } catch {
    return null;
  }
}

function sha256BytesText(value: string): string {
  return sha256Text(value);
}

function oauthHttpFailure(provider: McpOAuthProviderConfig, code: string, error: unknown): McpRuntimeError {
  const failure = normalizeFailure(error, {
    failureId: deterministicMcpId("mcp-oauth-http", { provider_id: provider.providerId, code }),
    category: "authentication",
    code,
    message: error instanceof Error ? error.message : String(error),
    serverId: provider.serverId,
    operation: "oauth/token",
    retryable: true,
    disposition: "retry_same_connection",
  });
  return new McpRuntimeError({
    failureId: failure.failure_id,
    category: failure.category,
    code: failure.code,
    message: failure.message,
    serverId: failure.server_id,
    operation: failure.operation,
    retryable: failure.retryable,
    disposition: failure.disposition,
    details: failure.details,
  }, { cause: error });
}

function oauthError(serverId: string, code: string, message: string, details: JsonObject = {}): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-oauth", { server_id: serverId, code, message }),
    category: "authentication",
    code,
    message,
    serverId,
    retryable: code.includes("conflict") || code.includes("http"),
    disposition: code.includes("conflict") ? "retry_same_connection" : "reauthorize",
    details,
  });
}
