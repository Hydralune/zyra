import { randomUUID } from "node:crypto";

import { RuntimeSqliteDatabase } from "./sqlite-runtime.ts";

export const PROVIDER_CONTROL_SCHEMA_VERSION = 1;

export type ProviderSchemaMigrationState =
  | "not_required"
  | "prepared"
  | "applying"
  | "verifying"
  | "committed"
  | "rolling_back"
  | "rolled_back";

export interface ProviderSchemaMigrationHealth {
  readonly schema: "zyra.provider-schema-migration-health/v1";
  readonly version: number;
  readonly targetVersion: number;
  readonly state: ProviderSchemaMigrationState;
  readonly transactionId: string | null;
  readonly recoveredTransactionIds: readonly string[];
  readonly integrity: "ok";
}

export interface ProviderSchemaMigrationOptions {
  readonly clock?: () => number;
  readonly transactionId?: () => string;
  readonly faultHook?: (
    phase: "after_prepare" | "after_apply" | "before_commit",
    transactionId: string,
  ) => void;
}

interface ValueRow {
  readonly value: string;
}

interface NameRow {
  readonly name: string;
}

interface IntegrityRow {
  readonly integrity_check?: string;
  readonly quick_check?: string;
}

interface MigrationRow {
  readonly transaction_id: string;
  readonly state: ProviderSchemaMigrationState;
}

const REQUIRED_TABLES = Object.freeze([
  "provider_control_meta",
  "provider_catalog_providers",
  "provider_catalog_models",
  "provider_integrations",
  "provider_catalog_snapshots",
  "provider_credentials",
  "provider_route_leases",
  "provider_route_credential_snapshots",
  "provider_dispatch_attempts",
  "provider_control_events",
  "provider_schema_migrations",
]);

const REQUIRED_COLUMNS: Readonly<Record<string, readonly string[]>> =
  Object.freeze({
    provider_control_meta: ["key", "value"],
    provider_catalog_providers: [
      "provider_id",
      "catalog_revision",
      "json",
      "checksum",
    ],
    provider_catalog_models: [
      "provider_id",
      "model_id",
      "catalog_revision",
      "json",
      "checksum",
    ],
    provider_integrations: [
      "integration_id",
      "catalog_revision",
      "json",
      "checksum",
    ],
    provider_catalog_snapshots: [
      "catalog_revision",
      "created_at",
      "json",
      "checksum",
    ],
    provider_credentials: [
      "credential_id",
      "provider_id",
      "integration_id",
      "version",
      "status",
      "expires_at",
      "json",
      "checksum",
    ],
    provider_route_leases: [
      "route_id",
      "run_id",
      "task_id",
      "session_id",
      "turn_id",
      "catalog_revision",
      "provider_id",
      "model_id",
      "credential_id",
      "credential_version",
      "created_at",
      "expires_at",
      "checksum",
      "json",
    ],
    provider_route_credential_snapshots: [
      "route_id",
      "credential_id",
      "credential_version",
      "created_at",
      "json",
      "checksum",
    ],
    provider_dispatch_attempts: [
      "attempt_id",
      "dispatch_id",
      "route_id",
      "attempt_number",
      "outcome",
      "started_at",
      "completed_at",
      "json",
      "checksum",
    ],
    provider_control_events: [
      "event_id",
      "event_type",
      "run_id",
      "task_id",
      "route_id",
      "dispatch_id",
      "causation_id",
      "correlation_id",
      "created_at",
      "payload_digest",
      "json",
    ],
    provider_schema_migrations: [
      "transaction_id",
      "source_version",
      "target_version",
      "state",
      "process_generation",
      "started_at",
      "updated_at",
      "error_code",
      "error_message",
    ],
  });

const SCHEMA_V1_SQL = `
  CREATE TABLE IF NOT EXISTS provider_control_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
  );
  INSERT OR IGNORE INTO provider_control_meta(key, value)
  VALUES ('catalog_revision', '0');

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
    FOREIGN KEY(provider_id)
      REFERENCES provider_catalog_providers(provider_id)
      ON DELETE CASCADE
  );

  CREATE TABLE IF NOT EXISTS provider_integrations (
    integration_id TEXT PRIMARY KEY,
    catalog_revision INTEGER NOT NULL,
    json TEXT NOT NULL,
    checksum TEXT NOT NULL
  );

  CREATE TABLE IF NOT EXISTS provider_catalog_snapshots (
    catalog_revision INTEGER PRIMARY KEY,
    created_at INTEGER NOT NULL,
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
    FOREIGN KEY(provider_id)
      REFERENCES provider_catalog_providers(provider_id)
      ON DELETE RESTRICT,
    FOREIGN KEY(integration_id)
      REFERENCES provider_integrations(integration_id)
      ON DELETE RESTRICT
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
    FOREIGN KEY(provider_id, model_id)
      REFERENCES provider_catalog_models(provider_id, model_id)
      ON DELETE RESTRICT,
    FOREIGN KEY(credential_id)
      REFERENCES provider_credentials(credential_id)
      ON DELETE RESTRICT
  );

  CREATE TABLE IF NOT EXISTS provider_route_credential_snapshots (
    route_id TEXT PRIMARY KEY,
    credential_id TEXT NOT NULL,
    credential_version INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    FOREIGN KEY(route_id)
      REFERENCES provider_route_leases(route_id)
      ON DELETE CASCADE
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
    FOREIGN KEY(route_id)
      REFERENCES provider_route_leases(route_id)
      ON DELETE RESTRICT,
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

  CREATE INDEX IF NOT EXISTS idx_provider_models_status
    ON provider_catalog_models(provider_id, model_id);
  CREATE INDEX IF NOT EXISTS idx_provider_credentials_select
    ON provider_credentials(provider_id, status, expires_at);
  CREATE INDEX IF NOT EXISTS idx_provider_routes_turn
    ON provider_route_leases(session_id, turn_id, created_at);
  CREATE INDEX IF NOT EXISTS idx_provider_attempts_dispatch
    ON provider_dispatch_attempts(dispatch_id, attempt_number);
  CREATE INDEX IF NOT EXISTS idx_provider_events_task
    ON provider_control_events(run_id, task_id, created_at);
`;

const JOURNAL_SQL = `
  CREATE TABLE IF NOT EXISTS provider_schema_migrations (
    transaction_id TEXT PRIMARY KEY,
    source_version INTEGER NOT NULL,
    target_version INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (
      state IN (
        'prepared',
        'applying',
        'verifying',
        'committed',
        'rolling_back',
        'rolled_back'
      )
    ),
    process_generation TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    error_code TEXT,
    error_message TEXT
  );
  CREATE INDEX IF NOT EXISTS idx_provider_schema_migrations_state
    ON provider_schema_migrations(state, updated_at);
`;

export class ProviderSchemaMigrationError extends Error {
  readonly code: string;
  readonly sourceVersion: number;
  readonly targetVersion: number;
  readonly transactionId: string | null;

  constructor(
    code: string,
    message: string,
    options: {
      readonly sourceVersion?: number;
      readonly targetVersion?: number;
      readonly transactionId?: string | null;
      readonly cause?: unknown;
    } = {},
  ) {
    super(message, { cause: options.cause });
    this.name = "ProviderSchemaMigrationError";
    this.code = code;
    this.sourceVersion = options.sourceVersion ?? 0;
    this.targetVersion = options.targetVersion
      ?? PROVIDER_CONTROL_SCHEMA_VERSION;
    this.transactionId = options.transactionId ?? null;
  }
}

function defaultTransactionId(now: number): string {
  return `provider-schema-v1-${now}-${randomUUID()}`;
}

function tableExists(db: RuntimeSqliteDatabase, table: string): boolean {
  const row = db.prepare(
    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
  ).get(table) as NameRow | undefined;
  return row?.name === table;
}

function readSchemaVersion(db: RuntimeSqliteDatabase): number {
  if (!tableExists(db, "provider_control_meta")) return 0;
  const row = db.prepare(
    "SELECT value FROM provider_control_meta WHERE key = 'schema_version'",
  ).get() as ValueRow | undefined;
  if (row === undefined) return 0;
  const version = Number(row.value);
  if (!Number.isSafeInteger(version) || version < 0) {
    throw new ProviderSchemaMigrationError(
      "provider_schema_version_invalid",
      `provider schema version is invalid: ${row.value}`,
    );
  }
  return version;
}

function ensureJournal(db: RuntimeSqliteDatabase): void {
  db.exec(JOURNAL_SQL);
}

function recoverInterruptedTransactions(
  db: RuntimeSqliteDatabase,
  now: number,
): string[] {
  const rows = db.prepare(`
    SELECT transaction_id, state
    FROM provider_schema_migrations
    WHERE state IN ('prepared', 'applying', 'verifying', 'rolling_back')
    ORDER BY started_at, transaction_id
  `).all() as unknown as MigrationRow[];
  if (rows.length === 0) return [];
  db.exec("BEGIN IMMEDIATE");
  try {
    const transition = db.prepare(`
      UPDATE provider_schema_migrations
      SET state = 'rolled_back',
          updated_at = ?,
          error_code = 'provider_schema_process_interrupted',
          error_message = 'previous process ended before atomic schema commit'
      WHERE transaction_id = ?
    `);
    for (const row of rows) transition.run(now, row.transaction_id);
    db.exec("COMMIT");
  } catch (error) {
    db.exec("ROLLBACK");
    throw error;
  }
  return rows.map((row) => row.transaction_id);
}

function verifySchema(db: RuntimeSqliteDatabase): void {
  const missing = REQUIRED_TABLES.filter((table) => !tableExists(db, table));
  if (missing.length > 0) {
    throw new ProviderSchemaMigrationError(
      "provider_schema_tables_missing",
      `provider schema is missing required tables: ${missing.join(", ")}`,
    );
  }
  const invalidColumns: Record<string, string[]> = {};
  for (const [table, required] of Object.entries(REQUIRED_COLUMNS)) {
    const rows = db.prepare(`PRAGMA table_info("${table}")`).all() as
      unknown as Array<{ readonly name: string }>;
    const actual = new Set(rows.map((row) => row.name));
    const absent = required.filter((column) => !actual.has(column));
    if (absent.length > 0) invalidColumns[table] = absent;
  }
  if (Object.keys(invalidColumns).length > 0) {
    throw new ProviderSchemaMigrationError(
      "provider_schema_columns_missing",
      `provider schema has incompatible tables: ${JSON.stringify(invalidColumns)}`,
    );
  }
  const result = db.prepare("PRAGMA quick_check").get() as
    | IntegrityRow
    | undefined;
  const value = result?.quick_check ?? result?.integrity_check;
  if (value !== "ok") {
    throw new ProviderSchemaMigrationError(
      "provider_schema_integrity_failed",
      `provider SQLite integrity check failed: ${String(value)}`,
    );
  }
  const revision = db.prepare(
    "SELECT value FROM provider_control_meta WHERE key = 'catalog_revision'",
  ).get() as ValueRow | undefined;
  if (
    revision === undefined
    || !Number.isSafeInteger(Number(revision.value))
    || Number(revision.value) < 0
  ) {
    throw new ProviderSchemaMigrationError(
      "provider_catalog_revision_invalid",
      "provider catalog revision is missing or invalid",
    );
  }
}

function lastCommittedTransaction(
  db: RuntimeSqliteDatabase,
): string | null {
  const row = db.prepare(`
    SELECT transaction_id, state
    FROM provider_schema_migrations
    WHERE state = 'committed'
    ORDER BY updated_at DESC, transaction_id DESC
    LIMIT 1
  `).get() as MigrationRow | undefined;
  return row?.transaction_id ?? null;
}

function currentHealth(
  db: RuntimeSqliteDatabase,
  recoveredTransactionIds: readonly string[],
): ProviderSchemaMigrationHealth {
  const version = readSchemaVersion(db);
  verifySchema(db);
  return {
    schema: "zyra.provider-schema-migration-health/v1",
    version,
    targetVersion: PROVIDER_CONTROL_SCHEMA_VERSION,
    state: "not_required",
    transactionId: lastCommittedTransaction(db),
    recoveredTransactionIds: [...recoveredTransactionIds],
    integrity: "ok",
  };
}

export function migrateProviderControlPlaneSchema(
  db: RuntimeSqliteDatabase,
  options: ProviderSchemaMigrationOptions = {},
): ProviderSchemaMigrationHealth {
  const clock = options.clock ?? Date.now;
  ensureJournal(db);
  const recoveredTransactionIds = recoverInterruptedTransactions(db, clock());
  const sourceVersion = readSchemaVersion(db);
  if (sourceVersion > PROVIDER_CONTROL_SCHEMA_VERSION) {
    throw new ProviderSchemaMigrationError(
      "provider_schema_future_version",
      `provider schema ${sourceVersion} is newer than supported version ${PROVIDER_CONTROL_SCHEMA_VERSION}`,
      { sourceVersion },
    );
  }
  if (sourceVersion === PROVIDER_CONTROL_SCHEMA_VERSION) {
    return currentHealth(db, recoveredTransactionIds);
  }
  if (sourceVersion !== 0) {
    throw new ProviderSchemaMigrationError(
      "provider_schema_migration_path_missing",
      `no provider schema migration path exists from version ${sourceVersion}`,
      { sourceVersion },
    );
  }

  const now = clock();
  const transactionId = options.transactionId?.()
    ?? defaultTransactionId(now);
  const generation = `${process.pid}:${process.ppid}:${now}`;
  db.prepare(`
    INSERT INTO provider_schema_migrations(
      transaction_id,
      source_version,
      target_version,
      state,
      process_generation,
      started_at,
      updated_at
    ) VALUES (?, ?, ?, 'prepared', ?, ?, ?)
  `).run(
    transactionId,
    sourceVersion,
    PROVIDER_CONTROL_SCHEMA_VERSION,
    generation,
    now,
    now,
  );
  options.faultHook?.("after_prepare", transactionId);

  try {
    db.exec("BEGIN IMMEDIATE");
    db.prepare(`
      UPDATE provider_schema_migrations
      SET state = 'applying', updated_at = ?
      WHERE transaction_id = ? AND state = 'prepared'
    `).run(clock(), transactionId);
    db.exec(SCHEMA_V1_SQL);
    options.faultHook?.("after_apply", transactionId);
    db.prepare(`
      UPDATE provider_schema_migrations
      SET state = 'verifying', updated_at = ?
      WHERE transaction_id = ? AND state = 'applying'
    `).run(clock(), transactionId);
    verifySchema(db);
    db.prepare(`
      INSERT INTO provider_control_meta(key, value)
      VALUES ('schema_version', ?)
      ON CONFLICT(key) DO UPDATE SET value = excluded.value
    `).run(String(PROVIDER_CONTROL_SCHEMA_VERSION));
    options.faultHook?.("before_commit", transactionId);
    db.prepare(`
      UPDATE provider_schema_migrations
      SET state = 'committed', updated_at = ?, error_code = NULL,
          error_message = NULL
      WHERE transaction_id = ? AND state = 'verifying'
    `).run(clock(), transactionId);
    db.exec("COMMIT");
  } catch (error) {
    try {
      db.exec("ROLLBACK");
    } catch {
      // The original migration error remains authoritative.
    }
    db.exec("BEGIN IMMEDIATE");
    try {
      db.prepare(`
        UPDATE provider_schema_migrations
        SET state = 'rolling_back', updated_at = ?,
            error_code = 'provider_schema_migration_failed',
            error_message = ?
        WHERE transaction_id = ? AND state = 'prepared'
      `).run(clock(), String(error), transactionId);
      db.prepare(`
        UPDATE provider_schema_migrations
        SET state = 'rolled_back', updated_at = ?
        WHERE transaction_id = ? AND state = 'rolling_back'
      `).run(clock(), transactionId);
      db.exec("COMMIT");
    } catch (rollbackError) {
      db.exec("ROLLBACK");
      throw new ProviderSchemaMigrationError(
        "provider_schema_rollback_failed",
        "provider schema migration and rollback journal update both failed",
        {
          sourceVersion,
          transactionId,
          cause: rollbackError,
        },
      );
    }
    throw new ProviderSchemaMigrationError(
      "provider_schema_migration_failed",
      "provider schema migration failed and its atomic transaction was rolled back",
      {
        sourceVersion,
        transactionId,
        cause: error,
      },
    );
  }

  verifySchema(db);
  return {
    schema: "zyra.provider-schema-migration-health/v1",
    version: PROVIDER_CONTROL_SCHEMA_VERSION,
    targetVersion: PROVIDER_CONTROL_SCHEMA_VERSION,
    state: "committed",
    transactionId,
    recoveredTransactionIds,
    integrity: "ok",
  };
}
