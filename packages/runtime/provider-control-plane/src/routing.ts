import type {
  ModelDefinition,
  ProviderDefinition,
  ProviderRouteLease,
  ProviderRouteCredentialSnapshot,
  RetryPolicy,
  RouteRequest,
  TransportProtocol,
} from "./contracts.ts";
import {
  type Clock,
  type IdFactory,
  RandomIdFactory,
  SystemClock,
  assertIdentifier,
  assertNonEmpty,
  assertNonNegativeInteger,
  assertPositiveInteger,
  deepClone,
  digestJson,
  joinUrl,
  normalizeHeaders,
} from "./canonical.ts";
import { ProviderCatalog, projectModel } from "./catalog.ts";
import { CredentialManager } from "./credentials.ts";
import { ProviderControlPlaneError } from "./errors.ts";
import { ProviderControlPlaneStore } from "./store.ts";
import { ProviderRouteHealthRuntime } from "./route-health.ts";
import { ProviderCredentialPoolRuntime } from "./credential-pool.ts";

interface Candidate {
  readonly provider: ProviderDefinition;
  readonly model: ModelDefinition;
  readonly score: number;
  readonly reasons: readonly string[];
}

export interface RoutePlannerOptions {
  readonly clock?: Clock;
  readonly ids?: IdFactory;
  readonly leaseMilliseconds?: number;
  readonly defaultRetryPolicy?: RetryPolicy;
}

const DEFAULT_RETRY_POLICY: RetryPolicy = {
  maximumAttempts: 3,
  baseDelayMilliseconds: 250,
  maximumDelayMilliseconds: 5_000,
  retryStatuses: [408, 409, 425, 429, 500, 502, 503, 504],
  rotateCredentialOnAuthenticationFailure: true,
  rotateRouteOnProviderUnavailable: true,
};

export class ProviderRoutePlanner {
  private readonly clock: Clock;
  private readonly ids: IdFactory;
  private readonly leaseMilliseconds: number;
  private readonly retryPolicy: RetryPolicy;
  private readonly store: ProviderControlPlaneStore;
  private readonly catalog: ProviderCatalog;
  private readonly credentials: CredentialManager;
  private readonly health: ProviderRouteHealthRuntime | null;
  private readonly credentialPool: ProviderCredentialPoolRuntime | null;

  constructor(
    store: ProviderControlPlaneStore,
    catalog: ProviderCatalog,
    credentials: CredentialManager,
    options: RoutePlannerOptions = {},
    health: ProviderRouteHealthRuntime | null = null,
    credentialPool: ProviderCredentialPoolRuntime | null = null,
  ) {
    this.store = store;
    this.catalog = catalog;
    this.credentials = credentials;
    this.health = health;
    this.credentialPool = credentialPool;
    this.clock = options.clock ?? new SystemClock();
    this.ids = options.ids ?? new RandomIdFactory();
    this.leaseMilliseconds = options.leaseMilliseconds ?? 15 * 60_000;
    this.retryPolicy = normalizeRetryPolicy(options.defaultRetryPolicy ?? DEFAULT_RETRY_POLICY);
    assertPositiveInteger(this.leaseMilliseconds, "leaseMilliseconds");
  }

  acquire(
    request: RouteRequest,
    previousRouteId: string | null = null,
    pinnedCatalogRevision: number | null = null,
  ): ProviderRouteLease {
    validateRouteRequest(request);
    if (previousRouteId !== null) this.require(previousRouteId);
    const candidates = this.candidates(request, previousRouteId, pinnedCatalogRevision);
    const selected = candidates[0];
    if (selected === undefined) {
      throw new ProviderControlPlaneError({
        layer: "route",
        kind: "route_policy_rejected",
        message: "no provider/model route satisfies the request constraints",
        providerId: request.preferredProviderId,
        modelId: request.preferredModelId,
        recoveryIntent: "surface_to_operator",
        detail: {
          purpose: request.purpose,
          providerIds: [...request.constraints.providerIds],
          modelIds: [...request.constraints.modelIds],
        },
      });
    }
    const poolExcluded = this.credentialPool?.excludedCredentialIds(selected.provider.providerId) ?? [];
    const credential = this.credentials.select({
      providerId: selected.provider.providerId,
      modelId: selected.model.modelId,
      excludedCredentialIds: [...new Set([...request.constraints.excludedCredentialIds, ...poolExcluded])],
      requiredScopes: request.constraints.requiredScopes,
      minimumValidityMilliseconds: this.leaseMilliseconds,
    });
    const catalogRevision = pinnedCatalogRevision ?? this.store.catalogRevision();
    const integration = snapshotIntegration(
      pinnedCatalogRevision === null ? this.catalog.snapshot() : this.catalog.snapshotAt(catalogRevision),
      credential.integrationId,
    );
    const createdAt = this.clock.now();
    const protocol = (selected.model.protocol ?? selected.provider.protocol) as TransportProtocol;
    const endpointPath = selected.model.endpointPath ?? defaultEndpointPath(protocol);
    const body = {
      routeId: this.ids.next("provider_route"),
      runId: request.runId,
      taskId: request.taskId,
      nodeId: request.nodeId,
      sessionId: request.sessionId,
      turnId: request.turnId,
      purpose: request.purpose,
      catalogRevision,
      providerId: selected.provider.providerId,
      modelId: selected.model.modelId,
      credentialId: credential.credentialId,
      credentialVersion: credential.version,
      credentialFingerprint: credential.fingerprint,
      integrationId: credential.integrationId,
      transportId: `transport:${protocol}:v1`,
      protocol,
      baseUrl: selected.provider.baseUrl,
      allowedHosts: [...selected.provider.allowedHosts],
      endpointPath,
      requestHeaders: normalizeHeaders(selected.provider.defaultHeaders),
      requestDefaults: {
        ...deepClone(selected.provider.requestDefaults),
        ...deepClone(selected.model.requestDefaults),
      },
      retryPolicy: deepClone(this.retryPolicy),
      createdAt,
      expiresAt: createdAt + this.leaseMilliseconds,
      previousRouteId,
      reason: selected.reasons.join("; "),
    };
    const lease: ProviderRouteLease = { ...body, checksum: digestJson(body) };
    const credentialBody = {
      routeId: lease.routeId,
      credentialId: credential.credentialId,
      credentialVersion: credential.version,
      credentialFingerprint: credential.fingerprint,
      providerId: credential.providerId,
      integrationId: credential.integrationId,
      integrationKind: integration.kind,
      secretRef: credential.secretRef,
      headerName: integration.headerName,
      authorizationScheme: integration.authorizationScheme,
      createdAt,
    };
    const credentialSnapshot: ProviderRouteCredentialSnapshot = {
      ...credentialBody,
      checksum: digestJson(credentialBody),
    };
    this.store.putRoute(lease, credentialSnapshot);
    return deepClone(lease);
  }

  failover(previousRouteId: string, reason: string): ProviderRouteLease {
    assertNonEmpty(reason, "reason");
    const previous = this.require(previousRouteId);
    const credentialFailure = ["authentication_failed", "usage_limited", "credential_blocked", "credential_expired"].includes(reason);
    return this.acquire(
      {
        runId: previous.runId,
        taskId: previous.taskId,
        nodeId: previous.nodeId,
        sessionId: previous.sessionId,
        turnId: previous.turnId,
        purpose: previous.purpose,
        preferredProviderId: credentialFailure ? previous.providerId : null,
        preferredModelId: credentialFailure ? previous.modelId : null,
        routeHint: null,
        constraints: {
          providerIds: [],
          modelIds: [],
          requiredInput: ["text"],
          requiredOutput: ["text"],
          requireTools: false,
          requireStreaming: true,
          minimumContextWindow: 0,
          maximumInputPricePerMillion: null,
          maximumOutputPricePerMillion: null,
          excludedCredentialIds: [previous.credentialId],
          requiredScopes: [],
        },
        metadata: { failoverReason: reason, allowSameModelCredentialRotation: credentialFailure },
      },
      previousRouteId,
      previous.catalogRevision,
    );
  }

  require(routeId: string): ProviderRouteLease {
    assertIdentifier(routeId, "routeId");
    const lease = this.store.getRoute(routeId);
    if (lease === null) {
      throw new ProviderControlPlaneError({
        layer: "route",
        kind: "route_not_found",
        message: `provider route not found: ${routeId}`,
        routeId,
      });
    }
    const { checksum, ...body } = lease;
    if (digestJson(body) !== checksum) {
      throw new ProviderControlPlaneError({
        layer: "route",
        kind: "route_revision_conflict",
        message: `provider route checksum mismatch: ${routeId}`,
        routeId,
      });
    }
    if (lease.expiresAt <= this.clock.now()) {
      throw new ProviderControlPlaneError({
        layer: "route",
        kind: "route_expired",
        message: `provider route expired: ${routeId}`,
        routeId,
        providerId: lease.providerId,
        modelId: lease.modelId,
        credentialId: lease.credentialId,
        recoveryIntent: "change_provider_route",
      });
    }
    return deepClone(lease);
  }

  list(runId?: string, taskId?: string): ProviderRouteLease[] {
    return this.store.listRoutes(runId, taskId);
  }

  private candidates(
    request: RouteRequest,
    previousRouteId: string | null,
    pinnedCatalogRevision: number | null,
  ): Candidate[] {
    const snapshot = pinnedCatalogRevision === null
      ? this.catalog.snapshot()
      : this.catalog.snapshotAt(pinnedCatalogRevision);
    const providers = new Map(
      snapshot.providers
        .filter((provider) => provider.status === "active")
        .map((provider) => [provider.providerId, provider]),
    );
    const previous = previousRouteId === null ? null : this.require(previousRouteId);
    const results: Candidate[] = [];
    const allowSameModel = request.metadata.allowSameModelCredentialRotation === true;
    for (const rawModel of snapshot.models.filter((model) => model.enabled && model.status === "active")) {
      const provider = providers.get(rawModel.providerId);
      if (!provider) continue;
      const model = projectModel(rawModel, provider);
      if (this.health !== null && !this.health.isAdmissible(provider.providerId, model.modelId)) continue;
      if (
        previous
        && !allowSameModel
        && provider.providerId === previous.providerId
        && model.modelId === previous.modelId
      ) continue;
      if (!matchesConstraints(provider, model, request)) continue;
      const reasons: string[] = [];
      let score = model.releasedAt / 1_000_000_000;
      const routingPriority = Number(provider.metadata.routing_priority ?? 0);
      if (Number.isFinite(routingPriority)) {
        score += routingPriority;
        reasons.push(`provider routing priority ${routingPriority}`);
      }
      if (this.health !== null) {
        const health = this.health.snapshot(provider.providerId, model.modelId);
        score += this.health.scoreAdjustment(provider.providerId, model.modelId);
        reasons.push(`health generation ${health.generation} circuit ${health.circuit}`);
      }
      if (request.preferredProviderId === provider.providerId) { score += 100; reasons.push("preferred provider"); }
      if (request.preferredModelId === model.modelId) { score += 200; reasons.push("preferred model"); }
      if (request.routeHint === `${provider.providerId}/${model.modelId}`) { score += 300; reasons.push("exact validated route hint"); }
      if (model.capabilities.tools && request.constraints.requireTools) { score += 20; reasons.push("tool capable"); }
      if (model.capabilities.reasoning && ["reason", "verify"].includes(request.purpose)) { score += 10; reasons.push("reasoning capable"); }
      const price = model.pricing[0];
      if (price) score -= (price.inputPerMillion + price.outputPerMillion) / 100;
      if (previous && provider.providerId !== previous.providerId) reasons.push("provider failover");
      if (reasons.length === 0) reasons.push("available catalog candidate");
      results.push({ provider, model, score, reasons });
    }
    return results.sort((left, right) => right.score - left.score || left.provider.providerId.localeCompare(right.provider.providerId) || left.model.modelId.localeCompare(right.model.modelId));
  }
}

function snapshotIntegration(snapshot: import("./contracts.ts").CatalogSnapshot, integrationId: string) {
  const integration = snapshot.integrations.find((candidate) => candidate.integrationId === integrationId);
  if (!integration) {
    throw new ProviderControlPlaneError({
      layer: "catalog",
      kind: "provider_not_found",
      message: `integration missing from pinned catalog revision: ${integrationId}`,
      detail: { integrationId, catalogRevision: snapshot.revision },
    });
  }
  return integration;
}

function matchesConstraints(provider: ProviderDefinition, model: ModelDefinition, request: RouteRequest): boolean {
  const constraint = request.constraints;
  if (constraint.providerIds.length > 0 && !constraint.providerIds.includes(provider.providerId)) return false;
  if (constraint.modelIds.length > 0 && !constraint.modelIds.includes(model.modelId) && !constraint.modelIds.includes(`${provider.providerId}/${model.modelId}`)) return false;
  if (constraint.requiredInput.some((kind) => !model.capabilities.input.includes(kind as never))) return false;
  if (constraint.requiredOutput.some((kind) => !model.capabilities.output.includes(kind as never))) return false;
  if (constraint.requireTools && !model.capabilities.tools) return false;
  if (constraint.requireStreaming && !model.capabilities.streaming) return false;
  if (model.contextWindow < constraint.minimumContextWindow) return false;
  const price = model.pricing[0];
  if (price && constraint.maximumInputPricePerMillion !== null && price.inputPerMillion > constraint.maximumInputPricePerMillion) return false;
  if (price && constraint.maximumOutputPricePerMillion !== null && price.outputPerMillion > constraint.maximumOutputPricePerMillion) return false;
  return true;
}

function validateRouteRequest(request: RouteRequest): void {
  for (const [name, value] of Object.entries({ runId: request.runId, taskId: request.taskId, sessionId: request.sessionId, turnId: request.turnId })) {
    assertNonEmpty(value, name);
  }
  if (request.preferredProviderId !== null) assertIdentifier(request.preferredProviderId, "preferredProviderId");
  if (request.preferredModelId !== null) assertIdentifier(request.preferredModelId, "preferredModelId");
  assertNonNegativeInteger(request.constraints.minimumContextWindow, "minimumContextWindow");
}

function normalizeRetryPolicy(policy: RetryPolicy): RetryPolicy {
  assertPositiveInteger(policy.maximumAttempts, "maximumAttempts");
  assertNonNegativeInteger(policy.baseDelayMilliseconds, "baseDelayMilliseconds");
  assertNonNegativeInteger(policy.maximumDelayMilliseconds, "maximumDelayMilliseconds");
  if (policy.maximumDelayMilliseconds < policy.baseDelayMilliseconds) throw new TypeError("maximum retry delay must not be less than base delay");
  return { ...deepClone(policy), retryStatuses: [...new Set(policy.retryStatuses)].sort((a, b) => a - b) };
}

function defaultEndpointPath(protocol: TransportProtocol): string {
  if (protocol === "openai_chat") return "/v1/chat/completions";
  if (protocol === "openai_responses") return "/v1/responses";
  return "/v1/messages";
}

export function routeUrl(lease: ProviderRouteLease): string {
  return joinUrl(lease.baseUrl, lease.endpointPath);
}
