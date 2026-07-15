import { ContextCompactionRuntime } from "../compact/context-runtime.ts";
import { CompactRestoreRuntime } from "../compact/restore-runtime.ts";
import { CompactSummaryRuntime } from "../compact/summary-runtime.ts";
import { ContextAssemblyRuntime } from "../context/assembly-runtime.ts";
import { ContextCacheRuntime } from "../context/cache-runtime.ts";
import { ContextTokenRuntime } from "../context/token-runtime.ts";
import type { JsonObject, JsonValue } from "../contracts.ts";
import { QueryInputRuntime } from "../input/query-input-runtime.ts";
import { EffectProtocolRuntime } from "../protocol/effect-runtime.ts";
import { ProtocolFramingRuntime } from "../protocol/framing-runtime.ts";
import { RuntimeCommandRuntime } from "../protocol/command-runtime.ts";
import {
  InMemorySecretVault,
  ProviderCredentialRuntime,
  type SecretVaultPort,
} from "../provider/credential-runtime.ts";
import {
  ProviderModelRuntime,
  type ProviderMessage,
} from "../provider/model-runtime.ts";
import { ProviderPromptRuntime } from "../provider/prompt-runtime.ts";
import { ProviderRateLimitRuntime } from "../provider/rate-limit-runtime.ts";
import { ProviderRecoveryRuntime, type RetryPlan } from "../provider/recovery-runtime.ts";
import { ProviderRequestRuntime } from "../provider/request-runtime.ts";
import { ProviderResponseRuntime } from "../provider/response-runtime.ts";
import {
  ProviderRoutingRuntime,
  type ProviderFailureClass,
} from "../provider/routing-runtime.ts";
import { ProviderTelemetryRuntime } from "../provider/telemetry-runtime.ts";
import { ProviderTransportRuntime } from "../provider/transport-runtime.ts";
import { QueryExecutionPlanRuntime } from "../query/execution-plan-runtime.ts";
import {
  QueryHookRuntime,
  type HookPhase,
  type HookPhaseResult,
} from "../query/hook-runtime.ts";
import { QueryLifecycleRuntime } from "../query/lifecycle-runtime.ts";
import { QueryStopRuntime, type StopSignal } from "../query/stop-runtime.ts";
import { QueryStreamRuntime } from "../query/stream-runtime.ts";
import { SessionCorrelationRuntime } from "../session/correlation-runtime.ts";
import { DurableSessionRuntime } from "../session/durable-runtime.ts";
import { SessionHistoryRuntime } from "../session/history-runtime.ts";
import { ToolExecutionRuntime } from "../tools/execution-runtime.ts";
import { ToolResultRuntime } from "../tools/result-runtime.ts";
import {
  Journal,
  command,
  digest,
  type JournalSnapshot,
  type O,
  type TransitionReceipt,
} from "./kernel.ts";

export const E01_COORDINATOR_SNAPSHOT_VERSION = "zyra.e01-runtime/v5";

type RuntimeDomain = "query" | "provider" | "context" | "tool" | "session" | "protocol";

const DOMAIN_BY_PHASE: Array<[RegExp, RuntimeDomain]> = [
  [/model|stream|api|provider|retry|usage|cache/i, "provider"],
  [/compact|context|budget|externaliz/i, "context"],
  [/tool|permission|artifact|capability/i, "tool"],
  [/session|resume|restore|snapshot|control/i, "session"],
  [/frame|commit|outbox|ack|settle|correlation/i, "protocol"],
];

const EFFECT_PHASES = new Set([
  "tool_call_completed",
  "context_compacted",
  "control_command",
  "session_completed",
  "session_failed",
  "query_session_snapshot",
]);

export interface RuntimeDecision {
  accepted: boolean;
  domain: RuntimeDomain;
  operation: string;
  reason: string;
  revision: number;
  receipt: TransitionReceipt;
}

export interface E01CoordinatorSnapshot {
  version: typeof E01_COORDINATOR_SNAPSHOT_VERSION;
  runId: string;
  sessionId: string;
  taskId: string;
  workerRequestId: string;
  bootstrapped: boolean;
  journal: JournalSnapshot;
  query: ReturnType<QueryLifecycleRuntime["snapshot"]>;
  hooks: ReturnType<QueryHookRuntime["snapshot"]>;
  executionPlan: ReturnType<QueryExecutionPlanRuntime["snapshot"]>;
  stop: ReturnType<QueryStopRuntime["snapshot"]>;
  stream: ReturnType<QueryStreamRuntime["snapshot"]>;
  input: ReturnType<QueryInputRuntime["snapshot"]>;
  context: ReturnType<ContextAssemblyRuntime["snapshot"]>;
  contextCache: ReturnType<ContextCacheRuntime["snapshot"]>;
  tokens: ReturnType<ContextTokenRuntime["snapshot"]>;
  compact: ReturnType<ContextCompactionRuntime["snapshot"]>;
  compactSummary: ReturnType<CompactSummaryRuntime["snapshot"]>;
  compactRestore: ReturnType<CompactRestoreRuntime["snapshot"]>;
  provider: ReturnType<ProviderModelRuntime["snapshot"]>;
  providerPrompt: ReturnType<ProviderPromptRuntime["snapshot"]>;
  providerTransport: ReturnType<ProviderTransportRuntime["snapshot"]>;
  providerCredentials: ReturnType<ProviderCredentialRuntime["snapshot"]>;
  providerRouting: ReturnType<ProviderRoutingRuntime["snapshot"]>;
  providerRequests: ReturnType<ProviderRequestRuntime["snapshot"]>;
  providerResponses: ReturnType<ProviderResponseRuntime["snapshot"]>;
  providerRateLimits: ReturnType<ProviderRateLimitRuntime["snapshot"]>;
  recovery: ReturnType<ProviderRecoveryRuntime["snapshot"]>;
  telemetry: ReturnType<ProviderTelemetryRuntime["snapshot"]>;
  tools: ReturnType<ToolExecutionRuntime["snapshot"]>;
  toolResults: ReturnType<ToolResultRuntime["snapshot"]>;
  session: ReturnType<DurableSessionRuntime["snapshot"]>;
  history: ReturnType<SessionHistoryRuntime["snapshot"]>;
  correlation: ReturnType<SessionCorrelationRuntime["snapshot"]>;
  protocol: ReturnType<EffectProtocolRuntime["snapshot"]>;
  framing: ReturnType<ProtocolFramingRuntime["snapshot"]>;
  commands: ReturnType<RuntimeCommandRuntime["snapshot"]>;
  checksum: string;
}

export class E01RuntimeCoordinator {
  readonly journal: Journal;
  readonly query: QueryLifecycleRuntime;
  readonly hooks: QueryHookRuntime;
  readonly executionPlan: QueryExecutionPlanRuntime;
  readonly stop: QueryStopRuntime;
  readonly stream: QueryStreamRuntime;
  readonly input: QueryInputRuntime;
  readonly context: ContextAssemblyRuntime;
  readonly contextCache: ContextCacheRuntime;
  readonly tokens: ContextTokenRuntime;
  readonly compact: ContextCompactionRuntime;
  readonly compactSummary: CompactSummaryRuntime;
  readonly compactRestore: CompactRestoreRuntime;
  readonly provider: ProviderModelRuntime;
  readonly providerPrompt: ProviderPromptRuntime;
  readonly providerTransport: ProviderTransportRuntime;
  readonly providerCredentials: ProviderCredentialRuntime;
  readonly providerRouting: ProviderRoutingRuntime;
  readonly providerRequests: ProviderRequestRuntime;
  readonly providerResponses: ProviderResponseRuntime;
  readonly providerRateLimits: ProviderRateLimitRuntime;
  readonly recovery: ProviderRecoveryRuntime;
  readonly telemetry: ProviderTelemetryRuntime;
  readonly tools: ToolExecutionRuntime;
  readonly toolResults: ToolResultRuntime;
  readonly session: DurableSessionRuntime;
  readonly history: SessionHistoryRuntime;
  readonly correlation: SessionCorrelationRuntime;
  readonly protocol: EffectProtocolRuntime;
  readonly framing: ProtocolFramingRuntime;
  readonly commands: RuntimeCommandRuntime;
  readonly runId: string;
  readonly sessionId: string;
  readonly taskId: string;
  readonly workerRequestId: string;
  private bootstrapped = false;
  private restored = false;

  constructor(
    runId: string,
    sessionId: string,
    taskId = "task-unknown",
    workerRequestId = "worker-request-unknown",
    credentialVault: SecretVaultPort = new InMemorySecretVault(),
  ) {
    this.runId = runId;
    this.sessionId = sessionId;
    this.taskId = taskId;
    this.workerRequestId = workerRequestId;
    this.journal = new Journal(runId, sessionId);
    this.query = new QueryLifecycleRuntime({
      identity: {
        queryId: `${sessionId}:query`,
        sessionId,
        runId,
        taskId,
        workerRequestId,
        parentQueryId: null,
        branchId: "main",
      },
    });
    this.hooks = new QueryHookRuntime();
    this.executionPlan = new QueryExecutionPlanRuntime();
    this.stop = new QueryStopRuntime();
    this.stream = new QueryStreamRuntime();
    this.input = new QueryInputRuntime();
    this.context = new ContextAssemblyRuntime();
    this.contextCache = new ContextCacheRuntime();
    this.tokens = new ContextTokenRuntime();
    this.compact = new ContextCompactionRuntime();
    this.compactSummary = new CompactSummaryRuntime();
    this.compactRestore = new CompactRestoreRuntime();
    this.provider = new ProviderModelRuntime();
    this.providerPrompt = new ProviderPromptRuntime();
    this.providerTransport = new ProviderTransportRuntime();
    this.providerCredentials = new ProviderCredentialRuntime(credentialVault);
    this.providerRouting = new ProviderRoutingRuntime();
    this.providerRequests = new ProviderRequestRuntime();
    this.providerResponses = new ProviderResponseRuntime();
    this.providerRateLimits = new ProviderRateLimitRuntime();
    this.recovery = new ProviderRecoveryRuntime();
    this.telemetry = new ProviderTelemetryRuntime();
    this.tools = new ToolExecutionRuntime();
    this.toolResults = new ToolResultRuntime();
    this.session = new DurableSessionRuntime({
      sessionId,
      runId,
      taskId,
      workerRequestId,
      tenantId: null,
      parentSessionId: null,
      branchId: "main",
    }, digest({ session_id: sessionId, runtime: "e01" }));
    this.history = new SessionHistoryRuntime();
    this.correlation = new SessionCorrelationRuntime();
    this.protocol = new EffectProtocolRuntime({
      sessionId,
      runId,
      taskId,
      workerId: "zyra-typescript-claude-runtime",
      restartEpoch: 0,
    });
    this.framing = new ProtocolFramingRuntime(runId, sessionId);
    this.commands = new RuntimeCommandRuntime();
    this.installDefaultStopRules();
    this.installDefaultHooks();
    this.installDefaultProviderPolicy();
    this.installDefaultCommands();
  }

  restore(snapshot: E01CoordinatorSnapshot | JournalSnapshot): void {
    if (this.bootstrapped) throw new Error("e01_restore_must_precede_bootstrap");
    if (snapshot.version === "zyra.e01-journal/v3") {
      this.journal.restore(snapshot, this.runId);
      this.restored = true;
      return;
    }
    if (snapshot.version !== E01_COORDINATOR_SNAPSHOT_VERSION) {
      throw new Error("e01_snapshot_version_unsupported");
    }
    const { checksum, ...unsigned } = snapshot;
    if (digest(unsigned) !== checksum) throw new Error("e01_snapshot_checksum_mismatch");
    if (snapshot.sessionId !== this.sessionId || snapshot.taskId !== this.taskId) {
      throw new Error("e01_snapshot_identity_mismatch");
    }
    this.journal.restore(snapshot.journal, this.runId);
    this.query.restore(snapshot.query);
    this.hooks.restore(snapshot.hooks);
    this.executionPlan.restore(snapshot.executionPlan);
    this.stop.restore(snapshot.stop);
    this.stream.restore(snapshot.stream);
    this.input.restore(snapshot.input);
    this.context.restore(snapshot.context);
    this.contextCache.restore(snapshot.contextCache);
    this.tokens.restore(snapshot.tokens);
    this.compact.restore(snapshot.compact);
    this.compactSummary.restore(snapshot.compactSummary);
    this.compactRestore.restore(snapshot.compactRestore);
    this.provider.restore(snapshot.provider);
    this.providerPrompt.restore(snapshot.providerPrompt);
    this.providerTransport.restore(snapshot.providerTransport);
    this.providerCredentials.restore(snapshot.providerCredentials);
    this.providerRouting.restore(snapshot.providerRouting);
    this.providerRequests.restore(snapshot.providerRequests);
    this.providerResponses.restore(snapshot.providerResponses);
    this.providerRateLimits.restore(snapshot.providerRateLimits);
    this.recovery.restore(snapshot.recovery);
    this.telemetry.restore(snapshot.telemetry);
    this.tools.restore(snapshot.tools);
    this.toolResults.restore(snapshot.toolResults);
    this.session.restore(snapshot.session);
    this.history.restore(snapshot.history);
    this.correlation.restore(snapshot.correlation);
    this.protocol.restore(snapshot.protocol, {
      allowRunRebind: snapshot.runId !== this.runId,
    });
    this.framing.restore(snapshot.framing, {
      allowRunRebind: snapshot.runId !== this.runId,
    });
    this.commands.restore(snapshot.commands);
    this.restored = true;
  }

  async bootstrap(): Promise<TransitionReceipt> {
    if (process.env.ZYRA_DISABLE_E01_TYPESCRIPT_RUNTIME === "1") {
      throw new Error("e01_typescript_runtime_disabled");
    }
    if (this.bootstrapped) throw new Error("e01_runtime_already_bootstrapped");
    this.bootstrapped = true;
    if (!this.restored) {
      this.session.activate(`${this.sessionId}:bootstrap`);
      this.input.ingest({
        source: "resume",
        text: "Initialize the Zyra TypeScript query runtime.",
        idempotencyKey: `${this.sessionId}:bootstrap-input`,
        correlationId: `${this.sessionId}:bootstrap`,
        metadata: { canonical_owner: "typescript", synthetic: true },
      });
      this.query.admit({
        inputId: `${this.sessionId}:bootstrap-input`,
        kind: "system_notice",
        content: "Initialize the Zyra TypeScript query runtime.",
        priority: "critical",
        idempotencyKey: `${this.sessionId}:bootstrap-query`,
        correlationId: `${this.sessionId}:bootstrap`,
        metadata: { canonical_owner: "typescript", synthetic: true },
        createdAt: new Date().toISOString(),
      });
      this.executionPlan.create({
        planId: `${this.sessionId}:execution-plan`,
        sessionId: this.sessionId,
        runId: this.runId,
        queryId: `${this.sessionId}:query`,
        objective: "Execute the Zyra query through reason, tool, observe, and revise.",
        contextDigest: digest({ session_id: this.sessionId, branch: "main" }),
        budget: {
          maximumTurns: 1_000,
          maximumToolCalls: 5_000,
          maximumReasoningTokens: 4_000_000,
          maximumOutputTokens: 1_000_000,
          maximumWallMilliseconds: 7 * 24 * 60 * 60 * 1_000,
          maximumConsecutiveToolFailures: 20,
        },
        metadata: { canonical_owner: "typescript" },
      });
      this.context.add({
        kind: "system",
        title: "Runtime custody",
        content: "Zyra TypeScript runtime is the canonical owner of query, provider, context, tool, session, and protocol state.",
        text: "Zyra TypeScript runtime is the canonical owner of query, provider, context, tool, session, and protocol state.",
        priority: 1_000,
        pinned: true,
        required: true,
        disclosure: "model_only",
        toolPairId: null,
        turnIndex: null,
        provenance: {
          source: "system",
          sourceId: "e01-bootstrap",
          sourceDigest: digest("e01-bootstrap"),
          parentSectionId: null,
          trust: "system",
          createdAt: new Date().toISOString(),
          expiresAt: null,
        },
        metadata: { canonical_owner: "typescript" },
      });
    } else if (this.session.project().status === "paused") {
      this.session.activate(`${this.sessionId}:resume-bootstrap`);
    }
    this.telemetry.logging_module({
      action: "log",
      level: "info",
      name: this.restored ? "runtime.resume.bootstrap" : "runtime.new.bootstrap",
      session_id: this.sessionId,
      run_id: this.runId,
      task_id: this.taskId,
      summary: "E01 TypeScript runtime bootstrapped",
      attributes: { restored: this.restored, restart_epoch: this.journal.restartEpoch },
    });
    return this.record("session", this.restored ? "resume_bootstrap" : "new_bootstrap", {
      canonical_owner: "typescript",
      runtime: "zyra-typescript-claude-runtime",
      restored: this.restored,
      restart_epoch: this.journal.restartEpoch,
      prior_revision: this.journal.revision,
      custody_version: E01_COORDINATOR_SNAPSHOT_VERSION,
    });
  }

  decideQuery(
    operation: string,
    input: {
      turnIndex: number;
      turnLimit: number;
      empty: boolean;
      allowEmpty: boolean;
      aborted: boolean;
    },
  ): RuntimeDecision {
    let accepted = true;
    let reason = "accepted";
    if (input.aborted) {
      accepted = false;
      reason = "user_cancelled";
    } else if (input.turnIndex >= input.turnLimit) {
      accepted = false;
      reason = "max_turns_exceeded";
    } else if (input.empty && !input.allowEmpty) {
      accepted = false;
      reason = "empty_query_turn";
    }
    const lifecycleDecision = this.query.shouldStop();
    if (lifecycleDecision.stop && lifecycleDecision.reason !== "end_turn") {
      accepted = false;
      reason = lifecycleDecision.reason ?? "query_stopped";
    }
    const stopSignal: StopSignal = input.aborted
      ? "explicit_cancel"
      : input.turnIndex >= input.turnLimit
        ? "maximum_turns"
        : "model_stop";
    const stopDecision = this.stop.evaluate({
      sessionId: this.sessionId,
      runId: this.runId,
      queryId: `${this.sessionId}:query`,
      turnId: null,
      signal: stopSignal,
      correlationId: `${this.sessionId}:${operation}:${this.journal.revision + 1}`,
      evidence: {
        operation,
        turn_index: input.turnIndex,
        turn_limit: input.turnLimit,
        empty: input.empty,
        allow_empty: input.allowEmpty,
        aborted: input.aborted,
      },
      terminalAnswer: null,
      currentRevision: this.journal.revision,
    });
    if (stopDecision.action === "stop" && accepted) {
      accepted = false;
      reason = stopDecision.reason;
    }
    if (input.aborted && !/completed|failed|cancelled/.test(asRuntimeString(this.query.project().status, ""))) {
      this.query.cancel(`${operation}:abort`, "user_cancelled");
    }
    const receipt = this.record("query", operation, {
      accepted,
      reason,
      turn_index: input.turnIndex,
      turn_limit: input.turnLimit,
      empty: input.empty,
      allow_empty: input.allowEmpty,
      aborted: input.aborted,
      lifecycle: this.query.project(),
      stop_decision_id: stopDecision.decisionId,
      stop_action: stopDecision.action,
      stop_signal: stopSignal,
    });
    return {
      accepted,
      domain: "query",
      operation,
      reason,
      revision: receipt.revisionAfter,
      receipt,
    };
  }

  decideContext(
    contextChars: number,
    maxContextChars: number,
    forceCompact: boolean,
    compactionCount: number,
  ): RuntimeDecision {
    const estimate = this.tokens.estimate("x".repeat(Math.max(0, Math.min(contextChars, 2_000_000))));
    const warning = this.compact.calculateTokenWarningState([], Math.max(8_192, Math.ceil(maxContextChars / 4)), 8_192);
    const accepted = compactionCount === 0 && (forceCompact || contextChars > maxContextChars || warning.shouldAutoCompact);
    const reason = accepted
      ? forceCompact ? "forced_compact" : contextChars > maxContextChars ? "context_threshold_exceeded" : "token_threshold_exceeded"
      : compactionCount > 0 ? "already_compacted" : "within_context_budget";
    if (accepted) this.query.requestCompaction(`context:${this.journal.revision + 1}`);
    const receipt = this.record("context", "compact_decision", {
      accepted,
      reason,
      context_chars: contextChars,
      estimated_tokens: estimate.estimatedTokens,
      max_context_chars: maxContextChars,
      force_compact: forceCompact,
      compaction_count: compactionCount,
      token_warning: warning.level,
    });
    return {
      accepted,
      domain: "context",
      operation: "compact_decision",
      reason,
      revision: receipt.revisionAfter,
      receipt,
    };
  }

  recordProvider(operation: string, payload: O): TransitionReceipt {
    const json = payload as unknown as JsonObject;
    const canonicalPayload: O = { ...payload };
    this.telemetry.logging_module({
      action: "log",
      level: /error|fail/i.test(operation) ? "error" : "debug",
      name: `provider.${operation}`,
      session_id: this.sessionId,
      run_id: this.runId,
      task_id: this.taskId,
      summary: `provider ${operation}`,
      attributes: json,
    });
    this.applyProviderLifecycle(operation, json, canonicalPayload);
    const prepared = asRuntimeObject(json.provider_request);
    if (operation === "model_request_prepared" && Object.keys(prepared).length > 0) {
      const prompt = this.telemetry.recordPromptState({
        sessionId: this.sessionId,
        agentId: null,
        requestId: asRuntimeString(prepared.request_id, `${this.workerRequestId}:model`),
        model: asRuntimeString(prepared.model, "unknown"),
        system: prepared.system ?? [],
        tools: prepared.tools ?? [],
        messages: prepared.messages ?? [],
      });
      canonicalPayload.prompt_cache_break = prompt.cacheBreak
        ? { kind: prompt.cacheBreak.kind, break_id: prompt.cacheBreak.breakId }
        : null;
    }
    const report = asRuntimeObject(json.model_stream);
    if (operation === "model_stream_report" && Object.keys(report).length > 0) {
      const requestId = asRuntimeString(report.request_id, `${this.runId}:${operation}:${this.journal.revision + 1}`);
      const provider = asRuntimeString(report.provider, "compatible");
      const model = asRuntimeString(report.model, "unknown");
      const usage = asRuntimeObject(report.usage);
      const sample = this.telemetry.recordUsage({
        requestId,
        sessionId: this.sessionId,
        runId: this.runId,
        taskId: this.taskId,
        model: {
          id: model,
          canonicalName: model,
          aliases: [],
          provider: provider === "local" ? "local" : "compatible",
          contextWindow: 200_000,
          maxOutputTokens: 16_000,
          inputPricePerMillion: 0,
          outputPricePerMillion: 0,
          cacheReadPricePerMillion: 0,
          cacheWritePricePerMillion: 0,
          capabilities: [],
          deprecated: false,
          replacement: null,
        },
        usage: {
          inputTokens: asRuntimeNumber(usage.input_tokens),
          outputTokens: asRuntimeNumber(usage.output_tokens),
          cacheReadInputTokens: asRuntimeNumber(usage.cache_read_input_tokens),
          cacheCreationInputTokens: asRuntimeNumber(usage.cache_creation_input_tokens),
          serverToolUseTokens: asRuntimeNumber(usage.server_tool_use_tokens),
        },
        durationMs: asRuntimeNumber(report.duration_ms),
        firstTokenMs: report.first_token_ms === undefined ? null : asRuntimeNumber(report.first_token_ms),
        success: report.ok === true,
        stopReason: asRuntimeString(report.decision, "unknown"),
        errorCode: report.ok === true ? null : asRuntimeString(report.error, `http_${asRuntimeNumber(report.status)}`),
      });
      canonicalPayload.usage_sample_id = sample.sampleId;
      if (report.ok !== true) {
        const declaredPlan = asRuntimeObject(report.recovery_plan);
        if (Object.keys(declaredPlan).length > 0) {
          canonicalPayload.recovery_plan = declaredPlan;
        } else {
          const fallback = asRuntimeString(report.fallback_model, "");
          this.recovery.createContext(requestId, provider, model, 16_000);
          const plan = this.recovery.plan(
            requestId,
            { status: asRuntimeNumber(report.status), message: asRuntimeString(report.error, "provider failure") },
            fallback ? [fallback] : [],
          );
          canonicalPayload.recovery_plan = plan as unknown as JsonValue;
        }
      }
    } else if (/error|fail/i.test(operation)) {
      const requestId = asRuntimeString(json.request_id, `${this.runId}:${operation}`);
      const provider = asRuntimeString(json.provider, "anthropic");
      const model = asRuntimeString(json.model, "unknown");
      this.recovery.createContext(requestId, provider, model, Math.max(3_000, Number(json.output_token_limit ?? 16_000)));
      canonicalPayload.recovery_error = this.recovery.errors_module({ error: json.error ?? json, provider, model, requestId }) as unknown as JsonValue;
    }
    return this.record("provider", operation, canonicalPayload);
  }

  decideProviderRecovery(input: {
    recoveryContextId: string;
    requestId: string;
    provider: string;
    model: string;
    status: number;
    error: string;
    headers: Record<string, string>;
    fallbackModels: string[];
    outputTokenLimit: number;
    maxRetries: number;
  }): RetryPlan {
    this.recovery.createContext(
      input.recoveryContextId,
      input.provider,
      input.model,
      input.outputTokenLimit,
      input.maxRetries,
    );
    return this.recovery.plan(
      input.recoveryContextId,
      {
        status: input.status,
        message: input.error,
        headers: input.headers,
        request_id: input.requestId,
      },
      input.fallbackModels,
    );
  }

  completeProviderRecovery(input: {
    recoveryContextId: string;
    provider: string;
    model: string;
  }): void {
    this.recovery.recordSuccess(input.recoveryContextId, input.provider, input.model);
  }

  recordTool(operation: string, payload: O, effect = false): TransitionReceipt {
    const json = payload as unknown as JsonObject;
    const toolName = asRuntimeString(json.tool_name, asRuntimeString(json.name, ""));
    if (toolName) this.observeTool(operation, toolName, json);
    return this.record("tool", operation, payload, effect);
  }

  recordProtocol(operation: string, payload: O): TransitionReceipt {
    return this.record("protocol", operation, payload);
  }

  recordRuntimeEvent(phase: string, payload: O = {}): TransitionReceipt {
    const domain = this.domainForPhase(phase);
    if (phase === "context_compacted") {
      if (payload.compaction_runtime_applied !== true) this.compact.runPostCompactCleanup("runtime");
      const project = this.query.project();
      if (project.control && typeof project.control === "object" && (project.control as JsonObject).pending_compact === true) {
        this.query.completeCompaction(`compact:${this.journal.revision + 1}`, digest(payload));
      }
      this.telemetry.notifyCompaction(this.sessionId);
      const summarySequence = this.journal.revision + 1;
      this.compactSummary.build({
        sessionId: this.sessionId,
        runId: this.runId,
        coveredSequenceStart: 0,
        coveredSequenceEnd: summarySequence,
        sourceDigest: digest(payload),
        candidates: [{
          kind: "fact",
          subject: "context",
          predicate: "was compacted",
          value: `revision ${summarySequence}`,
          confidence: "observed",
          importance: 0.7,
          citations: [{
            eventId: `${this.sessionId}:compact:${summarySequence}`,
            transitionId: null,
            messageId: null,
            artifactId: null,
            occurredAt: Date.now(),
            digest: digest(payload),
          }],
          tags: ["compact", "default-path"],
          metadata: { canonical_owner: "typescript" },
        }],
        maximumCharacters: 24_000,
        preserveOpenLoops: true,
        preserveErrors: true,
      });
      this.contextCache.invalidateSession(this.sessionId);
    }
    if (phase === "session_completed" && this.session.project().status === "active") this.session.finish("completed", `${phase}:${this.journal.revision + 1}`);
    if (phase === "session_failed" && this.session.project().status === "active") this.session.finish("failed", `${phase}:${this.journal.revision + 1}`);
    if (domain === "tool") this.observeTool(phase, asRuntimeString((payload as unknown as JsonObject).tool_name, ""), payload as unknown as JsonObject);
    const decorated = {
      ...payload,
      observed_phase: phase,
      canonical_owner: "typescript",
    };
    if (domain === "provider") return this.recordProvider(phase, decorated);
    return this.record(domain, phase, decorated, EFFECT_PHASES.has(phase));
  }

  inventory(): O {
    const cacheMetrics = this.contextCache.currentMetrics();
    return {
      owner: "typescript",
      coordinator_version: E01_COORDINATOR_SNAPSHOT_VERSION,
      journal_version: "zyra.e01-journal/v3",
      domains: ["query", "provider", "context", "tool", "session", "protocol"],
      owner_count: 32,
      restored: this.restored,
      restart_epoch: this.journal.restartEpoch,
      revision: this.journal.revision,
      pending: this.journal.pending().length,
      committed: this.journal.committed().length,
      undelivered: this.journal.undelivered().length,
      protocol: "prepare/effect/commit/ack",
      query_state_digest: asRuntimeString(this.query.project().state_digest, ""),
      session_state_digest: asRuntimeString(this.session.project().state_digest, ""),
      provider_requests: Number(this.provider.claude_module({ action: "inspect" }).request_count ?? 0),
      durable_provider_requests: this.providerRequests.pending().length,
      provider_routes: this.providerRouting.snapshot().definitions.length,
      provider_rate_limits: this.providerRateLimits.snapshot().definitions.length,
      provider_credentials: this.providerCredentials.snapshot().records.length,
      provider_responses: this.providerResponses.snapshot().responses.length,
      query_hooks: this.hooks.list().length,
      query_stop_rules: this.stop.listRules().length,
      query_streams: this.stream.snapshot().channels.length,
      execution_plans: this.executionPlan.snapshot().plans.length,
      compact_summaries: this.compactSummary.snapshot().summaries.length,
      context_cache_entries: cacheMetrics.currentEntries,
      tool_results: this.toolResults.snapshot().accumulators.length,
      history_nodes: Number(this.history.project().node_count ?? 0),
      correlation_nodes: this.correlation.snapshot().nodes.length,
      protocol_frames: Number(this.framing.project().outbound_sequence ?? 0),
      runtime_commands: this.commands.snapshot().descriptors.length,
      telemetry_events: Number(this.telemetry.logging_module({ action: "log", level: "debug", name: "inventory", summary: "runtime inventory" }).sequence ?? 0),
    };
  }

  snapshot(): E01CoordinatorSnapshot {
    const unsigned: Omit<E01CoordinatorSnapshot, "checksum"> = {
      version: E01_COORDINATOR_SNAPSHOT_VERSION,
      runId: this.runId,
      sessionId: this.sessionId,
      taskId: this.taskId,
      workerRequestId: this.workerRequestId,
      bootstrapped: this.bootstrapped,
      journal: this.journal.snapshot(),
      query: this.query.snapshot(),
      hooks: this.hooks.snapshot(),
      executionPlan: this.executionPlan.snapshot(),
      stop: this.stop.snapshot(),
      stream: this.stream.snapshot(),
      input: this.input.snapshot(),
      context: this.context.snapshot(),
      contextCache: this.contextCache.snapshot(),
      tokens: this.tokens.snapshot(),
      compact: this.compact.snapshot(),
      compactSummary: this.compactSummary.snapshot(),
      compactRestore: this.compactRestore.snapshot(),
      provider: this.provider.snapshot(),
      providerPrompt: this.providerPrompt.snapshot(),
      providerTransport: this.providerTransport.snapshot(),
      providerCredentials: this.providerCredentials.snapshot(),
      providerRouting: this.providerRouting.snapshot(),
      providerRequests: this.providerRequests.snapshot(),
      providerResponses: this.providerResponses.snapshot(),
      providerRateLimits: this.providerRateLimits.snapshot(),
      recovery: this.recovery.snapshot(),
      telemetry: this.telemetry.snapshot(),
      tools: this.tools.snapshot(),
      toolResults: this.toolResults.snapshot(),
      session: this.session.snapshot(),
      history: this.history.snapshot(),
      correlation: this.correlation.snapshot(),
      protocol: this.protocol.snapshot(),
      framing: this.framing.snapshot(),
      commands: this.commands.snapshot(),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  private record(
    domain: RuntimeDomain,
    operation: string,
    payload: O,
    requiresEffect = false,
  ): TransitionReceipt {
    if (!this.bootstrapped && operation !== "new_bootstrap" && operation !== "resume_bootstrap") {
      throw new Error("e01_runtime_not_bootstrapped");
    }
    this.ensureFramingHandshake();
    const value = command(this.journal, domain, operation, {
      ...payload,
      canonical_owner: "typescript",
      restart_epoch: this.journal.restartEpoch,
    }, {
      requiresEffect,
      readSet: [domain, "session"],
      writeSet: [domain],
    });
    const prepared = this.journal.prepare(value);
    if (prepared.status === "acked") return prepared;
    if (requiresEffect) {
      this.journal.effect(value, {
        domain,
        operation,
        payload_digest: value.payloadDigest,
        terminal: true,
      });
    }
    const patch = {
      [domain]: {
        last_operation: operation,
        last_transition_id: value.identity.transitionId,
        payload_digest: value.payloadDigest,
        revision: this.journal.revision + 1,
        restart_epoch: this.journal.restartEpoch,
      },
      session: {
        session_id: this.sessionId,
        last_run_id: this.runId,
        canonical_owner: "typescript",
      },
    } as O;
    const committed = this.journal.commit(value, patch, [{
      domain,
      operation,
      transition_id: value.identity.transitionId,
      payload_digest: value.payloadDigest,
      canonical_owner: "typescript",
    }]);
    this.applyTypedEffect(domain, operation, payload, value.identity.transitionId);
    const correlation = this.correlation.open({
      kind: "transition",
      externalId: value.identity.transitionId,
      sessionId: this.sessionId,
      runId: this.runId,
      correlationId: value.identity.transitionId,
      parentNodeId: null,
      sequenceStart: committed.revisionAfter,
      payload: {
        domain,
        operation,
        payload_digest: value.payloadDigest,
      },
      metadata: { canonical_owner: "typescript" },
    });
    this.correlation.close({
      nodeId: correlation.nodeId,
      status: "completed",
      sequenceEnd: committed.revisionAfter,
      result: {
        transition_id: value.identity.transitionId,
        outbox_ids: committed.outboxIds,
      },
    });
    const frame = this.framing.createOutbound("event", {
      domain,
      operation,
      transition_id: value.identity.transitionId,
      payload_digest: value.payloadDigest,
      revision: committed.revisionAfter,
    }, value.identity.transitionId, null);
    this.framing.ackOutbound(frame.sequence);
    this.history.append({
      role: "system",
      content: {
        domain,
        operation,
        transition_id: value.identity.transitionId,
        payload_digest: value.payloadDigest,
      },
      turnId: asRuntimeString((payload as unknown as JsonObject).turn_id, "") || null,
      toolCallId: asRuntimeString((payload as unknown as JsonObject).tool_call_id, "") || null,
      metadata: { canonical_owner: "typescript", revision: committed.revisionAfter },
    });
    this.session.appendMessage({
      role: "system",
      content: {
        domain,
        operation,
        payload_digest: value.payloadDigest,
        transition_id: value.identity.transitionId,
      },
      turnId: asRuntimeString((payload as unknown as JsonObject).turn_id, "") || null,
      toolCallId: asRuntimeString((payload as unknown as JsonObject).tool_call_id, "") || null,
      correlationId: value.identity.transitionId,
      causationId: null,
      metadata: { canonical_owner: "typescript", requires_effect: requiresEffect },
    });
    for (const outboxId of committed.outboxIds) this.journal.project(outboxId);
    return this.journal.ack(committed.identity.transitionId);
  }

  private ensureFramingHandshake(): void {
    if (this.framing.project().handshake_complete === true) return;
    const hello = this.framing.createOutbound("hello", {
      protocol: "zyra.runtime-jsonl/v2",
      runtime: "zyra-typescript-claude-runtime",
      canonical_owner: "typescript",
      restart_epoch: this.journal.restartEpoch,
    }, `${this.sessionId}:framing-handshake:${this.journal.restartEpoch}`, null);
    const receipt = this.framing.feed(this.framing.encode(hello))[0];
    if (receipt?.accepted !== true) {
      throw new Error(`e01_framing_handshake_failed:${receipt?.reason ?? "missing_receipt"}`);
    }
    this.framing.ackOutbound(hello.sequence);
  }

  private applyTypedEffect(
    domain: RuntimeDomain,
    operation: string,
    payload: O,
    correlationId: string,
  ): void {
    const aggregateId = `${this.sessionId}:${domain}`;
    const aggregate = this.protocol.projectAggregate(aggregateId);
    const envelope = this.protocol.createEnvelope({
      kind: "event",
      topic: `runtime.${domain}.${operation}`,
      aggregateId,
      expectedSequence: aggregate.sequence,
      idempotencyKey: `typed:${correlationId}`,
      correlationId,
      causationId: null,
      payload: payload as unknown as JsonObject,
      expiresAt: null,
    });
    const transaction = this.protocol.prepare(envelope.envelopeId);
    this.protocol.applyMutation(transaction.transactionId, {
      domain,
      operation: "merge",
      path: [],
      value: {
        last_operation: operation,
        last_payload_digest: envelope.payloadDigest,
        last_correlation_id: correlationId,
        restart_epoch: this.journal.restartEpoch,
      },
      effective: true,
    });
    this.protocol.commit(transaction.transactionId);
    const delivery = this.protocol.pendingDeliveries(1).find((item) => item.envelopeId === envelope.envelopeId);
    if (delivery) {
      const leased = this.protocol.lease(delivery.deliveryId, "e01-coordinator", 30_000);
      this.protocol.ack(leased.deliveryId, "e01-coordinator");
    }
  }

  private observeTool(operation: string, toolName: string, payload: JsonObject): void {
    if (!toolName) return;
    try {
      this.tools.registry_module({ action: "get", name: toolName });
    } catch {
      this.tools.register({
        name: toolName,
        namespace: "runtime",
        version: "1",
        description: `Runtime-observed tool ${toolName}`,
        inputSchema: { type: "object", properties: {}, additionalProperties: true },
        effects: /write|edit|delete|execute|bash|network/i.test(toolName) ? ["write"] : ["read"],
        risk: /write|edit|delete|execute|bash|network/i.test(toolName) ? "high" : "low",
        readOnly: !/write|edit|delete|execute|bash|network/i.test(toolName),
        supportsStreaming: true,
        supportsCancellation: true,
        idempotent: false,
        maximumResultChars: 8_000,
        timeoutMs: 120_000,
        concurrencyKey: toolName,
        metadata: { discovered_from_default_path: true },
      });
    }
    if (/result|completed|succeeded|failed/i.test(operation)) {
      this.tools.budget_module({ action: "inspect", tool_name: toolName, result_chars: payload.result_chars ?? 0 });
    }
  }

  private recordProviderObservation(operation: string, payload: JsonObject): void {
    this.telemetry.logging_module({
      action: "log",
      level: /error|fail/i.test(operation) ? "error" : "debug",
      name: `provider.${operation}`,
      session_id: this.sessionId,
      run_id: this.runId,
      task_id: this.taskId,
      summary: `provider ${operation}`,
      attributes: payload,
    });
  }

  private applyProviderLifecycle(
    operation: string,
    payload: JsonObject,
    canonicalPayload: O,
  ): void {
    if (operation === "model_request_prepared") {
      const prepared = asRuntimeObject(payload.provider_request);
      if (Object.keys(prepared).length > 0) {
        this.prepareProviderLifecycle(prepared, canonicalPayload);
      }
      return;
    }
    if (operation === "model_stream_frame") {
      const frame = asRuntimeObject(payload.model_stream_frame);
      if (Object.keys(frame).length > 0) {
        this.advanceProviderLifecycle(frame, canonicalPayload);
      }
      return;
    }
    if (operation === "model_stream_report") {
      const report = asRuntimeObject(payload.model_stream);
      if (Object.keys(report).length > 0) {
        this.settleProviderLifecycle(report, canonicalPayload);
      }
    }
  }

  private prepareProviderLifecycle(prepared: JsonObject, canonicalPayload: O): void {
    const requestId = asRuntimeString(prepared.request_id, "");
    if (!requestId) return;
    const existing = this.providerRequestOrNull(requestId);
    if (existing) {
      canonicalPayload.provider_lifecycle = {
        request_id: requestId,
        request_status: existing.status,
        replayed: true,
      };
      return;
    }
    const providerId = normalizeProviderId(asRuntimeString(prepared.provider, "compatible"));
    const modelId = asRuntimeString(prepared.model, "unknown");
    const messages = providerMessages(prepared.messages);
    for (const rawTool of asRuntimeArray(prepared.tools)) {
      const tool = asRuntimeObject(rawTool);
      const definition = asRuntimeObject(tool.function);
      const name = asRuntimeString(definition.name, "");
      if (!name) continue;
      this.providerPrompt.upsertTool({
        name,
        description: asRuntimeString(definition.description, `Runtime tool ${name}`),
        inputSchema: asRuntimeObject(definition.parameters),
        deferred: false,
        strict: false,
        priority: 0,
        usedRecently: true,
        required: false,
      });
    }
    const systemText = providerText(prepared.system);
    if (systemText) {
      this.providerPrompt.upsertSection({
        sectionId: "default-provider-system",
        kind: "runtime",
        title: "Default provider system prompt",
        content: systemText,
        priority: 1_000,
        stable: true,
        cacheTtl: "5m",
        metadata: { canonical_owner: "typescript" },
      });
    }
    const builtPrompt = this.providerPrompt.build(messages, {
      model: modelId,
      cacheEnabled: this.providerPrompt.getPromptCachingEnabled(modelId),
      cacheStrategy: "system_prompt",
      cacheTtl: "5m",
      maximumSystemChars: 250_000,
      maximumToolsChars: 250_000,
      maximumMedia: 20,
      enableDeferredTools: true,
      preserveLastUserMessage: true,
    });
    this.ensureProviderPolicy(providerId);
    const estimatedInputTokens = Math.max(
      1,
      Math.ceil((builtPrompt.systemChars + builtPrompt.messagesChars + builtPrompt.toolsChars) / 4),
    );
    const deadlineAt = Date.now() + 120_000;
    const route = this.providerRouting.decide({
      requestId,
      sessionId: this.sessionId,
      requiredCapabilities: ["text", "streaming"],
      privacyClass: "internal",
      estimatedInputTokens,
      requestedOutputTokens: 16_000,
      maximumCost: null,
      maximumLatencyMilliseconds: null,
      preferredProviderIds: [providerId],
      excludedRouteIds: [],
      stickyRouteId: null,
      allowDegraded: true,
      metadata: { model_id: modelId, canonical_owner: "typescript" },
    });
    this.providerRouting.acquire(route);
    let reservationId = "";
    try {
      const reservation = this.providerRateLimits.reserve({
        requestId,
        sessionId: this.sessionId,
        providerId,
        credentialId: `${providerId}:runtime`,
        modelId,
        requests: 1,
        inputTokens: estimatedInputTokens,
        outputTokens: 16_000,
        estimatedCost: route.estimatedCost,
        priority: 100,
        deadlineAt,
      });
      reservationId = reservation.reservationId;
      const request = this.providerRequests.create({
        requestId,
        sessionId: this.sessionId,
        runId: this.runId,
        queryId: `${this.sessionId}:query`,
        turnId: `${requestId}:turn`,
        idempotencyKey: requestId,
        modelPreference: modelId,
        payload: prepared,
        contextDigest: digest(builtPrompt.messages),
        toolSetDigest: digest(builtPrompt.tools),
        budget: {
          maximumAttempts: 1,
          maximumInputTokens: Math.max(estimatedInputTokens, 1_000_000),
          maximumOutputTokens: 128_000,
          maximumCost: null,
          deadlineAt,
        },
        correlationId: requestId,
        metadata: {
          provider_id: providerId,
          prompt_id: builtPrompt.promptId,
          prompt_fingerprint: builtPrompt.fingerprint,
          route_decision_digest: route.decisionDigest,
        },
      });
      const attempt = this.providerRequests.prepareAttempt({
        requestId,
        routeId: route.routeId,
        providerId,
        modelId,
        endpointId: route.endpointId,
        credentialId: `${providerId}:runtime`,
        requestBody: prepared,
        requestHeaders: {},
        correlationId: `${requestId}:prepare`,
      });
      this.providerRequests.dispatch({
        requestId,
        attemptId: attempt.attemptId,
        correlationId: `${requestId}:dispatch`,
      });
      const response = this.providerResponses.begin({
        requestId,
        attemptId: attempt.attemptId,
        providerId,
        modelId,
      });
      this.providerResponses.apply({
        responseId: response.responseId,
        eventId: `${requestId}:response:start`,
        sequence: 1,
        kind: "message_start",
        payload: { messageId: requestId },
      });
      canonicalPayload.provider_lifecycle = {
        request_id: request.requestId,
        request_status: "dispatched",
        attempt_id: attempt.attemptId,
        response_id: response.responseId,
        route_id: route.routeId,
        route_decision_digest: route.decisionDigest,
        reservation_id: reservation.reservationId,
        prompt_id: builtPrompt.promptId,
        prompt_fingerprint: builtPrompt.fingerprint,
      };
    } catch (error) {
      if (reservationId) {
        const reservation = this.providerRateLimits.snapshot().reservations.find(
          (item) => item.reservationId === reservationId,
        );
        if (reservation?.status === "held") this.providerRateLimits.release(reservationId);
      }
      this.providerRouting.abandon(requestId);
      const request = this.providerRequestOrNull(requestId);
      if (request && !/completed|failed|cancelled/.test(request.status)) {
        this.providerRequests.cancel(requestId, "provider_prepare_failed", `${requestId}:prepare-failed`);
      }
      throw error;
    }
  }

  private advanceProviderLifecycle(frame: JsonObject, canonicalPayload: O): void {
    const requestId = asRuntimeString(frame.request_id, "");
    const request = requestId ? this.providerRequestOrNull(requestId) : null;
    if (!request || /completed|failed|cancelled/.test(request.status)) return;
    const attemptId = request.activeAttemptId;
    if (!attemptId) return;
    const kind = asRuntimeString(frame.kind, "provider_frame");
    if (kind === "request_started") {
      canonicalPayload.provider_lifecycle = {
        request_id: requestId,
        request_status: request.status,
        attempt_id: attemptId,
        frame_kind: kind,
      };
      return;
    }
    if (request.status === "dispatched") {
      this.providerRequests.responseStarted({
        requestId,
        attemptId,
        responseStatus: asRuntimeNumber(frame.response_status) || 200,
        providerRequestId: requestId,
        correlationId: `${requestId}:response-started`,
      });
    }
    const attempt = this.providerRequests.getAttempt(attemptId);
    const sequence = attempt.expectedChunkSequence;
    const serialized = JSON.stringify(frame);
    const chunk = this.providerRequests.appendChunk({
      requestId,
      attemptId,
      chunkId: `${requestId}:chunk:${sequence}`,
      sequence,
      channel: /tool_call/u.test(serialized) ? "tool" : kind === "sse_chunk" ? "text" : "control",
      payload: frame,
      final: false,
    });
    const response = this.providerResponseForAttempt(attemptId);
    if (response && response.status === "open") {
      this.providerResponses.apply({
        responseId: response.responseId,
        eventId: `${requestId}:response:frame:${sequence}`,
        sequence: response.expectedSequence,
        kind: "usage",
        payload: {
          providerFields: {
            frame_kind: kind,
            frame_sequence: sequence,
            frame_digest: chunk.payloadDigest,
          },
        },
      });
    }
    canonicalPayload.provider_lifecycle = {
      request_id: requestId,
      request_status: "streaming",
      attempt_id: attemptId,
      chunk_id: chunk.chunkId,
      chunk_sequence: chunk.sequence,
      response_id: response?.responseId ?? null,
    };
  }

  private settleProviderLifecycle(report: JsonObject, canonicalPayload: O): void {
    const requestId = asRuntimeString(report.request_id, "");
    const request = requestId ? this.providerRequestOrNull(requestId) : null;
    if (!request) return;
    if (/completed|failed|cancelled/.test(request.status)) {
      canonicalPayload.provider_lifecycle = {
        request_id: requestId,
        request_status: request.status,
        replayed: true,
      };
      return;
    }
    const attemptId = request.activeAttemptId;
    if (!attemptId) return;
    const attempt = this.providerRequests.getAttempt(attemptId);
    const response = this.providerResponseForAttempt(attemptId);
    const reservation = this.providerRateLimits.snapshot().reservations.find(
      (item) => item.requestId === requestId && item.status === "held",
    );
    const usage = providerUsage(asRuntimeObject(report.usage));
    const providerId = attempt.providerId;
    const modelId = attempt.modelId;
    this.applyProviderRateHeaders(providerId, modelId, report);
    if (report.ok === true) {
      if (request.status === "dispatched") {
        this.providerRequests.responseStarted({
          requestId,
          attemptId,
          responseStatus: asRuntimeNumber(report.status) || 200,
          providerRequestId: requestId,
          correlationId: `${requestId}:response-started`,
        });
      }
      let normalizedResponse: JsonValue = report;
      if (response && response.status === "open") {
        this.providerResponses.apply({
          responseId: response.responseId,
          eventId: `${requestId}:response:usage`,
          sequence: response.expectedSequence,
          kind: "usage",
          payload: {
            inputTokens: usage.inputTokens,
            outputTokens: usage.outputTokens,
            cacheReadTokens: usage.cacheReadTokens,
            cacheWriteTokens: usage.cacheWriteTokens,
            providerFields: { transport: asRuntimeString(report.transport, "unknown") },
          },
        });
        const next = this.providerResponseForAttempt(attemptId);
        if (next) {
          this.providerResponses.apply({
            responseId: next.responseId,
            eventId: `${requestId}:response:stop`,
            sequence: next.expectedSequence,
            kind: "message_stop",
            payload: {
              stopReason: asRuntimeNumber(report.tool_call_count) > 0 ? "tool_use" : "end_turn",
              stopSequence: null,
            },
          });
          normalizedResponse = this.providerResponses.finalize(next.responseId) as unknown as JsonValue;
        }
      }
      const completed = this.providerRequests.complete({
        requestId,
        attemptId,
        output: normalizedResponse,
        usage,
        stopReason: asRuntimeString(report.decision, "end_turn"),
        correlationId: `${requestId}:complete`,
      });
      const route = this.providerRouting.recordSuccess(
        requestId,
        Math.floor(asRuntimeNumber(report.duration_ms)),
        this.sessionId,
      );
      const committedReservation = reservation
        ? this.providerRateLimits.commit(reservation.reservationId, {
          requests: 1,
          input_tokens: usage.inputTokens,
          output_tokens: usage.outputTokens,
          cost: usage.cost ?? 0,
        })
        : null;
      canonicalPayload.provider_lifecycle = {
        request_id: requestId,
        request_status: completed.status,
        attempt_id: attemptId,
        attempt_status: "succeeded",
        response_id: response?.responseId ?? null,
        response_status: "complete",
        route_id: route.routeId,
        route_status: route.status,
        reservation_id: committedReservation?.reservationId ?? null,
        reservation_status: committedReservation?.status ?? null,
      };
      return;
    }
    const status = asRuntimeNumber(report.status);
    const errorMessage = asRuntimeString(report.error, `provider_http_${status}`);
    const plan = asRuntimeObject(report.recovery_plan);
    const retryable = ["retry", "fallback", "reduce_output"].includes(
      asRuntimeString(plan.action, "stop"),
    );
    const failureClass = providerFailureClass(status, errorMessage);
    const failed = this.providerRequests.failAttempt({
      requestId,
      attemptId,
      errorClass: failureClass,
      errorMessage,
      retryable,
      retryAfterMilliseconds: asRuntimeNumber(plan.delayMs ?? plan.delay_ms),
      correlationId: `${requestId}:failed`,
    });
    if (response?.status === "open") {
      this.providerResponses.fail(response.responseId, failureClass, errorMessage);
    }
    const route = this.providerRouting.recordFailure(requestId, failureClass);
    const releasedReservation = reservation
      ? this.providerRateLimits.release(reservation.reservationId)
      : null;
    canonicalPayload.provider_lifecycle = {
      request_id: requestId,
      request_status: failed.status,
      attempt_id: attemptId,
      attempt_status: "failed",
      response_id: response?.responseId ?? null,
      response_status: "failed",
      route_id: route.routeId,
      route_status: route.status,
      failure_class: failureClass,
      reservation_id: releasedReservation?.reservationId ?? null,
      reservation_status: releasedReservation?.status ?? null,
    };
  }

  private providerRequestOrNull(requestId: string): ReturnType<ProviderRequestRuntime["get"]> | null {
    try {
      return this.providerRequests.get(requestId);
    } catch {
      return null;
    }
  }

  private providerResponseForAttempt(attemptId: string): ReturnType<ProviderResponseRuntime["snapshot"]>["builders"][number] | null {
    return this.providerResponses.snapshot().builders.find((item) => item.attemptId === attemptId) ?? null;
  }

  private applyProviderRateHeaders(providerId: string, modelId: string, report: JsonObject): void {
    const headers = asRuntimeObject(report.response_headers);
    if (Object.keys(headers).length === 0) return;
    this.providerRateLimits.applyProviderHeaders({
      providerId,
      credentialId: `${providerId}:runtime`,
      modelId,
      headers: {
        remainingRequests: nullableHeaderNumber(headers, "x-ratelimit-remaining-requests"),
        remainingInputTokens: nullableHeaderNumber(headers, "x-ratelimit-remaining-input-tokens"),
        remainingOutputTokens: nullableHeaderNumber(headers, "x-ratelimit-remaining-output-tokens"),
        resetRequestsAt: nullableHeaderNumber(headers, "x-ratelimit-reset-requests"),
        resetTokensAt: nullableHeaderNumber(headers, "x-ratelimit-reset-tokens"),
        retryAfterMilliseconds: retryAfterMilliseconds(headers),
      },
    });
  }

  private ensureProviderPolicy(providerId: "anthropic" | "compatible" | "local"): void {
    const routeId = `${providerId}-default`;
    if (!this.providerRouting.snapshot().definitions.some((item) => item.routeId === routeId)) {
      this.providerRouting.register({
        routeId,
        providerId,
        modelId: "claude-default",
        endpointId: `${providerId}-api`,
        region: providerId === "local" ? "local" : "global",
        capabilities: ["text", "reasoning", "tools", "streaming"],
        privacyClasses: ["public", "internal", "sensitive"],
        maximumContextTokens: 1_000_000,
        maximumOutputTokens: 128_000,
        inputCostPerMillion: providerId === "anthropic" ? 15 : 0,
        outputCostPerMillion: providerId === "anthropic" ? 75 : 0,
        baseLatencyMilliseconds: providerId === "local" ? 10 : 500,
        concurrencyLimit: 32,
        priority: 100,
        enabled: true,
        metadata: { canonical_owner: "typescript", dynamic_model: true },
      });
    }
    const limitId = `${providerId}-default-requests`;
    if (!this.providerRateLimits.snapshot().definitions.some((item) => item.limitId === limitId)) {
      this.providerRateLimits.register({
        limitId,
        scopeKind: "provider",
        scopeId: providerId,
        dimension: "requests",
        capacity: 1_000,
        refillAmount: 1_000,
        refillIntervalMilliseconds: 60_000,
        burstCapacity: 1_000,
        enabled: true,
        priority: 100,
        metadata: { canonical_owner: "typescript" },
      });
    }
  }

  async runHooks(phase: HookPhase, payload: JsonObject): Promise<HookPhaseResult> {
    return this.hooks.run({
      sessionId: this.sessionId,
      runId: this.runId,
      queryId: `${this.sessionId}:query`,
      turnId: asRuntimeString(payload.turn_id, "") || null,
      phase,
      correlationId: asRuntimeString(
        payload.correlation_id,
        `${this.sessionId}:hook:${phase}:${this.journal.revision + 1}`,
      ),
      payload,
      attempt: 1,
    });
  }

  private installDefaultStopRules(): void {
    this.stop.register({
      ruleId: "zyra.explicit-cancel",
      owner: "e01-coordinator",
      priority: -1_000,
      enabled: true,
      signals: ["explicit_cancel", "external_interrupt"],
      match: "all",
      conditions: [],
      action: "stop",
      reason: "user_cancelled",
      terminal: true,
      retryAfterMilliseconds: null,
      requireTerminalAnswer: false,
      revision: 1,
      metadata: { canonical_owner: "typescript" },
    });
    this.stop.register({
      ruleId: "zyra.maximum-turns",
      owner: "e01-coordinator",
      priority: -900,
      enabled: true,
      signals: ["maximum_turns", "maximum_tool_calls", "token_budget", "cost_budget", "wall_deadline"],
      match: "all",
      conditions: [],
      action: "stop",
      reason: "runtime_budget_exhausted",
      terminal: true,
      retryAfterMilliseconds: null,
      requireTerminalAnswer: false,
      revision: 1,
      metadata: { canonical_owner: "typescript" },
    });
  }

  private installDefaultHooks(): void {
    this.hooks.register({
      descriptor: {
        hookId: "zyra.query-abort-guard",
        phase: "query.accept",
        priority: -1_000,
        timeoutMilliseconds: 1_000,
        failureMode: "fail_closed",
        enabled: true,
        before: [],
        after: [],
        owner: "e01-coordinator",
        revision: 1,
        metadata: { canonical_owner: "typescript" },
      },
      handler: (context) =>
        context.payload.aborted === true
          ? { kind: "block", reason: "query_aborted" }
          : { kind: "continue", reason: "query_not_aborted" },
    });
  }

  private installDefaultProviderPolicy(): void {
    this.providerRouting.register({
      routeId: "anthropic-default",
      providerId: "anthropic",
      modelId: "claude-default",
      endpointId: "anthropic-api",
      region: "global",
      capabilities: ["text", "reasoning", "tools", "streaming"],
      privacyClasses: ["public", "internal", "sensitive"],
      maximumContextTokens: 1_000_000,
      maximumOutputTokens: 128_000,
      inputCostPerMillion: 15,
      outputCostPerMillion: 75,
      baseLatencyMilliseconds: 500,
      concurrencyLimit: 32,
      priority: 100,
      enabled: true,
      metadata: { canonical_owner: "typescript" },
    });
    this.providerRateLimits.register({
      limitId: "anthropic-default-requests",
      scopeKind: "provider",
      scopeId: "anthropic",
      dimension: "requests",
      capacity: 1_000,
      refillAmount: 1_000,
      refillIntervalMilliseconds: 60_000,
      burstCapacity: 1_000,
      enabled: true,
      priority: 100,
      metadata: { canonical_owner: "typescript" },
    });
  }

  private installDefaultCommands(): void {
    this.commands.register({
      commandName: "runtime.inventory",
      owner: "e01-coordinator",
      description: "Return the live E01 runtime custody inventory.",
      risk: "read",
      fields: [],
      requiredCapabilities: ["runtime.read"],
      allowedSessionStates: ["active", "paused"],
      timeoutMilliseconds: 5_000,
      idempotent: true,
      enabled: true,
      revision: 1,
      metadata: { canonical_owner: "typescript" },
    }, () => ({ result: this.inventory() }));
    this.commands.register({
      commandName: "session.cancel",
      owner: "e01-coordinator",
      description: "Cancel the active query using the canonical lifecycle owner.",
      risk: "interrupt",
      fields: [{
        name: "reason",
        type: "string",
        required: true,
        enumValues: [],
        minimum: null,
        maximum: null,
        maximumLength: 1_000,
      }],
      requiredCapabilities: ["runtime.control"],
      allowedSessionStates: ["active", "paused"],
      timeoutMilliseconds: 5_000,
      idempotent: true,
      enabled: true,
      revision: 1,
      metadata: { canonical_owner: "typescript" },
    }, (context) => {
      const reason = asRuntimeString(context.arguments.reason, "control_command_cancelled");
      const status = asRuntimeString(this.query.project().status, "");
      if (!/completed|failed|cancelled/.test(status)) {
        this.query.cancel(`command:${context.commandId}`, reason);
      }
      return {
        result: { cancelled: true, reason, query_status: this.query.project().status ?? null },
        effectIds: [`query-cancel:${context.commandId}`],
      };
    });
  }

  private domainForPhase(phase: string): RuntimeDomain {
    for (const [pattern, domain] of DOMAIN_BY_PHASE) {
      if (pattern.test(phase)) return domain;
    }
    return "query";
  }
}

function asRuntimeString(value: JsonValue | undefined, fallback: string): string {
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}

function asRuntimeObject(value: JsonValue | undefined): JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as JsonObject : {};
}

function asRuntimeNumber(value: JsonValue | undefined): number {
  const selected = Number(value);
  return Number.isFinite(selected) ? Math.max(0, selected) : 0;
}

function asRuntimeArray(value: JsonValue | undefined): JsonValue[] {
  return Array.isArray(value) ? value : [];
}

function providerMessages(value: JsonValue | undefined): ProviderMessage[] {
  return asRuntimeArray(value).map((raw) => {
    const message = asRuntimeObject(raw);
    const role = asRuntimeString(message.role, "user") === "assistant" ? "assistant" : "user";
    return {
      role,
      content: [{ type: "text", text: providerText(message.content) || " " }],
    };
  });
}

function providerText(value: JsonValue | undefined): string {
  if (typeof value === "string") return value.trim();
  if (value === undefined || value === null) return "";
  return JSON.stringify(value);
}

function providerUsage(value: JsonObject): {
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
  cost: number | null;
} {
  return {
    inputTokens: asRuntimeNumber(value.input_tokens),
    outputTokens: asRuntimeNumber(value.output_tokens),
    cacheReadTokens: asRuntimeNumber(value.cache_read_input_tokens),
    cacheWriteTokens: asRuntimeNumber(value.cache_creation_input_tokens),
    cost: value.cost === undefined || value.cost === null ? null : asRuntimeNumber(value.cost),
  };
}

function normalizeProviderId(value: string): "anthropic" | "compatible" | "local" {
  if (value === "anthropic" || value === "local") return value;
  return "compatible";
}

function providerFailureClass(status: number, message: string): ProviderFailureClass {
  if (status === 401 || status === 403) return "authentication";
  if (status === 429) return "rate_limit";
  if (status === 408 || /timeout/i.test(message)) return "timeout";
  if (status >= 500) return "overloaded";
  if (status >= 400) return "invalid_request";
  if (status === 0) return "transport";
  return "unknown";
}

function nullableHeaderNumber(headers: JsonObject, name: string): number | null {
  const value = headers[name];
  if (value === undefined || value === null || value === "") return null;
  const selected = Number(value);
  return Number.isFinite(selected) ? Math.max(0, selected) : null;
}

function retryAfterMilliseconds(headers: JsonObject): number | null {
  const value = headers["retry-after"];
  if (value === undefined || value === null || value === "") return null;
  const seconds = Number(value);
  if (Number.isFinite(seconds)) return Math.max(0, Math.floor(seconds * 1_000));
  const timestamp = Date.parse(String(value));
  return Number.isFinite(timestamp) ? Math.max(0, timestamp - Date.now()) : null;
}
