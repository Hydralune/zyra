import type { JsonRecord, JsonValue } from "./canonical.ts";

export const PROVIDER_PROTOCOL = "zyra.provider-control-plane/v1" as const;

export type ProviderStatus = "active" | "disabled" | "degraded";
export type ModelStatus = "active" | "deprecated" | "disabled";
export type CredentialStatus = "active" | "expired" | "revoked" | "blocked" | "refreshing";
export type IntegrationKind = "api_key" | "bearer" | "oauth2" | "anonymous" | "custom_header";
export type TransportProtocol = "openai_chat" | "openai_responses" | "anthropic_messages";
export type RoutePurpose = "reason" | "tool" | "verify" | "compact" | "embedding" | "general";
export type StreamFrameKind = "response_start" | "text_delta" | "thinking_delta" | "tool_call_delta" | "usage" | "response_end" | "provider_notice";

export interface RetryPolicy {
  readonly maximumAttempts: number;
  readonly baseDelayMilliseconds: number;
  readonly maximumDelayMilliseconds: number;
  readonly retryStatuses: readonly number[];
  readonly rotateCredentialOnAuthenticationFailure: boolean;
  readonly rotateRouteOnProviderUnavailable: boolean;
}

export interface ProviderDefinition {
  readonly providerId: string;
  readonly displayName: string;
  readonly integrationId: string | null;
  readonly status: ProviderStatus;
  readonly baseUrl: string;
  readonly protocol: TransportProtocol;
  readonly defaultHeaders: Readonly<Record<string, string>>;
  readonly requestDefaults: JsonRecord;
  readonly allowedHosts: readonly string[];
  readonly tags: readonly string[];
  readonly metadata: JsonRecord;
}

export interface ModelCapabilitySet {
  readonly input: readonly ("text" | "image" | "audio" | "file")[];
  readonly output: readonly ("text" | "image" | "audio" | "tool")[];
  readonly tools: boolean;
  readonly streaming: boolean;
  readonly reasoning: boolean;
  readonly structuredOutput: boolean;
}

export interface ModelPricingBand {
  readonly inputPerMillion: number;
  readonly outputPerMillion: number;
  readonly cachedInputPerMillion: number | null;
  readonly currency: string;
}

export interface ModelDefinition {
  readonly providerId: string;
  readonly modelId: string;
  readonly displayName: string;
  readonly family: string;
  readonly status: ModelStatus;
  readonly enabled: boolean;
  readonly releasedAt: number;
  readonly contextWindow: number;
  readonly maximumOutputTokens: number;
  readonly capabilities: ModelCapabilitySet;
  readonly pricing: readonly ModelPricingBand[];
  readonly endpointPath: string | null;
  readonly protocol: TransportProtocol | null;
  readonly requestDefaults: JsonRecord;
  readonly tags: readonly string[];
  readonly metadata: JsonRecord;
}

export interface IntegrationDefinition {
  readonly integrationId: string;
  readonly displayName: string;
  readonly kind: IntegrationKind;
  readonly envNames: readonly string[];
  readonly headerName: string | null;
  readonly authorizationScheme: string | null;
  readonly supportsRefresh: boolean;
  readonly metadata: JsonRecord;
}

export interface CredentialRecord {
  readonly credentialId: string;
  readonly integrationId: string;
  readonly providerId: string;
  readonly accountId: string;
  readonly secretRef: string;
  readonly fingerprint: string;
  readonly version: number;
  readonly status: CredentialStatus;
  readonly priority: number;
  readonly allowedModels: readonly string[];
  readonly scopes: readonly string[];
  readonly expiresAt: number | null;
  readonly refreshAfter: number | null;
  readonly blockedReason: string | null;
  readonly lastUsedAt: number | null;
  readonly failureCount: number;
  readonly successCount: number;
  readonly createdAt: number;
  readonly updatedAt: number;
  readonly metadata: JsonRecord;
}

export interface CatalogSnapshot {
  readonly schema: "zyra.provider-catalog/v1";
  readonly revision: number;
  readonly createdAt: number;
  readonly providers: readonly ProviderDefinition[];
  readonly models: readonly ModelDefinition[];
  readonly integrations: readonly IntegrationDefinition[];
  readonly checksum: string;
}

export interface RouteConstraint {
  readonly providerIds: readonly string[];
  readonly modelIds: readonly string[];
  readonly requiredInput: readonly string[];
  readonly requiredOutput: readonly string[];
  readonly requireTools: boolean;
  readonly requireStreaming: boolean;
  readonly minimumContextWindow: number;
  readonly maximumInputPricePerMillion: number | null;
  readonly maximumOutputPricePerMillion: number | null;
  readonly excludedCredentialIds: readonly string[];
  readonly requiredScopes: readonly string[];
}

export interface RouteRequest {
  readonly runId: string;
  readonly taskId: string;
  readonly nodeId: string | null;
  readonly sessionId: string;
  readonly turnId: string;
  readonly purpose: RoutePurpose;
  readonly preferredProviderId: string | null;
  readonly preferredModelId: string | null;
  readonly routeHint: string | null;
  readonly constraints: RouteConstraint;
  readonly metadata: JsonRecord;
}

export interface ProviderRouteLease {
  readonly routeId: string;
  readonly runId: string;
  readonly taskId: string;
  readonly nodeId: string | null;
  readonly sessionId: string;
  readonly turnId: string;
  readonly purpose: RoutePurpose;
  readonly catalogRevision: number;
  readonly providerId: string;
  readonly modelId: string;
  readonly credentialId: string;
  readonly credentialVersion: number;
  readonly credentialFingerprint: string;
  readonly integrationId: string;
  readonly transportId: string;
  readonly protocol: TransportProtocol;
  readonly baseUrl: string;
  readonly allowedHosts: readonly string[];
  readonly endpointPath: string;
  readonly requestHeaders: Readonly<Record<string, string>>;
  readonly requestDefaults: JsonRecord;
  readonly retryPolicy: RetryPolicy;
  readonly createdAt: number;
  readonly expiresAt: number;
  readonly previousRouteId: string | null;
  readonly reason: string;
  readonly checksum: string;
}

export interface ProviderRouteCredentialSnapshot {
  readonly routeId: string;
  readonly credentialId: string;
  readonly credentialVersion: number;
  readonly credentialFingerprint: string;
  readonly providerId: string;
  readonly integrationId: string;
  readonly integrationKind: IntegrationKind;
  readonly secretRef: string;
  readonly headerName: string | null;
  readonly authorizationScheme: string | null;
  readonly createdAt: number;
  readonly checksum: string;
}

export interface DispatchMessage {
  readonly role: "system" | "developer" | "user" | "assistant" | "tool";
  readonly content: string | readonly JsonValue[];
  readonly name?: string;
  readonly toolCallId?: string;
}

export interface DispatchTool {
  readonly name: string;
  readonly description: string;
  readonly inputSchema: JsonRecord;
}

export interface ProviderDispatchRequest {
  readonly dispatchId: string;
  readonly routeId: string;
  readonly runId: string;
  readonly taskId: string;
  readonly nodeId: string | null;
  readonly sessionId: string;
  readonly turnId: string;
  readonly routeFallbackPolicy: "allow_route_change" | "pin_initial_route";
  readonly messages: readonly DispatchMessage[];
  readonly tools: readonly DispatchTool[];
  readonly maximumOutputTokens: number;
  readonly temperature: number | null;
  readonly stream: boolean;
  readonly maximumAttempts?: number;
  readonly timeoutMilliseconds: number;
  readonly streamTotalTimeoutMilliseconds?: number;
  readonly chunkTimeoutMilliseconds: number;
  readonly idempotencyKey: string;
  readonly extraBody: JsonRecord;
  readonly metadata: JsonRecord;
}

export interface ProviderStreamFrame {
  readonly frameId: string;
  readonly dispatchId: string;
  readonly routeId: string;
  readonly sequence: number;
  readonly kind: StreamFrameKind;
  readonly text: string | null;
  readonly toolCallId: string | null;
  readonly toolName: string | null;
  readonly jsonDelta: string | null;
  readonly usage: JsonRecord;
  readonly providerEvent: string | null;
  readonly createdAt: number;
  readonly metadata: JsonRecord;
}

export interface ProviderDispatchAttempt {
  readonly attemptId: string;
  readonly dispatchId: string;
  readonly routeId: string;
  readonly attempt: number;
  readonly startedAt: number;
  readonly completedAt: number | null;
  readonly httpStatus: number | null;
  readonly requestBytes: number;
  readonly responseBytes: number;
  readonly outputObserved: boolean;
  readonly outcome: "running" | "succeeded" | "failed" | "aborted";
  readonly failureKind: string | null;
  readonly recoveryIntent: string | null;
  readonly retryAfterMilliseconds: number | null;
  readonly requestDigest: string;
  readonly responseDigest: string | null;
  readonly metadata: JsonRecord;
}

export interface ProviderDispatchResult {
  readonly dispatchId: string;
  readonly routeId: string;
  readonly providerId: string;
  readonly modelId: string;
  readonly protocol: TransportProtocol;
  readonly text: string;
  readonly frames: readonly ProviderStreamFrame[];
  readonly attempts: readonly ProviderDispatchAttempt[];
  readonly usage: JsonRecord;
  readonly stopReason: string;
  readonly completedAt: number;
  readonly metadata: JsonRecord;
}

export interface SecretMaterial {
  readonly value: string;
  readonly refreshToken?: string;
  readonly auxiliary?: Readonly<Record<string, string>>;
}

export interface SecretResolver {
  resolve(secretRef: string, signal?: AbortSignal): Promise<SecretMaterial | null>;
}

export interface ProviderControlEvent {
  readonly eventId: string;
  readonly eventType: string;
  readonly runId: string | null;
  readonly taskId: string | null;
  readonly nodeId: string | null;
  readonly routeId: string | null;
  readonly dispatchId: string | null;
  readonly causationId: string | null;
  readonly correlationId: string | null;
  readonly createdAt: number;
  readonly payload: JsonRecord;
  readonly payloadDigest: string;
}

export interface V1ProviderProjection {
  readonly id: string;
  readonly name: string;
  readonly api: string;
  readonly models: Readonly<Record<string, { readonly id: string; readonly name: string; readonly context: number; readonly output: number }>>;
}

export interface V1CompatibilitySnapshot {
  readonly schema: "zyra.provider-compat-v1/read-only";
  readonly catalogRevision: number;
  readonly providers: readonly V1ProviderProjection[];
  readonly defaultModel: null;
  readonly writable: false;
}
