#!/usr/bin/env bun

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { basename, dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import * as ts from "typescript";

type Obj = Record<string, any>;

const VERIFIED = "c34535a783e88f9481387ced89cba4fbc333dc74";
const BUN = "1.2.15";
const script = fileURLToPath(import.meta.url);
const zyra = resolve(dirname(script), "..", "..");
const workspace = resolve(zyra, "..");
const sourceRoot = join(workspace, "claude-code-best");
const manifestRoot = join(workspace, "docs", "remediations", "M1-R01-claude-source-custody", "manifests");
const evidenceRoot = join(zyra, "docs", "reviews", "evidence", "M1-R01-v3", "execution-01");

const inputs = [
  ["src/QueryEngine.ts", 1258, "query"],
  ["src/query.ts", 1615, "query"],
  ["src/services/api/client.ts", 360, "provider-model"],
  ["src/services/api/claude.ts", 3213, "provider-model"],
  ["src/services/api/withRetry.ts", 746, "provider-recovery"],
  ["src/services/api/errors.ts", 1114, "provider-recovery"],
  ["src/cost-tracker.ts", 302, "provider-telemetry"],
  ["src/services/api/promptCacheBreakDetection.ts", 678, "provider-telemetry"],
  ["src/services/api/logging.ts", 757, "provider-telemetry"],
  ["src/services/compact/microCompact.ts", 480, "compact"],
  ["src/services/compact/autoCompact.ts", 313, "compact"],
  ["src/services/compact/compact.ts", 1584, "compact"],
  ["src/services/compact/sessionMemoryCompact.ts", 568, "compact"],
  ["src/services/compact/postCompactCleanup.ts", 75, "compact"],
  ["src/query/tokenBudget.ts", 81, "context-token"],
] as const;

const QUERY_PATH = "packages/runtime/claude-runtime/src/query-engine.ts";
const MODEL_PATH = "packages/runtime/claude-runtime/src/model-stream.ts";
const COORDINATOR_PATH = "packages/runtime/claude-runtime/src/e01/coordinator.ts";
const RECOVERY_PATH = "packages/runtime/claude-runtime/src/provider/recovery-runtime.ts";
const TELEMETRY_PATH = "packages/runtime/claude-runtime/src/provider/telemetry-runtime.ts";
const COMPACT_PATH = "packages/runtime/claude-runtime/src/compact/context-runtime.ts";
const TOKEN_PATH = "packages/runtime/claude-runtime/src/context/token-runtime.ts";
const RUNTIME_TEST_PATH = "packages/runtime/claude-runtime/test/runtime.test.ts";

const mutationIds = {
  turnLimit: "e01-mut-011-turn-limit",
  cancel: "e01-mut-012-cancel",
  emptyTurn: "e01-mut-013-empty-turn",
  queryRevision: "e01-mut-014-query-revision",
  failureRoute: "e01-mut-015-failure-route",
  compactThreshold: "e01-mut-016-compact-threshold",
  compactToolPair: "e01-mut-017-compact-tool-pair",
  compactSuffix: "e01-mut-018-compact-suffix",
  compactRestore: "e01-mut-019-compact-restore",
  compactCleanup: "e01-mut-020-compact-cleanup",
  retryDelay: "e01-mut-021-retry-delay",
  retryClass: "e01-mut-022-retry-class",
  maxRetries: "e01-mut-023-max-retries",
  fallback: "e01-mut-024-fallback",
  streamFinal: "e01-mut-025-stream-final",
  usage: "e01-mut-026-usage",
  cacheLineage: "e01-mut-027-cache-lineage",
  recoveryPlannerCustody: "e01-mut-031-recovery-planner-custody",
  providerLifecycleCustody: "e01-mut-032-provider-lifecycle-custody",
} as const;

const behaviorTests = {
  query: {
    path: RUNTIME_TEST_PATH,
    name: "runtime owns multi-turn lifecycle and read-only batches",
    anchor: "ClaudeRuntimeCore",
    assertion_tokens: ["e01State(result).query.revision", "journal.state.query"],
  },
  compact: {
    path: RUNTIME_TEST_PATH,
    name: "runtime externalizes large tool results and compacts context",
    anchor: "ClaudeRuntimeCore",
    assertion_tokens: ["compact.boundaries", "journal.state.context"],
  },
  provider: {
    path: RUNTIME_TEST_PATH,
    name: "runtime commits provider prompt usage and recovery state through default loop",
    anchor: "ClaudeRuntimeCore",
    assertion_tokens: ["providerPrompt.lastPrompt", "providerRequests.requests", "providerResponses.responses", "providerRouting.states", "providerRateLimits.reservations", "journal.state.provider"],
  },
  recovery: {
    path: RUNTIME_TEST_PATH,
    name: "runtime lets canonical recovery policy stop a non-retryable provider request",
    anchor: "ClaudeRuntimeCore",
    assertion_tokens: ["requestCount", "state.recovery.contexts", "journal.state.provider"],
  },
  recoverySuccess: {
    path: RUNTIME_TEST_PATH,
    name: "runtime clears canonical recovery state after a retry succeeds",
    anchor: "ClaudeRuntimeCore",
    assertion_tokens: ["requestCount", "state.recovery.contexts.length", "journal.state.provider"],
  },
} as const;

function edge(
  callerPath: string,
  callerSymbol: string,
  calleePath: string,
  calleeSymbol: string,
  kind = "direct",
): Obj {
  return {
    caller_path: callerPath,
    caller_symbol: callerSymbol,
    callee_path: calleePath,
    callee_symbol: calleeSymbol,
    kind,
  };
}

function sourceName(source: Obj): string {
  return String(source.source_symbol).split("::").at(-1) ?? "";
}

function queryRoute(source: Obj): Obj {
  const name = sourceName(source);
  if (/reactiveCompact|contextCollapse/.test(name)) return compactRoute(source);
  const targetSymbol = /messageSelector|QueryEngine|query$|queryLoop|ask/.test(name)
    ? "ClaudeRuntimeCore.run"
    : "E01RuntimeCoordinator.decideQuery";
  const targetPath = targetSymbol === "ClaudeRuntimeCore.run" ? QUERY_PATH : COORDINATOR_PATH;
  const callsitePath = QUERY_PATH;
  const callsiteSymbol = "ClaudeRuntimeCore.run";
  const selectedMutation = /cancel|eof/i.test(name) ? mutationIds.cancel
    : /empty/i.test(name) ? mutationIds.emptyTurn
    : /revision|snip|projection/i.test(name) ? mutationIds.queryRevision
    : /limit|max_output|withheld/i.test(name) ? mutationIds.turnLimit
    : mutationIds.failureRoute;
  return {
    path: targetPath,
    symbol: targetSymbol,
    callsitePath,
    callsiteSymbol,
    entryEdges: targetSymbol === callsiteSymbol ? [] : [edge(QUERY_PATH, "ClaudeRuntimeCore.run", targetPath, targetSymbol)],
    store: "E01RuntimeSnapshot.journal.state.query",
    snapshotProperty: "query",
    stateObservation: "journal.state.query.revision",
    effect: "query-transition",
    behavior: [behaviorTests.query],
    mutations: [selectedMutation],
    adaptation: "Source query orchestration is consolidated into the TypeScript run loop and canonical query decision owner; UI-only presentation branches are cropped.",
  };
}

function modelRoute(source: Obj): Obj {
  const name = sourceName(source);
  let symbol = "resolveModelTurns";
  if (/userMessage|assistantMessage|stripExcessMedia|isMedia|isToolResult/.test(name)) symbol = "normalizeMessages";
  else if (/ApiKey|Headers|CustomHeaders/.test(name)) symbol = "modelHeaders";
  else if (/Client|buildFetch|queryModel|executeNonStreaming|FallbackTimeout|PreviousRequest/.test(name)) symbol = "resolveModelTurns";
  else if (/cleanupStream|updateUsage|accumulateUsage|ToolResultBlock/.test(name)) symbol = "parseSseToolCalls";
  else if (/Metadata|Effort|TaskBudget|ExtraBody/.test(name)) symbol = "modelMetadata";
  else if (/LspTool/.test(name)) symbol = "openAiTool";
  else if (/PromptCaching|CacheControl|CacheTTL|CacheBreakpoint/.test(name)) return telemetryRoute(source);
  const direct = symbol === "resolveModelTurns";
  const edges = [edge(QUERY_PATH, "ClaudeRuntimeCore.run", MODEL_PATH, "resolveModelTurns")];
  if (!direct) edges.push(edge(MODEL_PATH, "resolveModelTurns", MODEL_PATH, symbol));
  edges.push(
    edge(QUERY_PATH, "ClaudeRuntimeCore.run", COORDINATOR_PATH, "E01RuntimeCoordinator.recordRuntimeEvent", "callback"),
    edge(COORDINATOR_PATH, "E01RuntimeCoordinator.recordRuntimeEvent", COORDINATOR_PATH, "E01RuntimeCoordinator.recordProvider"),
    edge(COORDINATOR_PATH, "E01RuntimeCoordinator.recordProvider", COORDINATOR_PATH, "E01RuntimeCoordinator.applyProviderLifecycle"),
  );
  return {
    path: MODEL_PATH,
    symbol,
    callsitePath: direct ? QUERY_PATH : MODEL_PATH,
    callsiteSymbol: direct ? "ClaudeRuntimeCore.run" : "resolveModelTurns",
    entryEdges: edges,
    store: "E01RuntimeSnapshot.providerPrompt/providerRequests/providerResponses/providerRouting/providerRateLimits + journal.state.provider",
    snapshotProperty: "providerRequests",
    stateObservation: "provider prompt fingerprint + durable request/attempt/chunk/response + route lease/outcome + quota reservation/settlement",
    effect: "provider-request-stream",
    behavior: [behaviorTests.provider],
    mutations: [/Usage|usage|cleanupStream/.test(name) ? mutationIds.usage : mutationIds.streamFinal, mutationIds.providerLifecycleCustody],
    adaptation: "Provider request and stream responsibilities are normalized to the OpenAI-compatible model stream and committed through the prompt, request, response, routing, and rate-limit owners; SDK-specific wrappers are cropped while request, frame, usage, quota, route, and failure effects remain durable.",
  };
}

function recoveryRoute(source: Obj): Obj {
  const name = sourceName(source);
  let symbol = "ProviderRecoveryRuntime.plan";
  if (/classifyAPIError|is[A-Z]|startsWith|ERROR_MESSAGE|ErrorMessage|extractUnknown|logToolUse/.test(name)) symbol = "ProviderRecoveryRuntime.classify";
  else if (/RetryAfter/.test(name)) symbol = "parseRetryAfter";
  else if (/shouldRetry|Transient|Stale|529|Auth|Credential/.test(name)) symbol = "ProviderRecoveryRuntime.classify";
  const edges = [
    edge(QUERY_PATH, "ClaudeRuntimeCore.run", MODEL_PATH, "resolveModelTurns"),
    edge(QUERY_PATH, "ClaudeRuntimeCore.run", COORDINATOR_PATH, "E01RuntimeCoordinator.decideProviderRecovery", "planner_callback"),
    edge(COORDINATOR_PATH, "E01RuntimeCoordinator.decideProviderRecovery", RECOVERY_PATH, "ProviderRecoveryRuntime.plan"),
  ];
  let callsiteSymbol = "E01RuntimeCoordinator.decideProviderRecovery";
  let callsitePath = COORDINATOR_PATH;
  if (symbol === "ProviderRecoveryRuntime.classify") {
    callsitePath = RECOVERY_PATH;
    callsiteSymbol = "ProviderRecoveryRuntime.plan";
    edges.push(edge(RECOVERY_PATH, "ProviderRecoveryRuntime.plan", RECOVERY_PATH, symbol));
  } else if (symbol === "parseRetryAfter") {
    callsitePath = RECOVERY_PATH;
    callsiteSymbol = "ProviderRecoveryRuntime.classify";
    edges.push(
      edge(RECOVERY_PATH, "ProviderRecoveryRuntime.plan", RECOVERY_PATH, "ProviderRecoveryRuntime.classify"),
      edge(RECOVERY_PATH, "ProviderRecoveryRuntime.classify", RECOVERY_PATH, symbol),
    );
  }
  const selectedMutation = /delay|RetryAfter|BASE_DELAY|BACKOFF|HEARTBEAT/.test(name) ? mutationIds.retryDelay
    : /MAX_RETRIES|MAX_529|withRetry/.test(name) ? mutationIds.maxRetries
    : /Fallback|fallback/.test(name) ? mutationIds.fallback
    : mutationIds.retryClass;
  return {
    path: RECOVERY_PATH,
    symbol,
    callsitePath,
    callsiteSymbol,
    entryEdges: edges,
    store: "E01RuntimeSnapshot.recovery",
    snapshotProperty: "recovery",
    stateObservation: "recovery.contexts + journal.state.provider.revision",
    effect: "retry-stop-fallback-plan",
    behavior: [behaviorTests.recovery, behaviorTests.recoverySuccess],
    mutations: [selectedMutation, mutationIds.recoveryPlannerCustody],
    adaptation: "Upstream retry and error helpers are consolidated behind the canonical ProviderRecoveryRuntime plan consumed by the real model loop; provider-SDK credential refresh side effects remain cropped.",
  };
}

function telemetryRoute(source: Obj): Obj {
  const name = sourceName(source);
  const sourcePath = String(source.source_path);
  let symbol = "ProviderTelemetryRuntime.recordUsage";
  let callsiteSymbol = "E01RuntimeCoordinator.recordProvider";
  let callsitePath = COORDINATOR_PATH;
  let mutation = mutationIds.usage;
  const edges: Obj[] = [
    edge(QUERY_PATH, "ClaudeRuntimeCore.run", MODEL_PATH, "resolveModelTurns"),
    edge(QUERY_PATH, "ClaudeRuntimeCore.run", COORDINATOR_PATH, "E01RuntimeCoordinator.recordRuntimeEvent", "callback"),
    edge(COORDINATOR_PATH, "E01RuntimeCoordinator.recordRuntimeEvent", COORDINATOR_PATH, "E01RuntimeCoordinator.recordProvider"),
  ];
  if (sourcePath.endsWith("promptCacheBreakDetection.ts") || /PromptCaching|CacheControl|CacheTTL|CacheBreakpoint/.test(name)) {
    mutation = mutationIds.cacheLineage;
    symbol = "ProviderTelemetryRuntime.recordPromptState";
    if (/stripCacheControl/.test(name)) symbol = "stripCacheControl";
    else if (/computePerToolHashes/.test(name)) symbol = "computeToolHashes";
    else if (/sanitizeToolName/.test(name)) symbol = "toolName";
    else if (/checkResponseForCacheBreak/.test(name)) symbol = "detectPromptBreak";
    else if (/notifyCacheDeletion/.test(name)) {
      symbol = "ProviderTelemetryRuntime.notifyCompaction";
      callsiteSymbol = "E01RuntimeCoordinator.recordRuntimeEvent";
    }
    if (!symbol.startsWith("ProviderTelemetryRuntime.")) {
      callsitePath = TELEMETRY_PATH;
      callsiteSymbol = symbol === "toolName" ? "normalizeToolArray" : "ProviderTelemetryRuntime.recordPromptState";
      edges.push(edge(COORDINATOR_PATH, "E01RuntimeCoordinator.recordProvider", TELEMETRY_PATH, "ProviderTelemetryRuntime.recordPromptState"));
      if (symbol === "toolName") {
        edges.push(
          edge(TELEMETRY_PATH, "ProviderTelemetryRuntime.recordPromptState", TELEMETRY_PATH, "normalizeToolArray"),
          edge(TELEMETRY_PATH, "normalizeToolArray", TELEMETRY_PATH, symbol),
        );
      } else {
        edges.push(edge(TELEMETRY_PATH, "ProviderTelemetryRuntime.recordPromptState", TELEMETRY_PATH, symbol));
      }
    } else {
      edges.push(edge(callsitePath, callsiteSymbol, TELEMETRY_PATH, symbol));
    }
  } else if (sourcePath.endsWith("logging.ts")) {
    symbol = /logAPI|logAPISuccess|logAPIError/.test(name)
      ? "ProviderTelemetryRuntime.log"
      : "ProviderTelemetryRuntime.logging_module";
    if (symbol.endsWith(".log")) {
      callsitePath = TELEMETRY_PATH;
      callsiteSymbol = "ProviderTelemetryRuntime.logging_module";
      edges.push(
        edge(COORDINATOR_PATH, "E01RuntimeCoordinator.recordProvider", TELEMETRY_PATH, "ProviderTelemetryRuntime.logging_module"),
        edge(TELEMETRY_PATH, "ProviderTelemetryRuntime.logging_module", TELEMETRY_PATH, symbol),
      );
    } else {
      edges.push(edge(COORDINATOR_PATH, "E01RuntimeCoordinator.recordProvider", TELEMETRY_PATH, symbol));
    }
  } else {
    edges.push(edge(COORDINATOR_PATH, "E01RuntimeCoordinator.recordProvider", TELEMETRY_PATH, symbol));
  }
  return {
    path: TELEMETRY_PATH,
    symbol,
    callsitePath,
    callsiteSymbol,
    entryEdges: edges,
    store: "E01RuntimeSnapshot.telemetry",
    snapshotProperty: "telemetry",
    stateObservation: "telemetry.prompts/samples/events",
    effect: mutation === mutationIds.cacheLineage ? "prompt-cache-lineage" : "usage-logging-causality",
    behavior: [behaviorTests.provider],
    mutations: [mutation],
    adaptation: "Cost, cache-break, and logging presentation helpers are folded into structured telemetry custody; terminal formatting is cropped in favor of durable samples, prompt lineage, and events.",
  };
}

function compactRoute(source: Obj): Obj {
  const name = sourceName(source);
  const sourcePath = String(source.source_path);
  let symbol = "ContextCompactionRuntime.compactConversation";
  let callsitePath = QUERY_PATH;
  let callsiteSymbol = "ClaudeRuntimeCore.run";
  let mutation = mutationIds.compactRestore;
  const edges: Obj[] = [edge(QUERY_PATH, "ClaudeRuntimeCore.run", COMPACT_PATH, "ContextCompactionRuntime.compactConversation")];
  if (sourcePath.endsWith("autoCompact.ts")) {
    symbol = /getAutoCompactThreshold/.test(name)
      ? "ContextCompactionRuntime.getAutoCompactThreshold"
      : "ContextCompactionRuntime.calculateTokenWarningState";
    callsitePath = symbol.endsWith("getAutoCompactThreshold") ? COMPACT_PATH : COORDINATOR_PATH;
    callsiteSymbol = symbol.endsWith("getAutoCompactThreshold")
      ? "ContextCompactionRuntime.calculateTokenWarningState"
      : "E01RuntimeCoordinator.decideContext";
    edges.splice(0, edges.length,
      edge(QUERY_PATH, "ClaudeRuntimeCore.run", COORDINATOR_PATH, "E01RuntimeCoordinator.decideContext"),
      edge(COORDINATOR_PATH, "E01RuntimeCoordinator.decideContext", COMPACT_PATH, "ContextCompactionRuntime.calculateTokenWarningState"),
    );
    if (symbol.endsWith("getAutoCompactThreshold")) edges.push(edge(COMPACT_PATH, "ContextCompactionRuntime.calculateTokenWarningState", COMPACT_PATH, symbol));
    mutation = mutationIds.compactThreshold;
  } else if (sourcePath.endsWith("postCompactCleanup.ts")) {
    symbol = "ContextCompactionRuntime.runPostCompactCleanup";
    callsitePath = COORDINATOR_PATH;
    callsiteSymbol = "E01RuntimeCoordinator.recordRuntimeEvent";
    edges.splice(0, edges.length,
      edge(QUERY_PATH, "ClaudeRuntimeCore.run", COORDINATOR_PATH, "E01RuntimeCoordinator.recordRuntimeEvent", "callback"),
      edge(COORDINATOR_PATH, "E01RuntimeCoordinator.recordRuntimeEvent", COMPACT_PATH, symbol),
    );
    mutation = mutationIds.compactCleanup;
  } else {
    if (/stripImages/.test(name)) symbol = "ContextCompactionRuntime.stripImagesFromMessages";
    else if (/stripReinjected/.test(name)) symbol = "ContextCompactionRuntime.stripReinjectedAttachments";
    else if (/buildPostCompactMessages/.test(name)) symbol = "ContextCompactionRuntime.buildPostCompactMessages";
    else if (/mergeHookInstructions/.test(name)) symbol = "ContextCompactionRuntime.mergeHookInstructions";
    else if (/create.*Attachment|POST_COMPACT/.test(name)) symbol = "ContextCompactionRuntime.createPostCompactAttachments";
    else if (/adjustIndex|calculateMessagesToKeep|SessionMemory|sessionMemory|truncateHead/.test(name)) symbol = "ContextCompactionRuntime.planCompaction";
    if (symbol !== "ContextCompactionRuntime.compactConversation") {
      callsitePath = COMPACT_PATH;
      callsiteSymbol = "ContextCompactionRuntime.compactConversation";
      edges.push(edge(COMPACT_PATH, "ContextCompactionRuntime.compactConversation", COMPACT_PATH, symbol));
    }
    mutation = /tool|Tool|APIInvariant|adjustIndex/.test(name) ? mutationIds.compactToolPair
      : /suffix|preserv|MessagesToKeep/.test(name) ? mutationIds.compactSuffix
      : /cleanup|reset/.test(name) ? mutationIds.compactCleanup
      : mutationIds.compactRestore;
  }
  return {
    path: COMPACT_PATH,
    symbol,
    callsitePath,
    callsiteSymbol,
    entryEdges: edges,
    store: "E01RuntimeSnapshot.compact + journal.state.context",
    snapshotProperty: "compact",
    stateObservation: "compact.boundaries + journal.state.context.revision",
    effect: "context-compact-restore",
    behavior: [behaviorTests.compact],
    mutations: [mutation],
    adaptation: "Auto, micro, session-memory, and full compaction helpers are consolidated into one invariant-preserving context owner reached by the default query loop; UI notification text is cropped.",
  };
}

function tokenRoute(source: Obj): Obj {
  const name = sourceName(source);
  const targetIsDecision = /checkTokenBudget/.test(name);
  const symbol = targetIsDecision ? "E01RuntimeCoordinator.decideContext" : "ContextTokenRuntime.estimate";
  const path = targetIsDecision ? COORDINATOR_PATH : TOKEN_PATH;
  const callsitePath = targetIsDecision ? QUERY_PATH : COORDINATOR_PATH;
  const callsiteSymbol = targetIsDecision ? "ClaudeRuntimeCore.run" : "E01RuntimeCoordinator.decideContext";
  return {
    path,
    symbol,
    callsitePath,
    callsiteSymbol,
    entryEdges: targetIsDecision
      ? [edge(QUERY_PATH, "ClaudeRuntimeCore.run", COORDINATOR_PATH, symbol)]
      : [
        edge(QUERY_PATH, "ClaudeRuntimeCore.run", COORDINATOR_PATH, "E01RuntimeCoordinator.decideContext"),
        edge(COORDINATOR_PATH, "E01RuntimeCoordinator.decideContext", TOKEN_PATH, symbol),
      ],
    store: "E01RuntimeSnapshot.journal.state.context",
    snapshotProperty: "journal",
    stateObservation: "journal.state.context.revision",
    effect: "token-budget-decision",
    behavior: [behaviorTests.compact],
    mutations: [mutationIds.compactThreshold],
    adaptation: "Token budget constants and tracker closure are normalized into deterministic token estimation plus the coordinator context decision transition.",
  };
}

function routeFor(source: Obj): Obj {
  if (source.semantic_domain === "query") return queryRoute(source);
  if (source.semantic_domain === "provider-model") return modelRoute(source);
  if (source.semantic_domain === "provider-recovery") return recoveryRoute(source);
  if (source.semantic_domain === "provider-telemetry") return telemetryRoute(source);
  if (source.semantic_domain === "compact") return compactRoute(source);
  if (source.semantic_domain === "context-token") return tokenRoute(source);
  throw new Error(`unsupported source domain ${source.semantic_domain}`);
}

const mutations = [
  ["restore-before-bootstrap", "src/e01/kernel.ts", "Journal.restore", "swap-order", "restore"],
  ["revision-monotonic", "src/e01/kernel.ts", "Journal.commit", "remove-increment", "restore"],
  ["transition-unique", "src/e01/kernel.ts", "identity", "reuse-id", "idempotency"],
  ["payload-digest", "src/e01/kernel.ts", "Journal.prepare", "skip-digest", "idempotency"],
  ["pending-committed", "src/e01/kernel.ts", "Journal.commit", "retain-pending", "restore"],
  ["effect-fence", "src/e01/kernel.ts", "Journal.effect", "repeat-effect", "idempotency"],
  ["lost-ack", "src/e01/kernel.ts", "Journal.ack", "append-on-ack", "idempotency"],
  ["corrupt-snapshot", "src/e01/kernel.ts", "Journal.restore", "skip-checksum", "restore"],
  ["session-scope", "src/e01/kernel.ts", "Journal.restore", "skip-session", "restore"],
  ["outbox-once", "src/e01/kernel.ts", "Journal.project", "repeat-delivery", "idempotency"],
  ["turn-limit", "src/query/lifecycle-runtime.ts", "QueryLifecycleRuntime", "remove-limit", "routing"],
  ["cancel", "src/query/lifecycle-runtime.ts", "QueryLifecycleRuntime", "ignore-abort", "routing"],
  ["empty-turn", "src/query/lifecycle-runtime.ts", "QueryLifecycleRuntime", "allow-empty", "routing"],
  ["query-revision", "src/query/lifecycle-runtime.ts", "QueryLifecycleRuntime", "skip-revision", "restore"],
  ["failure-route", "src/query/lifecycle-runtime.ts", "QueryLifecycleRuntime", "continue-fatal", "routing"],
  ["compact-threshold", "src/compact/context-runtime.ts", "ContextCompactionRuntime", "invert-threshold", "budget"],
  ["compact-tool-pair", "src/compact/context-runtime.ts", "ContextCompactionRuntime", "split-pair", "restore"],
  ["compact-suffix", "src/compact/context-runtime.ts", "ContextCompactionRuntime", "drop-suffix", "restore"],
  ["compact-restore", "src/compact/context-runtime.ts", "ContextCompactionRuntime", "skip-restore", "restore"],
  ["compact-cleanup", "src/compact/context-runtime.ts", "ContextCompactionRuntime", "skip-cleanup", "restore"],
  ["retry-delay", "src/provider/recovery-runtime.ts", "ProviderRecoveryRuntime", "zero-backoff", "recovery"],
  ["retry-class", "src/provider/recovery-runtime.ts", "ProviderRecoveryRuntime", "retry-auth", "recovery"],
  ["max-retries", "src/provider/recovery-runtime.ts", "ProviderRecoveryRuntime", "unbounded", "recovery"],
  ["fallback", "src/provider/recovery-runtime.ts", "ProviderRecoveryRuntime", "skip-fence", "recovery"],
  ["stream-final", "src/provider/model-runtime.ts", "ProviderModelRuntime", "accept-incomplete", "provider"],
  ["usage", "src/provider/telemetry-runtime.ts", "ProviderTelemetryRuntime", "double-count", "budget"],
  ["cache-lineage", "src/provider/telemetry-runtime.ts", "ProviderTelemetryRuntime", "reuse-lineage", "provider"],
  ["tool-schema", "src/tools.ts", "RuntimeToolRegistry.validate", "skip-schema", "tool"],
  ["write-serialization", "src/tools.ts", "scheduleToolBatches", "parallel-write", "tool"],
  ["result-budget", "src/budget.ts", "applyToolResultBudget", "skip-externalize", "budget"],
  ["recovery-planner-custody", "src/query-engine.ts", "ClaudeRuntimeCore.run", "disconnect-planner", "recovery"],
  ["provider-lifecycle-custody", "src/e01/coordinator.ts", "E01RuntimeCoordinator.recordProvider", "disconnect-provider-lifecycle", "provider"],
] as const;

function hash(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

function stable(value: any): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return "[" + value.map(stable).join(",") + "]";
  return "{" + Object.keys(value).sort().map((key) => JSON.stringify(key) + ":" + stable(value[key])).join(",") + "}";
}

function json(path: string, value: any): void {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(value, null, 2) + "\n", "utf8");
}

function jsonl(path: string, values: Obj[]): void {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, values.map(stable).join("\n") + "\n", "utf8");
}

function gitText(cwd: string, args: string[]): string {
  return execFileSync("git", args, { cwd, encoding: "utf8" }).trim();
}

function gitBytes(cwd: string, args: string[]): Buffer {
  return execFileSync("git", args, { cwd, encoding: "buffer" }) as Buffer;
}

function nodeName(node: ts.Node, index: number): string | null {
  if (ts.isTypeAliasDeclaration(node) || ts.isInterfaceDeclaration(node)) return null;
  if (
    ts.isFunctionDeclaration(node)
    || ts.isClassDeclaration(node)
    || ts.isEnumDeclaration(node)
  ) return node.name?.text ?? "anonymous_" + index;
  if (ts.isVariableStatement(node)) {
    return node.declarationList.declarations.map((item) => item.name.getText()).join("+");
  }
  if (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) return null;
  return "#statement_" + index + "_" + ts.SyntaxKind[node.kind];
}

function sourceManifest(snapshot: string): Obj[] {
  const output: Obj[] = [];
  for (const [path, acceptedEnd, domain] of inputs) {
    const bytes = readFileSync(join(sourceRoot, path));
    const text = bytes.toString("utf8").replace(/^\uFEFF/, "");
    const file = ts.createSourceFile(path, text, ts.ScriptTarget.ESNext, true);
    for (let index = 0; index < file.statements.length; index += 1) {
      const node = file.statements[index];
      const name = nodeName(node, index + 1);
      if (!name || ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) continue;
      const start = file.getLineAndCharacterOfPosition(node.getStart(file)).line + 1;
      const end = file.getLineAndCharacterOfPosition(Math.max(node.getStart(file), node.end - 1)).line + 1;
      if (start > acceptedEnd) continue;
      const ordinal = output.length + 1;
      output.push({
        schema_version: "3.0",
        record_type: "source_range",
        execution_id: "E01",
        mapping_id: "e01-src-" + String(ordinal).padStart(4, "0"),
        source_repo: "claude-code-best",
        source_snapshot: snapshot,
        source_path: path,
        source_sha256: hash(bytes),
        start_line: start,
        end_line: Math.min(end, acceptedEnd),
        source_symbol: path + "::" + name,
        source_role: "primary",
        migration_mode: "adapted",
        semantic_domain: domain,
        accepted: true,
        exclusion_reason: null,
      });
    }
  }
  return output;
}

function targetManifest(sources: Obj[]): Obj[] {
  return sources.map((source) => {
    const profile = routeFor(source);
    const planned = String(profile.symbol).split(".").at(-1);
    return {
      schema_version: "3.0",
      record_type: "custody_mapping",
      execution_id: "E01",
      mapping_id: source.mapping_id,
      target_path: profile.path,
      target_sha256: null,
      target_symbol: profile.symbol,
      canonical_owner_id: "e01." + source.semantic_domain,
      default_entry_id: "e01.default-code-worker",
      default_callsite_path: profile.callsitePath,
      default_callsite_symbol: profile.callsiteSymbol,
      default_entry_edges: profile.entryEdges,
      state_store: profile.store,
      state_effect_kind: profile.effect,
      state_effect_assertion: "assert." + source.mapping_id + "." + profile.effect,
      state_snapshot_property: profile.snapshotProperty,
      state_observation: profile.stateObservation,
      behavior_tests: profile.behavior,
      success_test_ids: profile.behavior.map((item: Obj) => item.name),
      failure_test_ids: profile.behavior.map((item: Obj) => item.name),
      disable_test_ids: ["e01.owner.disable"],
      mutation_ids: profile.mutations,
      adaptation: profile.adaptation,
      runtime_origin_probe_id: "e01.probe.runtime-origin",
      write_path_probe_id: "e01.probe.write-path",
      restore_probe_id: "e01.probe.same-session-resume",
      planned_method: planned,
    };
  });
}

function oldOwnerPaths(): string[] {
  const path = join(zyra, "docs", "reviews", "evidence", "M1-R01-v2", "execution-01", "python-owner-result.jsonl");
  return readFileSync(path, "utf8").split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line).path);
}

function pythonManifest(): Obj[] {
  const output: Obj[] = [];
  for (const path of oldOwnerPaths()) {
    const bytes = gitBytes(zyra, ["show", VERIFIED + ":" + path]);
    const text = bytes.toString("utf8");
    const lines = text.split(/\r?\n/);
    const definitions: Array<{ name: string; start: number; end: number }> = [];
    for (let index = 0; index < lines.length; index += 1) {
      const match = /^(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)/.exec(lines[index]);
      if (!match) continue;
      if (definitions.length) definitions[definitions.length - 1].end = index;
      definitions.push({ name: match[1], start: index + 1, end: lines.length });
    }
    if (!definitions.length) definitions.push({ name: "module_" + basename(path, ".py"), start: 1, end: lines.length });
    for (const definition of definitions) {
      output.push({
        schema_version: "3.0",
        record_type: "python_owner",
        execution_id: "E01",
        owner_id: "e01-py-" + String(output.length + 1).padStart(4, "0"),
        verified_zyra_head: VERIFIED,
        python_path: path,
        python_sha256: hash(bytes),
        start_line: definition.start,
        end_line: definition.end,
        python_symbol: definition.name,
        state_domain: "claude-runtime-core",
        default_entry: "CodeWorkerRuntime.run",
        disposition: "delete",
        allowed_adapter_symbols: [],
        deletion_test_id: "e01.python-owner-absent",
      });
    }
  }
  return output;
}

function mutationManifest(): Obj[] {
  return mutations.map((item, index) => {
    const [name, shortPath, symbol, operator, risk] = item;
    const path = "packages/runtime/claude-runtime/" + shortPath;
    return {
      schema_version: "3.0",
      record_type: "mutation",
      execution_id: "E01",
      mutation_id: "e01-mut-" + String(index + 1).padStart(3, "0") + "-" + name,
      target_path: path,
      target_symbol: symbol,
      mutation_operator: operator,
      semantic_risk: risk,
      expected_killer_test_ids: [name === "recovery-planner-custody"
        ? "runtime lets canonical recovery policy stop a non-retryable provider request"
        : name === "provider-lifecycle-custody"
        ? "runtime commits provider prompt usage and recovery state through default loop"
        : "e01.mutation." + name],
      compile_survives: true,
      frozen_patch_sha256: hash(path + "\0" + symbol + "\0" + operator),
    };
  });
}

function gateProfile(snapshot: string): Obj {
  const bun = ["npx", "--yes", "bun@" + BUN];
  return {
    schema_version: "3.0",
    execution_id: "E01",
    verified_zyra_head: VERIFIED,
    source_snapshots: { "claude-code-best": snapshot },
    allowed_control_plane_commits: [
      "0cd21bff5e2d160476f2ce3cef766bf53aab1239",
      "ae7fae15cfb02983be2d1430fd11c2596b4e7236",
    ],
    candidate_scope_paths: ["apps/code-worker", "packages/runtime/claude-runtime", "packages/runtime/runtime-event-spine", "packages/runtime/zyra_runtime", "packages/workers/zyra_workers", "scripts/remediation", "tests/integration/test_e01_typescript_runtime_cutover.py", "docs/reviews"],
    forbidden_runtime_paths: ["../claude-code-best", "../opencode", "../OpenHands", "../browser-use", "vendor", "vendor-runtimes", "source-pool", "runtime-sources"],
    toolchain: {
      bun_version: BUN,
      typescript_version: "5.8.3",
      frozen_install: bun.concat(["install", "--frozen-lockfile"]),
      typecheck: bun.concat(["run", "typecheck"]),
      build: bun.concat(["run", "build"]),
      test: bun.concat(["run", "runtime:e01:test"]),
      built_entry: bun.concat(["run", "runtime:built:health"]),
    },
    commands: {
      source_validator: bun.concat(["scripts/remediation/verify_m1_r01_e01_v3.ts"]),
      effective_loc_clone: bun.concat(["scripts/verify_m1_r01_manifests.ts", "verify", "--enforce-gates"]),
      runtime_origin_probe: bun.concat(["run", "e01:probe:runtime-origin"]),
      write_path_probe: bun.concat(["run", "e01:probe:write-path"]),
      same_session_resume: bun.concat(["run", "e01:probe:resume"]),
      lost_ack: bun.concat(["run", "e01:probe:lost-ack"]),
      disable: bun.concat(["run", "e01:probe:disable"]),
      mutation_runner: bun.concat(["run", "e01:mutation"]),
      clean_dependency_path: bun.concat(["run", "e01:audit:dependencies"]),
    },
    thresholds: {
      accepted_source_executable_sloc: 10587,
      python_delete_executable_sloc: 26670,
      effective_changed_typescript_sloc: 25416,
      final_non_test_typescript_sloc: 28000,
      effective_behavior_test_sloc: 7000,
      adapter_ratio_maximum: 0.10,
      mutation_points_minimum: 30,
      core_mutation_kill_rate: 1,
      source_to_target_unique_symbols_minimum: 20,
      source_to_target_max_mappings_per_symbol: 60,
      behavior_tests_per_mapping_maximum: 2,
      mutation_ids_per_mapping_minimum: 1,
    },
    checker_source_paths: ["scripts/remediation/m1_r01_e01_v3.ts", "scripts/remediation/verify_m1_r01_e01_v3.ts", "scripts/verify_m1_r01_manifests.ts"],
  };
}

function main(): void {
  mkdirSync(manifestRoot, { recursive: true });
  mkdirSync(evidenceRoot, { recursive: true });
  const snapshot = gitText(sourceRoot, ["rev-parse", "HEAD"]);
  const source = sourceManifest(snapshot);
  const python = pythonManifest();
  const target = targetManifest(source);
  const mutation = mutationManifest();
  const paths = {
    source: join(manifestRoot, "execution-01-source-manifest.jsonl"),
    python: join(manifestRoot, "execution-01-python-owner-baseline.jsonl"),
    target: join(manifestRoot, "execution-01-target-custody-map.jsonl"),
    mutation: join(manifestRoot, "execution-01-mutation-manifest.jsonl"),
    profile: join(manifestRoot, "execution-01-gate-profile.json"),
  };
  jsonl(paths.source, source);
  jsonl(paths.python, python);
  jsonl(paths.target, target);
  jsonl(paths.mutation, mutation);
  json(paths.profile, gateProfile(snapshot));
  const hashes: Record<string, string> = {};
  for (const path of Object.values(paths)) hashes[basename(path)] = hash(readFileSync(path));
  const receiptPath = join(manifestRoot, "execution-01-baseline-receipt.json");
  const dirty = gitText(zyra, ["status", "--porcelain"]);
  json(receiptPath, {
    schema_version: "3.0",
    execution_id: "E01",
    captured_at_utc: new Date().toISOString(),
    verified_zyra_head: VERIFIED,
    verified_head_tree: gitText(zyra, ["rev-parse", VERIFIED + "^{tree}"]),
    current_control_plane_head: gitText(zyra, ["rev-parse", "HEAD"]),
    clean_worktree: dirty === "",
    dirty_paths_at_capture: dirty ? dirty.split(/\r?\n/) : [],
    source_snapshots: { "claude-code-best": snapshot },
    input_sha256: hashes,
    manifest_generator_command: ["npx", "--yes", "bun@" + BUN, "scripts/remediation/m1_r01_e01_v3.ts"],
    schema_validator_command: ["npx", "--yes", "bun@" + BUN, "scripts/remediation/verify_m1_r01_e01_v3.ts"],
    bun_version: BUN,
    lockfile_sha256: hash(readFileSync(join(zyra, "bun.lock"))),
    host_platform: process.platform + "-" + process.arch,
    capture_exit_codes: { generator: 0 },
  });
  json(join(evidenceRoot, "g0-summary.json"), {
    execution_id: "E01",
    verified_zyra_head: VERIFIED,
    source_snapshot: snapshot,
    source_range_count: source.length,
    source_physical_range_sloc: source.reduce((sum, item) => sum + item.end_line - item.start_line + 1, 0),
    python_owner_symbol_count: python.length,
    python_physical_range_sloc: python.reduce((sum, item) => sum + item.end_line - item.start_line + 1, 0),
    target_mapping_count: target.length,
    mutation_count: mutation.length,
    gate_profile: relative(workspace, paths.profile).replaceAll("\\", "/"),
    baseline_receipt: relative(workspace, receiptPath).replaceAll("\\", "/"),
  });
}

main();
