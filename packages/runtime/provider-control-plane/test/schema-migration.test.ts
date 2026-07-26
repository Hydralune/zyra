import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test, { type TestContext } from "node:test";

import {
  migrateProviderControlPlaneSchema,
  ProviderControlPlaneStore,
  ProviderSchemaMigrationError,
  RuntimeSqliteDatabase,
} from "../src/index.ts";

function temporaryDatabase(
  t: TestContext,
): { readonly directory: string; readonly path: string } {
  const directory = mkdtempSync(join(tmpdir(), "zyra-provider-schema-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  return { directory, path: join(directory, "provider.sqlite3") };
}

test("legacy provider state is adopted without losing catalog data", (t) => {
  const target = temporaryDatabase(t);
  const legacy = new RuntimeSqliteDatabase(target.path);
  legacy.exec(`
    CREATE TABLE provider_control_meta (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    );
    INSERT INTO provider_control_meta(key, value)
    VALUES ('catalog_revision', '7');
    CREATE TABLE provider_catalog_providers (
      provider_id TEXT PRIMARY KEY,
      catalog_revision INTEGER NOT NULL,
      json TEXT NOT NULL,
      checksum TEXT NOT NULL
    );
    INSERT INTO provider_catalog_providers(
      provider_id, catalog_revision, json, checksum
    ) VALUES ('legacy', 7, '{"providerId":"legacy"}', 'legacy-checksum');
  `);
  legacy.close();

  const store = new ProviderControlPlaneStore(target.path);
  const health = store.health();
  assert.equal(health.schemaVersion, 1);
  assert.equal(health.schemaMigrationState, "committed");
  assert.equal(health.catalogRevision, 7);
  assert.equal(health.providerCount, 1);
  assert.deepEqual(store.listProviders(), [{ providerId: "legacy" }]);
  store.close();

  const restarted = new ProviderControlPlaneStore(target.path);
  const restartHealth = restarted.health();
  assert.equal(restartHealth.schemaVersion, 1);
  assert.equal(restartHealth.schemaMigrationState, "not_required");
  assert.equal(restartHealth.catalogRevision, 7);
  assert.equal(restartHealth.providerCount, 1);
  restarted.close();
});

test("provider schema DDL failure is atomically rolled back", (t) => {
  const target = temporaryDatabase(t);
  const database = new RuntimeSqliteDatabase(target.path);
  try {
    assert.throws(
      () => migrateProviderControlPlaneSchema(database, {
        transactionId: () => "forced-rollback",
        faultHook: (phase) => {
          if (phase === "after_apply") throw new Error("forced verifier fault");
        },
      }),
      (error: unknown) => {
        assert(error instanceof ProviderSchemaMigrationError);
        assert.equal(error.code, "provider_schema_migration_failed");
        return true;
      },
    );
    const state = database.prepare(`
      SELECT state
      FROM provider_schema_migrations
      WHERE transaction_id = 'forced-rollback'
    `).get() as { state: string };
    assert.equal(state.state, "rolled_back");
    const metadataTable = database.prepare(`
      SELECT name
      FROM sqlite_master
      WHERE type = 'table' AND name = 'provider_control_meta'
    `).get();
    assert.equal(metadataTable, undefined);

    const health = migrateProviderControlPlaneSchema(database, {
      transactionId: () => "retry-after-rollback",
    });
    assert.equal(health.state, "committed");
    assert.equal(health.version, 1);
  } finally {
    database.close();
  }
});

test("prepared migration is recovered after process restart", (t) => {
  const target = temporaryDatabase(t);
  const first = new RuntimeSqliteDatabase(target.path);
  assert.throws(
    () => migrateProviderControlPlaneSchema(first, {
      transactionId: () => "interrupted-prepare",
      faultHook: (phase) => {
        if (phase === "after_prepare") throw new Error("process terminated");
      },
    }),
    /process terminated/,
  );
  first.close();

  const restarted = new RuntimeSqliteDatabase(target.path);
  try {
    const health = migrateProviderControlPlaneSchema(restarted, {
      transactionId: () => "restart-commit",
    });
    assert.deepEqual(
      health.recoveredTransactionIds,
      ["interrupted-prepare"],
    );
    assert.equal(health.state, "committed");
    const interrupted = restarted.prepare(`
      SELECT state, error_code
      FROM provider_schema_migrations
      WHERE transaction_id = 'interrupted-prepare'
    `).get() as { state: string; error_code: string };
    assert.equal(interrupted.state, "rolled_back");
    assert.equal(
      interrupted.error_code,
      "provider_schema_process_interrupted",
    );
  } finally {
    restarted.close();
  }
});

test("future provider schemas fail closed without downgrade", (t) => {
  const target = temporaryDatabase(t);
  const database = new RuntimeSqliteDatabase(target.path);
  try {
    database.exec(`
      CREATE TABLE provider_control_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
      );
      INSERT INTO provider_control_meta(key, value)
      VALUES ('catalog_revision', '0');
      INSERT INTO provider_control_meta(key, value)
      VALUES ('schema_version', '99');
    `);
    assert.throws(
      () => migrateProviderControlPlaneSchema(database),
      (error: unknown) => {
        assert(error instanceof ProviderSchemaMigrationError);
        assert.equal(error.code, "provider_schema_future_version");
        assert.equal(error.sourceVersion, 99);
        return true;
      },
    );
    const version = database.prepare(`
      SELECT value FROM provider_control_meta WHERE key = 'schema_version'
    `).get() as { value: string };
    assert.equal(version.value, "99");
  } finally {
    database.close();
  }
});
