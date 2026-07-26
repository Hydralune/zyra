import { RuntimeSqliteDatabase } from "./sqlite-runtime.ts";
import type {
  CatalogSnapshot,
  CredentialRecord,
  IntegrationDefinition,
  ModelDefinition,
  ProviderControlEvent,
  ProviderDefinition,
  ProviderDispatchAttempt,
  ProviderRouteLease,
  ProviderRouteCredentialSnapshot,
} from "./contracts.ts";
import { canonicalJson, deepClone, digestJson } from "./canonical.ts";
import { ProviderControlPlaneError } from "./errors.ts";
import {
  migrateProviderControlPlaneSchema,
  type ProviderSchemaMigrationHealth,
} from "./schema-migration.ts";

interface MetaRow { value: string }
interface JsonRow { json: string }
interface CountRow { count: number }

export interface ProviderStoreHealth {
  readonly open: boolean;
  readonly path: string;
  readonly catalogRevision: number;
  readonly providerCount: number;
  readonly modelCount: number;
  readonly integrationCount: number;
  readonly credentialCount: number;
  readonly routeCount: number;
  readonly eventCount: number;
  readonly attemptCount: number;
  readonly schemaVersion: number;
  readonly schemaMigrationState: ProviderSchemaMigrationHealth["state"];
  readonly schemaMigrationTransactionId: string | null;
  readonly recoveredSchemaTransactions: readonly string[];
}

export class ProviderControlPlaneStore {
  readonly path: string;
  readonly db: RuntimeSqliteDatabase;
  private closed = false;
  private schemaHealth!: ProviderSchemaMigrationHealth;

  constructor(path: string) {
    this.path = path;
    this.db = new RuntimeSqliteDatabase(path);
    try {
      this.initialize();
    } catch (error) {
      this.closed = true;
      this.db.close();
      throw error;
    }
  }

  initialize(): void {
    this.assertOpen();
    this.db.exec("PRAGMA journal_mode = WAL");
    this.db.exec("PRAGMA synchronous = FULL");
    this.db.exec("PRAGMA foreign_keys = ON");
    this.db.exec("PRAGMA busy_timeout = 5000");
    this.schemaHealth = migrateProviderControlPlaneSchema(this.db);
  }

  transaction<T>(callback: () => T): T {
    this.assertOpen();
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const result = callback();
      this.db.exec("COMMIT");
      return result;
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
  }

  catalogRevision(): number {
    this.assertOpen();
    const row = this.db.prepare("SELECT value FROM provider_control_meta WHERE key = 'catalog_revision'").get() as MetaRow | undefined;
    return Number(row?.value ?? 0);
  }

  mutateCatalog(expectedRevision: number | null, callback: (revision: number) => void): number {
    return this.transaction(() => {
      const current = this.catalogRevision();
      if (expectedRevision !== null && current !== expectedRevision) {
        throw new ProviderControlPlaneError({
          layer: "catalog",
          kind: "catalog_revision_conflict",
          message: `catalog revision conflict: expected ${expectedRevision}, actual ${current}`,
          detail: { expectedRevision, actualRevision: current },
        });
      }
      const next = current + 1;
      callback(next);
      this.db.prepare("UPDATE provider_control_meta SET value = ? WHERE key = 'catalog_revision'").run(String(next));
      const snapshot = this.snapshot(Date.now());
      this.putCatalogSnapshot(snapshot);
      return next;
    });
  }

  putProvider(provider: ProviderDefinition, revision: number): void {
    const json = canonicalJson(provider);
    this.db.prepare(`
      INSERT INTO provider_catalog_providers(provider_id, catalog_revision, json, checksum)
      VALUES (?, ?, ?, ?)
      ON CONFLICT(provider_id) DO UPDATE SET
        catalog_revision = excluded.catalog_revision,
        json = excluded.json,
        checksum = excluded.checksum
    `).run(provider.providerId, revision, json, digestJson(provider));
  }

  removeProvider(providerId: string): void {
    this.db.prepare("DELETE FROM provider_catalog_providers WHERE provider_id = ?").run(providerId);
  }

  getProvider(providerId: string): ProviderDefinition | null {
    return this.readJson<ProviderDefinition>(
      "SELECT json FROM provider_catalog_providers WHERE provider_id = ?",
      providerId,
    );
  }

  listProviders(): ProviderDefinition[] {
    return this.readJsonList<ProviderDefinition>("SELECT json FROM provider_catalog_providers ORDER BY provider_id");
  }

  putModel(model: ModelDefinition, revision: number): void {
    const json = canonicalJson(model);
    this.db.prepare(`
      INSERT INTO provider_catalog_models(provider_id, model_id, catalog_revision, json, checksum)
      VALUES (?, ?, ?, ?, ?)
      ON CONFLICT(provider_id, model_id) DO UPDATE SET
        catalog_revision = excluded.catalog_revision,
        json = excluded.json,
        checksum = excluded.checksum
    `).run(model.providerId, model.modelId, revision, json, digestJson(model));
  }

  removeModel(providerId: string, modelId: string): void {
    this.db.prepare("DELETE FROM provider_catalog_models WHERE provider_id = ? AND model_id = ?").run(providerId, modelId);
  }

  getModel(providerId: string, modelId: string): ModelDefinition | null {
    return this.readJson<ModelDefinition>(
      "SELECT json FROM provider_catalog_models WHERE provider_id = ? AND model_id = ?",
      providerId,
      modelId,
    );
  }

  listModels(providerId?: string): ModelDefinition[] {
    return providerId === undefined
      ? this.readJsonList<ModelDefinition>("SELECT json FROM provider_catalog_models ORDER BY provider_id, model_id")
      : this.readJsonList<ModelDefinition>(
          "SELECT json FROM provider_catalog_models WHERE provider_id = ? ORDER BY model_id",
          providerId,
        );
  }

  putIntegration(integration: IntegrationDefinition, revision: number): void {
    const json = canonicalJson(integration);
    this.db.prepare(`
      INSERT INTO provider_integrations(integration_id, catalog_revision, json, checksum)
      VALUES (?, ?, ?, ?)
      ON CONFLICT(integration_id) DO UPDATE SET
        catalog_revision = excluded.catalog_revision,
        json = excluded.json,
        checksum = excluded.checksum
    `).run(integration.integrationId, revision, json, digestJson(integration));
  }

  removeIntegration(integrationId: string): void {
    this.db.prepare("DELETE FROM provider_integrations WHERE integration_id = ?").run(integrationId);
  }

  getIntegration(integrationId: string): IntegrationDefinition | null {
    return this.readJson<IntegrationDefinition>(
      "SELECT json FROM provider_integrations WHERE integration_id = ?",
      integrationId,
    );
  }

  listIntegrations(): IntegrationDefinition[] {
    return this.readJsonList<IntegrationDefinition>("SELECT json FROM provider_integrations ORDER BY integration_id");
  }

  putCatalogSnapshot(snapshot: CatalogSnapshot): void {
    const json = canonicalJson(snapshot);
    this.db.prepare(`
      INSERT INTO provider_catalog_snapshots(catalog_revision, created_at, json, checksum)
      VALUES (?, ?, ?, ?)
      ON CONFLICT(catalog_revision) DO UPDATE SET
        created_at = excluded.created_at,
        json = excluded.json,
        checksum = excluded.checksum
    `).run(snapshot.revision, snapshot.createdAt, json, digestJson(snapshot));
  }

  getCatalogSnapshot(revision: number): CatalogSnapshot | null {
    const snapshot = this.readJson<CatalogSnapshot>(
      "SELECT json FROM provider_catalog_snapshots WHERE catalog_revision = ?",
      revision,
    );
    if (snapshot === null) return null;
    const { checksum, ...body } = snapshot;
    if (digestJson(body) !== checksum) {
      throw new ProviderControlPlaneError({
        layer: "catalog",
        kind: "catalog_revision_conflict",
        message: `catalog snapshot checksum mismatch: ${revision}`,
        detail: { revision },
      });
    }
    return snapshot;
  }

  putCredential(record: CredentialRecord): void {
    const json = canonicalJson(record);
    this.db.prepare(`
      INSERT INTO provider_credentials(
        credential_id, provider_id, integration_id, version, status, expires_at, json, checksum
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(credential_id) DO UPDATE SET
        provider_id = excluded.provider_id,
        integration_id = excluded.integration_id,
        version = excluded.version,
        status = excluded.status,
        expires_at = excluded.expires_at,
        json = excluded.json,
        checksum = excluded.checksum
    `).run(
      record.credentialId,
      record.providerId,
      record.integrationId,
      record.version,
      record.status,
      record.expiresAt,
      json,
      digestJson(record),
    );
  }

  getCredential(credentialId: string): CredentialRecord | null {
    return this.readJson<CredentialRecord>(
      "SELECT json FROM provider_credentials WHERE credential_id = ?",
      credentialId,
    );
  }

  listCredentials(providerId?: string): CredentialRecord[] {
    return providerId === undefined
      ? this.readJsonList<CredentialRecord>("SELECT json FROM provider_credentials ORDER BY provider_id, credential_id")
      : this.readJsonList<CredentialRecord>(
          "SELECT json FROM provider_credentials WHERE provider_id = ? ORDER BY credential_id",
          providerId,
        );
  }

  putRoute(
    lease: ProviderRouteLease,
    credentialSnapshot: ProviderRouteCredentialSnapshot,
  ): void {
    const json = canonicalJson(lease);
    const snapshotJson = canonicalJson(credentialSnapshot);
    this.transaction(() => {
      this.db.prepare(`
        INSERT INTO provider_route_leases(
          route_id, run_id, task_id, session_id, turn_id, catalog_revision,
          provider_id, model_id, credential_id, credential_version,
          created_at, expires_at, checksum, json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      `).run(
        lease.routeId,
        lease.runId,
        lease.taskId,
        lease.sessionId,
        lease.turnId,
        lease.catalogRevision,
        lease.providerId,
        lease.modelId,
        lease.credentialId,
        lease.credentialVersion,
        lease.createdAt,
        lease.expiresAt,
        lease.checksum,
        json,
      );
      this.db.prepare(`
        INSERT INTO provider_route_credential_snapshots(
          route_id, credential_id, credential_version, created_at, json, checksum
        ) VALUES (?, ?, ?, ?, ?, ?)
      `).run(
        credentialSnapshot.routeId,
        credentialSnapshot.credentialId,
        credentialSnapshot.credentialVersion,
        credentialSnapshot.createdAt,
        snapshotJson,
        credentialSnapshot.checksum,
      );
    });
  }

  getRouteCredentialSnapshot(routeId: string): ProviderRouteCredentialSnapshot | null {
    return this.readJson<ProviderRouteCredentialSnapshot>(
      "SELECT json FROM provider_route_credential_snapshots WHERE route_id = ?",
      routeId,
    );
  }

  getRoute(routeId: string): ProviderRouteLease | null {
    return this.readJson<ProviderRouteLease>(
      "SELECT json FROM provider_route_leases WHERE route_id = ?",
      routeId,
    );
  }

  listRoutes(runId?: string, taskId?: string): ProviderRouteLease[] {
    if (runId !== undefined && taskId !== undefined) {
      return this.readJsonList<ProviderRouteLease>(
        "SELECT json FROM provider_route_leases WHERE run_id = ? AND task_id = ? ORDER BY created_at, route_id",
        runId,
        taskId,
      );
    }
    return this.readJsonList<ProviderRouteLease>("SELECT json FROM provider_route_leases ORDER BY created_at, route_id");
  }

  putAttempt(attempt: ProviderDispatchAttempt): void {
    const json = canonicalJson(attempt);
    this.db.prepare(`
      INSERT INTO provider_dispatch_attempts(
        attempt_id, dispatch_id, route_id, attempt_number, outcome,
        started_at, completed_at, json, checksum
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(attempt_id) DO UPDATE SET
        outcome = excluded.outcome,
        completed_at = excluded.completed_at,
        json = excluded.json,
        checksum = excluded.checksum
    `).run(
      attempt.attemptId,
      attempt.dispatchId,
      attempt.routeId,
      attempt.attempt,
      attempt.outcome,
      attempt.startedAt,
      attempt.completedAt,
      json,
      digestJson(attempt),
    );
  }

  listAttempts(dispatchId: string): ProviderDispatchAttempt[] {
    return this.readJsonList<ProviderDispatchAttempt>(
      "SELECT json FROM provider_dispatch_attempts WHERE dispatch_id = ? ORDER BY attempt_number",
      dispatchId,
    );
  }

  appendEvent(event: ProviderControlEvent): void {
    this.db.prepare(`
      INSERT INTO provider_control_events(
        event_id, event_type, run_id, task_id, route_id, dispatch_id,
        causation_id, correlation_id, created_at, payload_digest, json
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      event.eventId,
      event.eventType,
      event.runId,
      event.taskId,
      event.routeId,
      event.dispatchId,
      event.causationId,
      event.correlationId,
      event.createdAt,
      event.payloadDigest,
      canonicalJson(event),
    );
  }

  listEvents(runId?: string, taskId?: string): ProviderControlEvent[] {
    if (runId !== undefined && taskId !== undefined) {
      return this.readJsonList<ProviderControlEvent>(
        "SELECT json FROM provider_control_events WHERE run_id = ? AND task_id = ? ORDER BY created_at, event_id",
        runId,
        taskId,
      );
    }
    return this.readJsonList<ProviderControlEvent>("SELECT json FROM provider_control_events ORDER BY created_at, event_id");
  }

  snapshot(createdAt: number): CatalogSnapshot {
    const body = {
      schema: "zyra.provider-catalog/v1" as const,
      revision: this.catalogRevision(),
      createdAt,
      providers: this.listProviders(),
      models: this.listModels(),
      integrations: this.listIntegrations(),
    };
    return { ...body, checksum: digestJson(body) };
  }

  health(): ProviderStoreHealth {
    return {
      open: !this.closed,
      path: this.path,
      catalogRevision: this.catalogRevision(),
      providerCount: this.count("provider_catalog_providers"),
      modelCount: this.count("provider_catalog_models"),
      integrationCount: this.count("provider_integrations"),
      credentialCount: this.count("provider_credentials"),
      routeCount: this.count("provider_route_leases"),
      eventCount: this.count("provider_control_events"),
      attemptCount: this.count("provider_dispatch_attempts"),
      schemaVersion: this.schemaHealth.version,
      schemaMigrationState: this.schemaHealth.state,
      schemaMigrationTransactionId: this.schemaHealth.transactionId,
      recoveredSchemaTransactions:
        this.schemaHealth.recoveredTransactionIds,
    };
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.db.close();
  }

  private readJson<T>(sql: string, ...parameters: (string | number | null)[]): T | null {
    this.assertOpen();
    const row = this.db.prepare(sql).get(...parameters) as JsonRow | undefined;
    return row === undefined ? null : deepClone(JSON.parse(row.json) as T);
  }

  private readJsonList<T>(sql: string, ...parameters: (string | number | null)[]): T[] {
    this.assertOpen();
    const rows = this.db.prepare(sql).all(...parameters) as unknown as JsonRow[];
    return rows.map((row) => deepClone(JSON.parse(row.json) as T));
  }

  private count(table: string): number {
    if (!/^provider_[a-z_]+$/.test(table)) throw new TypeError("invalid provider table name");
    const row = this.db.prepare(`SELECT COUNT(*) AS count FROM ${table}`).get() as unknown as CountRow;
    return Number(row.count);
  }

  private assertOpen(): void {
    if (this.closed) throw new Error("provider control-plane store is closed");
  }
}
