import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import readline from "node:readline";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const PROJECT_ROOT = path.resolve(__dirname, "../../..");
const VENDOR_ROOT = path.join(PROJECT_ROOT, "vendor", "claude-code-best");
const RUNTIME_ROOT = path.join(PROJECT_ROOT, "vendor-runtimes", "claude-code-runtime");
const PRODUCTIZED_ROOT = path.join(RUNTIME_ROOT, "productized", "claude-code-best");
const PRODUCTIZED_INVENTORY_PATH = path.join(RUNTIME_ROOT, "metadata", "productized_source_inventory.json");
const QUERY_SESSION_INVENTORY_PATH = path.join(RUNTIME_ROOT, "metadata", "query_session_source_inventory.json");
const TOOL_LOOP_INVENTORY_PATH = path.join(RUNTIME_ROOT, "metadata", "tool_loop_budget_source_inventory.json");
const REFERENCE_CROSSWALK_PATH = path.join(RUNTIME_ROOT, "metadata", "reference_crosswalk.json");

const PRIORITY_MODULES = [
  {
    name: "query-engine",
    paths: ["src/QueryEngine.ts", "src/query.ts"],
    targetBoundary: "CodeWorkerRuntime sidecar",
  },
  {
    name: "tool-runtime",
    paths: ["src/Tool.ts", "src/tools.ts", "src/tools/BashTool", "src/tools/FileReadTool"],
    targetBoundary: "ToolRegistry and CodeWorkerRuntime",
  },
  {
    name: "permission-runtime",
    paths: ["src/hooks/toolPermission"],
    targetBoundary: "ToolPermissionRuntime",
  },
  {
    name: "compact-runtime",
    paths: ["src/services/compact"],
    targetBoundary: "Memory and context management",
  },
  {
    name: "mcp-runtime",
    paths: ["src/services/mcp"],
    targetBoundary: "MCP integration adapter",
  },
  {
    name: "commands-runtime",
    paths: ["src/commands.ts", "src/commands/compact", "src/commands/mcp", "src/commands/skills"],
    targetBoundary: "ControlCommand and command panel",
  },
  {
    name: "skill-runtime",
    paths: ["src/tools/SkillTool", "src/skills"],
    targetBoundary: "SkillRuntime",
  },
  {
    name: "subagent-runtime",
    paths: ["src/tools/AgentTool"],
    targetBoundary: "SubagentRuntime",
  },
];

function exists(relativePath) {
  return fs.existsSync(path.join(VENDOR_ROOT, relativePath));
}

function vendorSnapshot() {
  const modules = PRIORITY_MODULES.map((module) => ({
    ...module,
    complete: module.paths.every(exists),
    missingPaths: module.paths.filter((relativePath) => !exists(relativePath)),
  }));

  return {
    projectRoot: PROJECT_ROOT,
    vendorRoot: VENDOR_ROOT,
    vendorPresent: fs.existsSync(VENDOR_ROOT),
    modules,
    complete: fs.existsSync(VENDOR_ROOT) && modules.every((module) => module.complete),
  };
}

function runtimeInventory() {
  const toolsSource = readRuntimeText("src/tools.ts");
  const commandsSource = readRuntimeText("src/commands.ts");
  const productizedRuntime = productizedRuntimeSnapshot();
  const toolDirectories = listRuntimeDirectories("src/tools").filter((name) => name.endsWith("Tool"));
  const importedTools = uniqueMatches(
    toolsSource,
    /import\s+\{\s*([A-Za-z0-9_]+Tool)\s*\}\s+from\s+['"]\.\/tools\/([^'"]+)['"]/g,
    (match) => ({ symbol: match[1], path: `src/tools/${match[2]}` }),
  );
  const baseToolSymbols = uniqueStrings([
    ...symbolsFromFunctionArray(toolsSource, "getAllBaseTools"),
    ...symbolsFromFunctionArray(toolsSource, "getTools"),
  ]).filter((symbol) => symbol.endsWith("Tool"));
  const commandPaths = uniqueStrings([
    ...matches(commandsSource, /from\s+['"]\.\/commands\/([^'"]+)['"]/g, (match) => match[1]),
    ...matches(commandsSource, /require\(['"]\.\/commands\/([^'"]+)['"]\)/g, (match) => match[1]),
  ]).sort();

  return {
    source: "claude-code-best",
    vendorRoot: VENDOR_ROOT,
    runtimeRoot: RUNTIME_ROOT,
    productizedRoot: PRODUCTIZED_ROOT,
    generatedBy: "zyra-code-worker-sidecar",
    moduleEntrypoints: {
      queryEngine: runtimeExists("src/QueryEngine.ts"),
      queryLoop: runtimeExists("src/query.ts"),
      toolRuntime: runtimeExists("src/tools.ts") && runtimeExists("src/Tool.ts"),
      commandRuntime: runtimeExists("src/commands.ts"),
      permissionRuntime: runtimeExists("src/hooks/toolPermission"),
      compactRuntime: runtimeExists("src/services/compact"),
      mcpRuntime: runtimeExists("src/services/mcp"),
      skillRuntime: runtimeExists("src/tools/SkillTool"),
      subagentRuntime: runtimeExists("src/tools/AgentTool"),
      sessionPersistenceRuntime: runtimeExists("src/utils/sessionStorage.ts") && runtimeExists("src/utils/sessionStoragePortable.ts"),
      sessionRestoreRuntime: runtimeExists("src/utils/sessionRestore.ts") && runtimeExists("src/utils/conversationRecovery.ts"),
      apiStreamRuntime: runtimeExists("src/services/api/claude.ts") && runtimeExists("src/services/api/withRetry.ts"),
      bridgeSessionRuntime: runtimeExists("src/bridge/sessionRunner.ts") && runtimeExists("src/bridge/createSession.ts"),
      shellRuntime: runtimeExists("src/utils/Shell.ts") && runtimeExists("src/utils/ShellCommand.ts"),
      bashParserRuntime: runtimeExists("src/utils/bash") && runtimeExists("src/utils/shell/readOnlyCommandValidation.ts"),
      sandboxRuntime: runtimeExists("src/utils/sandbox/sandbox-adapter.ts"),
      toolBudgetRuntime: runtimeExists("src/utils/toolResultStorage.ts") && runtimeExists("src/utils/truncate.ts"),
    },
    productizedRuntime,
    toolRuntime: {
      toolsSource: "src/tools.ts",
      toolDirectories,
      importedTools,
      baseToolSymbols,
      baseToolCount: baseToolSymbols.length,
      directoryToolCount: toolDirectories.length,
    },
    commandRuntime: {
      commandsSource: "src/commands.ts",
      commandPaths,
      commandCount: commandPaths.length,
      highValueCommandPaths: commandPaths.filter((item) =>
        /^(compact|context|cost|diff|doctor|mcp|memory|permissions|resume|skills|tasks|agents|hooks|plugin|branch|files|review)(\/|\.|$)/.test(
          item,
        ),
      ),
    },
    runtimeBoundaries: {
      permissionRuntimeFiles: listRuntimeFiles("src/hooks/toolPermission"),
      compactRuntimeFiles: listRuntimeFiles("src/services/compact"),
      skillRuntimeFiles: listRuntimeFiles("src/tools/SkillTool"),
      subagentRuntimeFiles: listRuntimeFiles("src/tools/AgentTool"),
      mcpRuntimeFiles: listRuntimeFiles("src/services/mcp").slice(0, 80),
      sessionRuntimeFiles: [
        ...listRuntimeFiles("src/utils").filter((item) => /\/(session|conversation|transcript|fileHistory|concurrentSessions|crossProjectResume)/.test(`/${item}`)),
        ...listRuntimeFiles("src/commands/resume"),
        ...listRuntimeFiles("src/commands/session"),
      ].slice(0, 80),
      apiStreamRuntimeFiles: listRuntimeFiles("src/services/api").filter((item) =>
        /\/(claude|withRetry|errors|logging|promptCacheBreakDetection|sessionIngress)\.ts$/.test(`/${item}`),
      ),
      bridgeSessionRuntimeFiles: listRuntimeFiles("src/bridge").filter((item) =>
        /\/(codeSessionApi|createSession|sessionIdCompat|sessionRunner)\.ts$/.test(`/${item}`),
      ),
      toolLoopBudgetRuntimeFiles: [
        "src/services/tools/toolExecution.ts",
        "src/services/tools/toolOrchestration.ts",
        "src/services/tools/StreamingToolExecutor.ts",
        "src/utils/toolResultStorage.ts",
        "src/utils/truncate.ts",
        "src/utils/groupToolUses.ts",
        "src/utils/toolErrors.ts",
        "src/utils/Shell.ts",
        "src/utils/ShellCommand.ts",
        ...listRuntimeFiles("src/utils/bash").slice(0, 40),
        ...listRuntimeFiles("src/utils/shell").slice(0, 20),
        ...listRuntimeFiles("src/utils/sandbox").slice(0, 20),
      ].filter(runtimeExists),
    },
  };
}

function queryContract() {
  const queryEngineSource = readRuntimeText("src/QueryEngine.ts");
  const queryLoopSource = readRuntimeText("src/query.ts");
  const toolOrchestrationSource = readRuntimeText("src/services/tools/toolOrchestration.ts");
  const toolResultStorageSource = readRuntimeText("src/utils/toolResultStorage.ts");
  const sourceFiles = [
    "src/QueryEngine.ts",
    "src/query.ts",
    "src/services/tools/toolOrchestration.ts",
    "src/utils/toolResultStorage.ts",
    ...listRuntimeFiles("src/services/compact").filter((item) =>
      /\/(autoCompact|compact|postCompactCleanup|reactiveCompact|sessionMemoryCompact)\.ts$/.test(`/${item}`),
    ),
    ...listRuntimeFiles("src/hooks/toolPermission").slice(0, 12),
  ].filter(runtimeExists);

  const queryEngineConfigFields = extractTypeFields(queryEngineSource, "QueryEngineConfig");
  const loopStateFields = extractTypeFields(queryLoopSource, "State");

  return {
    source: "claude-code-best",
    generatedBy: "zyra-code-worker-sidecar",
    sourceFiles,
    queryEngineConfigFields: queryEngineConfigFields.length
      ? queryEngineConfigFields
      : [
          "cwd",
          "tools",
          "commands",
          "mcpClients",
          "agents",
          "canUseTool",
          "getAppState",
          "setAppState",
          "initialMessages",
          "readFileCache",
          "customSystemPrompt",
          "customSystemPromptPrefix",
          "appendSystemPrompt",
          "userSpecifiedModel",
          "fallbackModel",
          "thinkingConfig",
          "maxTurns",
          "maxBudgetUsd",
          "taskBudget",
          "jsonSchema",
          "replay",
          "includePartial",
          "setSDKStatus",
          "abortController",
          "orphanedPermission",
          "snipReplay",
        ],
    loopStateFields: loopStateFields.length
      ? loopStateFields
      : [
          "messages",
          "toolUseContext",
          "autoCompactTracking",
          "maxOutputTokensRecoveryCount",
          "hasAttemptedReactiveCompact",
          "maxOutputTokensOverride",
          "pendingToolUseSummary",
          "stopHookActive",
          "turnCount",
          "transition",
        ],
    lifecycleEvents: [
      "session_started",
      "stream_request_start",
      "turn_started",
      "turn_start",
      "message_delta",
      "tool_batch_started",
      "tool_call_started",
      "tool_call_completed",
      "tool_batch_completed",
      "tool_use_summary",
      "context_compacted",
      "turn_completed",
      "turn_end",
      "error",
      "continue",
      "query_session_snapshot",
      "session_completed",
    ],
    toolOrchestration: {
      sourcePath: "src/services/tools/toolOrchestration.ts",
      hasRunTools: /\bexport\s+async\s+function\*\s+runTools\b/.test(toolOrchestrationSource),
      hasPartitionToolCalls: /\bfunction\s+partitionToolCalls\b/.test(toolOrchestrationSource),
      readOnlyConcurrent: /Run read-only batch concurrently/.test(toolOrchestrationSource),
      writeSerial: /Run non-read-only batch serially/.test(toolOrchestrationSource),
      maxConcurrencyDefault: 10,
      maxConcurrencyEnv: "CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY",
    },
    budgets: {
      maxTurns: /maxTurns/.test(queryEngineSource) && /max_turns_reached/.test(queryLoopSource),
      maxBudgetUsd: /maxBudgetUsd/.test(queryEngineSource),
      taskBudget: /taskBudget/.test(queryEngineSource),
      toolResultBudget: /applyToolResultBudget/.test(queryLoopSource) && /PERSISTED_OUTPUT_TAG/.test(toolResultStorageSource),
      queryContextBudget: /autoCompactTracking/.test(queryLoopSource),
      reactiveCompact: /reactiveCompact/.test(queryLoopSource),
      tokenBudgetContinuation: /maxOutputTokensRecoveryCount/.test(queryLoopSource),
    },
    compactRuntime: {
      sourceDir: "src/services/compact",
      available: runtimeExists("src/services/compact"),
      autoCompact: runtimeExists("src/services/compact/autoCompact.ts"),
      reactiveCompact: runtimeExists("src/services/compact/reactiveCompact.ts"),
      postCompactCleanup: runtimeExists("src/services/compact/postCompactCleanup.ts"),
      sessionMemoryCompact: runtimeExists("src/services/compact/sessionMemoryCompact.ts"),
    },
    permissionRuntime: {
      sourceDir: "src/hooks/toolPermission",
      available: runtimeExists("src/hooks/toolPermission"),
      canUseTool: /canUseTool/.test(queryEngineSource),
      tracksPermissionDenials: /permission_denials/.test(queryEngineSource),
      files: listRuntimeFiles("src/hooks/toolPermission").slice(0, 12),
    },
    resultFields: [
      "type",
      "subtype",
      "session_id",
      "usage",
      "modelUsage",
      "permission_denials",
      "total_cost_usd",
      "structured_output",
      "stop_reason",
      "num_turns",
    ],
  };
}

function sessionContract() {
  const sessionStorageSource = readRuntimeText("src/utils/sessionStorage.ts");
  const portableSource = readRuntimeText("src/utils/sessionStoragePortable.ts");
  const restoreSource = readRuntimeText("src/utils/sessionRestore.ts");
  const recoverySource = readRuntimeText("src/utils/conversationRecovery.ts");
  const listSessionsSource = readRuntimeText("src/utils/listSessionsImpl.ts");
  const withRetrySource = readRuntimeText("src/services/api/withRetry.ts");
  const claudeApiSource = readRuntimeText("src/services/api/claude.ts");
  const apiClientSource = readRuntimeText("src/services/api/client.ts");
  const filesApiSource = readRuntimeText("src/services/api/filesApi.ts");
  const dumpPromptsSource = readRuntimeText("src/services/api/dumpPrompts.ts");
  const queryProfilerSource = readRuntimeText("src/utils/queryProfiler.ts");
  const bridgeRunnerSource = readRuntimeText("src/bridge/sessionRunner.ts");
  const inboundBridgeSource = readRuntimeText("src/bridge/inboundMessages.ts");
  const clearConversationSource = readRuntimeText("src/commands/clear/conversation.ts");
  const renameSessionSource = readRuntimeText("src/commands/rename/generateSessionName.ts");
  const sourceFiles = [
    "src/assistant/sessionHistory.ts",
    "src/utils/sessionStorage.ts",
    "src/utils/sessionStoragePortable.ts",
    "src/utils/sessionRestore.ts",
    "src/utils/conversationRecovery.ts",
    "src/utils/listSessionsImpl.ts",
    "src/utils/crossProjectResume.ts",
    "src/utils/transcriptSearch.ts",
    "src/utils/agenticSessionSearch.ts",
    "src/utils/fileHistory.ts",
    "src/utils/queryProfiler.ts",
    "src/services/api/claude.ts",
    "src/services/api/bootstrap.ts",
    "src/services/api/client.ts",
    "src/services/api/dumpPrompts.ts",
    "src/services/api/errorUtils.ts",
    "src/services/api/filesApi.ts",
    "src/services/api/withRetry.ts",
    "src/services/api/sessionIngress.ts",
    "src/bridge/inboundMessages.ts",
    "src/bridge/sessionRunner.ts",
    "src/bridge/createSession.ts",
    "src/bridge/codeSessionApi.ts",
    "src/commands/clear/conversation.ts",
    "src/commands/rename/generateSessionName.ts",
    "src/commands/resume/resume.tsx",
    "src/commands/session/session.tsx",
  ].filter(runtimeExists);
  const transcriptEntryTypes = uniqueStrings([
    ...quotedTypeLiterals(sessionStorageSource),
    "user",
    "assistant",
    "system",
    "summary",
    "last-prompt",
    "custom-title",
    "ai-title",
    "content-replacement",
  ]).filter((item) =>
    /^(user|assistant|system|attachment|summary|custom-title|ai-title|last-prompt|tag|agent-|mode|worktree|pr-link|file-history|attribution|content-replacement|marble|queue)/.test(
      item,
    ),
  );
  const resumePipeline = [
    "loadConversationForResume",
    "loadTranscriptFile",
    "readTranscriptForLoad",
    "parseJSONL",
    "applyPreservedSegmentRelinks",
    "applySnipRemovals",
    "buildConversationChain",
    "recoverOrphanedParallelToolResults",
    "deserializeMessagesWithInterruptDetection",
  ];

  return {
    source: "claude-code-best",
    generatedBy: "zyra-code-worker-sidecar",
    ownerUnit: "M1-02B",
    sourceFiles,
    inventoryPath: QUERY_SESSION_INVENTORY_PATH,
    inventoryExists: fs.existsSync(QUERY_SESSION_INVENTORY_PATH),
    transcriptPersistence: {
      appendOnlyJsonl: /append(File|Entry)|JSONL|jsonl/i.test(sessionStorageSource),
      projectSingleton: /class\s+Project\b/.test(sessionStorageSource),
      pendingEntries: /pendingEntries/.test(sessionStorageSource),
      writeQueue: /enqueueWrite|drainWriteQueue|scheduleDrain/.test(sessionStorageSource),
      parentUuidChain: /parentUuid/.test(sessionStorageSource) && /uuid/.test(sessionStorageSource),
      liteReadWindowBytes: /65536|LITE_READ_BUF_SIZE/.test(portableSource) ? 65536 : null,
      transcriptEntryTypes,
      sourcePath: "src/utils/sessionStorage.ts",
      portableSourcePath: "src/utils/sessionStoragePortable.ts",
    },
    resumeRecovery: {
      sourcePath: "src/utils/sessionRestore.ts",
      conversationRecoverySourcePath: "src/utils/conversationRecovery.ts",
      pipeline: resumePipeline.filter((symbol) => new RegExp(`\\b${symbol}\\b`).test(restoreSource + recoverySource)),
      hasChainTraversal: /parentUuid/.test(restoreSource + recoverySource) && /buildConversationChain/.test(restoreSource + recoverySource),
      hasInterruptionDetection: /detectTurnInterruption|interrupted_turn|interrupted_prompt/.test(recoverySource),
      hasConsistencyCheck: /checkResumeConsistency|messageCount|consistency/.test(restoreSource + recoverySource),
      hasOrphanedToolResultRecovery: /recoverOrphanedParallelToolResults|filterUnresolvedToolUses/.test(recoverySource),
      listSessionsUsesLiteRead: /readSessionLite|readHeadAndTail|LITE_READ_BUF_SIZE/.test(listSessionsSource),
    },
    streamRuntime: {
      sourcePath: "src/services/api/claude.ts",
      withRetrySourcePath: "src/services/api/withRetry.ts",
      rawSseStateMachine: /message_start|content_block_start|content_block_delta|message_stop/.test(claudeApiSource),
      streamIdleWatchdog: /90|idle|watchdog|AbortController/.test(claudeApiSource),
      apiClientSourcePath: "src/services/api/client.ts",
      hasApiClient: /class|function|export/.test(apiClientSource) && /request|stream|fetch|retry/i.test(apiClientSource),
      filesApiSourcePath: "src/services/api/filesApi.ts",
      hasFilesApi: /file|upload|download|artifact|workspace/i.test(filesApiSource),
      dumpPromptsSourcePath: "src/services/api/dumpPrompts.ts",
      hasPromptDumpPipeline: /prompt|dump|message|conversation/i.test(dumpPromptsSource),
      queryProfilerSourcePath: "src/utils/queryProfiler.ts",
      hasQueryProfiler: /profile|query|duration|metric/i.test(queryProfilerSource),
      retryMatrix: {
        rateLimit429: /429/.test(withRetrySource),
        overloaded529: /529/.test(withRetrySource),
        unauthorized401: /401/.test(withRetrySource),
        revoked403: /403/.test(withRetrySource),
        connectionReset: /ECONNRESET|EPIPE/.test(withRetrySource),
        unattendedRetry: /UNATTENDED_RETRY|unattended/i.test(withRetrySource),
        fallbackTriggered: /FallbackTriggeredError|fallback/i.test(withRetrySource),
      },
    },
    bridgeSessionRuntime: {
      sourcePath: "src/bridge/sessionRunner.ts",
      hasSessionRunner: /session|runner|run|abort|message/i.test(bridgeRunnerSource),
      forwardsQueryEvents: /stream|message|event|query/i.test(bridgeRunnerSource),
      inboundMessagesSourcePath: "src/bridge/inboundMessages.ts",
      hasInboundMessages: /message|event|session|request/i.test(inboundBridgeSource),
      hasCreateSession: runtimeExists("src/bridge/createSession.ts"),
      hasCodeSessionApi: runtimeExists("src/bridge/codeSessionApi.ts"),
    },
    sessionCommands: {
      clearConversationSourcePath: "src/commands/clear/conversation.ts",
      hasClearConversation: /clear|conversation|message|session/i.test(clearConversationSource),
      renameSessionSourcePath: "src/commands/rename/generateSessionName.ts",
      hasRenameSession: /title|name|session|generate/i.test(renameSessionSource),
    },
    zyraRuntimeMapping: {
      querySession: "packages/runtime/zyra_runtime/query_session.py",
      codeQueryLoop: "packages/workers/zyra_workers/code_query_loop.py",
      codeWorkerRuntime: "packages/workers/zyra_workers/code_worker_runtime.py",
      apiCheckpoint: "apps/api/zyra_api/main.py",
      tests: [
        "tests/unit/test_query_session_lifecycle.py",
        "tests/integration/test_code_worker_query_session_lifecycle.py",
      ],
    },
  };
}

function toolLoopContract() {
  const toolInterfaceSource = readRuntimeText("src/Tool.ts");
  const toolsSource = readRuntimeText("src/tools.ts");
  const toolExecutionSource = readRuntimeText("src/services/tools/toolExecution.ts");
  const toolOrchestrationSource = readRuntimeText("src/services/tools/toolOrchestration.ts");
  const streamingExecutorSource = readRuntimeText("src/services/tools/StreamingToolExecutor.ts");
  const toolResultStorageSource = readRuntimeText("src/utils/toolResultStorage.ts");
  const shellSource = readRuntimeText("src/utils/Shell.ts");
  const shellCommandSource = readRuntimeText("src/utils/ShellCommand.ts");
  const bashParserSource = readRuntimeText("src/utils/bash/bashParser.ts");
  const readOnlyValidationSource = readRuntimeText("src/utils/shell/readOnlyCommandValidation.ts");
  const sandboxSource = readRuntimeText("src/utils/sandbox/sandbox-adapter.ts");
  const denialTrackingSource = readRuntimeText("src/utils/permissions/denialTracking.ts");
  const sourceFiles = [
    "src/Tool.ts",
    "src/tools.ts",
    "src/services/tools/toolExecution.ts",
    "src/services/tools/toolOrchestration.ts",
    "src/services/tools/StreamingToolExecutor.ts",
    "src/utils/toolResultStorage.ts",
    "src/utils/truncate.ts",
    "src/utils/groupToolUses.ts",
    "src/utils/toolErrors.ts",
    "src/utils/fileStateCache.ts",
    "src/utils/readEditContext.ts",
    "src/utils/Shell.ts",
    "src/utils/ShellCommand.ts",
    "src/utils/bash/bashParser.ts",
    "src/utils/bash/ast.ts",
    "src/utils/bash/commands.ts",
    "src/utils/shell/bashProvider.ts",
    "src/utils/shell/readOnlyCommandValidation.ts",
    "src/utils/sandbox/sandbox-adapter.ts",
    "src/utils/permissions/denialTracking.ts",
  ].filter(runtimeExists);

  return {
    source: "claude-code-best",
    generatedBy: "zyra-code-worker-sidecar",
    ownerUnit: "M1-02C",
    inventoryPath: TOOL_LOOP_INVENTORY_PATH,
    inventoryExists: fs.existsSync(TOOL_LOOP_INVENTORY_PATH),
    sourceFiles,
    toolInterface: {
      sourcePath: "src/Tool.ts",
      hasBuildToolFactory: /buildTool/.test(toolInterfaceSource),
      hasInputSchema: /inputSchema/.test(toolInterfaceSource),
      hasReadOnlyFlag: /isReadOnly/.test(toolInterfaceSource),
      hasConcurrencyFlag: /isConcurrencySafe/.test(toolInterfaceSource),
      hasContextModifier: /contextModifier/.test(toolInterfaceSource),
    },
    toolPool: {
      sourcePath: "src/tools.ts",
      hasBaseTools: /getAllBaseTools|getTools/.test(toolsSource),
      hasAssembleToolPool: /assembleToolPool/.test(toolsSource),
      denyRulesPreFilter: /filterToolsByDenyRules|deny/i.test(toolsSource),
      toolSearchAware: /ToolSearchTool|shouldDefer|alwaysLoad/.test(toolsSource + toolInterfaceSource),
    },
    executionPipeline: {
      sourcePath: "src/services/tools/toolExecution.ts",
      hasRunToolUse: /runToolUse/.test(toolExecutionSource),
      hasPermissionGate: /streamedCheckPermissionsAndCallTool|canUseTool|checkPermissions/.test(toolExecutionSource),
      hasSchemaValidation: /safeParse|validateInput|inputSchema/.test(toolExecutionSource),
      hasToolResultBlockMapping: /mapToolResultToToolResultBlockParam|processToolResultBlock/.test(toolExecutionSource),
      hasLargeResultExternalization: /processToolResultBlock|maxResultSizeChars|toolResultStorage/.test(toolExecutionSource + toolResultStorageSource),
      hasPostToolHooks: /PostToolUse|postToolUse/i.test(toolExecutionSource),
    },
    scheduling: {
      orchestrationSourcePath: "src/services/tools/toolOrchestration.ts",
      readOnlyConcurrent: /Run read-only batch concurrently|read-only batch concurrently/i.test(toolOrchestrationSource),
      writeSerial: /Run non-read-only batch serially|non-read-only batch serially|serial/i.test(toolOrchestrationSource),
      streamingExecutorSourcePath: "src/services/tools/StreamingToolExecutor.ts",
      hasStreamingExecutor: /AsyncGenerator|stream|progress/i.test(streamingExecutorSource),
      maxConcurrencyDefault: 10,
      maxConcurrencyEnv: "CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY",
    },
    resultBudget: {
      sourcePath: "src/utils/toolResultStorage.ts",
      hasPersistedOutputTag: /PERSISTED_OUTPUT_TAG|persist/i.test(toolResultStorageSource),
      hasMaxResultSizeChars: /maxResultSizeChars/.test(toolExecutionSource + toolResultStorageSource),
      hasPreviewAndPath: /preview|path|artifact|file/i.test(toolResultStorageSource),
      truncateSourcePath: "src/utils/truncate.ts",
      hasTruncateUtility: /truncate/i.test(readRuntimeText("src/utils/truncate.ts")),
    },
    shellRuntime: {
      shellSourcePath: "src/utils/Shell.ts",
      shellCommandSourcePath: "src/utils/ShellCommand.ts",
      hasShellDiscovery: /CLAUDE_CODE_SHELL|resolveDefaultShell|bash|zsh/i.test(shellSource),
      hasProcessLifecycle: /spawn|exit|background|kill|timeout/i.test(shellCommandSource),
      hasOutputSizeWatchdog: /size|watchdog|SIGKILL|kill/i.test(shellCommandSource),
      hasCwdTracking: /pwd -P|cwd/i.test(shellSource + shellCommandSource),
      bashParserSourcePath: "src/utils/bash/bashParser.ts",
      hasBashAstParser: /parse|heredoc|pipeline|command/i.test(bashParserSource),
      readOnlyValidationSourcePath: "src/utils/shell/readOnlyCommandValidation.ts",
      hasReadOnlyCommandValidation: /readOnly|readonly|mutation|write/i.test(readOnlyValidationSource),
    },
    sandboxRuntime: {
      sourcePath: "src/utils/sandbox/sandbox-adapter.ts",
      hasSandboxAdapter: /sandbox|bubblewrap|sandbox-exec|seatbelt/i.test(sandboxSource),
      protectsSettings: /settings|denyWrite|denyRead/i.test(sandboxSource),
      hasNetworkPolicy: /network|domain|deny/i.test(sandboxSource),
    },
    failureSignals: {
      denialTrackingSourcePath: "src/utils/permissions/denialTracking.ts",
      hasDenialLimits: /maxConsecutive|maxTotal|DENIAL_LIMITS/.test(denialTrackingSource),
      zyraSignalRuntime: "packages/runtime/zyra_runtime/tool_loop.py",
      signalKinds: [
        "schema_error",
        "permission_denied",
        "permission_required",
        "timeout",
        "runtime_error",
        "budget_exceeded",
      ],
    },
    zyraRuntimeMapping: {
      toolLoop: "packages/runtime/zyra_runtime/tool_loop.py",
      executor: "packages/runtime/zyra_runtime/executor.py",
      codeQueryLoop: "packages/workers/zyra_workers/code_query_loop.py",
      codeWorkerRuntime: "packages/workers/zyra_workers/code_worker_runtime.py",
      tests: [
        "tests/unit/test_tool_loop_budget_runtime.py",
        "tests/integration/test_code_worker_tool_loop_budget.py",
      ],
    },
  };
}

function health() {
  const productizedRuntime = productizedRuntimeSnapshot();
  return {
    ok: vendorSnapshot().complete === true && productizedRuntime.complete === true,
    worker: "CodeWorkerRuntime",
    runtime: "node-sidecar",
    phase: "m1-02a-productized-runtime-boundary",
    node: process.version,
    vendor: vendorSnapshot(),
    productizedRuntime,
  };
}

function handleRequest(request) {
  if (!request || typeof request !== "object") {
    throw new Error("request must be a JSON object");
  }

  switch (request.method) {
    case "health":
      return health();
    case "vendor_snapshot":
      return vendorSnapshot();
    case "runtime_inventory":
      return runtimeInventory();
    case "query_contract":
      return queryContract();
    case "session_contract":
      return sessionContract();
    case "tool_loop_contract":
      return toolLoopContract();
    default:
      throw new Error(`unknown method: ${request.method}`);
  }
}

function writeResponse(response) {
  process.stdout.write(`${JSON.stringify(response)}\n`);
}

async function runLineProtocol() {
  const rl = readline.createInterface({
    input: process.stdin,
    crlfDelay: Number.POSITIVE_INFINITY,
  });

  for await (const line of rl) {
    if (!line.trim()) continue;
    try {
      const request = JSON.parse(line);
      const result = handleRequest(request);
      writeResponse({ id: request.id ?? null, ok: true, result });
    } catch (error) {
      writeResponse({
        id: null,
        ok: false,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }
}

if (process.argv.includes("--health")) {
  writeResponse(health());
} else if (process.argv.includes("--snapshot")) {
  writeResponse(vendorSnapshot());
} else if (process.argv.includes("--inventory")) {
  writeResponse(runtimeInventory());
} else if (process.argv.includes("--query-contract")) {
  writeResponse(queryContract());
} else if (process.argv.includes("--session-contract")) {
  writeResponse(sessionContract());
} else if (process.argv.includes("--tool-loop-contract")) {
  writeResponse(toolLoopContract());
} else {
  runLineProtocol();
}

function readVendorText(relativePath) {
  const target = path.join(VENDOR_ROOT, relativePath);
  if (!fs.existsSync(target)) return "";
  return fs.readFileSync(target, "utf8");
}

function runtimeSourceRoot() {
  return fs.existsSync(PRODUCTIZED_ROOT) ? PRODUCTIZED_ROOT : VENDOR_ROOT;
}

function runtimeExists(relativePath) {
  return fs.existsSync(path.join(runtimeSourceRoot(), relativePath));
}

function readRuntimeText(relativePath) {
  const target = path.join(runtimeSourceRoot(), relativePath);
  if (!fs.existsSync(target)) return "";
  return fs.readFileSync(target, "utf8");
}

function listRuntimeDirectories(relativePath) {
  const target = path.join(runtimeSourceRoot(), relativePath);
  if (!fs.existsSync(target)) return [];
  return fs
    .readdirSync(target, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .sort();
}

function listRuntimeFiles(relativePath) {
  const root = path.join(runtimeSourceRoot(), relativePath);
  if (!fs.existsSync(root)) return [];
  const files = [];
  const stack = [root];
  while (stack.length > 0) {
    const current = stack.pop();
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const target = path.join(current, entry.name);
      if (entry.isDirectory()) {
        stack.push(target);
      } else if (entry.isFile()) {
        files.push(path.relative(runtimeSourceRoot(), target).replaceAll(path.sep, "/"));
      }
    }
  }
  return files.sort();
}

function productizedRuntimeSnapshot() {
  const inventory = readJsonIfExists(PRODUCTIZED_INVENTORY_PATH);
  const crosswalk = readJsonIfExists(REFERENCE_CROSSWALK_PATH);
  const summary = inventory.summary ?? {};
  const crosswalkSummary = crosswalk.summary ?? {};
  const moduleChecks = {
    queryEngine: runtimeExists("src/QueryEngine.ts"),
    queryLoop: runtimeExists("src/query.ts"),
    toolRuntime: runtimeExists("src/tools.ts") && runtimeExists("src/Tool.ts"),
    toolOrchestration: runtimeExists("src/services/tools/toolOrchestration.ts"),
    compactRuntime: runtimeExists("src/services/compact"),
    permissionRuntime: runtimeExists("src/hooks/toolPermission"),
    mcpRuntime: runtimeExists("src/services/mcp"),
    skillRuntime: runtimeExists("src/tools/SkillTool"),
    subagentRuntime: runtimeExists("src/tools/AgentTool"),
    sessionPersistenceRuntime: runtimeExists("src/utils/sessionStorage.ts") && runtimeExists("src/utils/sessionStoragePortable.ts"),
    sessionRestoreRuntime: runtimeExists("src/utils/sessionRestore.ts") && runtimeExists("src/utils/conversationRecovery.ts"),
    apiStreamRuntime: runtimeExists("src/services/api/claude.ts") && runtimeExists("src/services/api/withRetry.ts"),
    bridgeSessionRuntime: runtimeExists("src/bridge/sessionRunner.ts") && runtimeExists("src/bridge/createSession.ts"),
    shellRuntime: runtimeExists("src/utils/Shell.ts") && runtimeExists("src/utils/ShellCommand.ts"),
    bashParserRuntime: runtimeExists("src/utils/bash") && runtimeExists("src/utils/shell/readOnlyCommandValidation.ts"),
    sandboxRuntime: runtimeExists("src/utils/sandbox/sandbox-adapter.ts"),
    toolBudgetRuntime: runtimeExists("src/utils/toolResultStorage.ts") && runtimeExists("src/utils/truncate.ts"),
  };
  const effectiveLineCount = Number(summary.effective_line_count ?? 0);
  const copiedFileCount = Number(summary.copied_count ?? 0) + Number(summary.skipped_count ?? 0);
  return {
    runtimeRoot: RUNTIME_ROOT,
    productizedRoot: PRODUCTIZED_ROOT,
    sourceRoot: runtimeSourceRoot(),
    manifestPath: path.join(RUNTIME_ROOT, "src", "zyra-productized-manifest.mjs"),
    inventoryPath: PRODUCTIZED_INVENTORY_PATH,
    referenceCrosswalkPath: REFERENCE_CROSSWALK_PATH,
    manifestExists: fs.existsSync(path.join(RUNTIME_ROOT, "src", "zyra-productized-manifest.mjs")),
    inventoryExists: fs.existsSync(PRODUCTIZED_INVENTORY_PATH),
    referenceCrosswalkExists: fs.existsSync(REFERENCE_CROSSWALK_PATH),
    effectiveLineCount,
    copiedFileCount,
    moduleChecks,
    querySessionRuntime: {
      inventoryPath: QUERY_SESSION_INVENTORY_PATH,
      inventoryExists: fs.existsSync(QUERY_SESSION_INVENTORY_PATH),
      sessionPersistenceRuntime: moduleChecks.sessionPersistenceRuntime,
      sessionRestoreRuntime: moduleChecks.sessionRestoreRuntime,
      apiStreamRuntime: moduleChecks.apiStreamRuntime,
      bridgeSessionRuntime: moduleChecks.bridgeSessionRuntime,
    },
    toolLoopRuntime: {
      inventoryPath: TOOL_LOOP_INVENTORY_PATH,
      inventoryExists: fs.existsSync(TOOL_LOOP_INVENTORY_PATH),
      shellRuntime: moduleChecks.shellRuntime,
      bashParserRuntime: moduleChecks.bashParserRuntime,
      sandboxRuntime: moduleChecks.sandboxRuntime,
      toolBudgetRuntime: moduleChecks.toolBudgetRuntime,
    },
    referenceCrosswalk: {
      ok: crosswalk.ok === true,
      entryCount: crosswalkSummary.entry_count ?? 0,
      referenceOnlyRepos: crosswalkSummary.reference_only_repos ?? [],
      missingSourceCount: crosswalkSummary.missing_source_count ?? 0,
      missingReferenceCount: crosswalkSummary.missing_reference_count ?? 0,
      missingTargetCount: crosswalkSummary.missing_target_count ?? 0,
    },
    complete:
      fs.existsSync(PRODUCTIZED_ROOT) &&
      fs.existsSync(PRODUCTIZED_INVENTORY_PATH) &&
      fs.existsSync(REFERENCE_CROSSWALK_PATH) &&
      effectiveLineCount >= 18000 &&
      copiedFileCount >= 80 &&
      Object.values(moduleChecks).every((value) => value === true) &&
      crosswalk.ok === true,
  };
}

function readJsonIfExists(target) {
  if (!fs.existsSync(target)) return {};
  try {
    return JSON.parse(fs.readFileSync(target, "utf8"));
  } catch {
    return {};
  }
}

function listVendorDirectories(relativePath) {
  const target = path.join(VENDOR_ROOT, relativePath);
  if (!fs.existsSync(target)) return [];
  return fs
    .readdirSync(target, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .sort();
}

function listVendorFiles(relativePath) {
  const root = path.join(VENDOR_ROOT, relativePath);
  if (!fs.existsSync(root)) return [];
  const files = [];
  const stack = [root];
  while (stack.length > 0) {
    const current = stack.pop();
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const target = path.join(current, entry.name);
      if (entry.isDirectory()) {
        stack.push(target);
      } else if (entry.isFile()) {
        files.push(path.relative(VENDOR_ROOT, target).replaceAll(path.sep, "/"));
      }
    }
  }
  return files.sort();
}

function symbolsFromFunctionArray(source, functionName) {
  const match = source.match(new RegExp(`function\\s+${functionName}\\([^)]*\\)[^{]*{[\\s\\S]*?return\\s+\\[([\\s\\S]*?)\\n\\s*\\]`, "m"));
  if (!match) return [];
  return matches(match[1], /\b([A-Za-z][A-Za-z0-9_]*Tool)\b/g, (item) => item[1]);
}

function uniqueMatches(source, pattern, mapper) {
  const seen = new Set();
  const output = [];
  for (const item of matches(source, pattern, mapper)) {
    const key = JSON.stringify(item);
    if (seen.has(key)) continue;
    seen.add(key);
    output.push(item);
  }
  return output;
}

function matches(source, pattern, mapper) {
  const output = [];
  for (const match of source.matchAll(pattern)) {
    output.push(mapper(match));
  }
  return output;
}

function uniqueStrings(values) {
  return [...new Set(values)].sort();
}

function quotedTypeLiterals(source) {
  return matches(source, /type:\s*['"]([^'"]+)['"]/g, (match) => match[1]);
}

function extractTypeFields(source, typeName) {
  const start = source.search(new RegExp(`(?:export\\s+)?type\\s+${typeName}\\s*=\\s*\\{`, "m"));
  if (start < 0) return [];
  const body = source.slice(start).split(/\r?\n/).slice(1);
  const fields = [];
  let depth = 1;
  for (const line of body) {
    const openCount = (line.match(/\{/g) || []).length;
    const closeCount = (line.match(/\}/g) || []).length;
    depth += openCount - closeCount;
    if (depth <= 0) break;
    const match = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\??:/);
    if (match) fields.push(match[1]);
  }
  return uniqueStrings(fields);
}
