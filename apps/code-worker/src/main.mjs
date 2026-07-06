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
      "tool_batch_started",
      "tool_call_started",
      "tool_call_completed",
      "tool_batch_completed",
      "tool_use_summary",
      "context_compacted",
      "turn_completed",
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
