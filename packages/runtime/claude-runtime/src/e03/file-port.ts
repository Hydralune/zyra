import { open, readFile, rename, writeFile } from "node:fs/promises";
import { dirname, join, resolve, sep } from "node:path";
import { mkdirSync } from "node:fs";

import {
  assertSnapshotChecksum,
  digest,
  E03RuntimeError,
  type E03EffectReceipt,
  type E03EffectRequest,
  type E03PhysicalPort,
  type E03RegistrySnapshot,
} from "./contracts.ts";

interface PortFile {
  version: "zyra.e03-file-port/v1";
  revision: number;
  snapshot: E03RegistrySnapshot;
  effects: Record<string, E03EffectReceipt>;
  checksum: string;
}

export class FileE03PhysicalPort implements E03PhysicalPort {
  private queue: Promise<void> = Promise.resolve();
  private readonly root: string;
  private readonly path: string;

  constructor(
    root: string,
    private readonly namespace = "default",
  ) {
    this.root = resolve(root);
    if (!this.root.trim() || this.root === resolve(this.root, sep))
      throw new E03RuntimeError(
        "unsafe_state_root",
        `unsafe E03 state root ${this.root}`,
      );
    mkdirSync(this.root, { recursive: true });
    this.path = join(this.root, `${sanitize(namespace)}.json`);
  }

  async restore(
    runId: string,
    sessionId: string,
  ): Promise<E03RegistrySnapshot | null> {
    const file = await this.read();
    if (!file) return null;
    const tasks = Object.values(file.snapshot.tasks);
    if (
      tasks.some(
        (task) =>
          task.identity.runId !== runId ||
          (task.identity.sessionId !== sessionId &&
            task.identity.parentSessionId !== sessionId),
      )
    ) {
      throw new E03RuntimeError(
        "file_port_authority",
        "durable file contains a task outside requested run/session authority",
      );
    }
    return structuredClone(file.snapshot);
  }

  async effect(request: E03EffectRequest): Promise<E03EffectReceipt> {
    return this.serial(async () => {
      const file = await this.read();
      const prior =
        file?.effects[request.effectId] ??
        file?.snapshot.effects[request.effectId];
      if (prior) {
        if (
          prior.taskId !== request.taskId ||
          prior.requestId !== request.requestId ||
          prior.leaseId !== request.leaseId
        )
          throw new E03RuntimeError(
            "effect_idempotency_conflict",
            "effect id was reused with different custody",
          );
        return structuredClone({ ...prior, replayed: true });
      }
      const payload = {
        receiptId: `receipt-${digest(request.effectId).slice(0, 32)}`,
        effectId: request.effectId,
        requestId: request.requestId,
        taskId: request.taskId,
        leaseId: request.leaseId,
        expectedRevision: request.expectedRevision,
        accepted: true,
        replayed: false,
        result: {
          operation: request.operation,
          effect_kind: request.effectKind,
          persisted: request.effectKind === "persist",
          physical_port: "typescript.file-test-port",
        },
        artifacts: [],
        error: "",
        completedAt: new Date().toISOString(),
      };
      const receipt = { ...payload, digest: digest(payload) };
      if (file) {
        await this.write({
          ...file,
          effects: { ...file.effects, [request.effectId]: receipt },
          snapshot: resealSnapshot({
            ...file.snapshot,
            effects: { ...file.snapshot.effects, [request.effectId]: receipt },
          }),
        });
      }
      return receipt;
    });
  }

  async compareAndSwap(
    expectedRevision: number,
    snapshot: E03RegistrySnapshot,
  ): Promise<{
    accepted: boolean;
    revision: number;
    replayed: boolean;
    error: string;
  }> {
    return this.serial(async () => {
      assertSnapshotChecksum(snapshot);
      const file = await this.read();
      const actual = file?.revision ?? 0;
      if (actual !== expectedRevision) {
        if (file?.snapshot.checksum === snapshot.checksum)
          return {
            accepted: true,
            revision: actual,
            replayed: true,
            error: "",
          };
        return {
          accepted: false,
          revision: actual,
          replayed: false,
          error: "stale_revision",
        };
      }
      const effects = file?.effects ?? {};
      await this.write(
        sealFile({
          version: "zyra.e03-file-port/v1",
          revision: snapshot.revision,
          snapshot,
          effects,
        }),
      );
      return {
        accepted: true,
        revision: snapshot.revision,
        replayed: false,
        error: "",
      };
    });
  }

  private async read(): Promise<PortFile | null> {
    let text: string;
    try {
      text = await readFile(this.path, "utf8");
    } catch (error: any) {
      if (error?.code === "ENOENT") return null;
      throw error;
    }
    const value = JSON.parse(text) as PortFile;
    const { checksum, ...payload } = value;
    if (
      value.version !== "zyra.e03-file-port/v1" ||
      digest(payload) !== checksum
    )
      throw new E03RuntimeError(
        "file_port_checksum",
        `E03 state file ${this.path} is corrupt`,
      );
    assertSnapshotChecksum(value.snapshot);
    return value;
  }

  private async write(value: PortFile): Promise<void> {
    const sealed = sealFile(value);
    const temporary = `${this.path}.${process.pid}.${Date.now()}.tmp`;
    await writeFile(temporary, `${JSON.stringify(sealed)}\n`, {
      encoding: "utf8",
      flag: "wx",
    });
    const handle = await open(temporary, "r+");
    try {
      await handle.sync();
    } finally {
      await handle.close();
    }
    await rename(temporary, this.path);
  }

  private serial<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.queue.then(operation, operation);
    this.queue = result.then(
      () => undefined,
      () => undefined,
    );
    return result;
  }
}

function sealFile(value: Omit<PortFile, "checksum"> | PortFile): PortFile {
  const { checksum: _checksum, ...payload } = value as PortFile;
  return { ...payload, checksum: digest(payload) };
}

function sanitize(value: string): string {
  const normalized = value
    .replace(/[^A-Za-z0-9._-]+/g, "-")
    .replace(/^[-.]+|[-.]+$/g, "");
  if (!normalized)
    throw new E03RuntimeError(
      "invalid_state_namespace",
      "E03 state namespace is empty",
    );
  return normalized.slice(0, 200);
}

function resealSnapshot(snapshot: E03RegistrySnapshot): E03RegistrySnapshot {
  const { checksum: _checksum, ...payload } = snapshot;
  return { ...payload, checksum: digest(payload) };
}
