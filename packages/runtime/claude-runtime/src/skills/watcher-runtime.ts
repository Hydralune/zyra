import { watch, type FSWatcher } from "node:fs";
import { realpath, stat } from "node:fs/promises";
import { relative, resolve } from "node:path";

import type { JsonObject } from "../contracts.ts";
import { cloneJson, deterministicId, digest, monotonicNow } from "../e02/index.ts";
import type { SkillSourceRoot } from "./contracts-v2.ts";

export interface SkillWatchEvent {
  eventId: string;
  sourceId: string;
  rootPath: string;
  path: string;
  relativePath: string;
  kind: "rename" | "change" | "overflow" | "error";
  generation: number;
  sequence: number;
  observedAt: string;
  metadata: JsonObject;
}

export interface SkillWatchBatch {
  batchId: string;
  generation: number;
  events: SkillWatchEvent[];
  sourceIds: string[];
  paths: string[];
  startedAt: string;
  completedAt: string;
  digest: string;
}

export interface SkillWatcherSnapshot {
  version: "zyra.skill-watcher-runtime/v1";
  generation: number;
  sequence: number;
  roots: SkillSourceRoot[];
  pending: SkillWatchEvent[];
  history: SkillWatchBatch[];
  active: boolean;
  digest: string;
  capturedAt: string;
}

export class SkillWatcherRuntime {
  private readonly watchers = new Map<string, FSWatcher>();
  private readonly roots = new Map<string, SkillSourceRoot>();
  private readonly pending: SkillWatchEvent[] = [];
  private readonly history: SkillWatchBatch[] = [];
  private readonly listeners = new Set<(batch: SkillWatchBatch) => void | Promise<void>>();
  private readonly now: () => Date;
  private readonly debounceMs: number;
  private readonly maximumPending: number;
  private readonly maximumHistory: number;
  private generation = 0;
  private sequence = 0;
  private timer: NodeJS.Timeout | null = null;
  private lastTimestamp: string | null = null;

  constructor(options: { now?: () => Date; debounceMs?: number; maximumPending?: number; maximumHistory?: number; snapshot?: SkillWatcherSnapshot | null } = {}) {
    this.now = options.now ?? (() => new Date());
    this.debounceMs = options.debounceMs ?? 100;
    this.maximumPending = options.maximumPending ?? 10_000;
    this.maximumHistory = options.maximumHistory ?? 1_000;
    if (options.snapshot) this.restore(options.snapshot);
  }

  async start(rootsValue: SkillSourceRoot[]): Promise<void> {
    await this.stop();
    this.generation += 1;
    const roots = rootsValue.filter((root) => root.enabled).sort((left, right) => left.sourceId.localeCompare(right.sourceId));
    for (const root of roots) {
      if (this.roots.has(root.sourceId)) throw new Error(`duplicate watched skill source ${root.sourceId}`);
      const realRoot = await realpath(resolve(root.rootPath));
      const stats = await stat(realRoot);
      if (!stats.isDirectory()) throw new Error(`skill watch root ${root.rootPath} is not a directory`);
      const normalized = { ...cloneJson(root), rootPath: realRoot };
      this.roots.set(root.sourceId, normalized);
      const watcher = watch(realRoot, { recursive: root.recursive }, (kind, filename) => {
        this.observe(normalized, kind, filename?.toString() ?? "");
      });
      watcher.on("error", (error) => this.observe(normalized, "error", "", { message: error.message }));
      this.watchers.set(root.sourceId, watcher);
    }
  }

  async stop(): Promise<void> {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    for (const watcher of this.watchers.values()) watcher.close();
    this.watchers.clear();
    this.roots.clear();
    if (this.pending.length) await this.flush();
  }

  observe(root: SkillSourceRoot, kind: string, filename: string, metadata: JsonObject = {}): SkillWatchEvent {
    const rootPath = resolve(root.rootPath);
    const path = filename ? resolve(rootPath, filename) : rootPath;
    const relativePath = relative(rootPath, path).replace(/\\/g, "/");
    if (relativePath.startsWith("../") || relativePath === "..") throw new Error(`skill watch event escapes ${rootPath}`);
    this.sequence += 1;
    const normalizedKind: SkillWatchEvent["kind"] = kind === "rename" || kind === "change" || kind === "error" ? kind : "overflow";
    const base = {
      sourceId: root.sourceId,
      rootPath,
      path,
      relativePath,
      kind: normalizedKind,
      generation: this.generation,
      sequence: this.sequence,
      observedAt: this.timestamp(),
      metadata: cloneJson(metadata),
    };
    const event: SkillWatchEvent = { eventId: deterministicId("skill-watch-event", base, 32), ...base };
    this.pending.push(event);
    if (this.pending.length > this.maximumPending) {
      const removed = this.pending.splice(0, this.pending.length - this.maximumPending);
      this.sequence += 1;
      const overflowBase = {
        sourceId: root.sourceId,
        rootPath,
        path: rootPath,
        relativePath: "",
        kind: "overflow" as const,
        generation: this.generation,
        sequence: this.sequence,
        observedAt: this.timestamp(),
        metadata: { dropped: removed.length },
      };
      this.pending.unshift({ eventId: deterministicId("skill-watch-event", overflowBase, 32), ...overflowBase });
    }
    this.schedule();
    return cloneJson(event);
  }

  async flush(): Promise<SkillWatchBatch | null> {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    if (!this.pending.length) return null;
    const events = deduplicate(this.pending.splice(0));
    const startedAt = events[0]?.observedAt ?? this.timestamp();
    const completedAt = this.timestamp();
    const base = {
      generation: this.generation,
      events,
      sourceIds: [...new Set(events.map((event) => event.sourceId))].sort(),
      paths: [...new Set(events.map((event) => event.path))].sort(),
      startedAt,
      completedAt,
    };
    const batch: SkillWatchBatch = {
      batchId: deterministicId("skill-watch-batch", base, 32),
      ...base,
      digest: digest(base),
    };
    this.history.push(batch);
    while (this.history.length > this.maximumHistory) this.history.shift();
    for (const listener of this.listeners) await listener(cloneJson(batch));
    return cloneJson(batch);
  }

  onBatch(listener: (batch: SkillWatchBatch) => void | Promise<void>): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  batches(): SkillWatchBatch[] {
    return this.history.map(cloneJson);
  }

  snapshot(): SkillWatcherSnapshot {
    const withoutDigest = {
      version: "zyra.skill-watcher-runtime/v1" as const,
      generation: this.generation,
      sequence: this.sequence,
      roots: [...this.roots.values()].sort((left, right) => left.sourceId.localeCompare(right.sourceId)).map(cloneJson),
      pending: this.pending.map(cloneJson),
      history: this.history.map(cloneJson),
      active: this.watchers.size > 0,
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: digest(withoutDigest) };
  }

  restore(snapshot: SkillWatcherSnapshot): void {
    if (snapshot.version !== "zyra.skill-watcher-runtime/v1") throw new Error("unsupported skill watcher snapshot version");
    const { digest: expected, ...withoutDigest } = snapshot;
    if (digest(withoutDigest) !== expected) throw new Error("skill watcher snapshot digest mismatch");
    this.generation = snapshot.generation;
    this.sequence = snapshot.sequence;
    this.pending.splice(0);
    this.history.splice(0);
    for (const event of snapshot.pending) this.pending.push(cloneJson(event));
    for (const batch of snapshot.history.slice(-this.maximumHistory)) this.history.push(cloneJson(batch));
    this.roots.clear();
    this.watchers.clear();
  }

  private schedule(): void {
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => { void this.flush(); }, this.debounceMs);
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function deduplicate(events: SkillWatchEvent[]): SkillWatchEvent[] {
  const output = new Map<string, SkillWatchEvent>();
  for (const event of events) output.set(`${event.sourceId}\0${event.path}\0${event.kind}`, event);
  return [...output.values()].sort((left, right) => left.sequence - right.sequence);
}
