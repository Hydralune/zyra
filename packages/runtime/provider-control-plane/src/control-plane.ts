import type {
  CredentialRecord,
  IntegrationDefinition,
  ModelDefinition,
  ProviderControlEvent,
  ProviderDefinition,
  ProviderDispatchRequest,
  ProviderDispatchResult,
  ProviderRouteLease,
  RouteRequest,
  SecretResolver,
} from "./contracts.ts";
import {
  type Clock,
  type IdFactory,
  RandomIdFactory,
  SystemClock,
  canonicalize,
  deepClone,
  digestJson,
} from "./canonical.ts";
import { ProviderCatalog, type CatalogMutationReceipt } from "./catalog.ts";
import {
  CredentialManager,
  type CredentialRegistration,
  EnvironmentSecretResolver,
} from "./credentials.ts";
import { ProviderRoutePlanner, type RoutePlannerOptions } from "./routing.ts";
import { ProviderControlPlaneStore, type ProviderStoreHealth } from "./store.ts";
import { ProviderTransportRuntime, type ProviderTransportOptions } from "./transport/runtime.ts";
import { ProviderDispatchLifecycle } from "./dispatch-lifecycle.ts";
import { ProviderRouteHealthRuntime } from "./route-health.ts";
import { ProviderCatalogReconciler } from "./catalog-reconciler.ts";
import { ProviderCredentialPoolRuntime } from "./credential-pool.ts";
import { CredentialRefreshRuntime } from "./credential-refresh.ts";
import { ProviderDispatchCancellationRuntime } from "./dispatch-cancellation.ts";
import { ProviderControlPlaneError } from "./errors.ts";

export interface ProviderControlPlaneOptions {
  readonly databasePath: string;
  readonly secrets?: SecretResolver;
  readonly clock?: Clock;
  readonly ids?: IdFactory;
  readonly route?: Omit<RoutePlannerOptions, "clock" | "ids">;
  readonly transport?: Omit<ProviderTransportOptions, "clock" | "ids">;
}

export interface ProviderControlPlaneHealth extends ProviderStoreHealth {
  readonly schema: "zyra.provider-control-plane.health/v1";
  readonly stateOwner: "typescript.ProviderControlPlaneStore";
  readonly providerFallbackOwned: true;
  readonly backendFallbackOwned: false;
}

export class ProviderControlPlane {
  readonly store: ProviderControlPlaneStore;
  readonly catalog: ProviderCatalog;
  readonly catalogReconciler: ProviderCatalogReconciler;
  readonly credentials: CredentialManager;
  readonly credentialPool: ProviderCredentialPoolRuntime;
  readonly credentialRefresh: CredentialRefreshRuntime;
  readonly routes: ProviderRoutePlanner;
  readonly routeHealth: ProviderRouteHealthRuntime;
  readonly transport: ProviderTransportRuntime;
  readonly dispatches: ProviderDispatchLifecycle;
  readonly cancellations: ProviderDispatchCancellationRuntime;
  private readonly clock: Clock;
  private readonly ids: IdFactory;

  constructor(options: ProviderControlPlaneOptions) {
    this.clock = options.clock ?? new SystemClock();
    this.ids = options.ids ?? new RandomIdFactory();
    const secrets = options.secrets ?? new EnvironmentSecretResolver();
    this.store = new ProviderControlPlaneStore(options.databasePath);
    this.catalog = new ProviderCatalog(this.store);
    this.catalogReconciler = new ProviderCatalogReconciler(this.store, this.catalog, {
      clock: this.clock,
      ids: this.ids,
    });
    this.credentials = new CredentialManager(this.store, this.catalog, secrets, {
      clock: this.clock,
      ids: this.ids,
    });
    this.credentialPool = new ProviderCredentialPoolRuntime(this.store, {
      clock: this.clock,
    });
    this.credentialRefresh = new CredentialRefreshRuntime(
      this.store,
      this.catalog,
      this.credentials,
      { clock: this.clock, ids: this.ids },
    );
    this.routeHealth = new ProviderRouteHealthRuntime(this.store, {
      clock: this.clock,
      ids: this.ids,
    });
    this.routes = new ProviderRoutePlanner(this.store, this.catalog, this.credentials, {
      ...options.route,
      clock: this.clock,
      ids: this.ids,
    }, this.routeHealth, this.credentialPool);
    this.dispatches = new ProviderDispatchLifecycle(this.store, {
      clock: this.clock,
      ids: this.ids,
    });
    this.cancellations = new ProviderDispatchCancellationRuntime(this.store, {
      clock: this.clock,
    });
    this.transport = new ProviderTransportRuntime(this.store, this.routes, this.credentials, secrets, {
      ...options.transport,
      clock: this.clock,
      ids: this.ids,
    }, this.dispatches, this.routeHealth, this.credentialPool);
  }

  upsertProvider(provider: ProviderDefinition, expectedRevision: number | null = null): CatalogMutationReceipt {
    const receipt = this.catalog.upsertProvider(provider, expectedRevision);
    this.emit("provider.catalog.provider_upserted", {
      providerId: provider.providerId,
      catalogRevision: receipt.revision,
    });
    return receipt;
  }

  upsertModel(model: ModelDefinition, expectedRevision: number | null = null): CatalogMutationReceipt {
    const receipt = this.catalog.upsertModel(model, expectedRevision);
    this.emit("provider.catalog.model_upserted", {
      providerId: model.providerId,
      modelId: model.modelId,
      catalogRevision: receipt.revision,
    });
    return receipt;
  }

  upsertIntegration(integration: IntegrationDefinition, expectedRevision: number | null = null): CatalogMutationReceipt {
    const receipt = this.catalog.upsertIntegration(integration, expectedRevision);
    this.emit("provider.integration.upserted", {
      integrationId: integration.integrationId,
      catalogRevision: receipt.revision,
    });
    return receipt;
  }

  registerCredential(input: CredentialRegistration): CredentialRecord {
    const record = this.credentials.register(input);
    this.emit("provider.credential.registered", {
      credentialId: record.credentialId,
      providerId: record.providerId,
      integrationId: record.integrationId,
      fingerprint: record.fingerprint,
      credentialVersion: record.version,
    });
    return record;
  }

  acquireRoute(request: RouteRequest, previousRouteId: string | null = null): ProviderRouteLease {
    const lease = this.routes.acquire(request, previousRouteId);
    this.emit(
      previousRouteId === null ? "provider.route.acquired" : "provider.route.failover_acquired",
      {
        routeId: lease.routeId,
        previousRouteId: lease.previousRouteId,
        providerId: lease.providerId,
        modelId: lease.modelId,
        catalogRevision: lease.catalogRevision,
        credentialId: lease.credentialId,
        credentialVersion: lease.credentialVersion,
        transportId: lease.transportId,
      },
      {
        runId: lease.runId,
        taskId: lease.taskId,
        nodeId: lease.nodeId,
        routeId: lease.routeId,
        causationId: previousRouteId,
        correlationId: lease.turnId,
      },
    );
    return lease;
  }

  async dispatch(request: ProviderDispatchRequest, signal?: AbortSignal): Promise<ProviderDispatchResult> {
    const claim = this.dispatches.claim(request);
    if (claim.disposition === "cached" && claim.result !== null) {
      this.emit("provider.dispatch.cache_hit", {
        dispatchId: request.dispatchId,
        routeId: request.routeId,
        lifecycleEpoch: claim.snapshot.epoch,
      }, {
        runId: request.runId,
        taskId: request.taskId,
        nodeId: request.nodeId,
        routeId: request.routeId,
        dispatchId: request.dispatchId,
        correlationId: request.turnId,
      });
      return claim.result;
    }
    this.emit("provider.dispatch.started", {
      dispatchId: request.dispatchId,
      routeId: request.routeId,
      messageCount: request.messages.length,
      toolCount: request.tools.length,
      stream: request.stream,
    }, {
      runId: request.runId,
      taskId: request.taskId,
      nodeId: request.nodeId,
      routeId: request.routeId,
      dispatchId: request.dispatchId,
      correlationId: request.turnId,
    });
    const cancellation = this.cancellations.bind(request.dispatchId, signal);
    let result: ProviderDispatchResult;
    try {
      result = await this.transport.dispatch(request, cancellation.signal, claim.snapshot.ownerToken);
    } catch (error) {
      const outcome = error instanceof ProviderControlPlaneError && error.kind === "request_aborted"
        ? "aborted"
        : "failed";
      cancellation.release(outcome);
      try {
        this.dispatches.fail(request.dispatchId, claim.snapshot.ownerToken, error);
      } catch (lifecycleError) {
        throw new AggregateError([error, lifecycleError], "provider dispatch and lifecycle settlement both failed");
      }
      const failure = error && typeof error === "object" && "safe" in error && typeof error.safe === "function"
        ? (error as { safe(): unknown }).safe()
        : { message: error instanceof Error ? error.message : String(error) };
      this.emit("provider.dispatch.failed", {
        dispatchId: request.dispatchId,
        routeId: request.routeId,
        failure: canonicalize(failure),
      }, {
        runId: request.runId,
        taskId: request.taskId,
        nodeId: request.nodeId,
        routeId: request.routeId,
        dispatchId: request.dispatchId,
        causationId: request.routeId,
        correlationId: request.turnId,
      });
      throw error;
    }
    this.dispatches.succeed(request.dispatchId, claim.snapshot.ownerToken, result);
    cancellation.release("succeeded");
    this.emit("provider.dispatch.completed", {
      dispatchId: result.dispatchId,
      routeId: result.routeId,
      providerId: result.providerId,
      modelId: result.modelId,
      provider_id: result.providerId,
      model_id: result.modelId,
      mutation_id: result.dispatchId,
      revision: this.store.health().eventCount + 1,
      effect_committed: true,
      attemptCount: result.attempts.length,
      frameCount: result.frames.length,
      stopReason: result.stopReason,
    }, {
      runId: request.runId,
      taskId: request.taskId,
      nodeId: request.nodeId,
      routeId: result.routeId,
      dispatchId: result.dispatchId,
      causationId: request.routeId,
      correlationId: request.turnId,
    });
    return result;
  }

  health(): ProviderControlPlaneHealth {
    return {
      schema: "zyra.provider-control-plane.health/v1",
      stateOwner: "typescript.ProviderControlPlaneStore",
      providerFallbackOwned: true,
      backendFallbackOwned: false,
      ...this.store.health(),
    };
  }

  close(): void {
    this.cancellations.close();
    this.store.close();
  }

  private emit(
    eventType: string,
    payload: Record<string, unknown>,
    identity: {
      runId?: string | null;
      taskId?: string | null;
      nodeId?: string | null;
      routeId?: string | null;
      dispatchId?: string | null;
      causationId?: string | null;
      correlationId?: string | null;
    } = {},
  ): ProviderControlEvent {
    const canonicalPayload = canonicalize(payload) as ProviderControlEvent["payload"];
    const event: ProviderControlEvent = {
      eventId: this.ids.next("provider_event"),
      eventType,
      runId: identity.runId ?? null,
      taskId: identity.taskId ?? null,
      nodeId: identity.nodeId ?? null,
      routeId: identity.routeId ?? null,
      dispatchId: identity.dispatchId ?? null,
      causationId: identity.causationId ?? null,
      correlationId: identity.correlationId ?? null,
      createdAt: this.clock.now(),
      payload: deepClone(canonicalPayload),
      payloadDigest: digestJson(canonicalPayload),
    };
    this.store.appendEvent(event);
    return event;
  }
}
