interface StatementPort {
  run(...parameters: readonly unknown[]): unknown;
  get(...parameters: readonly unknown[]): unknown;
  all(...parameters: readonly unknown[]): readonly unknown[];
}

interface DatabasePort {
  exec(sql: string): unknown;
  prepare(sql: string): StatementPort;
  close(): unknown;
}

interface BunStatementLike {
  run(...parameters: readonly unknown[]): unknown;
  get(...parameters: readonly unknown[]): unknown;
  all(...parameters: readonly unknown[]): readonly unknown[];
  finalize(): void;
}

interface BunDatabaseLike {
  exec(sql: string): unknown;
  query(sql: string): BunStatementLike;
  close(throwOnError?: boolean): unknown;
}

type NodeDatabaseConstructor = new (path: string) => DatabasePort;
type BunDatabaseConstructor = new (
  path: string,
  options?: { readonly create?: boolean; readonly strict?: boolean },
) => BunDatabaseLike;

const runtimeKind = typeof globalThis === "object"
  && "Bun" in globalThis
  ? "bun"
  : "node";

// Keep runtime-specific module names out of static import analysis.  Bun
// versions that predate node:sqlite compatibility otherwise reject the
// Node-only branch before the runtimeKind guard can select bun:sqlite.
const sqliteModuleSpecifier = runtimeKind === "bun"
  ? ["bun", "sqlite"].join(":")
  : ["node", "sqlite"].join(":");
const sqliteModule = await import(sqliteModuleSpecifier);
const runtimeConstructor: NodeDatabaseConstructor | BunDatabaseConstructor = runtimeKind === "bun"
  ? sqliteModule.Database as unknown as BunDatabaseConstructor
  : sqliteModule.DatabaseSync as unknown as NodeDatabaseConstructor;

export type SqliteRuntimeKind = "node" | "bun";

export class RuntimeSqliteStatement implements StatementPort {
  private readonly statement: StatementPort | BunStatementLike;

  constructor(statement: StatementPort | BunStatementLike) {
    this.statement = statement;
  }

  run(...parameters: readonly unknown[]): unknown {
    try {
      return this.statement.run(...parameters);
    } finally {
      this.finalizeBunStatement();
    }
  }

  get(...parameters: readonly unknown[]): unknown {
    try {
      const row = this.statement.get(...parameters);
      // node:sqlite reports a miss as undefined while bun:sqlite reports null.
      // Store callers deliberately use the Node contract, so normalize here.
      return row === null ? undefined : row;
    } finally {
      this.finalizeBunStatement();
    }
  }

  all(...parameters: readonly unknown[]): readonly unknown[] {
    try {
      return this.statement.all(...parameters);
    } finally {
      this.finalizeBunStatement();
    }
  }

  private finalizeBunStatement(): void {
    if ("finalize" in this.statement && typeof this.statement.finalize === "function") {
      this.statement.finalize();
    }
  }
}

/**
 * Synchronous SQLite contract shared by the Node provider service and the Bun
 * CodeWorker process. SQL, transactions, and state custody remain in the
 * ProviderControlPlaneStore; this class only normalizes driver method names.
 */
export class RuntimeSqliteDatabase implements DatabasePort {
  readonly kind: SqliteRuntimeKind;
  readonly path: string;
  private readonly database: DatabasePort | BunDatabaseLike;
  private closed = false;

  constructor(path: string) {
    if (!path.trim()) throw new TypeError("SQLite database path is required");
    this.kind = runtimeKind;
    this.path = path;
    this.database = this.kind === "bun"
      ? new (runtimeConstructor as BunDatabaseConstructor)(path, { create: true, strict: true })
      : new (runtimeConstructor as NodeDatabaseConstructor)(path);
  }

  exec(sql: string): unknown {
    this.assertOpen();
    if (!sql.trim()) throw new TypeError("SQLite exec statement is required");
    if (this.kind === "bun") return (this.database as BunDatabaseLike).exec(sql);
    return (this.database as DatabasePort).exec(sql);
  }

  prepare(sql: string): RuntimeSqliteStatement {
    this.assertOpen();
    if (!sql.trim()) throw new TypeError("SQLite prepared statement is required");
    const statement = this.kind === "bun"
      ? (this.database as BunDatabaseLike).query(sql)
      : (this.database as DatabasePort).prepare(sql);
    return new RuntimeSqliteStatement(statement);
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    if (this.kind === "bun") {
      // Fail loudly if a caller retained an active statement. A silent Bun
      // close can otherwise leave the WAL handle open and make restart/
      // cleanroom teardown look successful while custody is still live.
      (this.database as BunDatabaseLike).close(true);
      return;
    }
    (this.database as DatabasePort).close();
  }

  private assertOpen(): void {
    if (this.closed) throw new Error("SQLite runtime database is closed");
  }
}

export function sqliteRuntimeKind(): SqliteRuntimeKind {
  return runtimeKind;
}
