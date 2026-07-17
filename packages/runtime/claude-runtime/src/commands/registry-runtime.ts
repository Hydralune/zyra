import type { JsonObject } from "../contracts.ts";
import { cloneJson, deterministicId, digest, monotonicNow, normalizeName } from "../e02/index.ts";
import type {
  CommandDescriptor,
  CommandRegistrySnapshot,
  CommandRevision,
} from "./contracts.ts";

export interface CommandRegistrationResult {
  revision: CommandRevision;
  active: CommandDescriptor[];
  conflicts: JsonObject[];
}

export class CommandRegistryRuntime {
  private readonly revisions = new Map<number, CommandRevision>();
  private readonly active = new Map<string, CommandDescriptor>();
  private readonly nameIndex = new Map<string, string>();
  private readonly aliasIndex = new Map<string, string>();
  private readonly pins = new Map<number, number>();
  private readonly now: () => Date;
  private readonly maximumRevisions: number;
  private revision = 0;
  private headRevisionId: string | null = null;
  private lastTimestamp: string | null = null;

  constructor(options: { now?: () => Date; maximumRevisions?: number; snapshot?: CommandRegistrySnapshot | null } = {}) {
    this.now = options.now ?? (() => new Date());
    this.maximumRevisions = options.maximumRevisions ?? 100;
    if (options.snapshot) this.restore(options.snapshot);
  }

  get currentRevision(): number {
    return this.revision;
  }

  register(
    descriptorsValue: CommandDescriptor[],
    expectedRevision = this.revision,
    metadata: JsonObject = {},
  ): CommandRegistrationResult {
    if (expectedRevision !== this.revision) throw registryError("command_revision_conflict", `command registry revision ${expectedRevision} does not match ${this.revision}`);
    const descriptors = descriptorsValue.map(validateDescriptor);
    const groups = new Map<string, CommandDescriptor[]>();
    for (const descriptor of descriptors) {
      const key = normalizeLookup(descriptor.name);
      const group = groups.get(key) ?? [];
      group.push(descriptor);
      groups.set(key, group);
    }
    const active: CommandDescriptor[] = [];
    const shadowed: string[] = [];
    const conflicts: JsonObject[] = [];
    for (const [name, group] of groups) {
      group.sort(comparePriority);
      const winner = group[0];
      active.push(winner);
      for (const loser of group.slice(1)) {
        shadowed.push(loser.commandId);
        conflicts.push({ type: "name", name, winner: winner.commandId, loser: loser.commandId });
      }
    }
    const aliasOwners = new Map<string, CommandDescriptor>();
    for (const descriptor of active.sort((left, right) => left.commandId.localeCompare(right.commandId))) {
      descriptor.aliases = descriptor.aliases.filter((alias) => {
        const key = normalizeLookup(alias);
        const commandOwner = active.find((candidate) => normalizeLookup(candidate.name) === key);
        if (commandOwner && commandOwner.commandId !== descriptor.commandId) {
          conflicts.push({ type: "alias_to_name", alias, winner: commandOwner.commandId, loser: descriptor.commandId });
          return false;
        }
        const aliasOwner = aliasOwners.get(key);
        if (aliasOwner) {
          const winner = comparePriority(aliasOwner, descriptor) <= 0 ? aliasOwner : descriptor;
          const loser = winner === aliasOwner ? descriptor : aliasOwner;
          conflicts.push({ type: "alias", alias, winner: winner.commandId, loser: loser.commandId });
          if (winner === descriptor) {
            aliasOwner.aliases = aliasOwner.aliases.filter((candidate) => normalizeLookup(candidate) !== key);
            aliasOwners.set(key, descriptor);
          }
          return winner === descriptor;
        }
        aliasOwners.set(key, descriptor);
        return true;
      });
    }
    const next = new Map(active.map((descriptor) => [descriptor.commandId, descriptor]));
    const added: string[] = [];
    const updated: string[] = [];
    const removed: string[] = [];
    for (const id of new Set([...this.active.keys(), ...next.keys()])) {
      const before = this.active.get(id);
      const after = next.get(id);
      if (!before && after) added.push(id);
      else if (before && !after) removed.push(id);
      else if (before && after && before.descriptorDigest !== after.descriptorDigest) updated.push(id);
    }
    this.revision += 1;
    const committedAt = this.timestamp();
    const base = {
      revision: this.revision,
      parentRevisionId: this.headRevisionId,
      commands: [...next.values()].sort((left, right) => left.name.localeCompare(right.name)).map(cloneJson),
      added: added.sort(),
      updated: updated.sort(),
      removed: removed.sort(),
      shadowed: shadowed.sort(),
      committedAt,
      metadata: { ...cloneJson(metadata), conflicts },
    };
    const revisionId = deterministicId("command-registry-revision", base, 32);
    const revision: CommandRevision = { revisionId, ...base, digest: digest({ revisionId, ...base }) };
    this.headRevisionId = revisionId;
    this.revisions.set(this.revision, revision);
    this.rebuild(revision.commands);
    this.trim();
    return { revision: cloneJson(revision), active: this.list(), conflicts };
  }

  resolve(name: string, revision = this.revision): CommandDescriptor {
    const record = this.revisions.get(revision);
    if (!record) throw registryError("command_revision_not_found", `command registry revision ${revision} was not found`);
    const key = normalizeLookup(name.replace(/^\//, ""));
    const descriptor = record.commands.find((command) => normalizeLookup(command.name) === key || command.aliases.some((alias) => normalizeLookup(alias) === key));
    if (!descriptor) throw registryError("command_not_found", `command ${name} was not found at revision ${revision}`);
    if (!descriptor.enabled) throw registryError("command_disabled", `command ${descriptor.name} is disabled`);
    return cloneJson(descriptor);
  }

  pin(revision = this.revision): () => void {
    if (!this.revisions.has(revision)) throw registryError("command_revision_not_found", `command registry revision ${revision} was not found`);
    this.pins.set(revision, (this.pins.get(revision) ?? 0) + 1);
    let released = false;
    return () => {
      if (released) return;
      released = true;
      const count = this.pins.get(revision) ?? 0;
      if (count <= 1) this.pins.delete(revision);
      else this.pins.set(revision, count - 1);
      this.trim();
    };
  }

  list(options: { revision?: number; hidden?: boolean; category?: string; sourceId?: string } = {}): CommandDescriptor[] {
    const record = this.revisions.get(options.revision ?? this.revision);
    if (!record) return [];
    return record.commands
      .filter((command) => options.hidden === undefined || command.hidden === options.hidden)
      .filter((command) => !options.category || command.category === options.category)
      .filter((command) => !options.sourceId || command.sourceId === options.sourceId)
      .sort((left, right) => left.category.localeCompare(right.category) || left.name.localeCompare(right.name))
      .map(cloneJson);
  }

  snapshot(): CommandRegistrySnapshot {
    const withoutDigest = {
      version: "zyra.command-registry/v2" as const,
      revision: this.revision,
      headRevisionId: this.headRevisionId,
      revisions: [...this.revisions.values()].sort((left, right) => left.revision - right.revision).map(cloneJson),
      active: [...this.active.values()].sort((left, right) => left.name.localeCompare(right.name)).map(cloneJson),
      aliasIndex: Object.fromEntries([...this.aliasIndex.entries()].sort(([left], [right]) => left.localeCompare(right))),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: digest(withoutDigest) };
  }

  restore(snapshot: CommandRegistrySnapshot): void {
    if (snapshot.version !== "zyra.command-registry/v2") throw registryError("unsupported_command_snapshot", "unsupported command registry snapshot version");
    const { digest: expected, ...withoutDigest } = snapshot;
    if (digest(withoutDigest) !== expected) throw registryError("command_snapshot_digest_mismatch", "command registry snapshot digest mismatch");
    this.revisions.clear();
    this.revision = snapshot.revision;
    this.headRevisionId = snapshot.headRevisionId;
    for (const revision of snapshot.revisions) this.revisions.set(revision.revision, cloneJson(revision));
    this.rebuild(snapshot.active.map(validateDescriptor));
  }

  private rebuild(commands: CommandDescriptor[]): void {
    this.active.clear();
    this.nameIndex.clear();
    this.aliasIndex.clear();
    for (const command of commands) {
      this.active.set(command.commandId, cloneJson(command));
      this.nameIndex.set(normalizeLookup(command.name), command.commandId);
      for (const alias of command.aliases) this.aliasIndex.set(normalizeLookup(alias), command.commandId);
    }
  }

  private trim(): void {
    const candidates = [...this.revisions.keys()].sort((left, right) => left - right);
    while (this.revisions.size > this.maximumRevisions && candidates.length) {
      const revision = candidates.shift()!;
      if (revision === this.revision || this.pins.has(revision)) continue;
      this.revisions.delete(revision);
    }
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function validateDescriptor(value: CommandDescriptor): CommandDescriptor {
  const descriptor = cloneJson(value);
  if (!descriptor.commandId || !descriptor.name || !descriptor.descriptorDigest) throw registryError("invalid_command_descriptor", "command descriptor identity is incomplete");
  return descriptor;
}

function comparePriority(left: CommandDescriptor, right: CommandDescriptor): number {
  return right.sourcePriority - left.sourcePriority || left.commandId.localeCompare(right.commandId);
}

function normalizeLookup(value: string): string {
  return normalizeName(value, 512).normalize("NFKC").toLowerCase();
}

function registryError(code: string, message: string): Error {
  const error = new Error(message);
  error.name = "CommandRegistryError";
  Object.assign(error, { code });
  return error;
}
