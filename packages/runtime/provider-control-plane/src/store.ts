import { DatabaseSync } from "node:sqlite";
import type {
  CatalogSnapshot,
  CredentialRecord,
  IntegrationDefinition,
  ModelDefinition,
  ProviderControlEvent,
  ProviderDefinition,
  ProviderDispatchAttempt,
  ProviderRouteLease,
} from "./contracts.ts";
import { canonicalJson, deepClone, digestJson } from "./canonical.ts";
import { ProviderControlPlaneError } from "./errors.ts";

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
}

export class ProviderControlPlaneStore {
  readonly path: string;
  readonly db: DatabaseSync;
  private closed = false;

  constructor(path: string) {
    this.path = path;
    this.db = new DatabaseSync(path);
    this.initialize();
  }

  initialize(): void {
    this.assertOpen();
    this.db.exec("PRAGMA journal_mode = WAL");
    this.db.exec("PRAGMA synchronous = FULL");
    this.db.exec("PRAGMA foreign_keys = ON");
    this.db.exec("PRAGMA busy_timeout = 5000");
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS provider_control_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
      );
      INSERT OR IGNORE INTO provider_control_meta(key, value) VALUES ('catalog_revision', '0');

      CREATE TABLE IF NOT EXISTS provider_catalog_providers (
        provider_id TEXT PRIMARY KEY,
        catalog_revision INTEGER NOT NULL,
        json TEXT NOT NULL,
        checksum TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS provider_catalog_models (
        provider_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        catalog_revision INTEGER NOT NULL,
        json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        PRIMARY KEY(provider_id, model_id),
        FOREIGN KEY(provider_id) REFERENCES provider_catalog_providers(provider_id) ON DELETE CASCADE
      );

      CREATE TABLE IF NOT EXISTS provider_integrations (
        integration_id TEXT PRIMARY KEY,
        catalog_revision INTEGER NOT NULL,
        json TEXT NOT NULL,
        checksum TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS provider_credentials (
        credential_id TEXT PRIMARY KEY,
        provider_id TEXT NOT NULL,
        integration_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        status TEXT NOT NULL,
        expires_at INTEGER,
        json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        FOREIGN KEY(provider_id) REFERENCES provider_catalog_providers(provider_id) ON DELETE RESTRICT,
        FOREIGN KEY(integration_id) REFERENCES provider_integrations(integration_id) ON DELETE RESTRICT
      );

      CREATE TABLE IF NOT EXISTS provider_route_leases (
        route_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        catalog_revision INTEGER NOT NULL,
        provider_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        credential_id TEXT NOT NULL,
        credential_version INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        checksum TEXT NOT NULL,
        json TEXT NOT NULL,
        UNIQUE(session_id, turn_id, route_id),
        FOREIGN KEY(provider_id, model_id) REFERENCES provider_catalog_models(provider_id, model_id) ON DELETE RESTRICT,
        FOREIGN KEY(credential_id) REFERENCES provider_credentials(credential_id) ON DELETE RESTRICT
      );

      CREATE TABLE IF NOT EXISTS provider_dispatch_attempts (
        attempt_id TEXT PRIMARY KEY,
        dispatch_id TEXT NOT NULL,
        route_id TEXT NOT NULL,
        attempt_number INTEGER NOT NULL,
        outcome TEXT NOT NULL,
        started_at INTEGER NOT NULL,
        completed_at INTEGER,
        json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        FOREIGN KEY(route_id) REFERENCES provider_route_leases(route_id) ON DELETE RESTRICT,
        UNIQUE(dispatch_id, attempt_number)
      );

      CREATE TABLE IF NOT EXISTS provider_control_events (
        event_id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        run_id TEXT,
        task_id TEXT,
        route_id TEXT,
        dispatch_id TEXT,
        causation_id TEXT,
        correlation_id TEXT,
        created_at INTEGER NOT NULL,
        payload_digest TEXT NOT NULL,
        json TEXT NOT NULL
      );

      CREATE INDEX IF NOT EXISTS idx_provider_models_status ON provider_catalog_models(provider_id, model_id);
      CREATE INDEX IF NOT EXISTS idx_provider_credentials_select ON provider_credentials(provider_id, status, expires_at);
      CREATE INDEX IF NOT EXISTS idx_provider_routes_turn ON provider_route_leases(session_id, turn_id, created_at);
      CREATE INDEX IF NOT EXISTS idx_provider_attempts_dispatch ON provider_dispatch_attempts(dispatch_id, attempt_number);
      CREATE INDEX IF NOT EXISTS idx_provider_events_task ON provider_control_events(run_id, task_id, created_at);
    `);
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

  putRoute(lease: ProviderRouteLease): void {
    const json = canonicalJson(lease);
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
