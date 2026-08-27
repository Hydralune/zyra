import { existsSync } from "node:fs";
import { resolve } from "node:path";

import type { CurrentSkillAuthority } from "@zyra/skill-memory-runtime";
import type { JsonObject, JsonValue, ToolSpecContract } from "../contracts.ts";
import { canonicalize, cloneJson, deterministicId, digest, monotonicNow } from "../e02/index.ts";
import { SkillContextRuntime, type SkillContextSnapshot, type SkillParentContext } from "./context-runtime.ts";
import type { SkillDescriptor, SkillInvocationIdentity, SkillInvocationRequest, SkillInvocationResult, SkillRegistrySnapshot, SkillReloadScan, SkillSourceRoot, SkillToolScope } from "./contracts-v2.ts";
import { SkillFrontmatterRuntime } from "./frontmatter-runtime.ts";
import { SkillInvocationJournal, type SkillInvocationJournalSnapshot } from "./invocation-journal.ts";
import { SkillInvocationRuntime, type SkillExecutor } from "./invocation-runtime.ts";
import { SkillRegistryRuntime } from "./registry-runtime.ts";
import { SkillReloadRuntime } from "./reload-runtime.ts";
import { SkillResourceRuntime } from "./resource-runtime.ts";
import { TypeScriptSkillRuntime } from "./runtime.ts";
import {
  SkillRootRuntime,
  type SkillRootFailure,
  type SkillRootRevision,
  type SkillRootSnapshot,
} from "./root-runtime.ts";
import { SkillSearchRuntime, type SkillSearchSnapshot } from "./search-runtime.ts";
import { SkillSourceRuntime } from "./source-runtime.ts";
import { SkillWatcherRuntime, type SkillWatcherSnapshot } from "./watcher-runtime.ts";

export interface SkillCoordinatorIdentity {
  runId: string;
  taskId: string;
  sessionId: string;
  sessionRevision: number;
  workerRequestId: string;
  toolCallId: string;
}

export interface SkillCoordinatorExecution {
  summary: string;
  output: JsonObject;
  metadata: Record<string, string>;
  contextDelta?: JsonObject;
  toolScopeDelta?: JsonObject;
}

export interface SkillCoordinatorSnapshot {
  version: "zyra.skill-coordinator/v2";
  workspaceRoot: string;
  epoch: number;
  opened: boolean;
  restoredBeforeBootstrap: boolean;
  roots: SkillSourceRoot[];
  rootRuntime: SkillRootSnapshot;
  registry: SkillRegistrySnapshot;
  context: SkillContextSnapshot;
  journal: SkillInvocationJournalSnapshot;
  search: SkillSearchSnapshot;
  watcher: SkillWatcherSnapshot;
  reloadFailures: SkillReloadFailure[];
  reloadHistory: SkillReloadReceipt[];
  digest: string;
  capturedAt: string;
}

export interface SkillReloadFailure extends JsonObject {
  failureId: string;
  code: string;
  message: string;
  source: string;
  fatal: boolean;
  occurredAt: string;
  metadata: JsonObject;
}

export interface SkillReloadReceipt extends JsonObject {
  receiptId: string;
  source: string;
  rootRevision: number;
  registryRevisionBefore: number;
  registryRevisionAfter: number;
  added: string[];
  updated: string[];
  removed: string[];
  errorCount: number;
  committedAt: string;
  metadata: JsonObject;
  digest: string;
}

export interface SkillPluginRootUpdate {
  pluginId: string;
  pluginRevision: number;
  manifestDigest: string;
  roots: SkillSourceRoot[];
  enabled: boolean;
  metadata?: JsonObject;
}

export interface SkillCoordinatorOptions {
  workspaceRoot: string;
  epoch: number;
  roots: SkillSourceRoot[];
  executor: SkillExecutor;
  now?: () => Date;
  watch?: boolean;
  snapshot?: SkillCoordinatorSnapshot | null;
}

const defaultParentScope: SkillToolScope = {
  allowed: ["*"],
  denied: [],
  namespaces: ["*"],
  mcpServers: ["*"],
  readOnly: false,
  inheritParent: false,
  maximumCalls: null,
  maximumParallel: 16,
  requireApproval: [],
};

export class SkillCoordinator {
  readonly registry: SkillRegistryRuntime;
  readonly sources: SkillSourceRuntime;
  readonly frontmatter: SkillFrontmatterRuntime;
  readonly resources: SkillResourceRuntime;
  readonly rootRuntime: SkillRootRuntime;
  readonly reload: SkillReloadRuntime;
  readonly context: SkillContextRuntime;
  readonly journal: SkillInvocationJournal;
  readonly search: SkillSearchRuntime;
  readonly watcher: SkillWatcherRuntime;
  readonly invocation: SkillInvocationRuntime;
  private readonly workspaceRoot: string;
  private readonly epoch: number;
  private readonly watchEnabled: boolean;
  private readonly now: () => Date;
  private readonly externalExecutor: SkillExecutor;
  private readonly journalByInvocation = new Map<string, { journalId: string; transitionId: string }>();
  private readonly reloadFailures = new Map<string, SkillReloadFailure>();
  private readonly reloadHistory = new Map<string, SkillReloadReceipt>();
  private opened = false;
  private restoredBeforeBootstrap = false;
  private reloadPromise: Promise<JsonObject | null> | null = null;
  private lastTimestamp: string | null = null;

  constructor(options: SkillCoordinatorOptions) {
    if (!options.workspaceRoot) throw new Error("skill coordinator workspace root is required");
    if (!Number.isSafeInteger(options.epoch) || options.epoch < 0) throw new Error("skill coordinator epoch is invalid");
    this.workspaceRoot = resolve(options.workspaceRoot);
    this.epoch = options.epoch;
    this.watchEnabled = options.watch !== false;
    this.now = options.now ?? (() => new Date());
    this.externalExecutor = options.executor;
    const snapshot = options.snapshot ?? null;
    if (snapshot && resolve(snapshot.workspaceRoot) !== this.workspaceRoot) throw new Error("skill coordinator restore workspace mismatch");
    if (snapshot && snapshot.version !== "zyra.skill-coordinator/v2") throw new Error(`unsupported skill coordinator snapshot ${snapshot.version}`);
    this.rootRuntime = new SkillRootRuntime({
      workspaceRoot: this.workspaceRoot,
      roots: options.roots,
      now: this.now,
      snapshot: snapshot?.rootRuntime ?? null,
    });
    this.registry = new SkillRegistryRuntime({ now: this.now, snapshot: snapshot?.registry ?? null });
    this.sources = new SkillSourceRuntime({ workspaceRoot: this.workspaceRoot, now: this.now });
    this.frontmatter = new SkillFrontmatterRuntime({ now: this.now });
    this.resources = new SkillResourceRuntime({ workspaceRoot: this.workspaceRoot, now: this.now });
    this.reload = new SkillReloadRuntime({ sources: this.sources, parser: this.frontmatter, registry: this.registry, now: this.now });
    this.context = new SkillContextRuntime({ now: this.now, snapshot: snapshot?.context ?? null });
    this.journal = new SkillInvocationJournal({ epoch: this.epoch, now: this.now, snapshot: snapshot?.journal ?? null });
    this.search = new SkillSearchRuntime({ now: this.now, snapshot: snapshot?.search ?? null });
    this.watcher = new SkillWatcherRuntime({ now: this.now, snapshot: snapshot?.watcher ?? null });
    this.invocation = new SkillInvocationRuntime({
      registry: this.registry,
      resources: this.resources,
      executor: (executionContext) => this.executeWithJournal(executionContext.plan.invocationId, executionContext.plan, executionContext),
      now: this.now,
    });
    this.restoredBeforeBootstrap = Boolean(snapshot);
    if (snapshot) {
      for (const failure of snapshot.reloadFailures) this.reloadFailures.set(failure.failureId, cloneJson(failure));
      for (const receipt of snapshot.reloadHistory) this.reloadHistory.set(receipt.receiptId, cloneJson(receipt));
    }
    this.watcher.onBatch(async (batch) => {
      try {
        await this.reloadNow({ source: "filesystem_watch", watch_batch_id: batch.batchId });
      } catch (error) {
        this.recordReloadFailure(error, "filesystem_watch", false, {
          watch_batch_id: batch.batchId,
          watch_generation: batch.generation,
          paths: batch.paths,
        });
      }
    });
  }

  async open(): Promise<void> {
    TypeScriptSkillRuntime.assertSourceRuntimeEnabled();
    if (this.opened) return;
    if (!this.restoredBeforeBootstrap || this.registry.revision === 0) await this.reloadNow({ source: "bootstrap" });
    else await this.refreshSearch();
    if (this.watchEnabled) await this.startWatcher();
    this.opened = true;
  }

  owns(toolName: string): boolean {
    return ["list_skills", "search_skills", "skill", "read_skill_resource", "reload_skills"].includes(toolName);
  }

  toolSpecs(): ToolSpecContract[] {
    return [
      toolSpec("list_skills", "List TypeScript-owned Markdown skills", {}, "read"),
      toolSpec("search_skills", "Search TypeScript-owned Markdown skills", { query: { type: "string" }, tags: { type: "array", items: { type: "string" } }, limit: { type: "integer" } }, "read"),
      toolSpec("skill", "Invoke a TypeScript-owned Markdown skill with context and tool-scope budgets", { skill: { type: "string" }, arguments: { type: "object" }, resources: { type: "array", items: { type: "string" } } }, "execute", ["skill"]),
      toolSpec("read_skill_resource", "Read a declared skill resource through the TypeScript resource boundary", { skill: { type: "string" }, resources: { type: "array", items: { type: "string" } } }, "read", ["skill"]),
      toolSpec("reload_skills", "Atomically rescan and commit disk skill changes", {}, "execute"),
    ];
  }

  /**
   * Resolves the authority that is valid now. 06C calls this after compact or
   * resume; the method intentionally returns digests and policy evidence, not
   * the executable body, so an outcome snapshot cannot become a second loader.
   */
  authority(
    skillNameValue: string,
    parentToolScope: SkillToolScope = defaultParentScope,
  ): CurrentSkillAuthority {
    TypeScriptSkillRuntime.assertSourceRuntimeEnabled();
    const skillName = skillNameValue.trim();
    const resolvedAt = this.timestamp();
    try {
      const resolution = this.registry.resolve(skillName);
      const effective = this.invocation.applyToolScope(parentToolScope, resolution.descriptor.toolScope);
      const policy = {
        decisionId: deterministicId("skill-current-authority", {
          skill_id: resolution.skillId,
          registry_revision: resolution.revision,
          registry_revision_id: resolution.revisionId,
          descriptor_digest: resolution.descriptor.descriptorDigest,
          effective_tool_scope: effective,
        }, 32),
        effect: "allow" as const,
        policyRevision: `${resolution.revision}:${resolution.revisionId}`,
        policyDigest: digest({ descriptor: resolution.descriptor.toolScope, parent: parentToolScope, effective }),
        requestedTools: [...resolution.descriptor.toolScope.allowed],
        effectiveTools: [...effective.allowed],
        deniedTools: [...effective.denied],
        approvalId: null,
      };
      return {
        protocol: "zyra.skill-coordinator-authority/v1",
        skillId: resolution.skillId,
        skillName: resolution.descriptor.name,
        availability: "available",
        registryRevision: resolution.revision,
        registryRevisionId: resolution.revisionId,
        descriptorDigest: resolution.descriptor.descriptorDigest,
        bodyDigest: resolution.descriptor.bodyDigest,
        sourceRevision: resolution.revisionId,
        trust: ["bundled", "managed"].includes(resolution.descriptor.source.sourceKind) ? "internal" : "verified",
        policy,
        resolvedAt,
        resolutionError: null,
        metadata: {
          canonical_owner: "03C SkillCoordinator",
          executable_body_present: false,
          resolution_kind: resolution.resolutionKind,
        },
      };
    } catch (error) {
      const normalized = skillName.toLowerCase();
      const descriptor = this.registry.list().find((candidate) =>
        candidate.skillId.toLowerCase() === normalized
        || candidate.name.toLowerCase() === normalized
        || candidate.aliases.some((alias) => alias.toLowerCase() === normalized)
      );
      const availability = descriptor?.availability === "missing"
        ? "removed"
        : descriptor?.availability ?? "removed";
      return {
        protocol: "zyra.skill-coordinator-authority/v1",
        skillId: descriptor?.skillId ?? skillName,
        skillName: descriptor?.name ?? skillName,
        availability,
        registryRevision: this.registry.revision,
        registryRevisionId: this.registry.headRevisionId ?? "registry-empty",
        descriptorDigest: descriptor?.descriptorDigest ?? null,
        bodyDigest: descriptor?.bodyDigest ?? null,
        sourceRevision: this.registry.headRevisionId,
        trust: "unknown",
        policy: null,
        resolvedAt,
        resolutionError: error instanceof Error ? error.message : String(error),
        metadata: {
          canonical_owner: "03C SkillCoordinator",
          executable_body_present: false,
          current_resolution_failed_closed: true,
        },
      };
    }
  }

  async execute(
    toolName: string,
    argumentsValue: JsonObject,
    identityValue: Partial<SkillCoordinatorIdentity> = {},
    parentContext: SkillParentContext = emptyParentContext(),
    parentToolScope: SkillToolScope = defaultParentScope,
    signal?: AbortSignal,
  ): Promise<SkillCoordinatorExecution> {
    TypeScriptSkillRuntime.assertSourceRuntimeEnabled();
    if (!this.opened) throw new Error("skill coordinator is not open");
    if (toolName === "list_skills") {
      const skills = this.registry.list({ availableOnly: true }).map((skill) => ({
        id: skill.skillId,
        name: skill.name,
        display_name: skill.displayName,
        description: skill.description,
        version: skill.version,
        source: skill.source.sourceKind,
        plugin_id: skill.source.pluginId,
        tags: skill.tags,
        aliases: skill.aliases,
        execution_mode: skill.execution.mode,
        descriptor_digest: skill.descriptorDigest,
      }));
      return result(`Listed ${skills.length} skills`, { skills: canonicalize(skills), revision: this.registry.revision });
    }
    if (toolName === "search_skills") {
      const search = this.search.search({
        text: requiredString(argumentsValue, "query"),
        tags: stringArray(argumentsValue.tags),
        maximumResults: integer(argumentsValue.limit, 50),
        includeUnavailable: false,
        metadata: { tool_call: true },
      });
      return result(`Found ${search.hits.length} skills`, canonicalize(search) as JsonObject);
    }
    if (toolName === "reload_skills") {
      await this.reloadNow({ source: "tool", identity: canonicalize(identityValue) as JsonObject });
      return result(`Reloaded ${this.registry.list().length} skills`, { registry_revision: this.registry.revision, head_revision_id: this.registry.headRevisionId });
    }
    const skillName = requiredString(argumentsValue, "skill");
    const resolution = this.registry.resolve(skillName);
    if (toolName === "read_skill_resource") {
      const contents = await this.resources.load(resolution.descriptor, stringArray(argumentsValue.resources));
      return result(`Loaded ${contents.length} resources for ${skillName}`, { resources: canonicalize(contents), skill_id: resolution.skillId, registry_revision: resolution.revision });
    }
    if (toolName !== "skill") throw new Error(`skill coordinator does not own ${toolName}`);
    const identity = this.identity(identityValue, skillName, argumentsValue);
    const selectedResources = stringArray(argumentsValue.resources);
    const resources = await this.resources.load(resolution.descriptor, selectedResources.length ? selectedResources : undefined);
    const composition = this.context.compose({
      descriptor: resolution.descriptor,
      parent: parentContext,
      resources,
      arguments: objectValue(argumentsValue.arguments),
      metadata: { registry_revision: resolution.revision },
    });
    const request: SkillInvocationRequest = {
      identity,
      skillName,
      registryRevision: resolution.revision,
      arguments: objectValue(argumentsValue.arguments),
      parentContext: composition.rendered,
      parentToolScope: cloneJson(parentToolScope),
      workspaceRoot: this.workspaceRoot,
      metadata: {
        composition_id: composition.compositionId,
        composition_digest: composition.digest,
        selected_resources: selectedResources,
      },
    };
    const currentAuthority = this.authority(skillName, parentToolScope);
    try {
      const invocation = await this.invocation.invoke(request, signal);
      const binding = this.journalByInvocation.get(invocation.invocationId);
      let sourceRecordDigest = digest(invocation);
      if (binding) {
        const committed = this.journal.commit(binding.journalId, binding.transitionId, invocation);
        this.journal.acknowledge(binding.journalId, binding.transitionId, committed.resultDigest!);
        sourceRecordDigest = committed.resultDigest!;
      }
      const outcomeReference = {
        protocol: "zyra.skill-coordinator-outcome/v1",
        invocation: canonicalize(invocation),
        provenance: {
          skill_id: resolution.skillId,
          skill_name: resolution.descriptor.name,
          registry_revision: resolution.revision,
          descriptor_digest: resolution.descriptor.descriptorDigest,
          body_digest: resolution.descriptor.bodyDigest,
          resource_digests: Object.fromEntries(
            resources
              .map((resource) => [resource.path, resource.digest] as const)
              .sort(([left], [right]) => left.localeCompare(right)),
          ),
          source_revision: resolution.revisionId,
        },
        policy: {
          decision_id: deterministicId("skill-tool-scope-decision", {
            invocation_id: invocation.invocationId,
            descriptor_digest: resolution.descriptor.descriptorDigest,
            effective_tool_scope: this.invocation.applyToolScope(parentToolScope, resolution.descriptor.toolScope),
          }, 32),
          effect: "allow",
          policy_revision: `${resolution.revision}:${resolution.revisionId}`,
          policy_digest: digest(resolution.descriptor.toolScope),
          requested_tools: [...resolution.descriptor.toolScope.allowed],
          effective_tools: [...this.invocation.applyToolScope(parentToolScope, resolution.descriptor.toolScope).allowed],
          denied_tools: [...this.invocation.applyToolScope(parentToolScope, resolution.descriptor.toolScope).denied],
          approval_id: null,
        },
        composition: {
          composition_id: composition.compositionId,
          composition_digest: composition.digest,
        },
        source_record_digest: sourceRecordDigest,
        reuse_conditions: [],
        metadata: {
          canonical_skill_owner: "03C SkillCoordinator",
          outcome_reference_only: true,
          executable_skill_cache: false,
          registry_resolution_required_before_reuse: true,
          current_authority_digest: digest(currentAuthority),
        },
      };
      return {
        summary: `Skill ${resolution.descriptor.name} ${invocation.status}`,
        output: {
          invocation: canonicalize(invocation),
          composition_id: composition.compositionId,
          registry_revision: resolution.revision,
          outcome_reference: canonicalize(outcomeReference),
          current_authority: canonicalize(currentAuthority),
        },
        contextDelta: {
          active_skill: resolution.skillId,
          skill_context: composition.rendered,
          composition_digest: composition.digest,
        },
        toolScopeDelta: canonicalize(this.invocation.applyToolScope(parentToolScope, resolution.descriptor.toolScope)) as JsonObject,
        metadata: metadata({
          skill_id: resolution.skillId,
          skill_revision: String(resolution.revision),
          invocation_id: invocation.invocationId,
          invocation_status: invocation.status,
          composition_id: composition.compositionId,
        }),
      };
    } catch (error) {
      const binding = this.journalByInvocation.get(identity.invocationId);
      if (binding) this.journal.fail(binding.journalId, binding.transitionId, error, signal?.aborted ?? false);
      throw error;
    }
  }

  async replacePluginRoots(input: SkillPluginRootUpdate): Promise<SkillRootRevision> {
    TypeScriptSkillRuntime.assertSourceRuntimeEnabled();
    return this.mutateRoots(
      () => this.rootRuntime.replacePlugin(input, this.rootRuntime.revision),
      {
        source: "plugin_reload",
        plugin_id: input.pluginId,
        plugin_revision: input.pluginRevision,
        plugin_manifest_digest: input.manifestDigest,
      },
    );
  }

  async removePluginRoots(pluginId: string, pluginRevision: number): Promise<SkillRootRevision> {
    TypeScriptSkillRuntime.assertSourceRuntimeEnabled();
    return this.mutateRoots(
      () => this.rootRuntime.removePlugin(pluginId, this.rootRuntime.revision, {
        plugin_revision: pluginRevision,
      }),
      {
        source: "plugin_remove",
        plugin_id: pluginId,
        plugin_revision: pluginRevision,
      },
    );
  }

  async replaceRoots(roots: SkillSourceRoot[], metadataValue: JsonObject = {}): Promise<SkillRootRevision> {
    TypeScriptSkillRuntime.assertSourceRuntimeEnabled();
    return this.mutateRoots(
      () => this.rootRuntime.replaceAll(roots, this.rootRuntime.revision, metadataValue),
      { source: "root_replace", ...metadataValue },
    );
  }

  snapshot(): SkillCoordinatorSnapshot {
    const withoutDigest = {
      version: "zyra.skill-coordinator/v2" as const,
      workspaceRoot: this.workspaceRoot,
      epoch: this.epoch,
      opened: this.opened,
      restoredBeforeBootstrap: this.restoredBeforeBootstrap,
      roots: this.rootRuntime.list(),
      rootRuntime: this.rootRuntime.snapshot(),
      registry: this.registry.snapshot(),
      context: this.context.snapshot(),
      journal: this.journal.snapshot(),
      search: this.search.snapshot(),
      watcher: this.watcher.snapshot(),
      reloadFailures: [...this.reloadFailures.values()]
        .sort((left, right) => left.occurredAt.localeCompare(right.occurredAt))
        .map(cloneJson),
      reloadHistory: [...this.reloadHistory.values()]
        .sort((left, right) => left.committedAt.localeCompare(right.committedAt))
        .map(cloneJson),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: digest(withoutDigest) };
  }

  health(): JsonObject {
    return {
      canonical_owner: "typescript",
      opened: this.opened,
      restored_before_bootstrap: this.restoredBeforeBootstrap,
      registry_revision: this.registry.revision,
      skill_count: this.registry.list().length,
      root_revision: this.rootRuntime.revision,
      active_root_count: this.rootRuntime.list({ enabledOnly: true, existingOnly: true }).length,
      root_failure_count: this.rootRuntime.listFailures().length,
      reload_failure_count: this.reloadFailures.size,
      pending_invocations: this.journal.pending().length,
      watcher_active: this.watcher.snapshot().active,
      source_custody: "claude-code-best:loadSkillsFromSkillsDir+executeForkedSkill",
      python_skill_fallback: false,
      snapshot_digest: this.snapshot().digest,
    };
  }

  async close(): Promise<void> {
    await this.watcher.stop();
    this.opened = false;
  }

  async reloadSkillsWithAdmission(
    identityValue: SkillCoordinatorIdentity,
    admit: (
      descriptors: readonly SkillDescriptor[],
      scan: SkillReloadScan,
    ) => JsonObject | Promise<JsonObject>,
  ): Promise<{
    execution: SkillCoordinatorExecution;
    admission: JsonObject;
  }> {
    const admission = await this.reloadNow(
      {
        source: "tool",
        identity: canonicalize(identityValue) as JsonObject,
        staged_owner_admission: true,
      },
      admit,
    );
    if (!admission)
      throw new Error("staged skill reload did not produce an owner admission");
    return {
      execution: result(
        `Reloaded ${this.registry.list().length} skills`,
        {
          registry_revision: this.registry.revision,
          head_revision_id: this.registry.headRevisionId,
        },
      ),
      admission,
    };
  }

  private async reloadNow(
    metadataValue: JsonObject,
    admit?: (
      descriptors: readonly SkillDescriptor[],
      scan: SkillReloadScan,
    ) => JsonObject | Promise<JsonObject>,
  ): Promise<JsonObject | null> {
    TypeScriptSkillRuntime.assertSourceRuntimeEnabled();
    if (this.reloadPromise) {
      if (!admit) return this.reloadPromise;
      await this.reloadPromise;
      return this.reloadNow(metadataValue, admit);
    }
    const registryRevisionBefore = this.registry.revision;
    const source = typeof metadataValue.source === "string" ? metadataValue.source : "unknown";
    this.reloadPromise = (async () => {
        const loaded = await this.reload.loadSkillsFromSkillsDir({
          roots: this.rootRuntime.list({ enabledOnly: true, existingOnly: true }),
          expectedRevision: registryRevisionBefore,
          metadata: metadataValue,
          commit: false,
        });
        const { scan } = loaded;
        let admission: JsonObject | null = null;
        try {
          admission = admit
            ? cloneJson(await admit(scan.descriptors.map(cloneJson), cloneJson(scan)))
            : null;
        } catch (error) {
          this.reload.discard(scan.scanId);
          throw error;
        }
        const revision = this.reload.commit(
          scan.scanId,
          registryRevisionBefore,
          metadataValue,
        );
        for (const skillId of revision.removed) this.resources.clear(skillId);
        await this.refreshSearch();
        const committedAt = this.timestamp();
        const receiptBase = {
          source,
          rootRevision: this.rootRuntime.revision,
          registryRevisionBefore,
          registryRevisionAfter: revision.revision,
          added: revision.added,
          updated: revision.updated,
          removed: revision.removed,
          errorCount: scan.errors.length,
          committedAt,
          metadata: canonicalize({
            ...metadataValue,
            source_custody: "claude-code-best:loadSkillsFromSkillsDir",
            source_owner: "TypeScriptSkillRuntime",
          }) as JsonObject,
        };
        const receipt: SkillReloadReceipt = {
          receiptId: deterministicId("skill-reload-receipt", receiptBase, 40),
          ...receiptBase,
          digest: digest(receiptBase),
        };
        this.reloadHistory.set(receipt.receiptId, receipt);
        while (this.reloadHistory.size > 2_000) {
          const oldest = [...this.reloadHistory.values()].sort((left, right) => left.committedAt.localeCompare(right.committedAt))[0];
          if (!oldest) break;
          this.reloadHistory.delete(oldest.receiptId);
        }
        return admission;
    })();
    try {
      return await this.reloadPromise;
    } catch (error) {
      this.recordReloadFailure(error, typeof metadataValue.source === "string" ? metadataValue.source : "unknown", true, metadataValue);
      throw error;
    } finally {
      this.reloadPromise = null;
    }
  }

  private async mutateRoots(
    mutate: () => SkillRootRevision,
    metadataValue: JsonObject,
  ): Promise<SkillRootRevision> {
    const before = this.rootRuntime.snapshot();
    const revision = mutate();
    try {
      await this.reloadNow({
        ...metadataValue,
        root_revision_id: revision.revisionId,
        root_revision: revision.revisionAfter,
      });
      if (this.watchEnabled && this.opened) await this.startWatcher();
      return revision;
    } catch (error) {
      this.rootRuntime.restore(before);
      if (this.watchEnabled && this.opened) {
        try {
          await this.startWatcher();
        } catch (watchError) {
          this.recordReloadFailure(watchError, "watcher_rollback", true, metadataValue);
        }
      }
      throw error;
    }
  }

  private async startWatcher(): Promise<void> {
    const roots = this.rootRuntime.list({ enabledOnly: true, existingOnly: true });
    await this.watcher.start(roots);
  }

  private recordReloadFailure(
    error: unknown,
    source: string,
    fatal: boolean,
    metadataValue: JsonObject,
  ): SkillReloadFailure {
    const occurredAt = this.timestamp();
    const code = errorCode(error);
    const message = error instanceof Error ? error.message : String(error);
    const base = {
      code,
      message,
      source,
      fatal,
      occurredAt,
      metadata: canonicalize(metadataValue) as JsonObject,
    };
    const failure: SkillReloadFailure = {
      failureId: deterministicId("skill-reload-failure", base, 40),
      ...base,
    };
    this.reloadFailures.set(failure.failureId, failure);
    while (this.reloadFailures.size > 2_000) {
      const oldest = [...this.reloadFailures.values()].sort((left, right) => left.occurredAt.localeCompare(right.occurredAt))[0];
      if (!oldest) break;
      this.reloadFailures.delete(oldest.failureId);
    }
    return cloneJson(failure);
  }

  private async refreshSearch(): Promise<void> {
    this.search.rebuild(this.registry.snapshot());
  }

  private async executeWithJournal(
    invocationId: string,
    plan: Parameters<SkillExecutor>[0]["plan"],
    executionContext: Parameters<SkillExecutor>[0],
  ): ReturnType<SkillExecutor> {
    const prepared = this.journal.prepare(plan);
    this.journalByInvocation.set(invocationId, { journalId: prepared.journalId, transitionId: prepared.transitionId });
    if (prepared.status === "committed" || prepared.status === "acknowledged") {
      if (!prepared.result) throw new Error(`committed skill journal ${prepared.journalId} lacks result`);
      return {
        output: prepared.result.output,
        artifacts: prepared.result.artifacts,
        inputTokens: prepared.result.inputTokens,
        outputTokens: prepared.result.outputTokens,
        costMicros: prepared.result.costMicros,
        metadata: { replayed_from_journal: true, journal_id: prepared.journalId },
      };
    }
    if (prepared.status === "indeterminate" && prepared.effectId) throw new Error(`skill invocation ${invocationId} requires effect reconciliation before retry`);
    this.journal.begin(prepared.journalId, prepared.transitionId);
    try {
      const output = await this.externalExecutor(executionContext);
      this.journal.recordEffect(prepared.journalId, prepared.transitionId, "skill_executor_result", {
        output_digest: digest(output.output),
        artifact_digests: (output.artifacts ?? []).map(digest),
        input_tokens: output.inputTokens ?? 0,
        output_tokens: output.outputTokens ?? 0,
        cost_micros: output.costMicros ?? 0,
      });
      return output;
    } catch (error) {
      this.journal.fail(prepared.journalId, prepared.transitionId, error, executionContext.signal?.aborted ?? false);
      throw error;
    }
  }

  private identity(value: Partial<SkillCoordinatorIdentity>, skillName: string, argumentsValue: JsonObject): SkillInvocationIdentity {
    const base = {
      runId: value.runId || "skill-run",
      taskId: value.taskId || "skill-task",
      sessionId: value.sessionId || "skill-session",
      sessionRevision: value.sessionRevision ?? 0,
      workerRequestId: value.workerRequestId || "skill-worker",
      toolCallId: value.toolCallId || deterministicId("skill-tool-call", { skill_name: skillName, arguments_digest: digest(argumentsValue) }, 32),
    };
    return {
      ...base,
      invocationId: deterministicId("skill-invocation", {
        run_id: base.runId,
        task_id: base.taskId,
        session_id: base.sessionId,
        session_revision: base.sessionRevision,
        worker_request_id: base.workerRequestId,
        tool_call_id: base.toolCallId,
        skill_name: skillName,
        registry_revision: this.registry.revision,
        arguments_digest: digest(objectValue(argumentsValue.arguments)),
      }, 32),
    };
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function toolSpec(name: string, purpose: string, properties: JsonObject, accessMode: string, required: string[] = []): ToolSpecContract {
  return {
    name,
    purpose,
    source: "typescript-skill",
    input_schema: { type: "object", properties, ...(required.length ? { required } : {}) },
    output_schema: { type: "object" },
    metadata: { access_mode: accessMode, canonical_runtime_owner: "typescript" },
    execution_provenance: { namespace: "skill", server_id: "", version: "2", source: "zyra-e02-skill-coordinator" },
  };
}

function result(summary: string, output: JsonObject): SkillCoordinatorExecution {
  return { summary, output, metadata: metadata({}) };
}

function metadata(values: Record<string, string>): Record<string, string> {
  return {
    canonical_runtime_owner: "typescript",
    capability_owner: "typescript-skill",
    source_custody: "claude-code-best:loadSkillsFromSkillsDir+executeForkedSkill",
    python_skill_fallback: "false",
    ...values,
  };
}

function requiredString(value: JsonObject, key: string): string {
  const item = value[key];
  if (typeof item !== "string" || !item.trim()) throw new Error(`skill argument ${key} is required`);
  return item;
}

function stringArray(value: JsonValue | undefined): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string");
}

function integer(value: JsonValue | undefined, fallback: number): number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0 ? value : fallback;
}

function objectValue(value: JsonValue | undefined): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? cloneJson(value as JsonObject) : {};
}

function errorCode(error: unknown): string {
  if (error && typeof error === "object") {
    const code = (error as { code?: unknown }).code;
    if (typeof code === "string" && code) return code;
  }
  return error instanceof Error && error.name ? error.name : "skill_reload_failed";
}

function emptyParentContext(): SkillParentContext {
  return { system: [], conversation: [], memory: [], workspaceInstructions: [], mcpInstructions: [], variables: {}, metadata: {} };
}
