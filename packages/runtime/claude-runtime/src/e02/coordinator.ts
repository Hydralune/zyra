import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import {
  isAbsolute,
  relative,
  resolve,
  sep,
} from "node:path";

import {
  McpRuntimeCoordinator,
  type McpCoordinatorSnapshot,
  type McpRuntimeCoordinatorOptions,
} from "../../../../integrations/claude-mcp/src/index.ts";
import {
  asBoolean,
  asObject,
  asString,
  type JsonObject,
  type JsonValue,
  type RuntimeRunInput,
  type ToolSpecContract,
} from "../contracts.ts";
import {
  TypeScriptAgentRuntime,
  type AgentCapabilityResult,
  type AgentExecutionContext,
} from "../agents/index.ts";
import {
  CommandCoordinator,
  CommandDescriptorRuntime,
  commandPermissionDecision,
  type CommandCoordinatorSnapshot,
  type CommandDescriptor,
  type CommandInvocationRequest,
  type CommandPermissionDecision,
  type CommandSourceRoot,
} from "../commands/index.ts";
import {
  PermissionCoordinator,
  type PermissionApprovalTransport,
  type PermissionCoordinatorSnapshot,
  type PermissionEnforcementResult,
  type PermissionEvaluationInput,
} from "../permission/index.ts";
import {
  PluginCoordinator,
  type PluginCoordinatorSnapshot,
  type PluginHookExecutorInput,
  type PluginHookExecutorOutput,
  type PluginManifest,
  type PluginRuntimeRecord,
  type PluginSourceRoot,
} from "../plugins/index.ts";
import {
  SkillCoordinator,
  TypeScriptSkillRuntime,
  type SkillCoordinatorSnapshot,
  type SkillDescriptor,
  type SkillExecutor,
  type SkillParentContext,
  type SkillReloadScan,
  type SkillSourceRoot,
} from "../skills/index.ts";
import {
  canonicalize,
  cloneJson,
  constantTimeDigestEquals,
  deterministicId,
  digest,
  mergeJson,
  monotonicNow,
  normalizeIdentifier,
} from "./canonical.ts";
import {
  E02CustodyRuntime,
  type E02CustodyDomain,
  type E02CustodyOwner,
  type E02CustodySnapshot,
} from "./custody-runtime.ts";
import {
  E02CheckpointBundleRuntime,
  type E02CheckpointPhase,
  type E02CheckpointBundleSnapshot,
} from "./checkpoint-bundle-runtime.ts";
import {
  E02ControlPlaneRuntime,
  type E02ControlPhase,
  type E02ControlOperationDescriptor,
  type E02ControlPlaneSnapshot,
  type E02ControlRequest,
  type E02ControlRisk,
} from "./control-plane-runtime.ts";
import type {
  CommitReceipt,
  E02CapabilityResult,
  E02Domain,
  E02EventEnvelope,
  E02EventSnapshot,
  E02HealthReport,
  E02RuntimeIdentity,
  PermissionApprovalResponse,
  PermissionMode,
  TransitionJournalSnapshot,
  TransitionRecord,
} from "./contracts.ts";
import { E02EventLog } from "./events.ts";
import {
  CapabilityExecutionLedger,
  type CapabilityExecutionLedgerSnapshot,
  type CapabilityExecutionRecord,
  type CapabilityExecutionPhase,
  type CapabilityPermit,
} from "./execution-ledger.ts";
import {
  E02HostPortRuntime,
  type E02HostPortSnapshot,
} from "./host-port-runtime.ts";
import {
  E02RouteRuntime,
  type E02RouteDomain,
  type E02RouteSnapshot,
} from "./route-runtime.ts";
import {
  E02ProjectionRuntime,
  type E02ProjectionDomain,
  type E02ProjectionSnapshot,
} from "./projection-runtime.ts";
import {
  E02RecoveryRuntime,
  type E02RecoveryRuntimeSnapshot,
} from "./recovery-runtime.ts";
import {
  DurableTransitionJournal,
  TransitionConflictError,
} from "./transition-journal.ts";

export interface E02CoordinatorPorts {
  emitEvent?: (event: E02EventEnvelope) => Promise<void>;
  checkpoint?: (snapshot: E02CapabilityCoordinatorSnapshot) => Promise<void>;
  approvalTransport?: PermissionApprovalTransport;
  skillExecutor?: SkillExecutor;
  controlCommand?: (
    descriptor: CommandDescriptor,
    request: CommandInvocationRequest,
    argumentsValue: JsonObject,
    signal?: AbortSignal,
  ) => Promise<JsonValue>;
  builtinCommand?: (
    descriptor: CommandDescriptor,
    request: CommandInvocationRequest,
    argumentsValue: JsonObject,
    signal?: AbortSignal,
  ) => Promise<JsonValue>;
  mcp?: Partial<Omit<McpRuntimeCoordinatorOptions,
    "sessionId" | "workspaceRoot" | "interactive" | "sealedAutonomous" | "epoch" | "snapshot"
  >>;
  now?: () => Date;
}

export interface E02AuthorizationInput extends PermissionEvaluationInput {
  awaitApprovalDelivery?: boolean;
  issueExecutionPermit?: boolean;
}

export interface E02AuthorizationResult {
  enforcement: PermissionEnforcementResult;
  permit: CapabilityPermit | null;
  permitId: string | null;
  finalArguments: JsonObject;
  stateDigest: string;
}

export interface E02ExecutionContext {
  runId?: string;
  taskId?: string;
  sessionId?: string;
  sessionRevision?: number;
  workerRequestId?: string;
  toolCallId: string;
  namespace?: string;
  serverId?: string;
  commandName?: string;
  resourceUri?: string;
  operation?: string;
  permitId?: string | null;
  metadata?: JsonObject;
  signal?: AbortSignal;
  agentContext?: AgentExecutionContext;
}

export interface E02ExecutionReceipt {
  executionId: string;
  transitionId: string;
  owner: string;
  result: E02CapabilityResult;
  commit: CommitReceipt;
  replayed: boolean;
  permitId: string;
  snapshotHash: string;
}

export interface E02RecoveryRecord {
  recoveryId: string;
  transitionId: string;
  executionId: string | null;
  domain: E02Domain;
  toolName: string;
  priorPhase: string;
  disposition:
    | "committed_from_effect_receipt"
    | "cancelled_before_effect"
    | "recovery_required"
    | "manually_reconciled"
    | "manually_cancelled";
  reason: string;
  sourceEpoch: number;
  targetEpoch: number;
  recoveredAt: string;
  metadata: JsonObject;
  digest: string;
}

export interface E02IntegrationAudit {
  auditId: string;
  pluginId: string;
  pluginRevision: number;
  operation: string;
  status: "prepared" | "committed" | "rolled_back" | "failed";
  affectedSkills: string[];
  affectedCommands: string[];
  affectedMcpServers: string[];
  affectedHooks: string[];
  startedAt: string;
  completedAt: string | null;
  error: JsonObject | null;
  metadata: JsonObject;
  digest: string;
}

export interface E02CapabilityCoordinatorSnapshot {
  version: "zyra.e02-runtime/v1";
  runtime: E02RuntimeIdentity;
  workspaceRoot: string;
  opened: boolean;
  restoredBeforeBootstrap: boolean;
  closing: boolean;
  permission: PermissionCoordinatorSnapshot;
  mcp: McpCoordinatorSnapshot;
  skills: SkillCoordinatorSnapshot;
  plugins: PluginCoordinatorSnapshot;
  commands: CommandCoordinatorSnapshot;
  agents: JsonObject;
  journal: TransitionJournalSnapshot;
  events: E02EventSnapshot;
  executionLedger: CapabilityExecutionLedgerSnapshot;
  hostPorts: E02HostPortSnapshot;
  custody: E02CustodySnapshot;
  routes: E02RouteSnapshot;
  checkpointBundles: E02CheckpointBundleSnapshot;
  controlPlane: E02ControlPlaneSnapshot;
  projections: E02ProjectionSnapshot;
  recoveryRuntime: E02RecoveryRuntimeSnapshot;
  recoveries: E02RecoveryRecord[];
  integrationAudit: E02IntegrationAudit[];
  pluginIntegrationRevisions: Array<[string, number]>;
  pluginMcpLayerRevisions: Array<[string, number]>;
  pluginAgents: Array<[string, JsonObject[]]>;
  executionSequence: number;
  snapshotSequence: number;
  lastCheckpointHash: string;
  snapshotHash: string;
  capturedAt: string;
}

export interface E02ManualReconciliation {
  transitionId: string;
  outcome: "confirm_effect" | "confirm_no_effect";
  result?: E02CapabilityResult;
  providerReceiptId?: string | null;
  reason: string;
  actor: string;
  metadata?: JsonObject;
}

const E02_CONTROL_TOOLS = new Set([
  "e02_health",
  "e02_recovery_status",
  "e02_execution_history",
  "e02_control",
  "e02_control_catalog",
  "e02_control_history",
  "e02_projection",
  "e02_recovery_plan",
]);

export class E02CapabilityCoordinator {
  readonly runtime: E02RuntimeIdentity;
  readonly workspaceRoot: string;
  readonly permission: PermissionCoordinator;
  readonly mcp: McpRuntimeCoordinator;
  readonly skills: SkillCoordinator;
  readonly plugins: PluginCoordinator;
  readonly commands: CommandCoordinator;
  readonly agents: TypeScriptAgentRuntime;
  readonly journal: DurableTransitionJournal;
  readonly events: E02EventLog;
  readonly executionLedger: CapabilityExecutionLedger;
  readonly hostPorts: E02HostPortRuntime;
  readonly custody: E02CustodyRuntime;
  readonly routes: E02RouteRuntime;
  readonly checkpointBundles: E02CheckpointBundleRuntime;
  readonly controlPlane: E02ControlPlaneRuntime;
  readonly projections: E02ProjectionRuntime;
  readonly recoveryRuntime: E02RecoveryRuntime;
  private readonly input: RuntimeRunInput;
  private readonly ports: E02CoordinatorPorts;
  private readonly now: () => Date;
  private readonly restoreDisposition: E02RestoreDisposition;
  private readonly recoveries = new Map<string, E02RecoveryRecord>();
  private readonly integrationAudit = new Map<string, E02IntegrationAudit>();
  private readonly pluginIntegrationRevisions = new Map<string, number>();
  private readonly pluginMcpLayerRevisions = new Map<string, number>();
  private readonly pluginAgents = new Map<string, JsonObject[]>();
  private readonly inFlight = new Map<string, Promise<E02ExecutionReceipt>>();
  private readonly activeAgentContexts = new Map<string, AgentExecutionContext>();
  private readonly activeCommandAuthorizations = new Map<string, {
    commandName: string;
    operation: string;
    decisionId: string;
    permitId: string;
    outerArgumentsDigest: string;
  }>();
  private opened = false;
  private restoredBeforeBootstrap = false;
  private closing = false;
  private executionSequence = 0;
  private snapshotSequence = 0;
  private lastCheckpointHash = "";
  private lastTimestamp: string | null = null;

  private constructor(
    input: RuntimeRunInput,
    ports: E02CoordinatorPorts,
    restore: E02RestoreSelection,
  ) {
    const snapshot = restore.snapshot;
    this.input = input;
    this.ports = ports;
    this.now = ports.now ?? (() => new Date());
    this.restoreDisposition = restore.disposition;
    this.workspaceRoot = workspaceRoot(input);
    this.runtime = runtimeIdentity(input, snapshot);
    if (snapshot) this.validateSnapshot(snapshot);
    this.journal = new DurableTransitionJournal(this.runtime, () => this.timestamp());
    this.events = new E02EventLog(this.runtime, () => this.timestamp());
    if (snapshot) {
      this.journal.restore(snapshot.journal, this.runtime.epoch);
      this.events.restore(snapshot.events);
    }
    this.executionLedger = new CapabilityExecutionLedger({
      runtime: this.runtime,
      now: this.now,
      snapshot: snapshot?.executionLedger ?? null,
    });
    this.hostPorts = new E02HostPortRuntime({
      runtime: this.runtime,
      now: this.now,
      snapshot: snapshot?.hostPorts ?? null,
    });
    this.custody = new E02CustodyRuntime({
      runtime: this.runtime,
      now: this.now,
      snapshot: snapshot?.custody ?? null,
    });
    if (ports.emitEvent) {
      this.custody.registerPhysicalPort("runtime-event-transport", "event-transport", {
        protocol: "zyra.claude-runtime.v1",
      });
    }
    if (ports.checkpoint) {
      this.custody.registerPhysicalPort("runtime-checkpoint-store", "checkpoint-store", {
        protocol: "zyra.e02-runtime/v1",
      });
    }
    if (ports.approvalTransport) {
      this.custody.registerPhysicalPort("permission-approval-transport", "approval-transport", {
        canonical_continuation_owner: "typescript",
      });
    }
    const policy = asObject(input.config.permissionPolicy);
    const runtimeConstraints = asObject(input.config.runtimeConstraints);
    const mode = permissionMode(policy.mode);
    // The policy carries its own interactive/headless custody and the caller is
    // the only party that knows whether an approver exists.  Deriving the flag
    // from ``mode === "sealed"`` alone left an autonomous dispatch marked
    // interactive, so ASK stayed ASK, suspended the tool call and ended the
    // task instead of converting to a deterministic denial the recovery
    // planner can replan around.
    const interactive = !(
      mode === "sealed"
      || asBoolean(runtimeConstraints.sealedAutonomous)
      || asBoolean(runtimeConstraints.sealed_autonomous)
      || policy.interactive === false
      || policy.headless === true
    );
    this.permission = new PermissionCoordinator({
      runtime: this.runtime,
      workspaceRoot: this.workspaceRoot,
      mode: {
        mode,
        revision: nonNegativeInteger(policy.mode_revision ?? policy.modeRevision, 0),
        interactive,
        headless: !interactive,
        sealedAutonomous: mode === "sealed",
        bypassAvailable: mode === "bypassPermissions"
          && (policy.bypass_available === true || policy.bypassAvailable === true),
        autoClassifierEnabled: policy.auto_classifier_enabled === true
          || policy.autoClassifierEnabled === true,
      },
      rules: permissionRules(policy),
      askTtlMs: boundedSecondsAsMilliseconds(
        runtimeConstraints.permission_approval_ttl_seconds,
        15 * 60_000,
        1_000,
        86_400_000,
      ),
      approvalTransport: ports.approvalTransport,
      interactive,
      now: this.now,
      clock: () => this.timestamp(),
      snapshot: snapshot?.permission ?? null,
    });
    this.mcp = new McpRuntimeCoordinator({
      sessionId: input.sessionId,
      workspaceRoot: this.workspaceRoot,
      interactive,
      sealedAutonomous: mode === "sealed",
      epoch: this.runtime.epoch,
      now: this.now,
      ...(ports.mcp ?? {}),
      snapshot: snapshot?.mcp ?? null,
    });
    this.agents = new TypeScriptAgentRuntime(input);
    const skillExecutor = ports.skillExecutor ?? this.executeSkillPlan.bind(this);
    this.skills = new SkillCoordinator({
      workspaceRoot: this.workspaceRoot,
      epoch: this.runtime.epoch,
      roots: skillRoots(input, this.workspaceRoot),
      executor: skillExecutor,
      now: this.now,
      watch: watcherEnabled(input, "skill"),
      snapshot: snapshot?.skills ?? null,
    });
    this.commands = new CommandCoordinator({
      workspaceRoot: this.workspaceRoot,
      roots: commandRoots(input, this.workspaceRoot),
      builtins: defaultE02CommandDescriptors(),
      permission: this.evaluateCommandPermission.bind(this),
      dispatchers: {
        skill: this.dispatchSkillCommand.bind(this),
        mcpPrompt: this.dispatchMcpPromptCommand.bind(this),
        plugin: this.dispatchPluginCommand.bind(this),
        control: this.dispatchControlCommand.bind(this),
        builtin: this.dispatchBuiltinCommand.bind(this),
      },
      now: this.now,
      snapshot: snapshot?.commands ?? null,
    });
    this.plugins = new PluginCoordinator({
      workspaceRoot: this.workspaceRoot,
      roots: pluginRoots(input, this.workspaceRoot),
      integration: {
        addSkills: this.integratePluginSkills.bind(this),
        addCommands: this.integratePluginCommands.bind(this),
        addHooks: this.integratePluginHooks.bind(this),
        addAgents: this.integratePluginAgents.bind(this),
        addMcpServers: this.integratePluginMcp.bind(this),
        remove: this.removePluginIntegration.bind(this),
      },
      hookExecutor: this.executePluginHook.bind(this),
      invokeCommand: this.invokePluginCommand.bind(this),
      watch: watcherEnabled(input, "plugin"),
      now: this.now,
      snapshot: snapshot?.plugins ?? null,
    });
    this.routes = new E02RouteRuntime({
      runtime: this.runtime,
      now: this.now,
      snapshot: snapshot?.routes ?? null,
    });
    this.checkpointBundles = new E02CheckpointBundleRuntime({
      runtime: this.runtime,
      now: this.now,
      snapshot: snapshot?.checkpointBundles ?? null,
    });
    this.controlPlane = new E02ControlPlaneRuntime({
      runtime: this.runtime,
      now: this.now,
      handler: this.executeControlPlaneOperation.bind(this),
      snapshot: snapshot?.controlPlane ?? null,
    });
    this.projections = new E02ProjectionRuntime({
      runtime: this.runtime,
      now: this.now,
      snapshot: snapshot?.projections ?? null,
    });
    this.recoveryRuntime = new E02RecoveryRuntime({
      runtime: this.runtime,
      now: this.now,
      snapshot: snapshot?.recoveryRuntime ?? null,
    });
    this.registerPluginPermissionBridge();
    if (snapshot) {
      this.restoredBeforeBootstrap = true;
      this.executionSequence = snapshot.executionSequence;
      this.snapshotSequence = snapshot.snapshotSequence;
      this.lastCheckpointHash = snapshot.lastCheckpointHash;
      for (const recovery of snapshot.recoveries) this.recoveries.set(recovery.recoveryId, cloneJson(recovery));
      for (const audit of snapshot.integrationAudit) this.integrationAudit.set(audit.auditId, cloneJson(audit));
      for (const [pluginId, revision] of snapshot.pluginIntegrationRevisions) this.pluginIntegrationRevisions.set(pluginId, revision);
      for (const [pluginId, revision] of snapshot.pluginMcpLayerRevisions) this.pluginMcpLayerRevisions.set(pluginId, revision);
      for (const [pluginId, agents] of snapshot.pluginAgents) this.pluginAgents.set(pluginId, agents.map(cloneJson));
    }
  }

  static async open(
    input: RuntimeRunInput,
    ports: E02CoordinatorPorts = {},
  ): Promise<E02CapabilityCoordinator> {
    if (process.env.ZYRA_DISABLE_E02_TYPESCRIPT_RUNTIME === "1") {
      throw coordinatorError(
        "e02_typescript_runtime_disabled",
        "The canonical TypeScript E02 capability runtime is disabled; no Python fallback is permitted",
      );
    }
    const restore = selectRestoredE02Snapshot(input.restoredState, input);
    const coordinator = new E02CapabilityCoordinator(input, ports, restore);
    await coordinator.openRuntime();
    return coordinator;
  }

  async openRuntime(): Promise<void> {
    if (this.opened) return;
    if (this.closing) throw coordinatorError("e02_runtime_closing", "E02 runtime is closing");
    const opened: Array<() => Promise<void> | void> = [];
    try {
      await this.permission.open();
      opened.push(() => this.permission.close());
      if (!this.restoredBeforeBootstrap && this.mcp.config.revision === 0) {
        const layer = initialMcpLayer(this.input);
        if (layer) this.mcp.mergeConfig(layer);
      }
      await this.mcp.open();
      opened.push(() => this.mcp.close());
      await this.skills.open();
      opened.push(() => this.skills.close());
      await this.commands.open();
      opened.push(() => this.commands.close());
      await this.plugins.open();
      opened.push(() => this.plugins.close());
      this.syncRoutes("runtime_bootstrap");
      this.syncCustody("runtime_bootstrap");
      this.custody.markBootstrapCaptured();
      this.opened = true;
      await this.recoverPendingTransitions();
      const event = this.events.append({
        eventType: "e02.runtime.opened",
        transitionId: deterministicId("e02-runtime-open", {
          runtime_id: this.runtime.runtimeId,
          epoch: this.runtime.epoch,
          restored: this.restoredBeforeBootstrap,
        }, 40),
        domain: "command",
        phase: "observation",
        revision: this.executionSequence,
        payload: {
          epoch: this.runtime.epoch,
          restored_before_bootstrap: this.restoredBeforeBootstrap,
          restore_disposition: this.restoreDisposition,
          canonical_permission_owner: "typescript",
          canonical_mcp_owner: "typescript",
          canonical_skill_owner: "typescript",
          canonical_plugin_owner: "typescript",
          canonical_command_owner: "typescript",
          python_decision_fallback: false,
        },
      });
      await this.publish(event);
      await this.checkpoint("runtime_opened");
    } catch (error) {
      this.opened = false;
      for (const close of opened.reverse()) {
        try {
          await close();
        } catch (closeError) {
          this.recordIntegrationFailure("runtime", 0, "bootstrap_close", closeError, {});
        }
      }
      throw error;
    }
  }

  mergeToolSpecs(existing: ToolSpecContract[]): ToolSpecContract[] {
    const local = this.toolSpecs();
    const retained = existing.filter((tool) => !isLegacyCapabilityTool(tool));
    const byName = new Map<string, ToolSpecContract>();
    for (const tool of [...retained, ...local]) byName.set(tool.name, tool);
    return [...byName.values()].sort((left, right) => left.name.localeCompare(right.name));
  }

  toolSpecs(): ToolSpecContract[] {
    const routed = this.routes.toolSpecs();
    if (routed.length > 0) return routed;
    return this.rawToolSpecs();
  }

  private rawToolSpecs(): ToolSpecContract[] {
    return [
      ...this.mcp.toolSpecs(),
      ...this.skills.toolSpecs(),
      ...this.plugins.toolSpecs(),
      ...this.commands.toolSpecs(),
      ...this.agents.toolSpecs(),
      e02Tool("e02_health", "Inspect the TypeScript E02 runtime health and state custody", {}, "read"),
      e02Tool("e02_recovery_status", "Inspect unresolved E02 recovery records without replaying effects", {}, "read"),
      e02Tool("e02_execution_history", "Read TypeScript-owned E02 capability execution receipts", {
        phase: { type: "string" },
        tool_name: { type: "string" },
        limit: { type: "integer" },
      }, "read"),
      e02Tool("e02_control", "Execute a permission-gated TypeScript E02 control operation", {
        operation: { type: "string" },
        payload: { type: "object" },
        expected_revision: { type: "integer" },
        idempotency_key: { type: "string" },
        reason: { type: "string" },
      }, "execute"),
      e02Tool("e02_control_catalog", "List TypeScript-owned E02 control operations", {
        domain: { type: "string" },
        risk: { type: "string" },
        enabled: { type: "boolean" },
      }, "read"),
      e02Tool("e02_control_history", "Read the durable E02 control operation journal", {
        operation: { type: "string" },
        domain: { type: "string" },
        phase: { type: "string" },
        actor: { type: "string" },
        limit: { type: "integer" },
      }, "read"),
      e02Tool("e02_projection", "Read a redacted, cursor-stable projection of TypeScript E02 state", {
        action: { type: "string" },
        domains: { type: "array", items: { type: "string" } },
        projection_id: { type: "string" },
        from_projection_id: { type: "string" },
        to_projection_id: { type: "string" },
        cursor: { type: "string" },
        consumer_id: { type: "string" },
        subscription_id: { type: "string" },
        include_details: { type: "boolean" },
        include_history: { type: "boolean" },
        include_payloads: { type: "boolean" },
        filters: { type: "object" },
        limit: { type: "integer" },
      }, "read"),
      e02Tool("e02_recovery_plan", "Inspect and prepare no-replay E02 recovery plans", {
        action: { type: "string" },
        incident_id: { type: "string" },
        owner: { type: "string" },
        claim_id: { type: "string" },
        step_id: { type: "string" },
        result: { type: "object" },
        reason: { type: "string" },
        limit: { type: "integer" },
      }, "read"),
    ] as ToolSpecContract[];
  }

  owns(toolName: string): boolean {
    return this.routes.owns(toolName);
  }

  owner(toolName: string): string {
    return this.routes.owner(toolName);
  }

  async authorize(inputValue: E02AuthorizationInput): Promise<E02AuthorizationResult> {
    this.requireOpen();
    const input = cloneAuthorizationInput(inputValue);
    const enforcement = await this.permission.enforce(input);
    let permit: CapabilityPermit | null = null;
    if (enforcement.allowed && inputValue.issueExecutionPermit !== false) {
      const externalSubject = !runtimeSubjectMatches(this.runtime, {
        runId: input.runId,
        taskId: input.taskId,
        sessionId: input.sessionId,
        workerRequestId: input.workerRequestId ?? input.toolCallId,
      });
      const permitInput = {
        decisionId: enforcement.decision.decisionId,
        runId: input.runId,
        taskId: input.taskId,
        sessionId: input.sessionId,
        sessionRevision: input.sessionRevision ?? 0,
        workerRequestId: input.workerRequestId ?? input.toolCallId,
        toolCallId: input.toolCallId,
        toolName: input.toolName,
        namespace: input.namespace ?? inferNamespace(input.toolName),
        argumentsDigest: enforcement.decision.finalArgumentsDigest,
        policyRevision: enforcement.decision.policyRevision,
        modeRevision: enforcement.decision.modeRevision,
        metadata: {
          reason_code: enforcement.decision.reasonCode,
          request_fingerprint: enforcement.decision.requestFingerprint,
          original_arguments_digest: enforcement.decision.originalArgumentsDigest,
          final_arguments_digest: enforcement.decision.finalArgumentsDigest,
          permission_request_binding: cloneJson(enforcement.decision.requestBinding),
          external_permission_subject: externalSubject,
          restart_safe_exact_approval: false,
          host_runtime_id: this.runtime.runtimeId,
        },
      };
      permit = externalSubject
        ? this.executionLedger.issueExternalPermit(permitInput)
        : this.executionLedger.issuePermit(permitInput);
    }
    return {
      enforcement,
      permit,
      permitId: permit?.permitId ?? null,
      finalArguments: cloneJson(enforcement.finalArguments),
      stateDigest: digest({
        enforcement: enforcement.stateDigest,
        permit: permit?.bindingDigest ?? null,
      }),
    };
  }

  async execute(
    toolName: string,
    argumentsValue: JsonObject,
    contextValue: E02ExecutionContext,
  ): Promise<E02ExecutionReceipt> {
    this.requireOpen();
    if (!this.owns(toolName)) {
      throw coordinatorError(
        "e02_capability_not_owned",
        `E02CapabilityCoordinator.execute cannot dispatch ${toolName}`,
      );
    }
    if (!contextValue.toolCallId) {
      throw coordinatorError("e02_tool_call_id_missing", "E02 capability execution requires a tool call id");
    }
    let effectiveContext = contextValue;
    if (toolName === "command") {
      const requestedName = commandNameFromArguments(argumentsValue);
      if (!requestedName) {
        throw coordinatorError("command_name_missing", "command capability requires a slash command name");
      }
      const descriptor = this.commands.registry.resolve(requestedName);
      const commandOperation = effectiveCommandOperation(
        descriptor,
        commandActionFromArguments(argumentsValue),
      );
      effectiveContext = {
        ...contextValue,
        commandName: descriptor.name,
        operation: commandOperation,
        metadata: {
          ...contextValue.metadata,
          requested_command_name: contextValue.commandName ?? "",
          requested_operation: contextValue.operation ?? "",
          canonical_command_id: descriptor.commandId,
          canonical_command_operation: commandOperation,
        },
      };
    }
    const context = this.executionContext(toolName, effectiveContext);
    if (toolName === "command") {
      assertSealedCommandMutationInput(argumentsValue, context);
    }
    const commandArgumentOverrides = toolName === "command"
      ? structuredCommandArgumentOverrides(argumentsValue)
      : {};
    let finalArguments = canonicalize(
      toolName === "command"
        ? durableCommandArguments(argumentsValue, commandArgumentOverrides)
        : argumentsValue,
    ) as JsonObject;
    const requestArgumentsDigest = digest(finalArguments);
    const idempotencyKey = capabilityIdempotencyKey(toolName, finalArguments, context);
    const priorExecution = this.executionLedger.executionByKey(idempotencyKey);
    if (priorExecution) {
      const priorRequestDigest = optionalString(priorExecution.metadata.request_arguments_digest)
        || priorExecution.argumentsDigest;
      if (
        priorExecution.toolName !== toolName
        || priorExecution.toolCallId !== context.toolCallId
        || !constantTimeDigestEquals(priorRequestDigest, requestArgumentsDigest)
      ) {
        throw coordinatorError(
          "e02_execution_idempotency_binding_mismatch",
          `execution idempotency key ${idempotencyKey} is bound to another physical request`,
          { execution_id: priorExecution.executionId },
        );
      }
      // A committed physical call is authoritative.  Re-running permission
      // first would create a new epoch-bound permit and can conflict with the
      // restored permission transition before the execution ledger gets a
      // chance to replay its result.  Exact committed replay therefore occurs
      // before any new authorization or external side effect.
      if (priorExecution.phase === "committed" && priorExecution.result) {
        return replayReceipt(priorExecution, this.snapshot().snapshotHash);
      }
      if (priorExecution.phase === "recovery_required") {
        throw coordinatorError(
          "e02_execution_recovery_required",
          `execution ${priorExecution.executionId} requires reconciliation before replay`,
          { execution_id: priorExecution.executionId, recovery: priorExecution.recovery },
        );
      }
    }
    let permitId = context.permitId ?? null;
    if (!permitId) {
      const authorization = await this.authorize({
        runId: context.runId,
        taskId: context.taskId,
        sessionId: context.sessionId,
        sessionRevision: context.sessionRevision,
        workerRequestId: context.workerRequestId,
        toolCallId: context.toolCallId,
        toolName,
        namespace: context.namespace,
        serverId: context.serverId,
        commandName: context.commandName,
        resourceUri: context.resourceUri,
        operation: context.operation,
        workspaceRoot: this.workspaceRoot,
        arguments: finalArguments,
        metadata: context.metadata,
        signal: context.signal,
      });
      if (!authorization.enforcement.allowed || !authorization.permitId) {
        throw permissionDeniedError(authorization.enforcement);
      }
      finalArguments = authorization.finalArguments;
      permitId = authorization.permitId;
    }
    const permit = this.executionLedger.consumePermit(permitId, {
      runId: context.runId,
      sessionId: context.sessionId,
      sessionRevision: context.sessionRevision,
      workerRequestId: context.workerRequestId,
      toolCallId: context.toolCallId,
      toolName,
      argumentsDigest: digest(finalArguments),
    });
    const routeLease = this.routes.issueLease({
      toolName,
      toolCallId: context.toolCallId,
      argumentsDigest: digest(finalArguments),
    });
    this.routes.consumeLease(routeLease.leaseId, {
      toolName,
      toolCallId: context.toolCallId,
      argumentsDigest: digest(finalArguments),
    });
    const domain = capabilityDomain(toolName, this);
    const owner = this.owner(toolName);
    const ledgerRecord = this.executionLedger.prepareExecution({
      permitId: permit.permitId,
      domain,
      owner,
      toolName,
      toolCallId: context.toolCallId,
      argumentsDigest: digest(finalArguments),
        idempotencyKey,
        metadata: {
          ...context.metadata,
          request_arguments_digest: requestArgumentsDigest,
          route_lease_id: routeLease.leaseId,
          route_id: routeLease.routeId,
          route_revision: routeLease.routeRevision,
        },
    });
    if (ledgerRecord.phase === "committed" && ledgerRecord.result) {
      return replayReceipt(ledgerRecord, this.snapshot().snapshotHash);
    }
    if (ledgerRecord.phase === "recovery_required") {
      throw coordinatorError(
        "e02_execution_recovery_required",
        `execution ${ledgerRecord.executionId} requires reconciliation before replay`,
        { execution_id: ledgerRecord.executionId, recovery: ledgerRecord.recovery },
      );
    }
    const running = this.inFlight.get(ledgerRecord.executionId);
    if (running) return running;
    const promise = this.performExecution(
      ledgerRecord,
      permit,
      toolName,
      finalArguments,
      context,
      domain,
      owner,
      idempotencyKey,
      commandArgumentOverrides,
    );
    this.inFlight.set(ledgerRecord.executionId, promise);
    try {
      return await promise;
    } finally {
      this.inFlight.delete(ledgerRecord.executionId);
    }
  }

  resumePermission(response: PermissionApprovalResponse): E02AuthorizationResult {
    this.requireOpen();
    const enforcement = this.permission.resumeAndEnforce(response);
    let permit: CapabilityPermit | null = null;
    if (enforcement.allowed) {
      const binding = enforcement.decision.requestBinding;
      const permitInput = {
        decisionId: enforcement.decision.decisionId,
        runId: stringField(binding, "run_id", response.runId),
        taskId: stringField(binding, "task_id", this.runtime.taskId),
        sessionId: stringField(binding, "session_id", response.sessionId),
        sessionRevision: numberField(binding, "session_revision", response.sessionRevision),
        workerRequestId: stringField(binding, "worker_request_id", response.workerRequestId),
        toolCallId: response.toolCallId,
        toolName: stringField(binding, "tool_name", "unknown"),
        namespace: stringField(binding, "namespace", "builtin"),
        argumentsDigest: enforcement.decision.finalArgumentsDigest,
        policyRevision: enforcement.decision.policyRevision,
        modeRevision: enforcement.decision.modeRevision,
        metadata: {
          resumed_approval_response_id: response.responseId,
          continuation_request_id: response.requestId,
          responder: response.responder,
          permission_request_binding: cloneJson(binding),
          restart_safe_suspended_batch: response.metadata.restart_safe_suspended_batch === true,
          external_permission_subject: !runtimeSubjectMatches(this.runtime, {
            runId: stringField(binding, "run_id", response.runId),
            taskId: stringField(binding, "task_id", this.runtime.taskId),
            sessionId: stringField(binding, "session_id", response.sessionId),
            workerRequestId: stringField(
              binding,
              "worker_request_id",
              response.workerRequestId,
            ),
          }),
          restart_safe_exact_approval: true,
          host_runtime_id: this.runtime.runtimeId,
        },
      };
      permit = permitInput.metadata.external_permission_subject
        ? this.executionLedger.issueExternalPermit(permitInput)
        : this.executionLedger.issuePermit(permitInput);
    }
    return {
      enforcement,
      permit,
      permitId: permit?.permitId ?? null,
      finalArguments: cloneJson(enforcement.finalArguments),
      stateDigest: digest({ enforcement: enforcement.stateDigest, permit: permit?.bindingDigest ?? null }),
    };
  }

  async reconcile(inputValue: E02ManualReconciliation): Promise<E02RecoveryRecord> {
    this.requireOpen();
    const input = cloneJson(inputValue);
    if (!input.transitionId || !input.reason || !input.actor) {
      throw coordinatorError(
        "e02_reconciliation_identity_incomplete",
        "manual reconciliation requires transition, reason, and actor",
      );
    }
    const transition = this.journal.transition(input.transitionId);
    if (!transition) {
      throw coordinatorError(
        "e02_reconciliation_transition_not_found",
        `transition ${input.transitionId} was not found`,
      );
    }
    if (transition.phase === "committed" || transition.phase === "acknowledged") {
      const existing = [...this.recoveries.values()].find(
        (record) => record.transitionId === transition.transitionId,
      );
      if (existing) return cloneJson(existing);
      throw coordinatorError(
        "e02_reconciliation_already_committed",
        `transition ${input.transitionId} is already committed`,
      );
    }
    const execution = this.executionForTransition(transition.transitionId);
    if (input.outcome === "confirm_no_effect") {
      if (transition.effectReceipt?.ok) {
        throw coordinatorError(
          "e02_reconciliation_effect_already_recorded",
          "a successful recorded effect cannot be cancelled as no-effect",
        );
      }
      this.journal.cancel(transition.transitionId, input.reason);
      if (execution) {
        this.executionLedger.failExecution(
          execution.executionId,
          coordinatorError("effect_confirmed_absent", input.reason),
          { actor: input.actor, ...input.metadata },
        );
      }
      const recovery = this.recordRecovery(
        transition,
        execution?.executionId ?? null,
        "manually_cancelled",
        input.reason,
        { actor: input.actor, ...input.metadata },
      );
      await this.checkpoint("manual_reconciliation_no_effect");
      return recovery;
    }
    if (!input.result) {
      throw coordinatorError(
        "e02_reconciliation_result_missing",
        "confirm_effect reconciliation requires the exact effect result",
      );
    }
    const resultObject = capabilityResultObject(input.result);
    if (!transition.effectReceipt) {
      this.journal.recordEffect(transition.transitionId, {
        effectKind: "manual_reconciliation",
        requestHash: transition.payloadHash,
        responseHash: digest(resultObject),
        ok: true,
        output: resultObject,
        error: null,
        providerReceiptId: input.providerReceiptId ?? null,
        metadata: {
          actor: input.actor,
          reason: input.reason,
          manually_reconciled: true,
          ...input.metadata,
        },
      });
    } else if (!constantTimeDigestEquals(transition.effectReceipt.responseHash, digest(resultObject))) {
      throw coordinatorError(
        "e02_reconciliation_result_mismatch",
        "manual reconciliation result differs from the durable effect receipt",
      );
    }
    this.journal.recordReceipt(transition.transitionId, {
      manually_reconciled: true,
      actor: input.actor,
    });
    const commit = this.journal.commit({
      transitionId: transition.transitionId,
      nextState: this.domainState(transition.domain),
      output: resultObject,
      expectedRevision: transition.revisionBefore,
      metadata: {
        reconciliation_actor: input.actor,
        reconciliation_reason: input.reason,
      },
    });
    this.journal.acknowledge(transition.transitionId, commit.commitHash);
    if (execution) {
      if (execution.phase === "recovery_required") {
        this.executionLedger.recordEffect(execution.executionId, resultObject);
      }
      this.executionLedger.commitExecution(execution.executionId, resultObject);
    }
    const recovery = this.recordRecovery(
      transition,
      execution?.executionId ?? null,
      "manually_reconciled",
      input.reason,
      {
        actor: input.actor,
        commit_hash: commit.commitHash,
        provider_receipt_id: input.providerReceiptId ?? null,
        ...input.metadata,
      },
    );
    const event = this.events.appendTransition(
      "e02.transition.manually_reconciled",
      transition.transitionId,
      transition.domain,
      "acknowledged",
      commit.revisionAfter,
      {
        recovery_id: recovery.recoveryId,
        commit_hash: commit.commitHash,
        actor: input.actor,
      },
    );
    await this.publish(event);
    await this.checkpoint("manual_reconciliation_effect");
    return recovery;
  }

  async drainBackground(context: AgentExecutionContext): Promise<void> {
    this.requireOpen();
    await this.agents.drainBackground(context);
  }

  health(): E02HealthReport {
    const snapshot = this.snapshot();
    const journal = snapshot.journal;
    const recoveryRequired = this.executionLedger.recoveryRequired();
    const status = this.opened && !this.closing && recoveryRequired.length === 0
      ? "candidate_pending_independent_review"
      : "failed";
    return {
      status,
      canonicalPermissionOwner: "typescript",
      canonicalMcpOwner: "typescript",
      canonicalSkillOwner: "typescript",
      canonicalPluginOwner: "typescript",
      canonicalCommandOwner: "typescript",
      pythonDecisionFallback: false,
      restoredBeforeBootstrap: this.restoredBeforeBootstrap,
      pendingTransitions: journal.pending.length,
      committedTransitions: journal.committed.length,
      capabilityRevision: snapshot.plugins.capabilities.revision,
      permissionPolicyRevision: snapshot.permission.evaluator.policyRevision as number
        || this.permission.evaluator.rules.revision,
      snapshotHash: snapshot.snapshotHash,
      details: {
        runtime: cloneJson(this.runtime),
        workspace_root: this.workspaceRoot,
        opened: this.opened,
        closing: this.closing,
        permission: this.permission.health(),
        mcp: this.mcp.health(),
        skills: this.skills.health(),
        plugins: this.plugins.health(),
        commands: this.commands.health(),
        execution_ledger: this.executionLedger.health(),
        host_ports: this.hostPorts.health(),
        custody: this.custody.health(),
        routes: this.routes.health(),
        checkpoint_bundles: this.checkpointBundles.health(),
        control_plane: this.controlPlane.health(),
        projections: this.projections.health(),
        recovery_runtime: this.recoveryRuntime.health(),
        recovery_required: recoveryRequired.map((record) => record.executionId),
        integration_failure_count: [...this.integrationAudit.values()].filter(
          (audit) => audit.status === "failed" || audit.status === "rolled_back",
        ).length,
        python_permission_fallback: false,
        python_mcp_fallback: false,
        python_skill_fallback: false,
        python_plugin_fallback: false,
        python_command_fallback: false,
      },
    };
  }

  snapshot(): E02CapabilityCoordinatorSnapshot {
    const capturedAt = this.timestamp();
    const permission = this.permission.snapshot();
    const mcp = this.mcp.snapshot();
    const skills = this.skills.snapshot();
    const plugins = this.plugins.snapshot();
    const commands = this.commands.snapshot();
    const journal = this.journal.snapshot();
    const events = this.events.snapshot();
    const executionLedger = this.executionLedger.snapshot();
    const hostPorts = this.hostPorts.snapshot();
    const custody = this.custody.snapshot();
    const routes = this.routes.snapshot();
    const checkpointBundles = this.checkpointBundles.snapshot();
    const controlPlane = this.controlPlane.snapshot();
    const projections = this.projections.snapshot();
    const recoveryRuntime = this.recoveryRuntime.snapshot();
    const withoutHash = {
      version: "zyra.e02-runtime/v1" as const,
      runtime: cloneJson(this.runtime),
      workspaceRoot: this.workspaceRoot,
      opened: this.opened,
      restoredBeforeBootstrap: this.restoredBeforeBootstrap,
      closing: this.closing,
      permission,
      mcp,
      skills,
      plugins,
      commands,
      agents: this.agents.snapshot(),
      journal,
      events,
      executionLedger,
      hostPorts,
      custody,
      routes,
      checkpointBundles,
      controlPlane,
      projections,
      recoveryRuntime,
      recoveries: [...this.recoveries.values()]
        .sort((left, right) => left.recoveredAt.localeCompare(right.recoveredAt))
        .map(cloneJson),
      integrationAudit: [...this.integrationAudit.values()]
        .sort((left, right) => left.startedAt.localeCompare(right.startedAt))
        .map(cloneJson),
      pluginIntegrationRevisions: [...this.pluginIntegrationRevisions.entries()]
        .sort(([left], [right]) => left.localeCompare(right)),
      pluginMcpLayerRevisions: [...this.pluginMcpLayerRevisions.entries()]
        .sort(([left], [right]) => left.localeCompare(right)),
      pluginAgents: [...this.pluginAgents.entries()]
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([pluginId, agents]) => [pluginId, agents.map(cloneJson)] as [string, JsonObject[]]),
      executionSequence: this.executionSequence,
      snapshotSequence: this.snapshotSequence,
      lastCheckpointHash: this.lastCheckpointHash,
      capturedAt,
    };
    return {
      ...withoutHash,
      snapshotHash: digest(withoutHash),
    };
  }

  async close(): Promise<void> {
    if (this.closing) return;
    this.closing = true;
    try {
      const pending = [...this.inFlight.values()];
      if (pending.length) await Promise.allSettled(pending);
      if (this.opened) await this.checkpoint("runtime_closing");
      await this.plugins.close();
      this.commands.close();
      await this.skills.close();
      await this.mcp.close();
      this.permission.close();
      this.opened = false;
    } finally {
      this.closing = false;
    }
  }

  private async performExecution(
    ledgerRecord: CapabilityExecutionRecord,
    permit: CapabilityPermit,
    toolName: string,
    argumentsValue: JsonObject,
    context: RequiredExecutionContext,
    domain: E02Domain,
    owner: string,
    idempotencyKey: string,
    commandArgumentOverrides: JsonObject,
  ): Promise<E02ExecutionReceipt> {
    const entityId = `e02-capability:${domain}:${toolName}`;
    const entity = this.journal.entityState(entityId);
    const prepared = this.journal.prepare({
      domain,
      operation: context.operation,
      binding: {
        runId: context.runId,
        taskId: context.taskId,
        sessionId: context.sessionId,
        workerRequestId: context.workerRequestId,
        toolCallId: context.toolCallId,
        requestId: ledgerRecord.executionId,
        entityId,
        expectedRevision: entity.revision,
      },
      payload: {
        execution_id: ledgerRecord.executionId,
        permit_id: permit.permitId,
        decision_id: permit.decisionId,
        tool_name: toolName,
        arguments: cloneJson(argumentsValue),
        arguments_digest: digest(argumentsValue),
        owner,
        domain,
        operation: context.operation,
        runtime_epoch: this.runtime.epoch,
        metadata: cloneJson(context.metadata),
      },
      idempotencyKey,
      replayPolicy: "return-committed",
      nonIdempotentEffect: context.operation !== "read",
      metadata: {
        capability_owner: owner,
        canonical_runtime_owner: "typescript",
      },
    });
    if (prepared.kind === "return_committed" && prepared.committedReceipt) {
      const result = parseCapabilityResult(prepared.committedReceipt.output);
      this.executionLedger.commitExecution(ledgerRecord.executionId, prepared.committedReceipt.output);
      return {
        executionId: ledgerRecord.executionId,
        transitionId: prepared.transition.transitionId,
        owner,
        result,
        commit: prepared.committedReceipt,
        replayed: true,
        permitId: permit.permitId,
        snapshotHash: this.snapshot().snapshotHash,
      };
    }
    if (prepared.kind === "resume_pending" && prepared.transition.phase === "effect_started") {
      this.executionLedger.requireRecovery(ledgerRecord.executionId, {
        kind: "indeterminate_non_idempotent_effect",
        transition_id: prepared.transition.transitionId,
        reexecute_without_receipt: false,
      });
      throw coordinatorError(
        "e02_indeterminate_effect",
        `transition ${prepared.transition.transitionId} has an indeterminate effect`,
      );
    }
    this.executionLedger.bindTransition(ledgerRecord.executionId, prepared.transition.transitionId);
    const preparedEvent = this.events.appendTransition(
      "e02.capability.prepared",
      prepared.transition.transitionId,
      domain,
      "prepared",
      prepared.transition.revisionBefore,
      {
        execution_id: ledgerRecord.executionId,
        tool_name: toolName,
        owner,
        arguments_digest: digest(argumentsValue),
      },
    );
    await this.publish(preparedEvent);
    this.executionLedger.beginExecution(ledgerRecord.executionId);
    if (context.operation !== "read") this.journal.beginEffect(prepared.transition.transitionId);
    if (context.agentContext) this.activeAgentContexts.set(context.toolCallId, context.agentContext);
    if (domain === "command") {
      this.activeCommandAuthorizations.set(context.toolCallId, {
        commandName: context.commandName,
        operation: context.operation,
        decisionId: permit.decisionId,
        permitId: permit.permitId,
        outerArgumentsDigest: digest(argumentsValue),
      });
    }
    try {
      // Persist the consumed permit, prepared transition, execution record,
      // and effect-start fence before crossing the physical capability port.
      // Without this pre-effect checkpoint a process kill after the provider
      // committed but before its response was observed restored an empty
      // ledger and silently allowed the same side effect to run again.
      await this.checkpoint("capability_effect_prepared");
      const result = await this.dispatchCapability(
        toolName,
        argumentsValue,
        context,
        commandArgumentOverrides,
      );
      const resultObject = capabilityResultObject(result);
      this.executionLedger.recordEffect(ledgerRecord.executionId, resultObject);
      if (context.operation !== "read") {
        this.journal.recordEffect(prepared.transition.transitionId, {
          effectKind: `${domain}:${context.operation}`,
          requestHash: prepared.transition.payloadHash,
          responseHash: digest(resultObject),
          ok: true,
          output: resultObject,
          error: null,
          providerReceiptId: stringMetadata(result.metadata, "provider_receipt_id") || null,
          metadata: {
            execution_id: ledgerRecord.executionId,
            capability_owner: owner,
          },
        });
      }
      this.journal.recordReceipt(prepared.transition.transitionId, {
        execution_id: ledgerRecord.executionId,
        result_digest: digest(resultObject),
      });
      const commit = this.journal.commit({
        transitionId: prepared.transition.transitionId,
        nextState: this.domainState(domain),
        output: resultObject,
        expectedRevision: prepared.transition.revisionBefore,
        metadata: {
          execution_id: ledgerRecord.executionId,
          permit_id: permit.permitId,
        },
      });
      this.journal.acknowledge(prepared.transition.transitionId, commit.commitHash);
      this.executionLedger.commitExecution(ledgerRecord.executionId, resultObject);
      this.executionSequence += 1;
      const committedEvent = this.events.appendTransition(
        "e02.capability.committed",
        prepared.transition.transitionId,
        domain,
        "acknowledged",
        commit.revisionAfter,
        {
          execution_id: ledgerRecord.executionId,
          tool_name: toolName,
          owner,
          result_digest: digest(resultObject),
          commit_hash: commit.commitHash,
          python_capability_fallback: false,
        },
        [preparedEvent.event_id],
      );
      await this.publish(committedEvent);
      const snapshot = await this.checkpoint("capability_committed");
      return {
        executionId: ledgerRecord.executionId,
        transitionId: prepared.transition.transitionId,
        owner,
        result,
        commit,
        replayed: false,
        permitId: permit.permitId,
        snapshotHash: snapshot.snapshotHash,
      };
    } catch (error) {
      const current = this.journal.transition(prepared.transition.transitionId);
      if (current && (current.phase === "committed" || current.phase === "acknowledged")) {
        const deliveryEvent = this.events.appendTransition(
          "e02.capability.commit_delivery_failed",
          current.transitionId,
          domain,
          "observation",
          current.revisionAfter ?? current.revisionBefore,
          {
            execution_id: ledgerRecord.executionId,
            tool_name: toolName,
            owner,
            commit_hash: current.commitReceipt?.commitHash ?? "",
            failure: errorObject(error),
            execution_remains_committed: true,
          },
          [preparedEvent.event_id],
        );
        const deliveryAttempts = await Promise.allSettled([
          this.publish(deliveryEvent),
          this.checkpoint("commit_delivery_failed"),
        ]);
        throw coordinatorError(
          "e02_commit_delivery_failed",
          `capability ${toolName} committed, but acknowledgement delivery failed`,
          {
            execution_id: ledgerRecord.executionId,
            transition_id: current.transitionId,
            commit_hash: current.commitReceipt?.commitHash ?? "",
            original_failure: errorObject(error),
            delivery_attempts: deliveryAttempts.map((attempt): JsonObject => attempt.status === "fulfilled"
              ? { status: "fulfilled" }
              : { status: "rejected", failure: errorObject(attempt.reason) }),
            retry_disposition: "return_committed",
          },
        );
      }
      if (current && current.phase !== "committed" && current.phase !== "acknowledged") {
        if (
          current.nonIdempotentEffect
          && (current.phase === "effect_started" || current.effectReceipt?.ok)
        ) {
          this.executionLedger.requireRecovery(ledgerRecord.executionId, {
            kind: "capability_effect_reconciliation",
            transition_id: current.transitionId,
            phase: current.phase,
            effect_receipt: canonicalize(current.effectReceipt),
            error: errorObject(error),
            reexecute_without_receipt: false,
          });
          this.recordRecovery(
            current,
            ledgerRecord.executionId,
            "recovery_required",
            error instanceof Error ? error.message : String(error),
            { failure: errorObject(error) },
          );
        } else {
          try {
            this.journal.reject(
              current.transitionId,
              errorCode(error),
              error instanceof Error ? error.message : String(error),
            );
          } catch (rejectError) {
            this.executionLedger.requireRecovery(ledgerRecord.executionId, {
              kind: "transition_rejection_failed",
              transition_id: current.transitionId,
              original_error: errorObject(error),
              rejection_error: errorObject(rejectError),
            });
          }
          const record = this.executionLedger.getExecution(ledgerRecord.executionId);
          if (record?.phase !== "recovery_required") {
            this.executionLedger.failExecution(ledgerRecord.executionId, error, {
              transition_id: current.transitionId,
            });
          }
        }
      }
      const failedEvent = this.events.appendTransition(
        "e02.capability.failed",
        prepared.transition.transitionId,
        domain,
        "rejected",
        prepared.transition.revisionBefore,
        {
          execution_id: ledgerRecord.executionId,
          tool_name: toolName,
          owner,
          failure: errorObject(error),
          recovery_required: this.executionLedger.getExecution(ledgerRecord.executionId)?.phase === "recovery_required",
        },
        [preparedEvent.event_id],
      );
      await this.publish(failedEvent);
      await this.checkpoint("capability_failed");
      throw error;
    } finally {
      this.activeAgentContexts.delete(context.toolCallId);
      this.activeCommandAuthorizations.delete(context.toolCallId);
    }
  }

  private async dispatchCapability(
    toolName: string,
    argumentsValue: JsonObject,
    context: RequiredExecutionContext,
    commandArgumentOverrides: JsonObject = {},
  ): Promise<E02CapabilityResult> {
    const route = this.routes.resolve(toolName);
    const [custodyDomain, custodyOwner] = routeCustody(route.selectedDomain!, route.selectedOwner!);
    this.custody.assertOwner(custodyDomain, custodyOwner);
    if (route.selectedDomain === "mcp") {
      return this.mcp.execute(toolName, argumentsValue, {
        runId: context.runId,
        taskId: context.taskId,
        sessionId: context.sessionId,
        sessionRevision: context.sessionRevision,
        workerRequestId: context.workerRequestId,
        toolCallId: context.toolCallId,
        workspaceRoot: this.workspaceRoot,
        interactive: this.permission.evaluator.modes.canAsk(),
        sealedAutonomous: this.permission.evaluator.modes.mode === "sealed",
      }, context.signal);
    }
    if (route.selectedDomain === "skill") {
      const parentContext = skillParentContext(context.agentContext?.parentInput ?? this.input);
      return this.skills.execute(toolName, argumentsValue, {
        runId: context.runId,
        taskId: context.taskId,
        sessionId: context.sessionId,
        sessionRevision: context.sessionRevision,
        workerRequestId: context.workerRequestId,
        toolCallId: context.toolCallId,
      }, parentContext, undefined, context.signal);
    }
    if (route.selectedDomain === "plugin") {
      return this.plugins.execute(toolName, argumentsValue, context.signal);
    }
    if (route.selectedDomain === "command") {
      const sealedAutonomous = this.permission.evaluator.modes.mode === "sealed"
        || commandSealedAutonomous(argumentsValue, context);
      return this.commands.execute(toolName, commandExecutionArguments(
        argumentsValue,
        commandArgumentOverrides,
      ), {
        runId: context.runId,
        taskId: context.taskId,
        sessionId: context.sessionId,
        sessionRevision: context.sessionRevision,
        workerRequestId: context.workerRequestId,
        toolCallId: context.toolCallId,
        interactive: this.permission.evaluator.modes.canAsk() && !sealedAutonomous,
        sealedAutonomous,
      }, context.signal);
    }
    if (route.selectedDomain === "agent") {
      if (!context.agentContext) {
        throw coordinatorError(
          "agent_execution_context_missing",
          `agent capability ${toolName} requires a child QueryEngine execution context`,
        );
      }
      const ownerSessionId = asString(
        this.input.config.runtimeConstraints?.typescriptAgentParentSessionId
          ?? this.input.config.runtimeConstraints?.typescript_agent_parent_session_id,
      );
      const agentContext = ownerSessionId
        ? {
            ...context.agentContext,
            parentInput: {
              ...context.agentContext.parentInput,
              sessionId: ownerSessionId,
            },
          }
        : context.agentContext;
      return this.agents.execute(toolName, argumentsValue, agentContext);
    }
    if (toolName === "e02_health") {
      return capabilityResult("E02 runtime health", this.health() as unknown as JsonObject, {
        capability_owner: "typescript-e02-control",
      });
    }
    if (toolName === "e02_recovery_status") {
      return capabilityResult("E02 recovery status", {
        recoveries: canonicalize([...this.recoveries.values()]),
        pending_transitions: canonicalize(this.journal.pendingTransitions()),
        ledger_recovery_required: canonicalize(this.executionLedger.recoveryRequired()),
      }, { capability_owner: "typescript-e02-control" });
    }
    if (toolName === "e02_execution_history") {
      return capabilityResult("E02 execution history", {
        executions: canonicalize(this.executionLedger.listExecutions({
          phase: executionPhase(argumentsValue.phase),
          toolName: optionalString(argumentsValue.tool_name) || undefined,
          limit: nonNegativeInteger(argumentsValue.limit, 100),
        })),
      }, { capability_owner: "typescript-e02-control" });
    }
    if (toolName === "e02_control_catalog") {
      return capabilityResult("E02 control operation catalog", {
        operations: canonicalize(this.controlPlane.listDescriptors({
          domain: optionalString(argumentsValue.domain) || undefined,
          risk: controlRisk(argumentsValue.risk),
          enabled: typeof argumentsValue.enabled === "boolean" ? argumentsValue.enabled : undefined,
        })),
        control_revision: this.controlPlane.snapshot().revision,
      }, { capability_owner: "typescript-e02-control" });
    }
    if (toolName === "e02_control_history") {
      return capabilityResult("E02 control operation history", {
        records: canonicalize(this.controlPlane.listRecords({
          operation: optionalString(argumentsValue.operation) || undefined,
          domain: optionalString(argumentsValue.domain) || undefined,
          phase: controlPhase(argumentsValue.phase),
          actor: optionalString(argumentsValue.actor) || undefined,
          limit: nonNegativeInteger(argumentsValue.limit, 100),
        })),
        recovery_required: canonicalize(this.controlPlane.pendingRecovery()),
      }, { capability_owner: "typescript-e02-control" });
    }
    if (toolName === "e02_control") {
      const operation = requiredControlString(argumentsValue, "operation");
      const record = await this.controlPlane.execute({
        operation,
        binding: {
          runId: context.runId,
          taskId: context.taskId,
          sessionId: context.sessionId,
          workerRequestId: context.workerRequestId,
          toolCallId: context.toolCallId,
          actor: optionalString(context.metadata.actor) || "e02-tool-caller",
          correlationId: optionalString(context.metadata.correlation_id) || context.toolCallId,
        },
        payload: asObject(argumentsValue.payload),
        expectedRevision: nullableNonNegativeInteger(argumentsValue.expected_revision),
        idempotencyKey: optionalString(argumentsValue.idempotency_key) || undefined,
        reason: optionalString(argumentsValue.reason) || undefined,
        metadata: context.metadata,
      }, context.signal);
      if (record.phase !== "committed") {
        throw coordinatorError(
          "e02_control_operation_not_committed",
          `control operation ${operation} ended in ${record.phase}`,
          { request_id: record.request.requestId, phase: record.phase, error: record.error },
        );
      }
      return capabilityResult(`E02 control ${operation} committed`, {
        request_id: record.request.requestId,
        operation,
        control_revision: record.revisionAfter,
        result: record.result,
        effect_receipt: record.effect,
      }, {
        capability_owner: "typescript-e02-control",
        control_request_id: record.request.requestId,
      });
    }
    if (toolName === "e02_projection") {
      const action = optionalString(argumentsValue.action) || "capture";
      if (action === "capture") {
        const projection = this.projections.capture(this.snapshot() as unknown as JsonObject, {
          domains: projectionDomains(argumentsValue.domains),
          includeDetails: argumentsValue.include_details === true,
          includeHistory: argumentsValue.include_history === true,
          includePayloads: argumentsValue.include_payloads === true,
          filters: asObject(argumentsValue.filters),
          limit: nonNegativeInteger(argumentsValue.limit, 100),
        }, {
          tool_call_id: context.toolCallId,
          actor: optionalString(context.metadata.actor) || "e02-tool-caller",
        });
        const page = this.projections.pageRecord(
          projection.projectionId,
          nonNegativeInteger(argumentsValue.limit, 100),
        );
        return capabilityResult("E02 state projection captured", {
          projection,
          page,
        }, { capability_owner: "typescript-e02-control" });
      }
      if (action === "page") {
        return capabilityResult("E02 state projection page", {
          page: this.projections.page(requiredControlString(argumentsValue, "cursor")),
        }, { capability_owner: "typescript-e02-control" });
      }
      if (action === "diff") {
        return capabilityResult("E02 state projection diff", {
          diff: this.projections.diff(
            requiredControlString(argumentsValue, "from_projection_id"),
            requiredControlString(argumentsValue, "to_projection_id"),
            nonNegativeInteger(argumentsValue.limit, 1_000),
          ),
        }, { capability_owner: "typescript-e02-control" });
      }
      if (action === "list") {
        return capabilityResult("E02 state projections", {
          projections: canonicalize(this.projections.list({
            domains: projectionDomains(argumentsValue.domains),
            limit: nonNegativeInteger(argumentsValue.limit, 100),
          })),
        }, { capability_owner: "typescript-e02-control" });
      }
      if (action === "subscribe") {
        return capabilityResult("E02 projection subscription created", {
          subscription: this.projections.subscribe(
            requiredControlString(argumentsValue, "consumer_id"),
            projectionDomains(argumentsValue.domains) ?? ["runtime"],
            { tool_call_id: context.toolCallId },
          ),
        }, { capability_owner: "typescript-e02-control" });
      }
      if (action === "pending") {
        return capabilityResult("E02 pending projection subscription items", {
          projections: canonicalize(this.projections.pendingForSubscription(
            requiredControlString(argumentsValue, "subscription_id"),
            nonNegativeInteger(argumentsValue.limit, 100),
          )),
        }, { capability_owner: "typescript-e02-control" });
      }
      if (action === "ack") {
        return capabilityResult("E02 projection subscription acknowledged", {
          subscription: this.projections.acknowledge(
            requiredControlString(argumentsValue, "subscription_id"),
            requiredControlString(argumentsValue, "projection_id"),
          ),
        }, { capability_owner: "typescript-e02-control" });
      }
      throw coordinatorError("e02_projection_action_unknown", `unknown projection action ${action}`);
    }
    if (toolName === "e02_recovery_plan") {
      this.syncRecovery("recovery_plan_tool");
      const action = optionalString(argumentsValue.action) || "list";
      if (action === "list") {
        return capabilityResult("E02 recovery incidents", {
          incidents: canonicalize(this.recoveryRuntime.list({
            limit: nonNegativeInteger(argumentsValue.limit, 100),
          })),
          health: this.recoveryRuntime.health(),
        }, { capability_owner: "typescript-e02-control" });
      }
      const incidentId = requiredControlString(argumentsValue, "incident_id");
      if (action === "diagnose") {
        return capabilityResult("E02 recovery incident diagnosed", {
          diagnosis: this.recoveryRuntime.diagnose(incidentId),
          evidence: canonicalize(this.recoveryRuntime.evidenceFor(incidentId)),
        }, { capability_owner: "typescript-e02-control" });
      }
      if (action === "plan") {
        return capabilityResult("E02 no-replay recovery plan prepared", {
          plan: this.recoveryRuntime.buildPlan(incidentId),
        }, { capability_owner: "typescript-e02-control" });
      }
      if (action === "claim") {
        return capabilityResult("E02 recovery incident claimed", {
          claim: this.recoveryRuntime.claim(
            incidentId,
            requiredControlString(argumentsValue, "owner"),
          ),
        }, { capability_owner: "typescript-e02-control" });
      }
      if (action === "start_step") {
        return capabilityResult("E02 recovery plan step started", {
          step: this.recoveryRuntime.startStep(
            incidentId,
            requiredControlString(argumentsValue, "step_id"),
            requiredControlString(argumentsValue, "claim_id"),
          ),
        }, { capability_owner: "typescript-e02-control" });
      }
      if (action === "complete_step") {
        return capabilityResult("E02 recovery plan step completed", {
          step: this.recoveryRuntime.completeStep(
            incidentId,
            requiredControlString(argumentsValue, "step_id"),
            requiredControlString(argumentsValue, "claim_id"),
            asObject(argumentsValue.result),
          ),
        }, { capability_owner: "typescript-e02-control" });
      }
      if (action === "release") {
        return capabilityResult("E02 recovery claim released", {
          claim: this.recoveryRuntime.releaseClaim(
            requiredControlString(argumentsValue, "claim_id"),
            requiredControlString(argumentsValue, "reason"),
          ),
        }, { capability_owner: "typescript-e02-control" });
      }
      throw coordinatorError("e02_recovery_plan_action_unknown", `unknown recovery plan action ${action}`);
    }
    throw coordinatorError("e02_capability_not_owned", `E02 runtime does not own ${toolName}`);
  }

  private async executeControlPlaneOperation(
    request: E02ControlRequest,
    _descriptor: E02ControlOperationDescriptor,
    signal?: AbortSignal,
  ): Promise<{ result: JsonValue; providerReceiptId?: string | null; metadata?: JsonObject }> {
    const payload = request.payload;
    if (request.operation === "runtime.health.read") {
      return { result: this.health() as unknown as JsonObject, metadata: { effectful: false } };
    }
    if (request.operation === "runtime.snapshot.read") {
      const snapshot = this.snapshot();
      return {
        result: {
          version: snapshot.version,
          runtime: snapshot.runtime,
          opened: snapshot.opened,
          restored_before_bootstrap: snapshot.restoredBeforeBootstrap,
          snapshot_sequence: snapshot.snapshotSequence,
          snapshot_hash: snapshot.snapshotHash,
          route_revision: snapshot.routes.revision,
          permission_revision: snapshot.permission.evaluator.policyRevision,
          mcp_revision: snapshot.mcp.config.revision,
          skill_revision: snapshot.skills.registry.revision,
          plugin_revision: snapshot.plugins.capabilities.revision,
          command_revision: snapshot.commands.registry.revision,
          checkpoint_sequence: snapshot.checkpointBundles.sequence,
          python_decision_fallback: false,
        },
        metadata: { effectful: false, redacted: true },
      };
    }
    if (request.operation === "route.catalog.read") {
      const toolName = optionalString(payload.tool_name);
      return {
        result: {
          health: this.routes.health(),
          tool_specs: canonicalize(toolName ? [this.routes.spec(toolName)] : this.routes.toolSpecs()),
          candidates: payload.include_candidates === true && toolName
            ? canonicalize(this.routes.candidatesFor(toolName))
            : [],
          revisions: canonicalize(this.routes.decisionHistory().slice(-nonNegativeInteger(payload.limit, 100))),
        },
        metadata: { effectful: false },
      };
    }
    if (request.operation === "checkpoint.history.read") {
      return {
        result: {
          health: this.checkpointBundles.health(),
          bundles: canonicalize(this.checkpointBundles.list({
            phase: checkpointPhase(payload.phase),
            reason: optionalString(payload.reason) || undefined,
            limit: nonNegativeInteger(payload.limit, 100),
          })),
          pending_delivery: canonicalize(this.checkpointBundles.pendingDelivery()),
        },
        metadata: { effectful: false },
      };
    }
    if (request.operation === "permission.mode.transition") {
      const transition = this.permission.transitionMode(permissionMode(requiredControlString(payload, "mode")), {
        actor: requiredControlString(payload, "actor"),
        reason: requiredControlString(payload, "reason"),
        expectedRevision: this.permission.evaluator.modes.revision,
        metadata: asObject(payload.metadata),
      });
      return { result: canonicalize(transition), providerReceiptId: optionalString(transition.transitionId) || null };
    }
    if (request.operation === "permission.rules.replace") {
      if (!Array.isArray(payload.rules)) {
        throw coordinatorError("e02_control_rules_invalid", "permission.rules.replace requires an array of rules");
      }
      const rules = payload.rules.map((value) => typeof value === "string" ? value : asObject(value));
      const commit = this.permission.replaceRules(rules, this.permission.evaluator.rules.revision, {
        actor: requiredControlString(payload, "actor"),
        reason: requiredControlString(payload, "reason"),
        ...asObject(payload.metadata),
      });
      return { result: canonicalize(commit), providerReceiptId: commit.commitId };
    }
    if (request.operation === "permission.settings.reload") {
      const commit = await this.permission.reloadSettings({
        actor: requiredControlString(payload, "actor"),
        reason: requiredControlString(payload, "reason"),
        ...asObject(payload.metadata),
      });
      return { result: canonicalize(commit), providerReceiptId: commit.commitId };
    }
    if (request.operation === "mcp.reload") {
      await this.mcp.reload(signal);
      this.syncRoutes("control_mcp_reload");
      return { result: this.mcp.health(), metadata: { reloaded: true } };
    }
    if (request.operation === "skill.reload") {
      const result = await this.skills.execute("reload_skills", {}, {
        runId: this.runtime.runId,
        taskId: this.runtime.taskId,
        sessionId: this.runtime.sessionId,
        sessionRevision: inputSessionRevision(this.input),
        workerRequestId: this.runtime.workerRequestId,
        toolCallId: request.binding.toolCallId,
      }, skillParentContext(this.input), undefined, signal);
      this.syncRoutes("control_skill_reload");
      return { result: canonicalize(result), metadata: { reloaded: true } };
    }
    if (request.operation === "plugin.reload") {
      const receipt = await this.plugins.reload({
        actor: requiredControlString(payload, "actor"),
        reason: requiredControlString(payload, "reason"),
        ...asObject(payload.metadata),
      });
      this.syncRoutes("control_plugin_reload");
      return { result: canonicalize(receipt), providerReceiptId: receipt.receiptId };
    }
    if (request.operation === "recovery.transition.reconcile") {
      const outcome = requiredControlString(payload, "outcome");
      if (outcome !== "confirm_effect" && outcome !== "confirm_no_effect") {
        throw coordinatorError("e02_control_recovery_outcome_invalid", `invalid transition recovery outcome ${outcome}`);
      }
      const record = await this.reconcile({
        transitionId: requiredControlString(payload, "transition_id"),
        outcome,
        result: payload.result ? asObject(payload.result) as unknown as E02CapabilityResult : undefined,
        providerReceiptId: optionalString(payload.provider_receipt_id) || null,
        actor: requiredControlString(payload, "actor"),
        reason: requiredControlString(payload, "reason"),
        metadata: asObject(payload.metadata),
      });
      return { result: canonicalize(record), providerReceiptId: record.recoveryId };
    }
    if (request.operation === "recovery.checkpoint.reconcile") {
      const outcome = requiredControlString(payload, "outcome");
      if (outcome !== "delivered" && outcome !== "not_delivered") {
        throw coordinatorError("e02_control_checkpoint_outcome_invalid", `invalid checkpoint outcome ${outcome}`);
      }
      const record = this.checkpointBundles.reconcileDelivery(
        requiredControlString(payload, "bundle_id"),
        {
          outcome,
          actor: requiredControlString(payload, "actor"),
          reason: requiredControlString(payload, "reason"),
          deliveredSnapshotHash: optionalString(payload.delivered_snapshot_hash) || undefined,
          providerReceiptId: optionalString(payload.provider_receipt_id) || null,
        },
      );
      return { result: canonicalize(record), providerReceiptId: record.deliveryReceiptId };
    }
    if (request.operation === "checkpoint.compact") {
      return {
        result: this.checkpointBundles.compact(nonNegativeInteger(payload.retain_count, 256)),
        metadata: { compacted: true },
      };
    }
    throw coordinatorError("e02_control_operation_unhandled", `unhandled E02 control operation ${request.operation}`);
  }

  private async executeSkillPlan(
    executorContext: Parameters<SkillExecutor>[0],
  ): ReturnType<SkillExecutor> {
    const plan = executorContext.plan;
    const context = this.activeAgentContexts.get(plan.identity.toolCallId);
    if (!context) {
      throw coordinatorError(
        "skill_query_context_disconnected",
        `skill ${plan.skillName} has no bound QueryEngine execution context`,
      );
    }
    if (executorContext.signal?.aborted) {
      throw abortError(executorContext.signal.reason);
    }
    const parent = context.parentInput;
    const parentSkillAncestry = skillInvocationAncestry(parent);
    if (parentSkillAncestry.includes(plan.skillId)) {
      throw coordinatorError(
        "skill_recursive_invocation_denied",
        `skill ${plan.skillId} is already active in the current skill ancestry`,
        {
          skill_id: plan.skillId,
          skill_ancestry: parentSkillAncestry,
          child_started: false,
        },
      );
    }
    const parentDepthRemaining = skillDepthRemaining(parent);
    if (parentDepthRemaining !== null && parentDepthRemaining <= 0) {
      throw coordinatorError(
        "skill_nested_invocation_denied",
        `active skill ancestry does not permit invoking nested skill ${plan.skillId}`,
        {
          skill_id: plan.skillId,
          skill_ancestry: parentSkillAncestry,
          skill_depth_remaining: parentDepthRemaining,
          child_started: false,
        },
      );
    }
    const descriptorDepth = plan.execution.maximumSkillDepth ?? 0;
    const childDepthRemaining = parentDepthRemaining === null
      ? descriptorDepth
      : Math.min(parentDepthRemaining - 1, descriptorDepth);
    const childSkillAncestry = [...parentSkillAncestry, plan.skillId];
    const childTaskId = `${parent.taskId}:skill:${plan.skillId}:${plan.invocationId.slice(-12)}`;
    const result = await TypeScriptSkillRuntime.executeForkedSkill(
      {
        parentInput: parent,
        childTaskId,
        workerRequestId: `${parent.workerRequestId}:skill:${plan.invocationId.slice(-12)}`,
        invocationId: plan.invocationId,
        skillId: plan.skillId,
        skillName: plan.skillName,
        descriptorDigest: plan.descriptorDigest,
        renderedBody: plan.renderedBody,
        skillContext: cloneJson(plan.context),
        skillArguments: cloneJson(plan.arguments),
        skillResources: canonicalize(plan.resources),
        effectiveToolScope: cloneJson(plan.effectiveToolScope),
        maximumTurns: plan.execution.maximumTurns,
        skillAncestry: childSkillAncestry,
        remainingSkillDepth: childDepthRemaining,
        sandbox: plan.execution.sandbox,
        allowNetwork: plan.execution.allowNetwork,
      },
      context.runChild,
      executorContext.signal,
    );
    if (!result.ok) {
      const stoppedReason = result.stoppedReason?.trim() || "unknown";
      throw coordinatorError(
        "skill_child_run_failed",
        `forked skill child ${childTaskId} failed: ${stoppedReason}`,
        {
          child_task_id: childTaskId,
          stopped_reason: stoppedReason,
        },
      );
    }
    return {
      output: {
        ok: result.ok,
        stopped_reason: result.stoppedReason,
        step_summaries: result.stepSummaries,
        session_snapshot_artifact_id:
          result.metadata.query_session_snapshot_artifact_id ?? null,
        metadata: result.metadata,
      },
      artifacts: result.artifacts.map((artifact) => canonicalize(artifact) as JsonObject),
      inputTokens: plan.inputTokenEstimate,
      outputTokens: estimateTokens(result.stepSummaries.join("\n")),
      costMicros: numberMetadata(result.metadata, "cost_micros"),
      metadata: {
        child_task_id: childTaskId,
        child_turn_count: result.turnCount,
        child_tool_call_count: result.toolCallCount,
        canonical_skill_owner: "typescript",
        execution_mode: plan.execution.mode,
        source_custody: "claude-code-best:executeForkedSkill",
        forked_subagent_owner: "AgentExecutionContext.runChild",
      },
    };
  }

  private async evaluateCommandPermission(
    descriptor: CommandDescriptor,
    request: CommandInvocationRequest,
    argumentsValue: JsonObject,
  ): Promise<CommandPermissionDecision> {
    const commandOperation = effectiveCommandOperation(
      descriptor,
      asString(argumentsValue.action),
    );
    const active = this.activeCommandAuthorizations.get(request.identity.commandCallId);
    if (active) {
      if (active.commandName !== descriptor.name || active.operation !== commandOperation) {
        throw coordinatorError(
          "command_permission_binding_mismatch",
          `outer permission binding does not match command ${descriptor.name}`,
          {
            outer_command_name: active.commandName,
            outer_operation: active.operation,
            command_operation: commandOperation,
          },
        );
      }
      return commandPermissionDecision(descriptor, request, argumentsValue, {
        effect: "allow",
        decisionId: active.decisionId,
        reasonCode: "outer_e02_permit_consumed",
        reason: "exact TypeScript E02 permit authorized this parsed command",
        continuationId: null,
        replanRequired: false,
        recoveryInput: null,
        metadata: {
          permit_id: active.permitId,
          outer_arguments_digest: active.outerArgumentsDigest,
          original_arguments_digest: digest(argumentsValue),
          arguments_unchanged: true,
          sensitive_arguments_projected: false,
          canonical_permission_owner: "typescript.PermissionCoordinator",
          nested_permission_bypass: false,
        },
      });
    }
    const authorization = await this.authorize({
      runId: request.identity.runId,
      taskId: request.identity.taskId,
      sessionId: request.identity.sessionId,
      sessionRevision: request.identity.sessionRevision,
      workerRequestId: request.identity.workerRequestId,
      toolCallId: `${request.identity.commandCallId}:command:${descriptor.commandId}`,
      toolName: "command",
      namespace: "command",
      commandName: descriptor.name,
      operation: commandOperation,
      workspaceRoot: request.workspaceRoot,
      arguments: argumentsValue,
      metadata: {
        command_id: descriptor.commandId,
        command_name: descriptor.name,
        descriptor_digest: descriptor.descriptorDigest,
        command_risk: descriptor.permission.risk,
        workspace_mutation: descriptor.permission.workspaceMutation,
        network_access: descriptor.permission.networkAccess,
        process_execution: descriptor.permission.processExecution,
        sealed_autonomous: request.sealedAutonomous,
      },
    });
    const decision = authorization.enforcement.decision;
    return commandPermissionDecision(descriptor, request, argumentsValue, {
      effect: decision.effect,
      decisionId: decision.decisionId,
      reasonCode: decision.reasonCode,
      reason: decision.reason,
      continuationId: decision.continuationRequestId,
      replanRequired: decision.replanRequired,
      recoveryInput: decision.recoveryInput,
      metadata: {
        permit_id: authorization.permitId,
        request_fingerprint: decision.requestFingerprint,
        original_arguments_digest: decision.originalArgumentsDigest,
        final_arguments_digest: decision.finalArgumentsDigest,
        final_arguments: cloneJson(decision.finalArguments),
        policy_revision: decision.policyRevision,
        mode_revision: decision.modeRevision,
      },
    });
  }

  private async dispatchSkillCommand(
    descriptor: CommandDescriptor,
    request: CommandInvocationRequest,
    argumentsValue: JsonObject,
    signal?: AbortSignal,
  ): Promise<JsonValue> {
    const skillName = descriptor.handler.skillName;
    if (!skillName) throw coordinatorError("command_skill_missing", `command ${descriptor.name} has no skill binding`);
    const result = await this.skills.execute("skill", {
      skill: skillName,
      arguments: argumentsValue,
    }, {
      runId: request.identity.runId,
      taskId: request.identity.taskId,
      sessionId: request.identity.sessionId,
      sessionRevision: request.identity.sessionRevision,
      workerRequestId: request.identity.workerRequestId,
      toolCallId: request.identity.commandCallId,
    }, skillParentContext(this.input), undefined, signal);
    return result.output;
  }

  private async dispatchMcpPromptCommand(
    descriptor: CommandDescriptor,
    request: CommandInvocationRequest,
    argumentsValue: JsonObject,
    signal?: AbortSignal,
  ): Promise<JsonValue> {
    if (!descriptor.handler.mcpServerId || !descriptor.handler.mcpPromptName) {
      throw coordinatorError("command_mcp_prompt_missing", `command ${descriptor.name} has no MCP prompt binding`);
    }
    const result = await this.mcp.execute("mcp_get_prompt", {
      server_id: descriptor.handler.mcpServerId,
      name: descriptor.handler.mcpPromptName,
      arguments: argumentsValue,
    }, {
      runId: request.identity.runId,
      taskId: request.identity.taskId,
      sessionId: request.identity.sessionId,
      sessionRevision: request.identity.sessionRevision,
      workerRequestId: request.identity.workerRequestId,
      toolCallId: request.identity.commandCallId,
      workspaceRoot: request.workspaceRoot,
      interactive: request.interactive,
      sealedAutonomous: request.sealedAutonomous,
    }, signal);
    return result.output;
  }

  private async dispatchPluginCommand(
    descriptor: CommandDescriptor,
    request: CommandInvocationRequest,
    argumentsValue: JsonObject,
    signal?: AbortSignal,
  ): Promise<JsonValue> {
    const pluginId = descriptor.handler.pluginId;
    if (!pluginId) throw coordinatorError("command_plugin_missing", `command ${descriptor.name} has no plugin binding`);
    const result = await this.plugins.execute("plugin_command", {
      plugin_id: pluginId,
      command: descriptor.name,
      arguments: argumentsValue,
      command_call_id: request.identity.commandCallId,
    }, signal);
    return result.output;
  }

  private async dispatchControlCommand(
    descriptor: CommandDescriptor,
    request: CommandInvocationRequest,
    argumentsValue: JsonObject,
    signal?: AbortSignal,
  ): Promise<JsonValue> {
    if (!this.ports.controlCommand) {
      throw coordinatorError(
        "control_command_port_disconnected",
        `control command ${descriptor.name} has no connected TypeScript host port`,
      );
    }
    return this.ports.controlCommand(descriptor, request, argumentsValue, signal);
  }

  private async dispatchBuiltinCommand(
    descriptor: CommandDescriptor,
    request: CommandInvocationRequest,
    argumentsValue: JsonObject,
    signal?: AbortSignal,
  ): Promise<JsonValue> {
    if (this.ports.builtinCommand) {
      return this.ports.builtinCommand(descriptor, request, argumentsValue, signal);
    }
    if (descriptor.handler.handlerId === "builtin:e02-health") return this.health() as unknown as JsonObject;
    if (descriptor.handler.handlerId === "builtin:e02-reload") {
      const identity = {
        runId: request.identity.runId,
        taskId: request.identity.taskId,
        sessionId: request.identity.sessionId,
        sessionRevision: request.identity.sessionRevision,
        workerRequestId: request.identity.workerRequestId,
        toolCallId: request.identity.commandCallId,
      };
      const [mcp, skills, commands, plugins] = await Promise.all([
        this.mcp.reload(signal).then(() => this.mcp.health()),
        this.skills.execute(
          "reload_skills",
          {},
          identity,
          skillParentContext(this.input),
          undefined,
          signal,
        ),
        this.commands.execute("reload_commands", {}, identity, signal),
        this.plugins.reload({ reason: "builtin_command", command: descriptor.name }),
      ]);
      this.syncRoutes("builtin_command_reload");
      this.syncCustody("builtin_command_reload");
      return {
        mcp,
        skills: canonicalize(skills),
        commands: canonicalize(commands),
        plugins: canonicalize(plugins),
      };
    }
    if (descriptor.handler.handlerId === "builtin:e02-mcp") {
      const action = asString(argumentsValue.action).toLowerCase() || "health";
      const snapshot = this.mcp.snapshot();
      if (action === "health" || action === "status") return this.mcp.health();
      if (action === "servers" || action === "list") {
        return {
          servers: canonicalize(this.mcp.config.list()),
          connections: canonicalize(this.mcp.connections.list()),
        };
      }
      if (action === "server" || action === "show") {
        const serverId = requiredCommandString(argumentsValue, "server");
        return {
          server: canonicalize(this.mcp.config.require(serverId)),
          connection: canonicalize(this.mcp.connections.get(serverId)),
          catalog: canonicalize(this.mcp.catalog.get(serverId)),
          session: canonicalize(this.mcp.session.server(serverId)),
        };
      }
      if (action === "tools") return { tools: canonicalize(this.mcp.toolSpecs()) };
      if (action === "catalog") return { catalog: canonicalize(snapshot.catalog) };
      if (action === "resources") {
        return {
          resources: canonicalize(snapshot.catalog.servers.flatMap((server) => server.resources)),
          resource_templates: canonicalize(snapshot.catalog.servers.flatMap((server) => server.resourceTemplates)),
        };
      }
      if (action === "prompts") {
        return { prompts: canonicalize(snapshot.catalog.servers.flatMap((server) => server.prompts)) };
      }
      if (action === "tasks") return { tasks: canonicalize(snapshot.tasks) };
      if (action === "elicitations") return { elicitations: canonicalize(snapshot.elicitation) };
      if (isMcpLifecycleAction(action)) {
        return this.dispatchMcpLifecycleCommand(action, request, argumentsValue, signal);
      }
      throw coordinatorError("builtin_mcp_action_unknown", `unknown /mcp action ${action}`);
    }
    if (descriptor.handler.handlerId === "builtin:e02-skills") {
      const action = asString(argumentsValue.action).toLowerCase() || "list";
      if (action === "health" || action === "status" || action === "list") {
        return {
          health: this.skills.health(),
          tools: canonicalize(this.skills.toolSpecs()),
          registry: canonicalize(this.skills.snapshot().registry),
          body_in_projection: false,
        };
      }
      if (action === "show") {
        const skillName = requiredCommandString(argumentsValue, "skill");
        return {
          authority: canonicalize(this.skills.authority(skillName)),
          registry_revision: this.skills.registry.revision,
          body_in_projection: false,
        };
      }
      if (action === "update" || action === "invoke") {
        return this.dispatchSkillLifecycleCommand(action, request, argumentsValue, signal);
      }
      throw coordinatorError("builtin_skill_action_unknown", `unknown /skills action ${action}`);
    }
    if (descriptor.handler.handlerId === "builtin:e02-plugins") {
      return {
        health: this.plugins.health(),
        tools: canonicalize(this.plugins.toolSpecs()),
        plugins: canonicalize(this.plugins.snapshot()),
      };
    }
    if (descriptor.handler.handlerId === "builtin:e02-permissions") {
      return {
        health: this.permission.health(),
        canonical_owner: "typescript.PermissionCoordinator",
        python_decision_fallback: false,
      };
    }
    if (descriptor.handler.handlerId === "builtin:e02-tools") {
      return {
        tools: canonicalize(this.toolSpecs()),
        route_revision: this.routes.snapshot().revision,
        canonical_owner: "typescript",
      };
    }
    if (descriptor.handler.handlerId === "builtin:e02-help") {
      return canonicalize(this.commands.help.index({ includeHidden: false }));
    }
    throw coordinatorError(
      "builtin_command_not_registered",
      `builtin command handler ${descriptor.handler.handlerId} is not registered`,
    );
  }

  private async dispatchMcpLifecycleCommand(
    action: McpLifecycleAction,
    request: CommandInvocationRequest,
    argumentsValue: JsonObject,
    signal?: AbortSignal,
  ): Promise<JsonObject> {
    assertInteractiveCommandMutation(request, "mcp", action);
    const serverId = requiredCommandString(argumentsValue, "server");
    const configured = this.mcp.config.require(serverId);
    const before = this.mcp.snapshot();
    let resultValue: JsonValue;
    let providerReceiptId: string | null = null;

    if (action === "enable" || action === "disable") {
      const enabled = action === "enable";
      if (configured.enabled === enabled) {
        throw coordinatorError(
          enabled ? "mcp_server_already_enabled" : "mcp_server_already_disabled",
          `MCP server ${serverId} is already ${enabled ? "enabled" : "disabled"}`,
          { server_id: serverId },
        );
      }
      const expectedRevision = commandOptionalInteger(
        argumentsValue,
        "expected-revision",
        before.config.revision,
      );
      if (expectedRevision !== before.config.revision) {
        throw coordinatorError(
          "mcp_config_revision_conflict",
          `MCP config revision ${expectedRevision} does not match ${before.config.revision}`,
          {
            server_id: serverId,
            expected_revision: expectedRevision,
            actual_revision: before.config.revision,
          },
        );
      }
      const layerId = "e02-command-mcp-lifecycle";
      const lifecycleLayer = before.config.layers.find((layer) => layer.layerId === layerId);
      const servers = cloneJson(lifecycleLayer?.servers ?? {});
      const priorPatch = asObject(servers[serverId]);
      const response = asObject(argumentsValue.response);
      servers[serverId] = {
        ...priorPatch,
        enabled,
        metadata: {
          ...asObject(priorPatch.metadata),
          lifecycle_action: action,
          lifecycle_reason: asString(response.reason)
            || asString(argumentsValue.reason)
            || `/${action}`,
          command_call_id: request.identity.commandCallId,
        },
      };
      const layerRevision = Math.max(
        0,
        ...before.config.layers
          .filter((layer) => layer.source === "session")
          .map((layer) => layer.revision),
      ) + 1;
      const merge = this.mcp.mergeConfig({
        layerId,
        source: "session",
        revision: layerRevision,
        servers,
        tombstones: [],
        metadata: {
          canonical_owner: "typescript.McpConfigStore",
          command_call_id: request.identity.commandCallId,
          lifecycle_action: action,
        },
      }, expectedRevision);
      try {
        await this.mcp.reload(signal);
      } catch (error) {
        const rollbackSnapshot = this.mcp.config.snapshot();
        const rollbackLayer = rollbackSnapshot.layers.find((layer) => layer.layerId === layerId);
        const rollbackServers = cloneJson(rollbackLayer?.servers ?? {});
        rollbackServers[serverId] = {
          ...asObject(rollbackServers[serverId]),
          enabled: configured.enabled,
          metadata: {
            ...asObject(asObject(rollbackServers[serverId]).metadata),
            lifecycle_rollback: true,
            failed_action: action,
          },
        };
        this.mcp.mergeConfig({
          layerId,
          source: "session",
          revision: Math.max(
            0,
            ...rollbackSnapshot.layers
              .filter((layer) => layer.source === "session")
              .map((layer) => layer.revision),
          ) + 1,
          servers: rollbackServers,
          tombstones: [],
          metadata: {
            canonical_owner: "typescript.McpConfigStore",
            lifecycle_rollback: true,
            failed_action: action,
          },
        }, rollbackSnapshot.revision);
        try {
          await this.mcp.reload(signal);
        } catch {
          // The original owner failure remains authoritative; the restored
          // config still records the compensation attempt for reconciliation.
        }
        throw error;
      }
      providerReceiptId = deterministicId("mcp-config-merge-receipt", {
        command_call_id: request.identity.commandCallId,
        server_id: serverId,
        action,
        revision: merge.revision,
        digest: merge.digest,
      }, 40);
      resultValue = {
        config_merge: canonicalize(merge),
        server: canonicalize(this.mcp.config.require(serverId)),
        connection: canonicalize(this.mcp.connections.get(serverId)),
        session: canonicalize(this.mcp.session.server(serverId)),
      };
    } else if (action === "reconnect") {
      if (!configured.enabled) {
        throw coordinatorError("mcp_server_disabled", `MCP server ${serverId} is disabled`);
      }
      const catalog = await this.mcp.client.reconnect(
        serverId,
        `command:${request.identity.commandCallId}`,
        signal,
      );
      await this.mcp.reload(signal);
      providerReceiptId = catalog.digest;
      resultValue = {
        catalog: canonicalize(catalog),
        connection: canonicalize(this.mcp.connections.get(serverId)),
      };
    } else if (action === "refresh") {
      if (!configured.enabled) {
        throw coordinatorError("mcp_server_disabled", `MCP server ${serverId} is disabled`);
      }
      const catalog = await this.mcp.client.refreshCatalog(serverId, signal);
      await this.mcp.reload(signal);
      providerReceiptId = catalog.digest;
      resultValue = { catalog: canonicalize(catalog) };
    } else if (action === "auth-refresh") {
      if (!configured.authProviderId) {
        throw coordinatorError(
          "mcp_auth_provider_missing",
          `MCP server ${serverId} has no configured OAuth provider`,
          { server_id: serverId },
        );
      }
      const token = await this.mcp.oauth.refresh(
        configured.authProviderId,
        `command:${request.identity.commandCallId}`,
        signal,
      );
      this.mcp.connections.updateAuthRevision(serverId, token.revision);
      await this.mcp.reload(signal);
      providerReceiptId = deterministicId("mcp-oauth-refresh-receipt", {
        provider_id: token.providerId,
        server_id: token.serverId,
        revision: token.revision,
        issued_at: token.issuedAt,
      }, 40);
      resultValue = {
        oauth: {
          provider_id: token.providerId,
          server_id: token.serverId,
          revision: token.revision,
          token_type: token.tokenType,
          scopes: token.scopes,
          expires_at: token.expiresAt,
          issued_at: token.issuedAt,
          credential_handles_projected: false,
        },
        connection: canonicalize(this.mcp.connections.get(serverId)),
      };
    } else {
      const elicitationId = requiredCommandString(argumentsValue, "request");
      const record = this.mcp.elicitation.get(elicitationId);
      if (!record) {
        throw coordinatorError(
          "mcp_elicitation_not_found",
          `MCP elicitation ${elicitationId} was not found`,
          { server_id: serverId, elicitation_id: elicitationId },
        );
      }
      if (
        record.identity.serverId !== serverId
        || record.identity.runId !== request.identity.runId
        || record.identity.taskId !== request.identity.taskId
        || record.identity.sessionId !== request.identity.sessionId
      ) {
        throw coordinatorError(
          "mcp_elicitation_scope_mismatch",
          `MCP elicitation ${elicitationId} belongs to another owner scope`,
          { server_id: serverId, elicitation_id: elicitationId },
        );
      }
      const responseEnvelope = requiredCommandObject(argumentsValue, "response");
      assertOptionalCommandBinding(responseEnvelope, "request_id", record.elicitationId);
      assertOptionalCommandBinding(responseEnvelope, "server_id", serverId);
      const content = Object.prototype.hasOwnProperty.call(responseEnvelope, "response")
        ? requiredCommandObject(responseEnvelope, "response")
        : responseEnvelope;
      const settled = this.mcp.elicitation.resume({
        ...record.identity,
        elicitationId: record.elicitationId,
        continuationId: record.continuationId,
        result: {
          action: "accept",
          content,
          meta: {
            command_call_id: request.identity.commandCallId,
            response_digest: asString(responseEnvelope.response_digest),
          },
        },
      });
      providerReceiptId = settled.elicitationId;
      resultValue = {
        elicitation: {
          continuationId: settled.continuationId,
          createdAt: settled.createdAt,
          elicitationId: settled.elicitationId,
          expiresAt: settled.expiresAt,
          identity: canonicalize(settled.identity),
          metadata: canonicalize(settled.metadata),
          rejectionCode: settled.rejectionCode,
          request: canonicalize(settled.request),
          requestDigest: settled.requestDigest,
          response: settled.response
            ? {
                action: settled.response.action,
                content: null,
                contentProjected: false,
                meta: canonicalize(settled.response.meta),
              }
            : null,
          responseDigest: settled.responseDigest,
          resumedAt: settled.resumedAt,
          status: settled.status,
        },
        response_content_projected: false,
      };
    }

    this.syncRoutes(`builtin_mcp_${action}`);
    this.syncCustody(`builtin_mcp_${action}`);
    const after = this.mcp.snapshot();
    const effectId = deterministicId("mcp-lifecycle-effect", {
      command_call_id: request.identity.commandCallId,
      action,
      server_id: serverId,
      config_revision_before: before.config.revision,
      config_revision_after: after.config.revision,
      connection_revision_before: before.connections.revision,
      connection_revision_after: after.connections.revision,
      catalog_revision_before: before.catalog.revision,
      catalog_revision_after: after.catalog.revision,
      oauth_revision_before: before.oauth.revision,
      oauth_revision_after: after.oauth.revision,
      elicitation_revision_before: before.elicitation.revision,
      elicitation_revision_after: after.elicitation.revision,
      result_digest: digest(resultValue),
    }, 48);
    return {
      protocol: "zyra.command-owner-effect/v1",
      canonical_owner: "typescript.McpRuntimeCoordinator",
      action,
      target_id: serverId,
      effect_id: effectId,
      receipt_id: providerReceiptId ?? effectId,
      state_before: mcpLifecycleState(before, serverId),
      state_after: mcpLifecycleState(after, serverId),
      result: canonicalize(resultValue),
      python_mutation_fallback: false,
    };
  }

  private async dispatchSkillLifecycleCommand(
    action: "update" | "invoke",
    request: CommandInvocationRequest,
    argumentsValue: JsonObject,
    signal?: AbortSignal,
  ): Promise<JsonObject> {
    assertInteractiveCommandMutation(request, "skill", action);
    const skillName = requiredCommandString(argumentsValue, "skill");
    const resolution = this.skills.registry.resolve(skillName);
    const identity = commandSkillIdentity(request);
    if (action === "update") {
      const expectedRevision = requiredCommandInteger(argumentsValue, "expected-revision");
      if (expectedRevision !== this.skills.registry.revision) {
        throw coordinatorError(
          "skill_registry_revision_conflict",
          `skill registry revision ${expectedRevision} does not match ${this.skills.registry.revision}`,
          {
            skill_id: resolution.skillId,
            expected_revision: expectedRevision,
            actual_revision: this.skills.registry.revision,
          },
        );
      }
      const expectedHash = requiredCommandString(argumentsValue, "expected-hash");
      if (!commandHashEquals(expectedHash, resolution.descriptor.bodyDigest)) {
        throw coordinatorError(
          "skill_update_hash_stale",
          `skill ${resolution.skillId} content hash no longer matches the update request`,
          {
            skill_id: resolution.skillId,
            expected_hash: expectedHash,
            actual_hash: resolution.descriptor.bodyDigest,
          },
        );
      }
      const before = this.skills.snapshot();
      const stagedReload = await this.skills.reloadSkillsWithAdmission(
        identity,
        (descriptors, scan) => {
          const proposed = descriptors.find(
            (descriptor) => descriptor.skillId === resolution.skillId,
          );
          if (!proposed) {
            throw coordinatorError(
              "skill_update_owner_admission_denied",
              `skill ${resolution.skillId} is absent from the staged reload`,
              {
                skill_id: resolution.skillId,
                scan_id: scan.scanId,
                scan_digest: scan.digest,
                owner_effect_started: false,
              },
            );
          }
          return skillOwnerUpdateAdmission(
            resolution.descriptor,
            proposed,
            request,
            scan,
          );
        },
      );
      const ownerResult = stagedReload.execution;
      const ownerAdmission = stagedReload.admission;
      this.syncRoutes("builtin_skills_update");
      this.syncCustody("builtin_skills_update");
      const after = this.skills.snapshot();
      const updated = this.skills.registry.resolve(skillName);
      const reloadReceipt = after.reloadHistory.at(-1);
      const effectId = reloadReceipt?.receiptId ?? deterministicId("skill-update-effect", {
        command_call_id: request.identity.commandCallId,
        skill_id: resolution.skillId,
        registry_revision_before: before.registry.revision,
        registry_revision_after: after.registry.revision,
        body_digest_before: resolution.descriptor.bodyDigest,
        body_digest_after: updated.descriptor.bodyDigest,
      }, 48);
      return {
        protocol: "zyra.command-owner-effect/v1",
        canonical_owner: "typescript.SkillCoordinator",
        action,
        target_id: resolution.skillId,
        effect_id: effectId,
        receipt_id: reloadReceipt?.receiptId ?? effectId,
        state_before: {
          registry_revision: before.registry.revision,
          revision_id: resolution.revisionId,
          descriptor_digest: resolution.descriptor.descriptorDigest,
          body_digest: resolution.descriptor.bodyDigest,
        },
        state_after: {
          registry_revision: after.registry.revision,
          revision_id: updated.revisionId,
          descriptor_digest: updated.descriptor.descriptorDigest,
          body_digest: updated.descriptor.bodyDigest,
        },
        owner_result: canonicalize(ownerResult),
        reload_receipt: canonicalize(reloadReceipt ?? null),
        owner_admission: ownerAdmission,
        caller_attestations: {
          dependency_digest: asString(argumentsValue["dependency-digest"]),
          supply_digest: asString(argumentsValue["supply-digest"]),
          approval_id: asString(argumentsValue["approval-id"]),
          authoritative: false,
          purpose: "browser_projection_reconciliation_only",
        },
        python_mutation_fallback: false,
      };
    }

    const registryRevision = requiredCommandInteger(argumentsValue, "registry-revision");
    if (registryRevision !== resolution.revision) {
      throw coordinatorError(
        "skill_invocation_revision_stale",
        `skill ${resolution.skillId} registry revision ${registryRevision} is stale`,
        {
          skill_id: resolution.skillId,
          expected_revision: registryRevision,
          actual_revision: resolution.revision,
        },
      );
    }
    const descriptorDigest = requiredCommandString(argumentsValue, "descriptor-digest");
    if (!commandHashEquals(descriptorDigest, resolution.descriptor.descriptorDigest)) {
      throw coordinatorError(
        "skill_invocation_descriptor_stale",
        `skill ${resolution.skillId} descriptor digest is stale`,
        { skill_id: resolution.skillId },
      );
    }
    const contentHash = requiredCommandString(argumentsValue, "content-hash");
    if (!commandHashEquals(contentHash, resolution.descriptor.bodyDigest)) {
      throw coordinatorError(
        "skill_invocation_content_stale",
        `skill ${resolution.skillId} content hash is stale`,
        { skill_id: resolution.skillId },
      );
    }
    const invocationArguments = requiredCommandObject(argumentsValue, "arguments-json");
    const argumentsDigest = requiredCommandString(argumentsValue, "arguments-digest");
    if (argumentsDigest !== skillCommandArgumentsDigest(invocationArguments)) {
      throw coordinatorError(
        "skill_invocation_arguments_digest_mismatch",
        `skill ${resolution.skillId} arguments digest is invalid`,
        { skill_id: resolution.skillId },
      );
    }
    const ownerResult = await this.skills.execute(
      "skill",
      {
        skill: skillName,
        arguments: invocationArguments,
      },
      identity,
      skillParentContext(this.input),
      undefined,
      signal,
    );
    const invocation = asObject(ownerResult.output.invocation);
    const invocationId = asString(invocation.invocationId);
    if (!invocationId) {
      throw coordinatorError(
        "skill_invocation_receipt_missing",
        `skill ${resolution.skillId} owner returned no invocation identity`,
      );
    }
    return {
      protocol: "zyra.command-owner-effect/v1",
      canonical_owner: "typescript.SkillCoordinator",
      action,
      target_id: resolution.skillId,
      effect_id: invocationId,
      receipt_id: invocationId,
      state_before: {
        registry_revision: resolution.revision,
        revision_id: resolution.revisionId,
        descriptor_digest: resolution.descriptor.descriptorDigest,
        body_digest: resolution.descriptor.bodyDigest,
      },
      state_after: {
        registry_revision: this.skills.registry.revision,
        invocation_status: asString(invocation.status),
        invocation_id: invocationId,
      },
      owner_result: canonicalize(ownerResult),
      python_mutation_fallback: false,
    };
  }

  private async integratePluginSkills(manifest: PluginManifest): Promise<JsonObject> {
    const revision = this.nextPluginIntegrationRevision(manifest.pluginId);
    const rootRevision = await this.skills.replacePluginRoots({
      pluginId: manifest.pluginId,
      pluginRevision: revision,
      manifestDigest: manifest.manifestDigest,
      roots: manifest.skillRoots,
      enabled: manifest.enabled,
      metadata: {
        plugin_version: manifest.version,
      },
    });
    return {
      plugin_id: manifest.pluginId,
      plugin_revision: revision,
      root_revision_id: rootRevision.revisionId,
      root_revision: rootRevision.revisionAfter,
      skill_source_ids: manifest.skillRoots.map((root) => root.sourceId),
      registry_revision: this.skills.registry.revision,
    };
  }

  private async integratePluginCommands(manifest: PluginManifest): Promise<JsonObject> {
    const receipt = await this.commands.replacePlugin(manifest, {
      plugin_integration_revision: this.currentPluginIntegrationRevision(manifest.pluginId),
    });
    return {
      plugin_id: manifest.pluginId,
      command_names: manifest.commands.map((command) => command.name),
      command_registry_revision: receipt.registryRevisionAfter,
      command_reload_receipt_id: receipt.receiptId,
    };
  }

  private async integratePluginHooks(manifest: PluginManifest): Promise<JsonObject> {
    return {
      plugin_id: manifest.pluginId,
      hook_ids: manifest.hooks.map((hook) => hook.hookId),
      hook_count: manifest.hooks.length,
      permission_bridge_id: "e02-plugin-permission-bridge",
      fail_closed_hooks: manifest.hooks.filter((hook) => hook.failClosed).map((hook) => hook.hookId),
      source_custody: "claude-code-best:loadPluginHooks",
      atomic_swap_owner: "PluginHookRuntime.replace",
      atomic_swap_phase: "global_commit",
    };
  }

  private async integratePluginAgents(manifest: PluginManifest): Promise<JsonObject> {
    const descriptors = manifest.agents.map((agent) => canonicalize({
      ...agent,
      plugin_id: manifest.pluginId,
      plugin_version: manifest.version,
      plugin_manifest_digest: manifest.manifestDigest,
    }) as JsonObject);
    this.pluginAgents.set(manifest.pluginId, descriptors);
    return {
      plugin_id: manifest.pluginId,
      agent_ids: manifest.agents.map((agent) => agent.agentId),
      agent_count: manifest.agents.length,
      canonical_registry_owner: "typescript",
    };
  }

  private async integratePluginMcp(manifest: PluginManifest): Promise<JsonObject> {
    const layerRevision = (this.pluginMcpLayerRevisions.get(manifest.pluginId) ?? 0) + 1;
    const servers: JsonObject = {};
    for (const server of manifest.mcpServers) {
      servers[server.serverId] = {
        ...cloneJson(server.config),
        enabled: manifest.enabled && server.enabledByDefault,
        metadata: {
          ...asObject(server.config.metadata),
          plugin_id: manifest.pluginId,
          plugin_version: manifest.version,
          plugin_manifest_digest: manifest.manifestDigest,
          permission_scope: cloneJson(server.permissionScope),
        },
      };
    }
    const layerId = pluginMcpLayerId(manifest.pluginId);
    const merge = this.mcp.mergeConfig({
      layerId,
      source: "plugin",
      revision: layerRevision,
      servers,
      tombstones: [],
      metadata: {
        plugin_id: manifest.pluginId,
        plugin_version: manifest.version,
        plugin_manifest_digest: manifest.manifestDigest,
      },
    });
    this.pluginMcpLayerRevisions.set(manifest.pluginId, layerRevision);
    if (this.opened || this.mcp.health().opened === true) await this.mcp.reload();
    return {
      plugin_id: manifest.pluginId,
      layer_id: layerId,
      layer_revision: layerRevision,
      config_revision: merge.revision,
      server_ids: manifest.mcpServers.map((server) => server.serverId),
      config_digest: merge.digest,
    };
  }

  private async removePluginIntegration(pluginId: string, revision: number): Promise<void> {
    const audit = this.beginIntegrationAudit(pluginId, revision, "remove", {
      reason: "plugin_runtime_remove",
    });
    try {
      await this.skills.removePluginRoots(pluginId, revision);
      await this.commands.removePlugin(pluginId, { plugin_revision: revision });
      this.pluginAgents.delete(pluginId);
      const layerId = pluginMcpLayerId(pluginId);
      if (this.mcp.config.snapshot().layers.some((layer) => layer.layerId === layerId)) {
        this.mcp.config.removeLayer(layerId, this.mcp.config.revision);
        if (this.opened || this.mcp.health().opened === true) await this.mcp.reload();
      }
      this.pluginIntegrationRevisions.delete(pluginId);
      this.pluginMcpLayerRevisions.delete(pluginId);
      this.completeIntegrationAudit(audit.auditId, "committed", null, {
        removed_plugin_id: pluginId,
      });
    } catch (error) {
      this.completeIntegrationAudit(audit.auditId, "failed", error, {});
      throw error;
    }
  }

  private async executePluginHook(
    input: PluginHookExecutorInput,
  ): Promise<PluginHookExecutorOutput> {
    if (input.signal.aborted) throw abortError(input.signal.reason);
    const hook = input.hook;
    let declaration: JsonObject;
    if (hook.path) {
      const path = resolve(hook.path);
      assertPathWithin(input.manifest.rootPath, path, `plugin hook ${hook.hookId}`);
      const bytes = await readFile(path);
      if (bytes.byteLength > 1_048_576) {
        throw coordinatorError(
          "plugin_hook_file_too_large",
          `plugin hook ${hook.hookId} exceeds 1 MiB`,
        );
      }
      declaration = parsePluginHookDeclaration(bytes.toString("utf8"), path);
    } else if (hook.command) {
      declaration = parsePluginHookCommand(hook.command);
    } else {
      throw coordinatorError(
        "plugin_hook_handler_missing",
        `plugin hook ${input.manifest.pluginId}/${hook.hookId} has no path or command`,
      );
    }
    const expectedDigest = optionalString(declaration.expected_arguments_digest);
    if (expectedDigest && !constantTimeDigestEquals(expectedDigest, input.context.argumentsDigest)) {
      throw coordinatorError(
        "plugin_hook_expected_digest_mismatch",
        `plugin hook ${hook.hookId} expected a different argument digest`,
      );
    }
    const effect = pluginHookEffect(declaration.effect);
    let argumentsValue = cloneJson(input.context.arguments);
    if (declaration.arguments !== undefined) {
      argumentsValue = requiredObject(declaration.arguments, "plugin hook arguments");
    }
    if (declaration.patch !== undefined) {
      argumentsValue = mergeJson(
        argumentsValue,
        requiredObject(declaration.patch, "plugin hook patch"),
      );
    }
    if (Array.isArray(declaration.remove)) {
      for (const key of declaration.remove) {
        if (typeof key !== "string" || !key) {
          throw coordinatorError("plugin_hook_remove_invalid", "plugin hook remove entries must be strings");
        }
        delete argumentsValue[key];
      }
    }
    if (!hook.canMutate && digest(argumentsValue) !== input.context.argumentsDigest) {
      throw coordinatorError(
        "plugin_hook_mutation_forbidden",
        `plugin hook ${hook.hookId} attempted to mutate arguments without authorization`,
      );
    }
    return {
      effect,
      reason: optionalString(declaration.reason)
        || `${input.manifest.pluginId}/${hook.hookId} returned ${effect}`,
      arguments: argumentsValue,
      metadata: {
        plugin_id: input.manifest.pluginId,
        plugin_version: input.manifest.version,
        plugin_manifest_digest: input.manifest.manifestDigest,
        hook_id: hook.hookId,
        hook_declaration_digest: digest(declaration),
        original_arguments_digest: input.context.argumentsDigest,
        final_arguments_digest: digest(argumentsValue),
      },
    };
  }

  private async invokePluginCommand(
    record: PluginRuntimeRecord,
    commandName: string,
    argumentsValue: JsonObject,
    signal?: AbortSignal,
  ): Promise<JsonValue> {
    if (signal?.aborted) throw abortError(signal.reason);
    const command = record.manifest.commands.find((candidate) => candidate.name === commandName);
    if (!command) {
      throw coordinatorError(
        "plugin_command_not_found",
        `plugin ${record.pluginId} does not provide ${commandName}`,
      );
    }
    assertPathWithin(record.manifest.rootPath, command.path, `plugin command ${commandName}`);
    const descriptor = await this.commands.descriptor.parse({
      path: command.path,
      sourceKind: "plugin",
      sourceId: record.pluginId,
      sourcePriority: 1,
      pluginId: record.pluginId,
      overrides: {
        name: command.name,
        aliases: command.aliases,
        hidden: command.hidden,
      },
    });
    const rendered = renderPluginCommandBody(descriptor.body, argumentsValue);
    return {
      plugin_id: record.pluginId,
      plugin_revision: record.revision,
      plugin_version: record.manifest.version,
      plugin_manifest_digest: record.manifest.manifestDigest,
      command_id: descriptor.commandId,
      command_name: descriptor.name,
      descriptor_digest: descriptor.descriptorDigest,
      rendered_command: rendered,
      arguments: cloneJson(argumentsValue),
      instruction_digest: digest({
        descriptor_digest: descriptor.descriptorDigest,
        rendered,
        arguments: argumentsValue,
      }),
    };
  }

  private registerPluginPermissionBridge(): void {
    this.permission.registerHook({
      hookId: "e02-plugin-permission-bridge",
      pluginId: null,
      enabled: true,
      order: -10_000,
      toolPattern: "*",
      namespacePattern: "*",
      serverPattern: "*",
      operationPattern: "*",
      timeoutMs: 30_000,
      canMutateArguments: true,
      failClosed: true,
      metadata: {
        canonical_owner: "typescript",
        bridge_target: "PluginCoordinator.beforeTool",
      },
    }, async (context) => {
      const request = context.identity.context;
      const result = await this.plugins.beforeTool({
        runId: request.runId,
        taskId: request.taskId,
        sessionId: request.sessionId,
        sessionRevision: request.sessionRevision,
        workerRequestId: request.workerRequestId,
        toolCallId: request.toolCallId,
        toolName: request.toolName,
        namespace: request.namespace,
        serverId: request.serverId,
        operation: request.operation,
        arguments: cloneJson(request.arguments),
        argumentsDigest: context.identity.argumentsDigest,
        metadata: cloneJson(request.metadata),
      }, context.signal);
      return {
        effect: result.effect === "continue" ? "passthrough" : result.effect,
        arguments: cloneJson(result.arguments),
        reason: result.results.length
          ? result.results.map((entry) => `${entry.pluginId}/${entry.hookId}:${entry.effect}`).join(", ")
          : "no plugin hook matched",
        metadata: {
          plugin_hook_results: canonicalize(result.results),
          plugin_hook_failed_closed: result.failedClosed,
          original_arguments_digest: context.identity.argumentsDigest,
          final_arguments_digest: digest(result.arguments),
        },
      };
    });
  }

  private async recoverPendingTransitions(): Promise<void> {
    const pending = this.journal.pendingTransitions();
    for (const transition of pending) {
      const execution = this.executionForTransition(transition.transitionId);
      if (
        transition.phase === "effect_recorded"
        || transition.phase === "receipt_recorded"
      ) {
        if (!transition.effectReceipt?.ok) {
          this.journal.reject(
            transition.transitionId,
            "restored_failed_effect",
            transition.effectReceipt?.error ?? "restored transition has a failed effect receipt",
          );
          if (execution) {
            this.executionLedger.failExecution(
              execution.executionId,
              coordinatorError("restored_failed_effect", transition.effectReceipt?.error ?? "failed effect"),
            );
          }
          continue;
        }
        const resultObject = cloneJson(transition.effectReceipt.output);
        const commit = this.journal.commit({
          transitionId: transition.transitionId,
          nextState: this.domainState(transition.domain),
          output: resultObject,
          expectedRevision: transition.revisionBefore,
          metadata: {
            restored_effect_receipt: true,
            source_epoch: transition.epochPrepared,
            target_epoch: this.runtime.epoch,
          },
        });
        this.journal.acknowledge(transition.transitionId, commit.commitHash);
        if (execution) {
          const current = this.executionLedger.getExecution(execution.executionId);
          if (current?.phase === "recovery_required" || current?.phase === "executing") {
            this.executionLedger.recordEffect(execution.executionId, resultObject);
          }
          this.executionLedger.commitExecution(execution.executionId, resultObject);
        }
        this.recordRecovery(
          transition,
          execution?.executionId ?? null,
          "committed_from_effect_receipt",
          "durable effect receipt was committed without re-executing the effect",
          { commit_hash: commit.commitHash },
        );
        continue;
      }
      if (transition.phase === "prepared" && !transition.nonIdempotentEffect) {
        this.journal.cancel(
          transition.transitionId,
          "idempotent prepared transition cancelled at restart; caller may retry with a new permit",
        );
        if (execution) {
          this.executionLedger.failExecution(
            execution.executionId,
            coordinatorError("restart_before_effect", "execution restarted before its effect"),
          );
        }
        this.recordRecovery(
          transition,
          execution?.executionId ?? null,
          "cancelled_before_effect",
          "no effect was started before restart",
          {},
        );
        continue;
      }
      if (execution) {
        this.executionLedger.requireRecovery(execution.executionId, {
          kind: "indeterminate_restart_effect",
          transition_id: transition.transitionId,
          transition_phase: transition.phase,
          non_idempotent: transition.nonIdempotentEffect,
          source_epoch: transition.epochPrepared,
          target_epoch: this.runtime.epoch,
          reexecute_without_receipt: false,
        });
      }
      this.recordRecovery(
        transition,
        execution?.executionId ?? null,
        "recovery_required",
        "restart observed an effect boundary without a durable success/no-effect receipt",
        { reexecute_without_receipt: false },
      );
    }
  }

  private recordRecovery(
    transition: TransitionRecord,
    executionId: string | null,
    disposition: E02RecoveryRecord["disposition"],
    reason: string,
    metadataValue: JsonObject,
  ): E02RecoveryRecord {
    const recoveredAt = this.timestamp();
    const toolName = stringField(transition.payload, "tool_name", "unknown");
    const base = {
      transitionId: transition.transitionId,
      executionId,
      domain: transition.domain,
      toolName,
      priorPhase: transition.phase,
      disposition,
      reason,
      sourceEpoch: transition.epochPrepared,
      targetEpoch: this.runtime.epoch,
      recoveredAt,
      metadata: canonicalize(metadataValue) as JsonObject,
    };
    const recoveryId = deterministicId("e02-recovery", base, 40);
    const record: E02RecoveryRecord = {
      recoveryId,
      ...base,
      digest: digest({ recoveryId, ...base }),
    };
    this.recoveries.set(record.recoveryId, record);
    return cloneJson(record);
  }

  private executionForTransition(transitionId: string): CapabilityExecutionRecord | null {
    return this.executionLedger.listExecutions({ limit: 20_000 })
      .find((record) => record.transitionId === transitionId) ?? null;
  }

  private domainState(domain: E02Domain): JsonObject {
    if (domain === "permission") return this.permission.snapshot() as unknown as JsonObject;
    if (domain.startsWith("mcp-")) return this.mcp.snapshot() as unknown as JsonObject;
    if (domain === "skill") return this.skills.snapshot() as unknown as JsonObject;
    if (domain === "plugin") return this.plugins.snapshot() as unknown as JsonObject;
    if (domain === "command") return this.commands.snapshot() as unknown as JsonObject;
    return {};
  }

  private async checkpoint(reason: string): Promise<E02CapabilityCoordinatorSnapshot> {
    this.syncRoutes(reason);
    this.syncCustody(reason);
    this.syncRecovery(reason);
    this.snapshotSequence += 1;
    const checkpointBundle = this.commitCheckpointBundle(reason);
    const snapshot = this.snapshot();
    const checkpointHash = digest({
      reason,
      snapshot_sequence: this.snapshotSequence,
      snapshot_hash: snapshot.snapshotHash,
      prior_checkpoint_hash: this.lastCheckpointHash,
      runtime_epoch: this.runtime.epoch,
    });
    this.lastCheckpointHash = checkpointHash;
    const committed = this.snapshot();
    if (!this.ports.checkpoint) return committed;
    const entity = this.hostPorts.entityState("e02-host:checkpoint");
    const prepared = this.hostPorts.prepare({
      kind: "checkpoint-delivery",
      operation: "persist-checkpoint",
      binding: {
        runId: this.runtime.runId,
        taskId: this.runtime.taskId,
        sessionId: this.runtime.sessionId,
        workerRequestId: this.runtime.workerRequestId,
        correlationId: checkpointHash,
        entityId: entity.entityId,
        expectedRevision: entity.revision,
      },
      payload: {
        reason,
        snapshot_sequence: this.snapshotSequence,
        snapshot_hash: committed.snapshotHash,
        checkpoint_hash: checkpointHash,
      },
      idempotencyKey: `checkpoint:${checkpointHash}`,
      nonIdempotent: false,
      replayPolicy: "return-committed",
      metadata: { canonical_state_owner: "typescript" },
    });
    if (prepared.disposition === "return-committed") return this.snapshot();
    this.checkpointBundles.recordDeliveryAttempt(checkpointBundle.bundleId);
    this.hostPorts.markSent(prepared.record.portRequestId);
    try {
      const deliverySnapshot = this.snapshot();
      await this.ports.checkpoint(deliverySnapshot);
      this.hostPorts.recordReceipt(prepared.record.portRequestId, {
        ok: true,
        output: {
          accepted: true,
          checkpoint_hash: checkpointHash,
          delivered_snapshot_hash: deliverySnapshot.snapshotHash,
        },
        providerReceiptId: checkpointHash,
        metadata: { reason, host_role: "durable-checkpoint-port" },
      });
      this.hostPorts.commit(prepared.record.portRequestId);
      this.checkpointBundles.markDelivered(checkpointBundle.bundleId, {
        providerReceiptId: checkpointHash,
        deliveredSnapshotHash: deliverySnapshot.snapshotHash,
        metadata: { reason, host_port_request_id: prepared.record.portRequestId },
      });
      return this.snapshot();
    } catch (error) {
      this.hostPorts.fail(prepared.record.portRequestId, error);
      this.checkpointBundles.markDeliveryFailure(checkpointBundle.bundleId, error);
      throw error;
    }
  }

  private async publish(event: E02EventEnvelope): Promise<void> {
    if (!this.ports.emitEvent) return;
    const entity = this.hostPorts.entityState("e02-host:event-stream");
    const prepared = this.hostPorts.prepare({
      kind: "event-delivery",
      operation: "append-event",
      binding: {
        runId: this.runtime.runId,
        taskId: this.runtime.taskId,
        sessionId: this.runtime.sessionId,
        workerRequestId: this.runtime.workerRequestId,
        correlationId: event.event_id,
        entityId: entity.entityId,
        expectedRevision: entity.revision,
      },
      payload: {
        event_id: event.event_id,
        event_hash: event.event_hash,
        event_type: event.event_type,
        transition_id: event.transition_id,
        domain: event.domain,
        phase: event.phase,
        revision: event.revision,
      },
      idempotencyKey: `event:${event.event_hash}`,
      nonIdempotent: false,
      replayPolicy: "return-committed",
      metadata: { canonical_event_owner: "typescript" },
    });
    if (prepared.disposition === "return-committed") return;
    this.hostPorts.markSent(prepared.record.portRequestId);
    try {
      await this.ports.emitEvent(cloneJson(event));
      this.hostPorts.recordReceipt(prepared.record.portRequestId, {
        ok: true,
        output: { accepted: true, event_id: event.event_id, event_hash: event.event_hash },
        providerReceiptId: event.event_id,
        metadata: { host_role: "event-transport" },
      });
      this.hostPorts.commit(prepared.record.portRequestId);
    } catch (error) {
      this.hostPorts.fail(prepared.record.portRequestId, error);
      throw error;
    }
  }

  private syncCustody(reason: string): void {
    this.custody.captureAll({
      permission: this.permission.snapshot() as unknown as JsonObject,
      mcp: this.mcp.snapshot() as unknown as JsonObject,
      skill: this.skills.snapshot() as unknown as JsonObject,
      plugin: this.plugins.snapshot() as unknown as JsonObject,
      command: this.commands.snapshot() as unknown as JsonObject,
      agent: this.agents.snapshot(),
    }, reason);
  }

  private syncRecovery(reason: string): void {
    const sources = [];
    for (const transition of this.journal.pendingTransitions()) {
      sources.push({
        source: "transition" as const,
        sourceId: transition.transitionId,
        sourcePhase: transition.phase,
        summary: `transition ${transition.transitionId} is ${transition.phase}`,
        payload: canonicalize(transition) as JsonObject,
        severity: transition.nonIdempotentEffect ? "critical" as const : "high" as const,
        metadata: { reason, domain: transition.domain },
      });
    }
    for (const execution of this.executionLedger.recoveryRequired()) {
      sources.push({
        source: "execution" as const,
        sourceId: execution.executionId,
        sourcePhase: execution.phase,
        summary: `capability execution ${execution.executionId} requires reconciliation`,
        payload: canonicalize(execution) as JsonObject,
        severity: "critical" as const,
        metadata: { reason, tool_name: execution.toolName },
      });
    }
    for (const bundle of this.checkpointBundles.pendingDelivery()) {
      if (bundle.phase !== "delivery_indeterminate") continue;
      sources.push({
        source: "checkpoint" as const,
        sourceId: bundle.bundleId,
        sourcePhase: bundle.phase,
        summary: `checkpoint bundle ${bundle.bundleId} delivery is indeterminate`,
        payload: canonicalize(bundle) as JsonObject,
        severity: "medium" as const,
        metadata: { reason, bundle_hash: bundle.bundleHash },
      });
    }
    for (const control of this.controlPlane.pendingRecovery()) {
      sources.push({
        source: "control" as const,
        sourceId: control.request.requestId,
        sourcePhase: control.phase,
        summary: `control operation ${control.request.operation} requires reconciliation`,
        payload: canonicalize(control) as JsonObject,
        severity: "high" as const,
        metadata: { reason, operation: control.request.operation },
      });
    }
    this.recoveryRuntime.observeAll(sources);
  }

  private commitCheckpointBundle(reason: string) {
    const bundle = this.checkpointBundles.begin(reason, {
      snapshot_sequence: this.snapshotSequence,
      runtime_epoch: this.runtime.epoch,
      canonical_state_owner: "typescript",
    });
    const permission = this.permission.snapshot();
    const mcp = this.mcp.snapshot();
    const skill = this.skills.snapshot();
    const plugin = this.plugins.snapshot();
    const command = this.commands.snapshot();
    const agent = this.agents.snapshot();
    const route = this.routes.snapshot();
    const transition = this.journal.snapshot();
    const event = this.events.snapshot();
    const execution = this.executionLedger.snapshot();
    const hostPort = this.hostPorts.snapshot();
    const custody = this.custody.snapshot();
    this.checkpointBundles.stageAll(bundle.bundleId, {
      permission: { value: permission as unknown as JsonValue, revision: this.snapshotSequence },
      mcp: { value: mcp as unknown as JsonValue, revision: this.snapshotSequence },
      skill: { value: skill as unknown as JsonValue, revision: this.snapshotSequence },
      plugin: { value: plugin as unknown as JsonValue, revision: this.snapshotSequence },
      command: { value: command as unknown as JsonValue, revision: this.snapshotSequence },
      agent: { value: agent, revision: this.snapshotSequence },
      route: { value: route as unknown as JsonValue, revision: route.revision },
      transition: { value: transition as unknown as JsonValue, revision: this.snapshotSequence },
      event: { value: event as unknown as JsonValue, revision: this.snapshotSequence },
      execution: { value: execution as unknown as JsonValue, revision: this.snapshotSequence },
      "host-port": { value: hostPort as unknown as JsonValue, revision: this.snapshotSequence },
      custody: { value: custody as unknown as JsonValue, revision: custody.sequence },
    });
    this.checkpointBundles.seal(bundle.bundleId);
    return this.checkpointBundles.commit(bundle.bundleId);
  }

  private syncRoutes(reason: string): void {
    const candidates: Array<{
      spec: ToolSpecContract;
      domain: E02RouteDomain;
      owner: string;
      sourceId: string;
      sourceRevision: string;
      metadata: JsonObject;
    }> = [];
    const append = (
      specs: ToolSpecContract[],
      domain: E02RouteDomain,
      owner: string,
      sourceId: string,
      sourceRevision: string,
    ): void => {
      for (const spec of specs) {
        candidates.push({
          spec,
          domain,
          owner,
          sourceId,
          sourceRevision,
          metadata: {
            canonical_state_owner: "typescript",
            coordinator: "E02CapabilityCoordinator.execute",
          },
        });
      }
    };
    append(this.mcp.toolSpecs(), "mcp", "typescript-mcp", "McpRuntimeCoordinator", String(this.mcp.config.revision));
    append(this.skills.toolSpecs(), "skill", "typescript-skill", "SkillCoordinator", String(this.skills.snapshot().registry.revision));
    append(this.plugins.toolSpecs(), "plugin", "typescript-plugin", "PluginCoordinator", String(this.plugins.snapshot().capabilities.revision));
    append(this.commands.toolSpecs(), "command", "typescript-command", "CommandCoordinator", String(this.commands.snapshot().registry.revision));
    append(this.agents.toolSpecs(), "agent", "typescript-agent", "TypeScriptAgentRuntime", String(this.runtime.epoch));
    append(this.rawToolSpecs().filter((spec) => E02_CONTROL_TOOLS.has(spec.name)), "control", "typescript-e02-control", "E02CapabilityCoordinator", String(this.runtime.epoch));
    this.routes.reconcile(candidates, {
      reason,
      runtime_epoch: this.runtime.epoch,
      python_route_fallback: false,
    });
  }

  private executionContext(
    toolName: string,
    value: E02ExecutionContext,
  ): RequiredExecutionContext {
    const runId = value.runId ?? this.runtime.runId;
    const taskId = value.taskId ?? this.runtime.taskId;
    const sessionId = value.sessionId ?? this.runtime.sessionId;
    const workerRequestId = value.workerRequestId ?? this.runtime.workerRequestId;
    if (
      runId !== this.runtime.runId
      || taskId !== this.runtime.taskId
      || sessionId !== this.runtime.sessionId
      || workerRequestId !== this.runtime.workerRequestId
    ) {
      throw coordinatorError(
        "e02_execution_binding_mismatch",
        "capability execution binding differs from the coordinator runtime",
      );
    }
    return {
      runId,
      taskId,
      sessionId,
      sessionRevision: value.sessionRevision ?? inputSessionRevision(this.input),
      workerRequestId,
      toolCallId: value.toolCallId,
      namespace: value.namespace ?? inferNamespace(toolName),
      serverId: value.serverId ?? inferServerId(toolName),
      commandName: value.commandName ?? "",
      resourceUri: value.resourceUri ?? "",
      operation: value.operation ?? inferOperation(toolName, this.toolSpecs().find((tool) => tool.name === toolName)),
      permitId: value.permitId ?? null,
      metadata: canonicalize(value.metadata ?? {}) as JsonObject,
      signal: value.signal,
      agentContext: value.agentContext,
    };
  }

  private validateSnapshot(snapshot: E02CapabilityCoordinatorSnapshot): void {
    if (snapshot.version !== "zyra.e02-runtime/v1") {
      throw coordinatorError(
        "unsupported_e02_snapshot",
        `unsupported E02 snapshot ${snapshot.version}`,
      );
    }
    const { snapshotHash: expectedHash, ...withoutHash } = snapshot;
    if (!constantTimeDigestEquals(digest(withoutHash), expectedHash)) {
      throw coordinatorError(
        "e02_snapshot_digest_mismatch",
        "E02 snapshot digest does not match its payload",
      );
    }
    if (
      snapshot.runtime.runId !== this.runtime.runId
      || snapshot.runtime.taskId !== this.runtime.taskId
      || snapshot.runtime.sessionId !== this.runtime.sessionId
      || snapshot.runtime.workerRequestId !== this.runtime.workerRequestId
    ) {
      throw coordinatorError(
        "e02_snapshot_binding_mismatch",
        "E02 snapshot belongs to another run/task/session/worker binding",
      );
    }
    if (resolve(snapshot.workspaceRoot) !== this.workspaceRoot) {
      throw coordinatorError(
        "e02_snapshot_workspace_mismatch",
        "E02 snapshot belongs to another workspace",
      );
    }
    if (this.runtime.epoch <= snapshot.runtime.epoch) {
      throw coordinatorError(
        "e02_snapshot_epoch_not_advanced",
        "E02 restore target epoch must advance the snapshot epoch",
      );
    }
  }

  private nextPluginIntegrationRevision(pluginId: string): number {
    const revision = (this.pluginIntegrationRevisions.get(pluginId) ?? 0) + 1;
    this.pluginIntegrationRevisions.set(pluginId, revision);
    return revision;
  }

  private currentPluginIntegrationRevision(pluginId: string): number {
    return this.pluginIntegrationRevisions.get(pluginId) ?? this.nextPluginIntegrationRevision(pluginId);
  }

  private beginIntegrationAudit(
    pluginId: string,
    pluginRevision: number,
    operation: string,
    metadataValue: JsonObject,
  ): E02IntegrationAudit {
    const startedAt = this.timestamp();
    const base = {
      pluginId,
      pluginRevision,
      operation,
      startedAt,
      runtimeEpoch: this.runtime.epoch,
    };
    const auditId = deterministicId("e02-plugin-integration", base, 40);
    const audit: E02IntegrationAudit = {
      auditId,
      pluginId,
      pluginRevision,
      operation,
      status: "prepared",
      affectedSkills: [],
      affectedCommands: [],
      affectedMcpServers: [],
      affectedHooks: [],
      startedAt,
      completedAt: null,
      error: null,
      metadata: canonicalize(metadataValue) as JsonObject,
      digest: "",
    };
    audit.digest = integrationAuditDigest(audit);
    this.integrationAudit.set(audit.auditId, audit);
    return cloneJson(audit);
  }

  private completeIntegrationAudit(
    auditId: string,
    status: E02IntegrationAudit["status"],
    error: unknown,
    metadataValue: JsonObject,
  ): E02IntegrationAudit {
    const audit = this.integrationAudit.get(auditId);
    if (!audit) throw coordinatorError("integration_audit_not_found", `integration audit ${auditId} was not found`);
    audit.status = status;
    audit.completedAt = this.timestamp();
    audit.error = error ? errorObject(error) : null;
    audit.metadata = {
      ...audit.metadata,
      ...canonicalize(metadataValue) as JsonObject,
    };
    audit.digest = integrationAuditDigest(audit);
    return cloneJson(audit);
  }

  private recordIntegrationFailure(
    pluginId: string,
    pluginRevision: number,
    operation: string,
    error: unknown,
    metadataValue: JsonObject,
  ): E02IntegrationAudit {
    const audit = this.beginIntegrationAudit(pluginId, pluginRevision, operation, metadataValue);
    return this.completeIntegrationAudit(audit.auditId, "failed", error, {});
  }

  private requireOpen(): void {
    if (!this.opened || this.closing) {
      throw coordinatorError(
        this.closing ? "e02_runtime_closing" : "e02_runtime_not_open",
        this.closing ? "E02 runtime is closing" : "E02 runtime must restore and open before use",
      );
    }
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

interface RequiredExecutionContext {
  runId: string;
  taskId: string;
  sessionId: string;
  sessionRevision: number;
  workerRequestId: string;
  toolCallId: string;
  namespace: string;
  serverId: string;
  commandName: string;
  resourceUri: string;
  operation: string;
  permitId: string | null;
  metadata: JsonObject;
  signal?: AbortSignal;
  agentContext?: AgentExecutionContext;
}

type E02RestoreDisposition = "none" | "restored" | "prior_worker_request";

interface E02RestoreSelection {
  snapshot: E02CapabilityCoordinatorSnapshot | null;
  disposition: E02RestoreDisposition;
}

function selectRestoredE02Snapshot(
  value: JsonObject | null | undefined,
  input: RuntimeRunInput,
): E02RestoreSelection {
  const root = asObject(value);
  const visited = new Set<JsonObject>();
  const queue: JsonObject[] = [root];
  const preferredKeys = [
    "e02",
    "e02_runtime",
    "typescriptCapabilities",
    "typescript_capabilities",
    "typescript_runtime_snapshot",
    "typescript_runtime",
    "query_engine",
    "sessionSnapshot",
    "session_snapshot",
    "metadata",
  ];
  while (queue.length) {
    const candidate = queue.shift()!;
    if (visited.has(candidate)) continue;
    visited.add(candidate);
    if (candidate.version === "zyra.e02-runtime/v1") {
      const snapshot = candidate as unknown as E02CapabilityCoordinatorSnapshot;
      if (
        snapshot.runtime?.runId === input.runId
        && snapshot.runtime.taskId === input.taskId
        && snapshot.runtime.sessionId === input.sessionId
        && snapshot.runtime.workerRequestId !== input.workerRequestId
      ) {
        // E02 permits, leases, continuations, and idempotency records are bound to
        // one worker request. A later request in the same durable session starts a
        // new E02 epoch instead of rebinding those request-scoped authorities.
        return { snapshot: null, disposition: "prior_worker_request" };
      }
      return { snapshot, disposition: "restored" };
    }
    for (const key of preferredKeys) {
      const child = asObject(candidate[key]);
      if (Object.keys(child).length) queue.push(child);
    }
    for (const childValue of Object.values(candidate)) {
      const child = asObject(childValue);
      if (Object.keys(child).length && !visited.has(child)) queue.push(child);
    }
  }
  return { snapshot: null, disposition: "none" };
}

function runtimeIdentity(
  input: RuntimeRunInput,
  snapshot: E02CapabilityCoordinatorSnapshot | null,
): E02RuntimeIdentity {
  const constraints = asObject(input.config.runtimeConstraints);
  const configuredEpoch = nonNegativeInteger(
    constraints.e02Epoch ?? constraints.e02_epoch,
    1,
  );
  const epoch = snapshot ? snapshot.runtime.epoch + 1 : Math.max(1, configuredEpoch);
  return {
    runtimeId: snapshot?.runtime.runtimeId ?? deterministicId("zyra-e02-runtime", {
      run_id: input.runId,
      task_id: input.taskId,
      session_id: input.sessionId,
      worker_request_id: input.workerRequestId,
    }, 40),
    runId: input.runId,
    taskId: input.taskId,
    sessionId: input.sessionId,
    workerRequestId: input.workerRequestId,
    epoch,
  };
}

function inputSessionRevision(input: RuntimeRunInput): number {
  const metadata = asObject(input.metadata);
  const context = asObject(input.contextSnapshot);
  const constraints = asObject(input.config.runtimeConstraints);
  for (const value of [
    metadata.session_revision,
    metadata.sessionRevision,
    context.session_revision,
    context.sessionRevision,
    constraints.session_revision,
    constraints.sessionRevision,
  ]) {
    if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) return value;
  }
  return 0;
}

function workspaceRoot(input: RuntimeRunInput): string {
  const constraints = asObject(input.config.runtimeConstraints);
  const root = asString(constraints.workspaceRoot ?? constraints.workspace_root)
    || asString(constraints.projectRoot ?? constraints.project_root)
    || process.cwd();
  return resolve(root);
}

function permissionMode(value: JsonValue | undefined): PermissionMode {
  const aliases: Record<string, PermissionMode> = {
    default: "default",
    acceptedits: "acceptEdits",
    accept_edits: "acceptEdits",
    dontask: "dontAsk",
    dont_ask: "dontAsk",
    plan: "plan",
    bypass: "bypassPermissions",
    bypasspermissions: "bypassPermissions",
    bypass_permissions: "bypassPermissions",
    auto: "auto",
    sealed: "sealed",
  };
  const normalized = typeof value === "string" ? value.replace(/[\s-]/g, "").toLowerCase() : "default";
  return aliases[normalized] ?? "default";
}

function permissionRules(policy: JsonObject): Array<string | JsonObject> {
  const rules = Array.isArray(policy.rules) ? policy.rules : [];
  const normalized = rules
    .filter((rule): rule is string | JsonObject =>
      typeof rule === "string"
      || Boolean(rule && typeof rule === "object" && !Array.isArray(rule)))
    .map((rule) => typeof rule === "string" ? rule : canonicalize(rule) as JsonObject);
  if (!normalized.length) {
    const effect = policy.default_effect ?? policy.defaultEffect;
    if (effect === "allow" || effect === "deny" || effect === "ask") {
      normalized.push({
        ruleId: "e02-explicit-default-effect",
        effect,
        source: "policy",
        kind: "tool",
        toolPattern: "*",
        priority: -1_000_000,
        reason: `explicit permission policy default_effect=${effect}`,
      });
    }
  }
  return normalized;
}

function skillRoots(input: RuntimeRunInput, workspace: string): SkillSourceRoot[] {
  const constraints = asObject(input.config.runtimeConstraints);
  const configured = constraints.typescriptSkillRoots ?? constraints.typescript_skill_roots;
  const parsed = parseSkillRootValues(configured, workspace);
  if (parsed.length) return parsed;
  const defaults: Array<[string, string, number]> = [
    ["managed-skills", resolve(workspace, ".zyra", "skills"), 900],
    ["project-skills", resolve(workspace, "skills"), 700],
    ["claude-skills", resolve(workspace, ".claude", "skills"), 650],
  ];
  return defaults.map(([sourceId, rootPath, priority]) => ({
    sourceId,
    kind: sourceId === "managed-skills" ? "managed" : "project",
    rootPath,
    priority,
    enabled: existsSync(rootPath),
    recursive: true,
    followSymlinks: false,
    maximumDepth: 12,
    includePatterns: ["**/SKILL.md", "SKILL.md", "**/skill.md", "skill.md"],
    excludePatterns: ["**/node_modules/**", "**/.git/**", "**/.cache/**"],
    pluginId: null,
    revision: 0,
    metadata: { default_root: true },
  } as SkillSourceRoot));
}

function parseSkillRootValues(value: JsonValue | undefined, workspace: string): SkillSourceRoot[] {
  if (!Array.isArray(value)) return [];
  const output: SkillSourceRoot[] = [];
  for (const [index, item] of value.entries()) {
    if (typeof item === "string") {
      const rootPath = resolve(workspace, item);
      output.push({
        sourceId: `configured-skill-${index + 1}`,
        kind: "session",
        rootPath,
        priority: 800 - index,
        enabled: existsSync(rootPath),
        recursive: true,
        followSymlinks: false,
        maximumDepth: 12,
        includePatterns: ["**/SKILL.md", "SKILL.md"],
        excludePatterns: ["**/node_modules/**", "**/.git/**"],
        pluginId: null,
        revision: 0,
        metadata: { configured: true },
      });
      continue;
    }
    const object = asObject(item);
    const rootPath = resolve(workspace, asString(object.rootPath ?? object.root_path ?? object.path));
    output.push({
      sourceId: normalizeIdentifier(
        asString(object.sourceId ?? object.source_id) || `configured-skill-${index + 1}`,
        "skill source id",
      ),
      kind: skillSourceKind(object.kind),
      rootPath,
      priority: signedInteger(object.priority, 800 - index),
      enabled: object.enabled !== false && existsSync(rootPath),
      recursive: object.recursive !== false,
      followSymlinks: object.followSymlinks === true || object.follow_symlinks === true,
      maximumDepth: nonNegativeInteger(object.maximumDepth ?? object.maximum_depth, 12),
      includePatterns: stringArray(object.includePatterns ?? object.include_patterns, ["**/SKILL.md", "SKILL.md"]),
      excludePatterns: stringArray(object.excludePatterns ?? object.exclude_patterns, ["**/node_modules/**", "**/.git/**"]),
      pluginId: optionalString(object.pluginId ?? object.plugin_id) || null,
      revision: nonNegativeInteger(object.revision, 0),
      metadata: asObject(object.metadata),
    });
  }
  return output;
}

function defaultE02CommandDescriptors(): CommandDescriptor[] {
  const parser = new CommandDescriptorRuntime();
  const descriptor = (
    name: string,
    handlerId: string,
    description: string,
    options: {
      risk?: "low" | "medium" | "high" | "critical";
      operation?: string;
      arguments?: JsonValue[];
      commandOptions?: JsonValue[];
      networkAccess?: boolean;
    } = {},
  ): CommandDescriptor => parser.parseText({
    text: "",
    path: null,
    sourceKind: "builtin",
    sourceId: "zyra-e02",
    sourcePriority: 1_000,
    overrides: {
      id: `zyra-e02:${name}`,
      name,
      display_name: `/${name}`,
      description,
      usage: `/${name}`,
      category: "e02-runtime",
      arguments: options.arguments ?? [],
      options: options.commandOptions ?? [],
      handler: {
        kind: "builtin",
        id: handlerId,
      },
      permission: {
        // Built-in projections are genuine read capabilities.  Keeping the
        // operation aligned with the outer `command` tool's read binding lets
        // PermissionCoordinator deterministically authorize both layers while
        // mutations (notably e02-reload) retain their explicit high-risk op.
        operation: options.operation ?? "read",
        risk: options.risk ?? "low",
        ask_in_interactive: (options.risk ?? "low") !== "low",
        deny_in_sealed: (options.risk ?? "low") === "high" || (options.risk ?? "low") === "critical",
        network_access: options.networkAccess === true,
        workspace_mutation: false,
        process_execution: false,
        scope: {
          canonical_owner: "typescript.CommandCoordinator",
          python_dispatch: false,
        },
      },
      metadata: {
        canonical_owner: "typescript.CommandCoordinator",
        canonical_entrypoint: "E02CapabilityCoordinator.execute",
        python_dispatch: false,
      },
    },
  });
  const optionalAction: JsonValue[] = [{
    name: "action",
    description: "projection or owner lifecycle action",
    required: false,
    positional: true,
    type: "string",
    default: "health",
  }];
  const optionalTarget = (name: string): JsonValue => ({
    name,
    description: `optional canonical ${name} identity`,
    required: false,
    positional: true,
    type: "string",
  });
  const commandOption = (
    name: string,
    type: "string" | "integer" | "json" = "string",
  ): JsonValue => ({
    name,
    description: `${name} owner binding`,
    type,
    required: false,
  });
  return [
    descriptor("e02-health", "builtin:e02-health", "Inspect TypeScript E02 runtime health."),
    descriptor("e02-reload", "builtin:e02-reload", "Atomically reload MCP, skills, commands, and plugins.", {
      risk: "high",
      operation: "runtime/reload",
      networkAccess: true,
    }),
    descriptor("mcp", "builtin:e02-mcp", "Inspect TypeScript-owned MCP capabilities.", {
      arguments: [...optionalAction, optionalTarget("server")],
      commandOptions: [
        commandOption("request"),
        commandOption("response", "json"),
        commandOption("reason"),
        commandOption("expected-revision", "integer"),
        commandOption("provider"),
        commandOption("nonce"),
        commandOption("idempotency-key"),
      ],
      networkAccess: true,
    }),
    descriptor("skills", "builtin:e02-skills", "Inspect or execute TypeScript-owned skills.", {
      arguments: optionalAction,
      commandOptions: [
        commandOption("skill"),
        commandOption("expected-hash"),
        commandOption("expected-revision", "integer"),
        commandOption("dependency-digest"),
        commandOption("supply-digest"),
        commandOption("approval-id"),
        commandOption("registry-revision", "integer"),
        commandOption("descriptor-digest"),
        commandOption("content-hash"),
        commandOption("arguments-json", "json"),
        commandOption("arguments-digest"),
        commandOption("nonce"),
        commandOption("idempotency-key"),
      ],
    }),
    descriptor("plugins", "builtin:e02-plugins", "Inspect TypeScript-owned plugins."),
    descriptor("permissions", "builtin:e02-permissions", "Inspect TypeScript permission state."),
    descriptor("tools", "builtin:e02-tools", "Inspect the canonical TypeScript tool registry."),
    descriptor("help", "builtin:e02-help", "List TypeScript-owned commands."),
  ];
}

function commandRoots(input: RuntimeRunInput, workspace: string): CommandSourceRoot[] {
  const constraints = asObject(input.config.runtimeConstraints);
  const configured = constraints.typescriptCommandRoots ?? constraints.typescript_command_roots;
  const parsed = parseCommandRootValues(configured, workspace);
  if (parsed.length) return parsed;
  return [
    commandRoot("managed-commands", resolve(workspace, ".zyra", "commands"), "managed", 900),
    commandRoot("project-commands", resolve(workspace, "commands"), "project", 700),
    commandRoot("claude-commands", resolve(workspace, ".claude", "commands"), "project", 650),
  ];
}

function parseCommandRootValues(value: JsonValue | undefined, workspace: string): CommandSourceRoot[] {
  if (!Array.isArray(value)) return [];
  return value.map((item, index) => {
    const object = typeof item === "string" ? { path: item } : asObject(item);
    const rootPath = resolve(workspace, asString(object.rootPath ?? object.root_path ?? object.path));
    return {
      rootId: normalizeIdentifier(asString(object.rootId ?? object.root_id) || `configured-command-${index + 1}`),
      rootPath,
      sourceKind: commandSourceKind(object.sourceKind ?? object.source_kind),
      sourcePriority: signedInteger(object.sourcePriority ?? object.source_priority ?? object.priority, 800 - index),
      required: object.required === true,
      recursive: object.recursive !== false,
      maximumDepth: nonNegativeInteger(object.maximumDepth ?? object.maximum_depth, 12),
      extensions: stringArray(object.extensions, [".md", ".mdx"]),
      enabled: object.enabled !== false && existsSync(rootPath),
      metadata: asObject(object.metadata),
    };
  });
}

function commandRoot(
  rootId: string,
  rootPath: string,
  sourceKind: CommandSourceRoot["sourceKind"],
  sourcePriority: number,
): CommandSourceRoot {
  return {
    rootId,
    rootPath,
    sourceKind,
    sourcePriority,
    required: false,
    recursive: true,
    maximumDepth: 12,
    extensions: [".md", ".mdx"],
    enabled: existsSync(rootPath),
    metadata: { default_root: true },
  };
}

function pluginRoots(input: RuntimeRunInput, workspace: string): PluginSourceRoot[] {
  const constraints = asObject(input.config.runtimeConstraints);
  const configured = constraints.typescriptPluginRoots ?? constraints.typescript_plugin_roots;
  const parsed = parsePluginRootValues(configured, workspace);
  if (parsed.length) return parsed;
  return [
    pluginRoot("managed-plugins", resolve(workspace, ".zyra", "plugins"), "managed", 900),
    pluginRoot("project-plugins", resolve(workspace, "plugins"), "project", 700),
    pluginRoot("claude-plugins", resolve(workspace, ".claude", "plugins"), "project", 650),
  ];
}

function parsePluginRootValues(value: JsonValue | undefined, workspace: string): PluginSourceRoot[] {
  if (!Array.isArray(value)) return [];
  return value.map((item, index) => {
    const object = typeof item === "string" ? { path: item } : asObject(item);
    const rootPath = resolve(workspace, asString(object.rootPath ?? object.root_path ?? object.path));
    return {
      rootId: normalizeIdentifier(asString(object.rootId ?? object.root_id) || `configured-plugin-${index + 1}`),
      rootPath,
      sourceKind: pluginSourceKind(object.sourceKind ?? object.source_kind),
      priority: signedInteger(object.priority, 800 - index),
      required: object.required === true,
      recursive: object.recursive !== false,
      maximumDepth: nonNegativeInteger(object.maximumDepth ?? object.maximum_depth, 8),
      enabled: object.enabled !== false && existsSync(rootPath),
      metadata: asObject(object.metadata),
    };
  });
}

function pluginRoot(
  rootId: string,
  rootPath: string,
  sourceKind: PluginSourceRoot["sourceKind"],
  priority: number,
): PluginSourceRoot {
  return {
    rootId,
    rootPath,
    sourceKind,
    priority,
    required: false,
    recursive: true,
    maximumDepth: 8,
    enabled: existsSync(rootPath),
    metadata: { default_root: true },
  };
}

function initialMcpLayer(input: RuntimeRunInput): JsonObject | null {
  const constraints = asObject(input.config.runtimeConstraints);
  const value = constraints.typescriptMcpServers ?? constraints.typescript_mcp_servers;
  if (value === undefined || value === null) return null;
  const servers: JsonObject = {};
  if (Array.isArray(value)) {
    for (const [index, item] of value.entries()) {
      const server = asObject(item);
      const serverId = asString(server.serverId ?? server.server_id ?? server.name)
        || `configured-mcp-${index + 1}`;
      servers[serverId] = cloneJson(server);
    }
  } else {
    for (const [serverId, serverValue] of Object.entries(asObject(value))) {
      const server = asObject(serverValue);
      if (Object.keys(server).length) servers[serverId] = cloneJson(server);
    }
  }
  if (!Object.keys(servers).length) return null;
  return {
    layerId: "e02-session-mcp-config",
    source: "session",
    revision: 1,
    servers,
    tombstones: [],
    metadata: {
      runtime_id: input.workerRequestId,
      canonical_owner: "typescript",
    },
  };
}

function capabilityDomain(toolName: string, runtime: E02CapabilityCoordinator): E02Domain {
  if (runtime.mcp.owns(toolName)) {
    if (/oauth|auth/i.test(toolName)) return "mcp-auth";
    if (/resource|prompt|instruction|tool/i.test(toolName)) return "mcp-capability";
    if (/elicitation/i.test(toolName)) return "mcp-elicitation";
    if (/task/i.test(toolName)) return "mcp-task";
    return "mcp-request";
  }
  if (runtime.skills.owns(toolName)) return "skill";
  if (runtime.plugins.owns(toolName)) return "plugin";
  return "command";
}

function capabilityCustody(
  toolName: string,
  runtime: E02CapabilityCoordinator,
): [E02CustodyDomain, E02CustodyOwner] {
  if (runtime.mcp.owns(toolName)) return ["mcp", "McpRuntimeCoordinator"];
  if (runtime.skills.owns(toolName)) return ["skill", "SkillCoordinator"];
  if (runtime.plugins.owns(toolName)) return ["plugin", "PluginCoordinator"];
  if (runtime.commands.owns(toolName)) return ["command", "CommandCoordinator"];
  if (runtime.agents.owns(toolName)) return ["agent", "TypeScriptAgentRuntime"];
  return ["command", "CommandCoordinator"];
}

function routeCustody(
  domain: E02RouteDomain,
  selectedOwner: string,
): [E02CustodyDomain, E02CustodyOwner] {
  if (!selectedOwner.startsWith("typescript-")) {
    throw coordinatorError(
      "e02_route_owner_not_typescript",
      `route owner ${selectedOwner} is outside the TypeScript custody boundary`,
    );
  }
  if (domain === "mcp") return ["mcp", "McpRuntimeCoordinator"];
  if (domain === "skill") return ["skill", "SkillCoordinator"];
  if (domain === "plugin") return ["plugin", "PluginCoordinator"];
  if (domain === "command") return ["command", "CommandCoordinator"];
  if (domain === "agent") return ["agent", "TypeScriptAgentRuntime"];
  return ["command", "CommandCoordinator"];
}

function capabilityIdempotencyKey(
  toolName: string,
  argumentsValue: JsonObject,
  context: RequiredExecutionContext,
): string {
  const explicit = optionalString(argumentsValue.idempotency_key)
    || optionalString(context.metadata.idempotency_key);
  return explicit || deterministicId("e02-capability-idempotency", {
    run_id: context.runId,
    task_id: context.taskId,
    session_id: context.sessionId,
    session_revision: context.sessionRevision,
    worker_request_id: context.workerRequestId,
    tool_call_id: context.toolCallId,
    tool_name: toolName,
    arguments_digest: digest(argumentsValue),
  }, 48);
}

function capabilityResultObject(result: E02CapabilityResult): JsonObject {
  return canonicalize({
    summary: result.summary,
    output: result.output,
    metadata: result.metadata,
    context_delta: result.contextDelta ?? null,
    tool_scope_delta: result.toolScopeDelta ?? null,
    events: result.events ?? [],
  }) as JsonObject;
}

function parseCapabilityResult(value: JsonObject): E02CapabilityResult {
  return {
    summary: stringField(value, "summary", "E02 capability completed"),
    output: asObject(value.output),
    metadata: stringRecord(value.metadata),
    ...(Object.keys(asObject(value.context_delta)).length
      ? { contextDelta: asObject(value.context_delta) }
      : {}),
    ...(Object.keys(asObject(value.tool_scope_delta)).length
      ? { toolScopeDelta: asObject(value.tool_scope_delta) }
      : {}),
  };
}

function replayReceipt(
  record: CapabilityExecutionRecord,
  snapshotHash: string,
): E02ExecutionReceipt {
  if (!record.result || !record.transitionId) {
    throw coordinatorError(
      "e02_committed_execution_incomplete",
      `committed execution ${record.executionId} lacks result or transition binding`,
    );
  }
  const result = parseCapabilityResult(record.result);
  const outputHash = digest(record.result);
  const commit: CommitReceipt = {
    transitionId: record.transitionId,
    entityId: `replayed:${record.domain}:${record.toolName}`,
    domain: record.domain,
    operation: "replay",
    revisionBefore: 0,
    revisionAfter: 0,
    stateHashBefore: digest({ replay: true }),
    stateHashAfter: digest({ replay: true }),
    payloadHash: record.argumentsDigest,
    effectReceiptHash: record.resultDigest,
    outputHash,
    output: cloneJson(record.result),
    committedAt: record.completedAt ?? record.updatedAt,
    commitHash: digest({
      replayed_execution_id: record.executionId,
      transition_id: record.transitionId,
      result_digest: record.resultDigest,
    }),
  };
  return {
    executionId: record.executionId,
    transitionId: record.transitionId,
    owner: record.owner,
    result,
    commit,
    replayed: true,
    permitId: record.permitId,
    snapshotHash,
  };
}

function capabilityResult(
  summary: string,
  output: JsonObject,
  metadataValue: Record<string, string> = {},
): E02CapabilityResult {
  return {
    summary,
    output: canonicalize(output) as JsonObject,
    metadata: {
      canonical_runtime_owner: "typescript",
      python_capability_fallback: "false",
      ...metadataValue,
    },
  };
}

function e02Tool(
  name: string,
  purpose: string,
  properties: JsonObject,
  accessMode: string,
): ToolSpecContract {
  return {
    name,
    purpose,
    source: "typescript-e02-control",
    input_schema: { type: "object", properties },
    output_schema: { type: "object" },
    metadata: {
      access_mode: accessMode,
      canonical_runtime_owner: "typescript",
    },
    execution_provenance: {
      namespace: "e02",
      version: "1",
      source: "E02CapabilityCoordinator",
    },
  };
}

function isLegacyCapabilityTool(tool: ToolSpecContract): boolean {
  const provenance = asObject(tool.execution_provenance);
  const namespace = asString(provenance.namespace);
  return tool.name.startsWith("mcp__")
    || tool.name.startsWith("mcp_")
    || namespace === "mcp"
    || namespace === "skill"
    || namespace === "plugin"
    || namespace === "command"
    || namespace === "agent"
    || tool.source.includes("python-mcp")
    || tool.source.includes("python-skill")
    || tool.source.includes("legacy");
}

function commandNameFromArguments(argumentsValue: JsonObject): string {
  const explicit = asString(argumentsValue.command).trim();
  if (explicit) return explicit.replace(/^\//, "");
  const input = asString(argumentsValue.input).trim();
  if (!input.startsWith("/")) return "";
  return input.slice(1).split(/\s+/, 1)[0] ?? "";
}

type McpLifecycleAction =
  | "enable"
  | "disable"
  | "reconnect"
  | "refresh"
  | "auth-refresh"
  | "elicit";

function isMcpLifecycleAction(value: string): value is McpLifecycleAction {
  return value === "enable"
    || value === "disable"
    || value === "reconnect"
    || value === "refresh"
    || value === "auth-refresh"
    || value === "elicit";
}

function commandActionFromArguments(argumentsValue: JsonObject): string {
  const explicit = asString(argumentsValue.action).trim();
  if (explicit) return explicit.toLowerCase();
  const structured = structuredCommandArgumentOverrides(argumentsValue);
  const structuredAction = asString(structured.action).trim();
  if (structuredAction) return structuredAction.toLowerCase();
  if (Array.isArray(argumentsValue.arguments)) {
    const first = argumentsValue.arguments[0];
    if (typeof first === "string" && first.trim()) return first.trim().toLowerCase();
  }
  const input = asString(argumentsValue.input).trim().replace(/^\//, "");
  const match = input.match(/^\S+(?:\s+("[^"]*"|'[^']*'|\S+))?/);
  return (match?.[1] ?? "").replace(/^['"]|['"]$/g, "").trim().toLowerCase();
}

function effectiveCommandOperation(
  descriptor: CommandDescriptor,
  actionValue: string,
): string {
  const action = actionValue.trim().toLowerCase();
  if (descriptor.name === "mcp") {
    return isMcpLifecycleAction(action)
      ? `mcp.lifecycle.${action}`
      : "read";
  }
  if (descriptor.name === "skills") {
    if (action === "update") return "skill.update";
    if (action === "invoke") return "skill.invoke";
    return "read";
  }
  return descriptor.permission.operation;
}

function structuredCommandArgumentOverrides(argumentsValue: JsonObject): JsonObject {
  const output: JsonObject = {};
  const merge = (value: JsonObject): void => {
    const nestedOptions = asObject(value.options);
    for (const [key, child] of Object.entries(nestedOptions)) output[key] = cloneJson(child);
    for (const [key, child] of Object.entries(value)) {
      if ([
        "options",
        "raw",
        "text",
        "input",
        "display",
        "display_value",
        "sealed",
        "competition_mode",
      ].includes(key)) continue;
      output[key] = cloneJson(child);
    }
  };
  if (
    argumentsValue.arguments
    && typeof argumentsValue.arguments === "object"
    && !Array.isArray(argumentsValue.arguments)
  ) {
    merge(argumentsValue.arguments as JsonObject);
  }
  merge(asObject(argumentsValue.argument_overrides));
  merge(asObject(argumentsValue.structured_arguments));
  return canonicalize(output) as JsonObject;
}

function durableCommandArguments(
  argumentsValue: JsonObject,
  overrides: JsonObject,
): JsonObject {
  if (!Object.keys(overrides).length) return cloneJson(argumentsValue);
  const durableOverrides: JsonObject = {};
  for (const [key, value] of Object.entries(overrides)) {
    durableOverrides[key] = key === "response"
      ? {
        redacted: true,
        digest: digest(value),
        object: Boolean(value && typeof value === "object" && !Array.isArray(value)),
      }
      : cloneJson(value);
  }
  const output = cloneJson(argumentsValue);
  if (
    output.arguments
    && typeof output.arguments === "object"
    && !Array.isArray(output.arguments)
  ) {
    output.arguments = durableOverrides;
  }
  if (Object.keys(asObject(output.argument_overrides)).length) {
    output.argument_overrides = durableOverrides;
  }
  if (Object.keys(asObject(output.structured_arguments)).length) {
    output.structured_arguments = durableOverrides;
  }
  return output;
}

function commandExecutionArguments(
  argumentsValue: JsonObject,
  overrides: JsonObject,
): JsonObject {
  if (!Object.keys(overrides).length) return cloneJson(argumentsValue);
  const commandName = commandNameFromArguments(argumentsValue);
  const action = commandActionFromArguments(argumentsValue);
  const target = commandTargetFromArguments(argumentsValue, commandName);
  const options = mergeJson(asObject(argumentsValue.options), overrides);
  const response = asObject(options.response);
  if (commandName === "mcp" && !options.request && asString(response.request_id)) {
    options.request = asString(response.request_id);
  }
  return {
    ...cloneJson(argumentsValue),
    command: commandName,
    input: "",
    arguments: [
      ...(action ? [action] : []),
      ...(target ? [target] : []),
    ],
    options,
  };
}

function commandTargetFromArguments(
  argumentsValue: JsonObject,
  commandName: string,
): string {
  const overrides = structuredCommandArgumentOverrides(argumentsValue);
  const explicit = commandName === "skills"
    ? asString(overrides.skill ?? argumentsValue.skill)
    : asString(overrides.server ?? argumentsValue.server);
  if (explicit.trim()) return explicit.trim();
  if (Array.isArray(argumentsValue.arguments)) {
    const second = argumentsValue.arguments[1];
    if (typeof second === "string" && second.trim()) return second.trim();
  }
  const input = asString(argumentsValue.input).trim().replace(/^\//, "");
  const match = input.match(/^\S+\s+\S+\s+("[^"]*"|'[^']*'|\S+)/);
  return (match?.[1] ?? "").replace(/^['"]|['"]$/g, "").trim();
}

function commandSealedAutonomous(
  argumentsValue: JsonObject,
  context: RequiredExecutionContext,
): boolean {
  const competitionMode = asString(
    argumentsValue.competition_mode
    ?? argumentsValue.competitionMode
    ?? context.metadata.competition_mode
    ?? context.metadata.competitionMode,
  ).toLowerCase();
  return context.metadata.sealed === true
    || context.metadata.sealed_autonomous === true
    || argumentsValue.sealed === true
    || argumentsValue.sealed_autonomous === true
    || competitionMode === "sealed"
    || competitionMode === "competition"
    || competitionMode === "benchmark"
    || context.operation.startsWith("sealed.")
    || false;
}

function assertSealedCommandMutationInput(
  argumentsValue: JsonObject,
  context: RequiredExecutionContext,
): void {
  if (!commandSealedAutonomous(argumentsValue, context)) return;
  const command = commandNameFromArguments(argumentsValue);
  const action = commandActionFromArguments(argumentsValue);
  const domain = command === "skills" ? "skill" : command === "mcp" ? "mcp" : "";
  const mutation = domain === "mcp"
    ? isMcpLifecycleAction(action)
    : domain === "skill" && ["update", "invoke"].includes(action);
  if (!mutation) return;
  throw coordinatorError(
    `sealed_${domain}_mutation_denied`,
    `sealed autonomous mode denies human /${command} ${action}`,
    {
      action,
      human_intervention_count: 0,
      owner_effect_started: false,
      replan_required: true,
    },
  );
}

function assertInteractiveCommandMutation(
  request: CommandInvocationRequest,
  domain: "mcp" | "skill",
  action: string,
): void {
  if (!request.sealedAutonomous) return;
  throw coordinatorError(
    `sealed_${domain}_mutation_denied`,
    `sealed autonomous mode denies human /${domain === "skill" ? "skills" : "mcp"} ${action}`,
    {
      action,
      command_call_id: request.identity.commandCallId,
      human_intervention_count: 0,
      owner_effect_started: false,
      replan_required: true,
    },
  );
}

function requiredCommandString(value: JsonObject, key: string): string {
  const item = value[key];
  if (typeof item !== "string" || !item.trim()) {
    throw coordinatorError(
      "command_argument_missing",
      `command argument ${key} must be a non-empty string`,
      { field: key },
    );
  }
  return item.trim();
}

function requiredCommandObject(value: JsonObject, key: string): JsonObject {
  const item = value[key];
  if (!item || typeof item !== "object" || Array.isArray(item)) {
    throw coordinatorError(
      "command_argument_object_required",
      `command argument ${key} must be an object`,
      { field: key },
    );
  }
  return canonicalize(item) as JsonObject;
}

function requiredCommandInteger(value: JsonObject, key: string): number {
  const item = value[key];
  if (typeof item !== "number" || !Number.isSafeInteger(item) || item < 0) {
    throw coordinatorError(
      "command_argument_integer_required",
      `command argument ${key} must be a non-negative integer`,
      { field: key },
    );
  }
  return item;
}

function commandOptionalInteger(
  value: JsonObject,
  key: string,
  fallback: number,
): number {
  return value[key] === undefined
    ? fallback
    : requiredCommandInteger(value, key);
}

function assertOptionalCommandBinding(
  value: JsonObject,
  key: string,
  expected: string,
): void {
  const actual = asString(value[key]);
  if (!actual) return;
  if (actual !== expected) {
    throw coordinatorError(
      "command_owner_binding_mismatch",
      `command ${key} is bound to another owner identity`,
      { field: key, expected, actual },
    );
  }
}

function commandHashEquals(left: string, right: string): boolean {
  const normalize = (value: string): string => value.trim().toLowerCase().replace(/^sha256:/, "");
  return constantTimeDigestEquals(normalize(left), normalize(right));
}

function commandSkillIdentity(
  request: CommandInvocationRequest,
): {
  runId: string;
  taskId: string;
  sessionId: string;
  sessionRevision: number;
  workerRequestId: string;
  toolCallId: string;
} {
  return {
    runId: request.identity.runId,
    taskId: request.identity.taskId,
    sessionId: request.identity.sessionId,
    sessionRevision: request.identity.sessionRevision,
    workerRequestId: request.identity.workerRequestId,
    toolCallId: request.identity.commandCallId,
  };
}

function skillOwnerUpdateAdmission(
  previous: SkillDescriptor,
  proposed: SkillDescriptor,
  request: CommandInvocationRequest,
  scan: SkillReloadScan,
): JsonObject {
  const permission = asObject(request.metadata.canonical_permission);
  const effect = asString(permission.effect);
  const decisionId = asString(permission.decision_id);
  const requestDigest = asString(permission.request_digest);
  if (effect !== "allow" || !decisionId || !requestDigest) {
    throw coordinatorError(
      "skill_update_owner_approval_missing",
      `skill ${previous.skillId} update lacks an exact canonical permission decision`,
      {
        skill_id: previous.skillId,
        permission_effect: effect,
        owner_effect_started: false,
      },
    );
  }
  if (
    proposed.skillId !== previous.skillId ||
    proposed.source.sourceId !== previous.source.sourceId ||
    proposed.source.realPath !== previous.source.realPath ||
    proposed.source.manifestPath !== previous.source.manifestPath
  ) {
    throw coordinatorError(
      "skill_update_owner_identity_changed",
      `skill ${previous.skillId} staged source identity changed during update`,
      {
        previous_skill_id: previous.skillId,
        proposed_skill_id: proposed.skillId,
        previous_source_id: previous.source.sourceId,
        proposed_source_id: proposed.source.sourceId,
        scan_id: scan.scanId,
        owner_effect_started: false,
      },
    );
  }
  if (proposed.availability !== "available" || proposed.disabledReason) {
    throw coordinatorError(
      "skill_update_owner_admission_denied",
      `skill ${proposed.skillId} is not available to the canonical owner`,
      {
        skill_id: proposed.skillId,
        availability: proposed.availability,
        disabled_reason: proposed.disabledReason,
        scan_id: scan.scanId,
        owner_effect_started: false,
      },
    );
  }
  const dependencyState = {
    skill_id: proposed.skillId,
    registry_descriptor_digest: proposed.descriptorDigest,
    declared_dependencies: canonicalize(
      asObject(proposed.metadata).dependencies ?? [],
    ),
    resources: proposed.resources
      .map((resource) => ({
        resource_id: resource.resourceId,
        path: resource.path,
        required: resource.required,
        digest: resource.digest,
      }))
      .sort((left, right) => left.resource_id.localeCompare(right.resource_id)),
  };
  const supplyState = {
    skill_id: proposed.skillId,
    source_id: proposed.source.sourceId,
    source_real_path_digest: digest(proposed.source.realPath),
    source_digest: proposed.source.contentDigest,
    body_digest: proposed.bodyDigest,
    frontmatter_digest: proposed.frontmatterDigest,
    descriptor_digest: proposed.descriptorDigest,
    tool_scope_digest: digest(proposed.toolScope),
    execution_policy_digest: digest(proposed.execution),
    environment_handle_digest: digest(proposed.environmentHandles),
    hook_digests: proposed.hooks
      .map((hook) => digest(hook))
      .sort(),
    resource_digests: proposed.resources
      .map((resource) => resource.digest ?? digest({
        resource_id: resource.resourceId,
        path: resource.path,
        required: resource.required,
      }))
      .sort(),
  };
  const dependencyDigest = digest(dependencyState);
  const supplyDigest = digest(supplyState);
  const approvalBindingDigest = digest({
    permission_owner: "typescript.PermissionCoordinator",
    permission_decision_id: decisionId,
    permission_request_digest: requestDigest,
    command_call_id: request.identity.commandCallId,
    skill_id: proposed.skillId,
    previous_descriptor_digest: previous.descriptorDigest,
    proposed_descriptor_digest: proposed.descriptorDigest,
    staged_scan_id: scan.scanId,
    staged_scan_digest: scan.digest,
    dependency_digest: dependencyDigest,
    supply_digest: supplyDigest,
  });
  return {
    protocol: "zyra.skill-update-owner-admission/v1",
    canonical_owner: "typescript.SkillCoordinator",
    canonical_owner_verified: true,
    approval_owner: "typescript.PermissionCoordinator",
    approval_decision_id: decisionId,
    approval_request_digest: requestDigest,
    approval_binding_digest: approvalBindingDigest,
    staged_scan_id: scan.scanId,
    staged_scan_digest: scan.digest,
    previous_descriptor_digest: previous.descriptorDigest,
    proposed_descriptor_digest: proposed.descriptorDigest,
    proposed_body_digest: proposed.bodyDigest,
    dependency_digest: dependencyDigest,
    supply_digest: supplyDigest,
    registry_descriptor_digest: proposed.descriptorDigest,
    admitted: true,
  };
}

function skillCommandArgumentsDigest(value: JsonObject): string {
  const serialized = JSON.stringify(canonicalize(value));
  const input = JSON.stringify([serialized]);
  let first = 0x811c9dc5;
  let second = 0x9e3779b9;
  for (let index = 0; index < input.length; index += 1) {
    const code = input.charCodeAt(index);
    first ^= code;
    first = Math.imul(first, 0x01000193);
    second ^= first + code + Math.imul(second, 33);
    second = Math.imul(second ^ (second >>> 16), 0x85ebca6b);
  }
  const left = (first >>> 0).toString(16).padStart(8, "0");
  const right = (second >>> 0).toString(16).padStart(8, "0");
  return `skill:${left}${right}`;
}

function mcpLifecycleState(
  snapshot: McpCoordinatorSnapshot,
  serverId: string,
): JsonObject {
  return {
    config_revision: snapshot.config.revision,
    config_digest: snapshot.config.digest,
    server: canonicalize(
      snapshot.config.servers.find((server) => server.serverId === serverId) ?? null,
    ),
    connection_revision: snapshot.connections.revision,
    connection: canonicalize(
      snapshot.connections.connections.find((connection) => connection.serverId === serverId) ?? null,
    ),
    catalog_revision: snapshot.catalog.revision,
    catalog: canonicalize(
      snapshot.catalog.servers.find((server) => server.serverId === serverId) ?? null,
    ),
    oauth_revision: snapshot.oauth.revision,
    elicitation_revision: snapshot.elicitation.revision,
    session_revision: snapshot.session.revision,
    session: canonicalize(
      snapshot.session.servers.find((server) => server.serverId === serverId) ?? null,
    ),
  };
}

function inferNamespace(toolName: string): string {
  if (toolName.startsWith("mcp__") || toolName.startsWith("mcp_")) return "mcp";
  if (["skill", "list_skills", "search_skills", "read_skill_resource", "reload_skills"].includes(toolName)) return "skill";
  if (["list_plugins", "reload_plugins", "plugin_command", "plugin_status", "plugin_disable"].includes(toolName)) return "plugin";
  if (["list_commands", "command", "command_help", "complete_command", "reload_commands", "command_history"].includes(toolName)) return "command";
  if (["Agent", "Task", "agent_status", "agent_cancel", "agent_resume", "agent_message"].includes(toolName)) return "agent";
  if (toolName.startsWith("e02_")) return "e02";
  return "builtin";
}

function inferServerId(toolName: string): string {
  if (!toolName.startsWith("mcp__")) return "";
  return toolName.split("__", 3)[1] ?? "";
}

function inferOperation(toolName: string, spec?: ToolSpecContract): string {
  const access = spec?.metadata.access_mode;
  if (access === "read") return "read";
  if (access === "write") return "write";
  if (/^(?:list|read|get|search|inspect|status|complete|e02_health)/i.test(toolName)) return "read";
  if (/^(?:write|edit|create|update|delete|remove|disable|reload)/i.test(toolName)) return "write";
  return "execute";
}

function skillParentContext(input: RuntimeRunInput): SkillParentContext {
  const context = asObject(input.contextSnapshot);
  return {
    conversation: canonicalize(input.messages) as JsonValue[],
    system: stringArrayOrEmpty(context.system),
    memory: jsonArrayOrEmpty(context.memory),
    workspaceInstructions: stringArrayOrEmpty(context.workspace_instructions),
    mcpInstructions: stringArrayOrEmpty(context.mcp_instructions),
    variables: asObject(context.variables),
    metadata: {
      run_id: input.runId,
      task_id: input.taskId,
      session_id: input.sessionId,
      worker_request_id: input.workerRequestId,
    },
  };
}

function skillInvocationAncestry(input: RuntimeRunInput): string[] {
  const constraints = asObject(input.config.runtimeConstraints);
  const ancestry = stringArrayOrEmpty(constraints.skill_ancestry);
  const current = asString(constraints.skill_id).trim();
  if (current && !ancestry.includes(current)) ancestry.push(current);
  return ancestry;
}

function skillDepthRemaining(input: RuntimeRunInput): number | null {
  const value = asObject(input.config.runtimeConstraints).skill_depth_remaining;
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0
    ? value
    : null;
}

function cloneAuthorizationInput(input: E02AuthorizationInput): E02AuthorizationInput {
  return {
    runId: input.runId,
    taskId: input.taskId,
    sessionId: input.sessionId,
    sessionRevision: input.sessionRevision,
    workerRequestId: input.workerRequestId,
    toolCallId: input.toolCallId,
    toolName: input.toolName,
    namespace: input.namespace,
    serverId: input.serverId,
    commandName: input.commandName,
    resourceUri: input.resourceUri,
    operation: input.operation,
    workspaceRoot: input.workspaceRoot,
    arguments: canonicalize(input.arguments ?? {}) as JsonObject,
    metadata: canonicalize(input.metadata ?? {}) as JsonObject,
    signal: input.signal,
    awaitApprovalDelivery: input.awaitApprovalDelivery,
  };
}

function parsePluginHookDeclaration(textValue: string, path: string): JsonObject {
  const text = textValue.trim();
  if (!text) throw coordinatorError("plugin_hook_declaration_empty", `plugin hook ${path} is empty`);
  let candidate = text;
  const fenced = /^```(?:json)?\s*\n([\s\S]*?)\n```$/i.exec(text);
  if (fenced) candidate = fenced[1]!;
  try {
    const parsed = JSON.parse(candidate) as unknown;
    return requiredObject(parsed, `plugin hook ${path}`);
  } catch (error) {
    throw coordinatorError(
      "plugin_hook_declaration_invalid",
      `plugin hook ${path} must contain one JSON object: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
}

function parsePluginHookCommand(commandValue: string): JsonObject {
  const command = commandValue.trim();
  if (!command) throw coordinatorError("plugin_hook_command_empty", "plugin hook command is empty");
  if (command.startsWith("json:")) {
    return parsePluginHookDeclaration(command.slice(5), "inline command");
  }
  const [effect, ...reason] = command.split(/\s+/);
  if (!["continue", "deny", "ask"].includes(effect.toLowerCase())) {
    throw coordinatorError(
      "plugin_hook_command_unsupported",
      "plugin hook commands must be declarative: continue, deny, ask, or json:<object>",
    );
  }
  return { effect: effect.toLowerCase(), reason: reason.join(" ") };
}

function pluginHookEffect(value: JsonValue | undefined): "continue" | "deny" | "ask" {
  return value === "deny" || value === "ask" || value === "continue" ? value : "continue";
}

function renderPluginCommandBody(body: string, argumentsValue: JsonObject): string {
  let rendered = body;
  for (const [key, value] of Object.entries(argumentsValue)) {
    const replacement = typeof value === "string" ? value : JSON.stringify(value);
    rendered = rendered
      .replaceAll(`{{${key}}}`, replacement)
      .replaceAll(`\${${key}}`, replacement);
  }
  if (/{{[^}]+}}|\$\{[^}]+}/.test(rendered)) {
    throw coordinatorError(
      "plugin_command_template_unresolved",
      "plugin command contains unresolved template variables",
    );
  }
  return rendered;
}

function assertPathWithin(rootValue: string, pathValue: string, label: string): void {
  const root = resolve(rootValue);
  const path = resolve(pathValue);
  const value = relative(root, path);
  if (
    value.startsWith("..")
    || isAbsolute(value)
    || path.toLowerCase() !== root.toLowerCase()
      && !path.toLowerCase().startsWith(`${root.toLowerCase()}${sep}`)
  ) {
    throw coordinatorError(
      "plugin_path_outside_root",
      `${label} path ${path} is outside plugin root ${root}`,
    );
  }
}

function pluginMcpLayerId(pluginId: string): string {
  return `e02-plugin-mcp-${normalizeIdentifier(pluginId)}`;
}

function watcherEnabled(input: RuntimeRunInput, kind: "skill" | "plugin"): boolean {
  const constraints = asObject(input.config.runtimeConstraints);
  const key = kind === "skill" ? "watchSkills" : "watchPlugins";
  const snake = kind === "skill" ? "watch_skills" : "watch_plugins";
  const value = constraints[key] ?? constraints[snake];
  return value === undefined ? true : value === true;
}

function skillSourceKind(value: JsonValue | undefined): SkillSourceRoot["kind"] {
  return value === "managed"
    || value === "user"
    || value === "project"
    || value === "plugin"
    || value === "bundled"
    || value === "session"
    ? value
    : "session";
}

function jsonArrayOrEmpty(value: JsonValue | undefined): JsonValue[] {
  return Array.isArray(value) ? canonicalize(value) as JsonValue[] : [];
}

function stringArrayOrEmpty(value: JsonValue | undefined): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : typeof value === "string" && value
      ? [value]
      : [];
}

function commandSourceKind(value: JsonValue | undefined): CommandSourceRoot["sourceKind"] {
  return value === "managed"
    || value === "user"
    || value === "project"
    || value === "plugin"
    || value === "builtin"
    || value === "session"
    ? value
    : "session";
}

function pluginSourceKind(value: JsonValue | undefined): PluginSourceRoot["sourceKind"] {
  return value === "managed"
    || value === "user"
    || value === "project"
    || value === "marketplace"
    || value === "session"
    ? value
    : "session";
}

function stringArray(value: JsonValue | undefined, fallback: string[] = []): string[] {
  if (value === undefined || value === null) return [...fallback];
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw coordinatorError("e02_string_array_invalid", "expected an array of strings");
  }
  return [...new Set(value as string[])];
}

function stringRecord(value: JsonValue | undefined): Record<string, string> {
  const object = asObject(value);
  const output: Record<string, string> = {};
  for (const [key, item] of Object.entries(object)) {
    if (typeof item === "string") output[key] = item;
    else if (item !== undefined) output[key] = JSON.stringify(item);
  }
  return output;
}

function requiredObject(value: unknown, label: string): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw coordinatorError("e02_object_required", `${label} must be an object`);
  }
  return canonicalize(value) as JsonObject;
}

function optionalString(value: JsonValue | undefined): string {
  return typeof value === "string" ? value : "";
}

function stringField(value: JsonObject, key: string, fallback: string): string {
  const item = value[key];
  return typeof item === "string" && item ? item : fallback;
}

function runtimeSubjectMatches(
  runtime: E02RuntimeIdentity,
  subject: {
    runId: string;
    taskId: string;
    sessionId: string;
    workerRequestId: string;
  },
): boolean {
  return subject.runId === runtime.runId
    && subject.taskId === runtime.taskId
    && subject.sessionId === runtime.sessionId
    && subject.workerRequestId === runtime.workerRequestId;
}

function numberField(value: JsonObject, key: string, fallback: number): number {
  const item = value[key];
  return typeof item === "number" && Number.isSafeInteger(item) && item >= 0 ? item : fallback;
}

function nonNegativeInteger(value: JsonValue | undefined, fallback: number): number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : fallback;
}

function nullableNonNegativeInteger(value: JsonValue | undefined): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function requiredControlString(value: JsonObject, key: string): string {
  const item = value[key];
  if (typeof item !== "string" || !item.trim()) {
    throw coordinatorError("e02_control_payload_string_missing", `control payload.${key} must be a non-empty string`, {
      field: key,
    });
  }
  return item.trim();
}

function controlRisk(value: JsonValue | undefined): E02ControlRisk | undefined {
  return value === "read"
    || value === "bounded_write"
    || value === "privileged_write"
    || value === "recovery"
    ? value
    : undefined;
}

function controlPhase(value: JsonValue | undefined): E02ControlPhase | undefined {
  return value === "prepared"
    || value === "running"
    || value === "effect_recorded"
    || value === "committed"
    || value === "failed"
    || value === "recovery_required"
    || value === "cancelled"
    ? value
    : undefined;
}

function checkpointPhase(value: JsonValue | undefined): E02CheckpointPhase | undefined {
  return value === "open"
    || value === "sealed"
    || value === "committed"
    || value === "delivery_indeterminate"
    || value === "delivered"
    || value === "aborted"
    ? value
    : undefined;
}

function projectionDomains(value: JsonValue | undefined): E02ProjectionDomain[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const allowed = new Set<E02ProjectionDomain>([
    "runtime",
    "permission",
    "mcp",
    "skill",
    "plugin",
    "command",
    "agent",
    "route",
    "checkpoint",
    "execution",
    "recovery",
    "event",
  ]);
  const output: E02ProjectionDomain[] = [];
  for (const item of value) {
    if (typeof item !== "string" || !allowed.has(item as E02ProjectionDomain)) {
      throw coordinatorError("e02_projection_domain_invalid", `invalid E02 projection domain ${String(item)}`);
    }
    if (!output.includes(item as E02ProjectionDomain)) output.push(item as E02ProjectionDomain);
  }
  return output.length > 0 ? output : undefined;
}

function signedInteger(value: JsonValue | undefined, fallback: number): number {
  return typeof value === "number" && Number.isSafeInteger(value) ? value : fallback;
}

function boundedSecondsAsMilliseconds(
  value: JsonValue | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return fallback;
  const milliseconds = Math.trunc(value * 1_000);
  return milliseconds >= minimum && milliseconds <= maximum ? milliseconds : fallback;
}

function numberMetadata(value: Record<string, string>, key: string): number {
  const parsed = Number(value[key]);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : 0;
}

function stringMetadata(value: Record<string, string>, key: string): string {
  return typeof value[key] === "string" ? value[key]! : "";
}

function estimateTokens(value: string): number {
  return Math.max(1, Math.ceil(value.length / 4));
}

function executionPhase(value: JsonValue | undefined): CapabilityExecutionPhase | undefined {
  return value === "authorized"
    || value === "prepared"
    || value === "executing"
    || value === "effect_recorded"
    || value === "committed"
    || value === "failed"
    || value === "recovery_required"
    ? value
    : undefined;
}

function integrationAuditDigest(audit: E02IntegrationAudit): string {
  const { digest: _digest, ...withoutDigest } = audit;
  return digest(withoutDigest);
}

function errorObject(error: unknown): JsonObject {
  const details = error && typeof error === "object"
    ? asObject((error as { details?: unknown }).details)
    : {};
  return {
    name: error instanceof Error ? error.name : "Error",
    message: error instanceof Error ? error.message : String(error),
    code: errorCode(error),
    details: cloneJson(details),
  };
}

function errorCode(error: unknown): string {
  if (error && typeof error === "object") {
    const code = (error as { code?: unknown }).code;
    if (typeof code === "string" && code) return code;
  }
  return error instanceof Error && error.name ? error.name : "e02_runtime_failed";
}

function abortError(reason: unknown): Error {
  return Object.assign(new Error(
    reason instanceof Error ? reason.message : String(reason ?? "operation aborted"),
  ), { name: "AbortError", code: "operation_aborted" });
}

function permissionDeniedError(enforcement: PermissionEnforcementResult): Error {
  return Object.assign(new Error(enforcement.decision.reason), {
    name: "E02PermissionDeniedError",
    code: enforcement.pendingApproval
      ? "permission_approval_required"
      : "permission_denied",
    decision: cloneJson(enforcement.decision),
    recoveryInput: enforcement.recoveryInput
      ? cloneJson(enforcement.recoveryInput)
      : null,
    replanRequired: enforcement.replanRequired,
  });
}

function coordinatorError(
  code: string,
  message: string,
  details: JsonObject = {},
): Error {
  return Object.assign(new Error(message), {
    name: "E02CapabilityCoordinatorError",
    code,
    details: canonicalize(details),
  });
}
