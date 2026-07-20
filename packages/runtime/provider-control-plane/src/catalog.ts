import type {
  CatalogSnapshot,
  IntegrationDefinition,
  ModelDefinition,
  ProviderDefinition,
  V1CompatibilitySnapshot,
  V1ProviderProjection,
} from "./contracts.ts";
import {
  assertIdentifier,
  assertNonNegativeInteger,
  assertPositiveInteger,
  assertUrl,
  deepClone,
  normalizeHeaders,
  uniqueSorted,
} from "./canonical.ts";
import { ProviderControlPlaneError } from "./errors.ts";
import { ProviderControlPlaneStore } from "./store.ts";

export interface CatalogMutationReceipt {
  readonly revision: number;
  readonly entity: "provider" | "model" | "integration";
  readonly action: "upsert" | "remove";
  readonly key: string;
}

export class ProviderCatalog {
  private readonly store: ProviderControlPlaneStore;

  constructor(store: ProviderControlPlaneStore) {
    this.store = store;
  }

  upsertProvider(input: ProviderDefinition, expectedRevision: number | null = null): CatalogMutationReceipt {
    const provider = normalizeProvider(input);
    const revision = this.store.mutateCatalog(expectedRevision, (next) => this.store.putProvider(provider, next));
    return { revision, entity: "provider", action: "upsert", key: provider.providerId };
  }

  removeProvider(providerId: string, expectedRevision: number | null = null): CatalogMutationReceipt {
    assertIdentifier(providerId, "providerId");
    if (this.store.getProvider(providerId) === null) this.notFound("provider", providerId);
    const revision = this.store.mutateCatalog(expectedRevision, () => this.store.removeProvider(providerId));
    return { revision, entity: "provider", action: "remove", key: providerId };
  }

  upsertModel(input: ModelDefinition, expectedRevision: number | null = null): CatalogMutationReceipt {
    const model = normalizeModel(input);
    if (this.store.getProvider(model.providerId) === null) this.notFound("provider", model.providerId);
    const revision = this.store.mutateCatalog(expectedRevision, (next) => this.store.putModel(model, next));
    return { revision, entity: "model", action: "upsert", key: `${model.providerId}/${model.modelId}` };
  }

  removeModel(providerId: string, modelId: string, expectedRevision: number | null = null): CatalogMutationReceipt {
    assertIdentifier(providerId, "providerId");
    assertIdentifier(modelId, "modelId");
    if (this.store.getModel(providerId, modelId) === null) this.notFound("model", `${providerId}/${modelId}`);
    const revision = this.store.mutateCatalog(expectedRevision, () => this.store.removeModel(providerId, modelId));
    return { revision, entity: "model", action: "remove", key: `${providerId}/${modelId}` };
  }

  upsertIntegration(input: IntegrationDefinition, expectedRevision: number | null = null): CatalogMutationReceipt {
    const integration = normalizeIntegration(input);
    const revision = this.store.mutateCatalog(expectedRevision, (next) => this.store.putIntegration(integration, next));
    return { revision, entity: "integration", action: "upsert", key: integration.integrationId };
  }

  removeIntegration(integrationId: string, expectedRevision: number | null = null): CatalogMutationReceipt {
    assertIdentifier(integrationId, "integrationId");
    if (this.store.getIntegration(integrationId) === null) this.notFound("integration", integrationId);
    const revision = this.store.mutateCatalog(expectedRevision, () => this.store.removeIntegration(integrationId));
    return { revision, entity: "integration", action: "remove", key: integrationId };
  }

  provider(providerId: string): ProviderDefinition {
    assertIdentifier(providerId, "providerId");
    const provider = this.store.getProvider(providerId);
    if (provider === null) return this.notFound("provider", providerId);
    return provider;
  }

  model(providerId: string, modelId: string): ModelDefinition {
    assertIdentifier(providerId, "providerId");
    assertIdentifier(modelId, "modelId");
    const model = this.store.getModel(providerId, modelId);
    if (model === null) return this.notFound("model", `${providerId}/${modelId}`);
    return projectModel(model, this.provider(providerId));
  }

  integration(integrationId: string): IntegrationDefinition {
    assertIdentifier(integrationId, "integrationId");
    const integration = this.store.getIntegration(integrationId);
    if (integration === null) return this.notFound("integration", integrationId);
    return integration;
  }

  providers(options: { availableOnly?: boolean } = {}): ProviderDefinition[] {
    return this.store.listProviders().filter((provider) => !options.availableOnly || provider.status === "active");
  }

  models(options: { providerId?: string; availableOnly?: boolean } = {}): ModelDefinition[] {
    const providers = new Map(this.store.listProviders().map((provider) => [provider.providerId, provider]));
    return this.store
      .listModels(options.providerId)
      .filter((model) => {
        const provider = providers.get(model.providerId);
        return provider !== undefined && (!options.availableOnly || (provider.status === "active" && model.enabled && model.status === "active"));
      })
      .map((model) => projectModel(model, providers.get(model.providerId)!))
      .sort((left, right) => right.releasedAt - left.releasedAt || left.modelId.localeCompare(right.modelId));
  }

  integrations(): IntegrationDefinition[] {
    return this.store.listIntegrations();
  }

  snapshot(now = Date.now()): CatalogSnapshot {
    return this.store.snapshot(now);
  }

  compatibilityV1(): V1CompatibilitySnapshot {
    const snapshot = this.snapshot();
    const grouped = new Map<string, ModelDefinition[]>();
    for (const model of snapshot.models) {
      const list = grouped.get(model.providerId) ?? [];
      list.push(model);
      grouped.set(model.providerId, list);
    }
    const providers: V1ProviderProjection[] = snapshot.providers.map((provider) => ({
      id: provider.providerId,
      name: provider.displayName,
      api: provider.baseUrl,
      models: Object.fromEntries(
        (grouped.get(provider.providerId) ?? []).map((model) => [
          model.modelId,
          {
            id: model.modelId,
            name: model.displayName,
            context: model.contextWindow,
            output: model.maximumOutputTokens,
          },
        ]),
      ),
    }));
    return {
      schema: "zyra.provider-compat-v1/read-only",
      catalogRevision: snapshot.revision,
      providers,
      defaultModel: null,
      writable: false,
    };
  }

  private notFound(entity: string, key: string): never {
    throw new ProviderControlPlaneError({
      layer: "catalog",
      kind: entity === "model" ? "model_not_found" : "provider_not_found",
      message: `${entity} not found: ${key}`,
      detail: { entity, key },
    });
  }
}

export function normalizeProvider(input: ProviderDefinition): ProviderDefinition {
  assertIdentifier(input.providerId, "providerId");
  if (input.integrationId !== null) assertIdentifier(input.integrationId, "integrationId");
  assertUrl(input.baseUrl, "baseUrl");
  if (!["active", "disabled", "degraded"].includes(input.status)) throw new TypeError("unsupported provider status");
  if (!["openai_chat", "openai_responses", "anthropic_messages"].includes(input.protocol)) throw new TypeError("unsupported provider protocol");
  return {
    ...deepClone(input),
    displayName: input.displayName.trim() || input.providerId,
    baseUrl: input.baseUrl.replace(/\/+$/, ""),
    defaultHeaders: normalizeHeaders(input.defaultHeaders),
    allowedHosts: uniqueSorted(input.allowedHosts.map((value) => value.toLowerCase())),
    tags: uniqueSorted(input.tags),
  };
}

export function normalizeModel(input: ModelDefinition): ModelDefinition {
  assertIdentifier(input.providerId, "providerId");
  assertIdentifier(input.modelId, "modelId");
  assertNonNegativeInteger(input.releasedAt, "releasedAt");
  assertPositiveInteger(input.contextWindow, "contextWindow");
  assertPositiveInteger(input.maximumOutputTokens, "maximumOutputTokens");
  if (input.maximumOutputTokens > input.contextWindow) throw new TypeError("maximumOutputTokens exceeds contextWindow");
  if (!["active", "deprecated", "disabled"].includes(input.status)) throw new TypeError("unsupported model status");
  for (const price of input.pricing) {
    if (price.inputPerMillion < 0 || price.outputPerMillion < 0 || (price.cachedInputPerMillion ?? 0) < 0) {
      throw new TypeError("model pricing must be non-negative");
    }
  }
  return {
    ...deepClone(input),
    displayName: input.displayName.trim() || input.modelId,
    family: input.family.trim(),
    endpointPath: input.endpointPath?.trim() || null,
    capabilities: {
      ...deepClone(input.capabilities),
      input: uniqueSorted(input.capabilities.input) as ModelDefinition["capabilities"]["input"],
      output: uniqueSorted(input.capabilities.output) as ModelDefinition["capabilities"]["output"],
    },
    tags: uniqueSorted(input.tags),
  };
}

export function normalizeIntegration(input: IntegrationDefinition): IntegrationDefinition {
  assertIdentifier(input.integrationId, "integrationId");
  if (!["api_key", "bearer", "oauth2", "anonymous", "custom_header"].includes(input.kind)) throw new TypeError("unsupported integration kind");
  if (input.kind === "custom_header" && !input.headerName) throw new TypeError("custom-header integration requires headerName");
  return {
    ...deepClone(input),
    displayName: input.displayName.trim() || input.integrationId,
    envNames: uniqueSorted(input.envNames),
    headerName: input.headerName?.trim().toLowerCase() || null,
    authorizationScheme: input.authorizationScheme?.trim() || null,
  };
}

export function projectModel(model: ModelDefinition, provider: ProviderDefinition): ModelDefinition {
  const protocol = model.protocol ?? provider.protocol;
  const requestDefaults = { ...deepClone(provider.requestDefaults), ...deepClone(model.requestDefaults) };
  return { ...deepClone(model), protocol, requestDefaults };
}
