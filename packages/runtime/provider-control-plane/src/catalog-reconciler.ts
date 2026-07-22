import type { JsonRecord } from "./canonical.ts";
import {
  type Clock,
  type IdFactory,
  RandomIdFactory,
  SystemClock,
  canonicalJson,
  deepClone,
  digestJson,
} from "./canonical.ts";
import {
  ProviderCatalog,
  normalizeIntegration,
  normalizeModel,
  normalizeProvider,
} from "./catalog.ts";
import type {
  IntegrationDefinition,
  ModelDefinition,
  ProviderDefinition,
} from "./contracts.ts";
import { ProviderControlPlaneError } from "./errors.ts";
import { ProviderControlPlaneStore } from "./store.ts";

// Cropped from opencode catalog/provider/integration reconciliation at
// adf178a6. Zyra deliberately excludes hidden defaults and network discovery;
// callers submit an explicit document that is validated and atomically applied.

export interface CatalogDiscoveryDocument {
  readonly sourceId: string;
  readonly sourceRevision: string;
  readonly providers: readonly ProviderDefinition[];
  readonly models: readonly ModelDefinition[];
  readonly integrations: readonly IntegrationDefinition[];
  readonly removeMissing: boolean;
  readonly metadata: JsonRecord;
}

export interface CatalogReconcileDiff {
  readonly providerUpserts: readonly string[];
  readonly providerRemovals: readonly string[];
  readonly modelUpserts: readonly string[];
  readonly modelRemovals: readonly string[];
  readonly integrationUpserts: readonly string[];
  readonly integrationRemovals: readonly string[];
  readonly unchangedProviders: readonly string[];
  readonly unchangedModels: readonly string[];
  readonly unchangedIntegrations: readonly string[];
}

export interface CatalogReconcileReceipt {
  readonly runId: string;
  readonly sourceId: string;
  readonly sourceRevision: string;
  readonly sourceDigest: string;
  readonly baseCatalogRevision: number;
  readonly catalogRevision: number;
  readonly state: "validated" | "published" | "failed";
  readonly dryRun: boolean;
  readonly diff: CatalogReconcileDiff;
  readonly createdAt: number;
  readonly completedAt: number;
  readonly failure: JsonRecord | null;
}

interface JsonRow { readonly json: string }

export class ProviderCatalogReconciler {
  private readonly store: ProviderControlPlaneStore;
  private readonly catalog: ProviderCatalog;
  private readonly clock: Clock;
  private readonly ids: IdFactory;

  constructor(
    store: ProviderControlPlaneStore,
    catalog: ProviderCatalog,
    options: { readonly clock?: Clock; readonly ids?: IdFactory } = {},
  ) {
    this.store = store;
    this.catalog = catalog;
    this.clock = options.clock ?? new SystemClock();
    this.ids = options.ids ?? new RandomIdFactory();
    this.initialize();
  }

  reconcile(
    raw: CatalogDiscoveryDocument,
    options: { readonly expectedRevision?: number | null; readonly dryRun?: boolean } = {},
  ): CatalogReconcileReceipt {
    const createdAt = this.clock.now();
    const runId = this.ids.next("provider_catalog_reconcile");
    let document: CatalogDiscoveryDocument;
    let diff: CatalogReconcileDiff;
    let sourceDigest = "";
    let baseCatalogRevision = this.store.catalogRevision();
    try {
      document = normalizeDiscovery(raw);
      sourceDigest = digestJson(document);
      diff = this.diff(document);
      baseCatalogRevision = this.store.catalogRevision();
      const expectedRevision = options.expectedRevision ?? null;
      if (expectedRevision !== null && expectedRevision !== baseCatalogRevision) {
        throw new ProviderControlPlaneError({
          layer: "catalog",
          kind: "catalog_revision_conflict",
          message: `catalog reconcile revision conflict: expected ${expectedRevision}, actual ${baseCatalogRevision}`,
          detail: { expectedRevision, actualRevision: baseCatalogRevision, runId },
        });
      }
      if (options.dryRun === true || diffEmpty(diff)) {
        const receipt: CatalogReconcileReceipt = {
          runId,
          sourceId: document.sourceId,
          sourceRevision: document.sourceRevision,
          sourceDigest,
          baseCatalogRevision,
          catalogRevision: baseCatalogRevision,
          state: "validated",
          dryRun: options.dryRun === true,
          diff,
          createdAt,
          completedAt: this.clock.now(),
          failure: null,
        };
        this.write(receipt);
        return deepClone(receipt);
      }
      const revision = this.store.mutateCatalog(baseCatalogRevision, (nextRevision) => {
        const integrations = new Map(document.integrations.map((item) => [item.integrationId, item]));
        const providers = new Map(document.providers.map((item) => [item.providerId, item]));
        const models = new Map(document.models.map((item) => [`${item.providerId}/${item.modelId}`, item]));
        for (const key of diff.integrationUpserts) this.store.putIntegration(requireMap(integrations, key), nextRevision);
        for (const key of diff.providerUpserts) this.store.putProvider(requireMap(providers, key), nextRevision);
        for (const key of diff.modelUpserts) this.store.putModel(requireMap(models, key), nextRevision);
        if (document.removeMissing) {
          for (const key of diff.modelRemovals) {
            const [providerId = "", modelId = ""] = key.split("/", 2);
            this.store.removeModel(providerId, modelId);
          }
          for (const key of diff.providerRemovals) this.store.removeProvider(key);
          for (const key of diff.integrationRemovals) this.store.removeIntegration(key);
        }
      });
      const receipt: CatalogReconcileReceipt = {
        runId,
        sourceId: document.sourceId,
        sourceRevision: document.sourceRevision,
        sourceDigest,
        baseCatalogRevision,
        catalogRevision: revision,
        state: "published",
        dryRun: false,
        diff,
        createdAt,
        completedAt: this.clock.now(),
        failure: null,
      };
      this.write(receipt);
      return deepClone(receipt);
    } catch (error) {
      const receipt: CatalogReconcileReceipt = {
        runId,
        sourceId: typeof raw?.sourceId === "string" ? raw.sourceId : "invalid",
        sourceRevision: typeof raw?.sourceRevision === "string" ? raw.sourceRevision : "invalid",
        sourceDigest,
        baseCatalogRevision,
        catalogRevision: baseCatalogRevision,
        state: "failed",
        dryRun: options.dryRun === true,
        diff: emptyDiff(),
        createdAt,
        completedAt: this.clock.now(),
        failure: {
          type: error instanceof Error ? error.name : "Error",
          message: error instanceof Error ? error.message : String(error),
        },
      };
      this.write(receipt);
      throw error;
    }
  }

  runs(sourceId?: string): CatalogReconcileReceipt[] {
    const rows = sourceId === undefined
      ? this.store.db.prepare("SELECT json FROM provider_catalog_reconcile_runs ORDER BY created_at, run_id").all()
      : this.store.db.prepare("SELECT json FROM provider_catalog_reconcile_runs WHERE source_id = ? ORDER BY created_at, run_id").all(sourceId);
    return (rows as unknown as JsonRow[]).map((row) => deepClone(JSON.parse(row.json) as CatalogReconcileReceipt));
  }

  private diff(document: CatalogDiscoveryDocument): CatalogReconcileDiff {
    const currentProviders = new Map(this.catalog.providers().map((item) => [item.providerId, item]));
    const currentModels = new Map(this.catalog.models().map((item) => [`${item.providerId}/${item.modelId}`, item]));
    const currentIntegrations = new Map(this.catalog.integrations().map((item) => [item.integrationId, item]));
    const desiredProviders = new Map(document.providers.map((item) => [item.providerId, item]));
    const desiredModels = new Map(document.models.map((item) => [`${item.providerId}/${item.modelId}`, item]));
    const desiredIntegrations = new Map(document.integrations.map((item) => [item.integrationId, item]));
    const providerDiff = mapDiff(currentProviders, desiredProviders, document.removeMissing);
    const modelDiff = mapDiff(currentModels, desiredModels, document.removeMissing);
    const integrationDiff = mapDiff(currentIntegrations, desiredIntegrations, document.removeMissing);
    return {
      providerUpserts: providerDiff.upserts,
      providerRemovals: providerDiff.removals,
      modelUpserts: modelDiff.upserts,
      modelRemovals: modelDiff.removals,
      integrationUpserts: integrationDiff.upserts,
      integrationRemovals: integrationDiff.removals,
      unchangedProviders: providerDiff.unchanged,
      unchangedModels: modelDiff.unchanged,
      unchangedIntegrations: integrationDiff.unchanged,
    };
  }

  private initialize(): void {
    this.store.db.exec(`
      CREATE TABLE IF NOT EXISTS provider_catalog_reconcile_runs (
        run_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        source_revision TEXT NOT NULL,
        source_digest TEXT NOT NULL,
        base_catalog_revision INTEGER NOT NULL,
        catalog_revision INTEGER NOT NULL,
        state TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        completed_at INTEGER NOT NULL,
        json TEXT NOT NULL,
        checksum TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_provider_catalog_reconcile_source
        ON provider_catalog_reconcile_runs(source_id, created_at);
    `);
  }

  private write(receipt: CatalogReconcileReceipt): void {
    const json = canonicalJson(receipt);
    this.store.db.prepare(`
      INSERT INTO provider_catalog_reconcile_runs(
        run_id, source_id, source_revision, source_digest,
        base_catalog_revision, catalog_revision, state, created_at,
        completed_at, json, checksum
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(run_id) DO UPDATE SET
        state = excluded.state,
        completed_at = excluded.completed_at,
        json = excluded.json,
        checksum = excluded.checksum
    `).run(
      receipt.runId,
      receipt.sourceId,
      receipt.sourceRevision,
      receipt.sourceDigest,
      receipt.baseCatalogRevision,
      receipt.catalogRevision,
      receipt.state,
      receipt.createdAt,
      receipt.completedAt,
      json,
      digestJson(receipt),
    );
  }
}

function normalizeDiscovery(raw: CatalogDiscoveryDocument): CatalogDiscoveryDocument {
  if (!raw || typeof raw !== "object") throw new TypeError("catalog discovery document is required");
  const sourceId = String(raw.sourceId ?? "").trim();
  const sourceRevision = String(raw.sourceRevision ?? "").trim();
  if (!sourceId) throw new TypeError("catalog discovery sourceId is required");
  if (!sourceRevision) throw new TypeError("catalog discovery sourceRevision is required");
  if (!Array.isArray(raw.providers) || !Array.isArray(raw.models) || !Array.isArray(raw.integrations)) {
    throw new TypeError("catalog discovery entity collections must be arrays");
  }
  const providers = raw.providers.map(normalizeProvider);
  const models = raw.models.map(normalizeModel);
  const integrations = raw.integrations.map(normalizeIntegration);
  assertUnique(providers.map((item) => item.providerId), "provider");
  assertUnique(models.map((item) => `${item.providerId}/${item.modelId}`), "model");
  assertUnique(integrations.map((item) => item.integrationId), "integration");
  const providerIds = new Set(providers.map((item) => item.providerId));
  const integrationIds = new Set(integrations.map((item) => item.integrationId));
  for (const provider of providers) {
    if (provider.integrationId !== null && !integrationIds.has(provider.integrationId)) {
      throw new TypeError(`provider references missing integration: ${provider.providerId}/${provider.integrationId}`);
    }
  }
  for (const model of models) {
    if (!providerIds.has(model.providerId)) throw new TypeError(`model references missing provider: ${model.providerId}/${model.modelId}`);
  }
  return {
    sourceId,
    sourceRevision,
    providers,
    models,
    integrations,
    removeMissing: raw.removeMissing === true,
    metadata: deepClone(raw.metadata ?? {}),
  };
}

function mapDiff<T>(
  current: ReadonlyMap<string, T>,
  desired: ReadonlyMap<string, T>,
  removeMissing: boolean,
): { readonly upserts: string[]; readonly removals: string[]; readonly unchanged: string[] } {
  const upserts: string[] = [];
  const unchanged: string[] = [];
  for (const [key, value] of desired) {
    const existing = current.get(key);
    if (existing !== undefined && digestJson(existing) === digestJson(value)) unchanged.push(key);
    else upserts.push(key);
  }
  const removals = removeMissing ? [...current.keys()].filter((key) => !desired.has(key)) : [];
  return {
    upserts: upserts.sort(),
    removals: removals.sort(),
    unchanged: unchanged.sort(),
  };
}

function assertUnique(values: readonly string[], name: string): void {
  const seen = new Set<string>();
  for (const value of values) {
    if (seen.has(value)) throw new TypeError(`duplicate ${name} identity: ${value}`);
    seen.add(value);
  }
}

function requireMap<T>(values: ReadonlyMap<string, T>, key: string): T {
  const value = values.get(key);
  if (value === undefined) throw new Error(`reconcile diff references missing entity: ${key}`);
  return value;
}

function emptyDiff(): CatalogReconcileDiff {
  return {
    providerUpserts: [],
    providerRemovals: [],
    modelUpserts: [],
    modelRemovals: [],
    integrationUpserts: [],
    integrationRemovals: [],
    unchangedProviders: [],
    unchangedModels: [],
    unchangedIntegrations: [],
  };
}

function diffEmpty(diff: CatalogReconcileDiff): boolean {
  return diff.providerUpserts.length === 0
    && diff.providerRemovals.length === 0
    && diff.modelUpserts.length === 0
    && diff.modelRemovals.length === 0
    && diff.integrationUpserts.length === 0
    && diff.integrationRemovals.length === 0;
}
