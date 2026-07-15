import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "../..");
const workspaceRoot = resolve(repoRoot, "..");
const sourceRoot = join(workspaceRoot, "claude-code-best");
const manifestRoot = join(
  workspaceRoot,
  "docs/remediations/M1-R01-claude-source-custody/manifests",
);
const sourceManifestPath = join(manifestRoot, "execution-01-source-manifest.jsonl");
const targetManifestPath = join(manifestRoot, "execution-01-target-custody-map.jsonl");
const mutationManifestPath = join(manifestRoot, "execution-01-mutation-manifest.jsonl");
const gateProfilePath = join(manifestRoot, "execution-01-gate-profile.json");
const receiptPath = join(manifestRoot, "execution-01-baseline-receipt.json");
const candidateMetadataPath = join(
  repoRoot,
  "docs/reviews/evidence/M1-R01-v3/execution-01/candidate-metadata.json",
);

const SOURCE_SNAPSHOT = "c57f5a29e88e9a814bea47abeb9a0a6f725dc102";
const VERIFIED_BASELINE = "c34535a783e88f9481387ced89cba4fbc333dc74";
const IMPLEMENTATION_DIFF_BASELINE = "0cd21bff5e2d160476f2ce3cef766bf53aab1239";
const DEFAULT_ENTRY_ID = "e01.default-code-worker";
const GENERATOR = "scripts/remediation/m1_r01_e01_v4.ts";
const SCHEMA_VERIFIER = "scripts/remediation/verify_m1_r01_e01_v4.ts";

type JsonRecord = Record<string, unknown>;

const sha256 = (value: Uint8Array | string): string =>
  createHash("sha256").update(value).digest("hex");

const mutationFingerprint = (search: string, replacement: string): string =>
  sha256(
    JSON.stringify([
      {
        search_sha256: sha256(search),
        replacement_sha256: sha256(replacement),
        removed_chars: search.length,
        added_chars: replacement.length,
      },
    ]),
  );

const gitText = (cwd: string, args: readonly string[]): string =>
  execFileSync("git", [...args], { cwd, encoding: "utf8", maxBuffer: 128 * 1024 * 1024 }).trim();

const gitBytes = (cwd: string, args: readonly string[]): Buffer =>
  execFileSync("git", [...args], { cwd, encoding: "buffer", maxBuffer: 128 * 1024 * 1024 });

const readJson = (path: string): JsonRecord => JSON.parse(readFileSync(path, "utf8")) as JsonRecord;

const readJsonLines = (path: string): JsonRecord[] =>
  readFileSync(path, "utf8")
    .split(/\r?\n/)
    .filter((line) => line.trim().length > 0)
    .map((line) => JSON.parse(line) as JsonRecord);

const stable = (value: unknown): unknown => {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === "object") {
    const output: JsonRecord = {};
    for (const key of Object.keys(value as JsonRecord).sort()) output[key] = stable((value as JsonRecord)[key]);
    return output;
  }
  return value;
};

const writeJson = (path: string, value: unknown): void => {
  writeFileSync(path, `${JSON.stringify(stable(value), null, 2)}\n`, "utf8");
};

const writeJsonLines = (path: string, values: readonly JsonRecord[]): void => {
  const content = values.map((value) => JSON.stringify(stable(value))).join("\n");
  writeFileSync(path, `${content}\n`, "utf8");
};

const symbolName = (record: JsonRecord): string => {
  const value = String(record.source_symbol ?? "");
  return value.slice(value.lastIndexOf("::") + 2);
};

const toolResultAccepted = new Set([
  "isToolResultContentEmpty",
  "createContentReplacementState",
  "cloneContentReplacementState",
  "getPerMessageBudgetLimit",
  "provisionContentReplacementState",
  "isContentAlreadyCompacted",
  "hasImageBlock",
  "contentSize",
  "collectCandidatesFromMessage",
  "collectCandidatesByMessage",
  "partitionByPriorDecision",
  "selectFreshToReplace",
  "replaceToolResultContents",
  "buildReplacement",
  "enforceToolResultBudget",
  "applyToolResultBudget",
  "reconstructContentReplacementState",
  "reconstructForSubagentResume",
  "generatePreview",
]);

const toolResultExternalizationAccepted = new Set([
  "PERSISTED_OUTPUT_TAG",
  "PERSISTED_OUTPUT_CLOSING_TAG",
  "TOOL_RESULT_CLEARED_MESSAGE",
  "PERSIST_THRESHOLD_OVERRIDE_FLAG",
  "PREVIEW_SIZE_BYTES",
  "persistToolResult",
  "buildLargeToolResultMessage",
  "processToolResultBlock",
  "processPreMappedToolResultBlock",
  "maybePersistLargeToolResult",
  "isPersistError",
  "buildToolNameMap",
]);

for (const name of toolResultExternalizationAccepted) toolResultAccepted.add(name);

const historyAccepted = new Set([
  "deserializeLogEntry",
  "makeLogEntryReader",
  "makeHistoryReader",
  "getTimestampedHistory",
  "getHistory",
  "resolveStoredPastedContent",
  "logEntryToHistoryEntry",
  "immediateFlushHistory",
  "flushPromptHistory",
  "addToPromptHistory",
  "addToHistory",
]);
const sessionStateAccepted = new Set(["hasPendingAction", "getSessionState"]);
const sessionRestoreAccepted = new Set([
  "processResumedConversation",
]);

const independentlyRejectedMappings = new Map([
  [
    "e01-src-0004",
    "conditional snipProjection module binding has no migrated projection or module-loading effect in E01",
  ],
  [
    "e01-src-0011",
    "conditional snipModule binding has no migrated snip loading or compaction effect in E01",
  ],
  [
    "e01-src-0151",
    "process-local cachedMCModule slot is deliberately rejected; no module-cache state is owned by ContextCompactionRuntime",
  ],
  [
    "e01-src-0264",
    "assistant-history tool-name reconstruction is not implemented by ToolResultRuntime delivery custody",
  ],
]);

const rejectionReason = (record: JsonRecord): string | null => {
  const path = String(record.source_path ?? "").replaceAll("\\", "/");
  const name = symbolName(record);
  const independentReason = independentlyRejectedMappings.get(String(record.mapping_id));
  if (independentReason) return independentReason;
  if (path.endsWith("/tokenBudget.ts") && name === "createBudgetTracker") {
    return "factory shape is not migrated; Zyra owns budget construction inside ContextTokenRuntime";
  }
  if (path.endsWith("/toolResultStorage.ts") && !toolResultAccepted.has(name)) {
    return "filesystem persistence and process-global replacement state are deliberately not migrated";
  }
  if (path.endsWith("/sessionRestore.ts") && !sessionRestoreAccepted.has(name)) {
    return "Claude process-global restore choreography is rejected in favor of Zyra durable session restore";
  }
  if (path.endsWith("/history.ts") && !historyAccepted.has(name)) {
    return "terminal formatting, global flush promises and image reference rewriting are outside E01 custody";
  }
  if (path.endsWith("/sessionState.ts") && !sessionStateAccepted.has(name)) {
    return "process-global listener state is rejected; canonical session state is held by DurableSessionRuntime";
  }
  return null;
};

const behaviorTest = (
  name: string,
  path: string,
  anchor: string,
  assertionTokens: readonly string[],
): JsonRecord => ({ name, path, anchor, assertion_tokens: assertionTokens });

const defaultEntryEdges = (callsitePath: string, callsiteSymbol: string): JsonRecord[] => [
  {
    caller_path: "apps/code-worker/src/main.ts",
    caller_symbol: "main",
    callee_path: "packages/runtime/claude-runtime/src/query-engine.ts",
    callee_symbol: "ClaudeRuntimeCore.run",
    kind: "default-entry",
  },
  {
    caller_path: "packages/runtime/claude-runtime/src/query-engine.ts",
    caller_symbol: "ClaudeRuntimeCore.run",
    callee_path: callsitePath,
    callee_symbol: callsiteSymbol,
    kind: "runtime-call",
  },
];

const routeToolResult = (target: JsonRecord, source: JsonRecord): void => {
  const name = symbolName(source);
  if (toolResultExternalizationAccepted.has(name)) {
    const targetPath = "packages/runtime/claude-runtime/src/tools/result-runtime.ts";
    const callsitePath = "packages/runtime/claude-runtime/src/e01/coordinator.ts";
    const callsiteSymbol = "E01RuntimeCoordinator.completeToolExecution";
    Object.assign(target, {
      target_path: targetPath,
      target_symbol: "ToolResultRuntime.deliver",
      planned_method: "deliver",
      default_callsite_path: callsitePath,
      default_callsite_symbol: callsiteSymbol,
      default_entry_edges: defaultEntryEdges(callsitePath, callsiteSymbol),
      canonical_owner_id: "e01.tool-result-receipt",
      state_store: "E01RuntimeSnapshot.toolResults",
      state_snapshot_property: "toolResults",
      state_effect_kind: "tool-result-externalization",
      state_observation: "toolResults.receipts[].delivery",
      state_effect_assertion: `assert.${String(source.mapping_id)}.tool-result-externalization`,
      adaptation:
        "Claude filesystem result persistence is adapted to ToolResultRuntime delivery and the Zyra artifact writer; source path helpers are excluded while threshold, block processing and externalization effects are retained.",
      source_behavior_claim: `${name} contributes threshold-driven externalization of a large tool result before it reaches model context`,
      target_behavior_claim:
        "ToolResultRuntime.deliver enforces result budgets, writes oversized payloads through the artifact port and persists delivery metadata in the canonical receipt",
      semantic_equivalence:
        "Both mechanisms replace oversized inline results with durable references; Zyra delegates physical paths to its artifact boundary.",
    });
    return;
  }
  const methodBySource: Record<string, string> = {
    isToolResultContentEmpty: "candidate",
    createContentReplacementState: "snapshot",
    cloneContentReplacementState: "snapshot",
    getPerMessageBudgetLimit: "currentPolicy",
    provisionContentReplacementState: "register",
    isContentAlreadyCompacted: "decisionFor",
    hasImageBlock: "candidate",
    contentSize: "candidate",
    collectCandidatesFromMessage: "candidatesForRound",
    collectCandidatesByMessage: "candidatesForRound",
    partitionByPriorDecision: "partitionCandidates",
    selectFreshToReplace: "enforceRound",
    replaceToolResultContents: "contentFor",
    buildReplacement: "contentFor",
    enforceToolResultBudget: "enforceRound",
    applyToolResultBudget: "enforceRound",
    reconstructContentReplacementState: "restore",
    reconstructForSubagentResume: "reconstructDecision",
    generatePreview: "candidate",
  };
  const method = methodBySource[name] ?? "enforceRound";
  const targetPath = "packages/runtime/claude-runtime/src/loop/tool-observation-budget-runtime.ts";
  const callsitePath = "packages/runtime/claude-runtime/src/loop/model-iteration-runtime.ts";
  const callsiteSymbol = "ModelIterationRuntime.buildRevisionMessages";
  Object.assign(target, {
    target_path: targetPath,
    target_symbol: `ToolObservationBudgetRuntime.${method}`,
    planned_method: method,
    default_callsite_path: callsitePath,
    default_callsite_symbol: callsiteSymbol,
    default_entry_edges: defaultEntryEdges(callsitePath, callsiteSymbol),
    canonical_owner_id: "e01.tool-observation-budget",
    state_store: "ModelIterationSnapshot.toolObservationBudget",
    state_snapshot_property: "toolObservationBudget",
    state_effect_kind: "provider-observation-budget",
    state_observation: "toolObservationBudget.plans[].digest",
    state_effect_assertion: `assert.${String(source.mapping_id)}.provider-observation-budget`,
    adaptation:
      "Claude tool-result replacement and budget semantics are adapted into a Zyra-owned, checksum-restorable provider-transcript budget runtime; raw receipts remain owned by ToolResultRuntime.",
    source_behavior_claim: `${name} contributes candidate selection, replacement, preview or reconstruction before a subsequent model call`,
    target_behavior_claim: `ToolObservationBudgetRuntime.${method} changes the provider-visible observation content or its durable replacement state`,
    semantic_equivalence:
      "Both sides constrain accumulated tool observations before the next provider request; Zyra removes process-global and filesystem state.",
    behavior_tests: [
      behaviorTest(
        "e01.mutation.observation-budget-enforces-cross-result-limit",
        "packages/runtime/claude-runtime/test/e01/tool-observation-budget.behavior.test.ts",
        "ToolObservationBudgetRuntime.enforceRound",
        ["plan.renderedChars <= plan.limitChars", "runtime.contentFor"],
      ),
      behaviorTest(
        "e01.mutation.observation-budget-restore-rejects-tampering",
        "packages/runtime/claude-runtime/test/e01/tool-observation-budget.behavior.test.ts",
        "ToolObservationBudgetRuntime.restore",
        ["restored.restore(snapshot)", "snapshot checksum mismatch"],
      ),
      behaviorTest(
        "e01.integration.model-iteration-applies-observation-budget",
        "packages/runtime/claude-runtime/test/e01/tool-observation-budget.behavior.test.ts",
        "ModelIterationRuntime.buildRevisionMessages",
        [
          "observation_budget_plan_digest",
          "snapshot.toolObservationBudget.plans.length",
          "tool_observation_omitted",
        ],
      ),
    ],
    success_test_ids: [
      "e01.mutation.observation-budget-enforces-cross-result-limit",
      "e01.integration.model-iteration-applies-observation-budget",
    ],
    failure_test_ids: ["e01.mutation.observation-budget-restore-rejects-tampering"],
    mutation_ids: [
      "e01-mut-045-observation-budget-enforcement",
      "e01-mut-046-observation-budget-snapshot-checksum",
    ],
  });
};

const routeSession = (target: JsonRecord, source: JsonRecord): void => {
  const path = String(source.source_path ?? "").replaceAll("\\", "/");
  const name = symbolName(source);
  let targetPath: string;
  let targetSymbol: string;
  let callsiteSymbol: string;
  if (path.endsWith("/history.ts")) {
    targetPath = "packages/runtime/claude-runtime/src/session/history-runtime.ts";
    if (name === "addToHistory" || name === "addToPromptHistory") {
      targetSymbol = "SessionHistoryRuntime.append";
      callsiteSymbol = "E01RuntimeCoordinator.beginCanonicalTurn";
    } else if (
      name === "deserializeLogEntry" ||
      name === "makeLogEntryReader" ||
      name === "makeHistoryReader" ||
      name === "resolveStoredPastedContent" ||
      name === "logEntryToHistoryEntry"
    ) {
      targetSymbol = "SessionHistoryRuntime.restore";
      callsiteSymbol = "E01RuntimeCoordinator.restore";
    } else {
      targetSymbol = "SessionHistoryRuntime.snapshot";
      callsiteSymbol = "E01RuntimeCoordinator.snapshot";
    }
  } else {
    targetPath = "packages/runtime/claude-runtime/src/session/durable-runtime.ts";
    targetSymbol = "DurableSessionRuntime.project";
    callsiteSymbol = "E01RuntimeCoordinator.snapshot";
  }
  const callsitePath = "packages/runtime/claude-runtime/src/e01/coordinator.ts";
  Object.assign(target, {
    target_path: targetPath,
    target_symbol: targetSymbol,
    planned_method: targetSymbol.slice(targetSymbol.lastIndexOf(".") + 1),
    default_callsite_path: callsitePath,
    default_callsite_symbol: callsiteSymbol,
    default_entry_edges: defaultEntryEdges(callsitePath, callsiteSymbol),
    canonical_owner_id: path.endsWith("/history.ts") ? "e01.session-history" : "e01.durable-session",
    state_store: path.endsWith("/history.ts")
      ? "E01RuntimeSnapshot.history"
      : "E01RuntimeSnapshot.session",
    state_snapshot_property: path.endsWith("/history.ts") ? "history" : "session",
    state_effect_kind: path.endsWith("/history.ts") ? "history-mutation" : "session-projection",
    state_observation: path.endsWith("/history.ts") ? "history.entries" : "session.phase",
    state_effect_assertion: `assert.${String(source.mapping_id)}.session-state`,
    adaptation:
      "Claude history/session observation is adapted to Zyra durable session and append-only history owners; global listeners and flush state are excluded.",
    source_behavior_claim: `${name} reads or appends session history/state used by a resumed query`,
    target_behavior_claim: `${targetSymbol} changes or exposes the canonical session snapshot restored by the coordinator`,
    semantic_equivalence: "Both sides preserve query-visible session continuity; Zyra supplies explicit snapshot custody and checksums.",
  });
};

const routeSessionRestore = (target: JsonRecord, source: JsonRecord): void => {
  const targetPath = "packages/runtime/claude-runtime/src/e01/coordinator.ts";
  const callsitePath = "packages/runtime/claude-runtime/src/query-engine.ts";
  const callsiteSymbol = "ClaudeRuntimeCore.restore";
  Object.assign(target, {
    target_path: targetPath,
    target_symbol: "E01RuntimeCoordinator.restore",
    planned_method: "restore",
    default_callsite_path: callsitePath,
    default_callsite_symbol: callsiteSymbol,
    default_entry_edges: defaultEntryEdges(callsitePath, callsiteSymbol),
    canonical_owner_id: "e01.session-restore-coordinator",
    state_store: "E01RuntimeSnapshot",
    state_snapshot_property: "session",
    state_effect_kind: "same-session-resume",
    state_observation: "journal.transitions[].transitionId",
    state_effect_assertion: `assert.${String(source.mapping_id)}.same-session-resume`,
    adaptation:
      "Claude resume orchestration is adapted to the Zyra coordinator's checksum-verified restoration of session, history, model iteration, tool receipts and journal state; worktree and process-global restoration remain excluded.",
    source_behavior_claim: `${symbolName(source)} reconstructs a resumable conversation from durable session evidence`,
    target_behavior_claim:
      "E01RuntimeCoordinator.restore atomically reconstructs all E01 canonical owners and rejects identity, checksum or replay conflicts",
    semantic_equivalence:
      "Both mechanisms resume the same conversation without replaying completed effects; Zyra replaces process globals with explicit snapshot owners.",
  });
};

const routeContextBudget = (target: JsonRecord, source: JsonRecord): void => {
  const callsitePath = "packages/runtime/claude-runtime/src/e01/coordinator.ts";
  const callsiteSymbol = "E01RuntimeCoordinator.decideContext";
  Object.assign(target, {
    target_path: "packages/runtime/claude-runtime/src/context/token-runtime.ts",
    target_symbol: "ContextTokenRuntime.decide",
    planned_method: "decide",
    default_callsite_path: callsitePath,
    default_callsite_symbol: callsiteSymbol,
    default_entry_edges: defaultEntryEdges(callsitePath, callsiteSymbol),
    canonical_owner_id: "e01.context-token-budget",
    state_store: "E01RuntimeSnapshot.contextTokens",
    state_snapshot_property: "contextTokens",
    state_effect_kind: "context-budget-decision",
    state_observation: "contextTokens.remainingTokens",
    state_effect_assertion: `assert.${String(source.mapping_id)}.context-budget`,
    source_behavior_claim: `${symbolName(source)} decides whether the active query can continue within its token budget`,
    target_behavior_claim: "ContextTokenRuntime.decide emits the continue/compact/stop action from durable consumption state",
    semantic_equivalence: "Both sides enforce diminishing-budget termination; Zyra makes the tracker state snapshot-owned.",
  });
};

const addStrictMutations = (records: JsonRecord[]): JsonRecord[] => {
  const additions: JsonRecord[] = [
    {
      schema_version: "3.0",
      execution_id: "E01",
      record_type: "mutation",
      mutation_id: "e01-mut-045-observation-budget-enforcement",
      mutation_operator: "disable-loop",
      semantic_risk: "provider-observation-budget",
      target_path: "packages/runtime/claude-runtime/src/loop/tool-observation-budget-runtime.ts",
      target_symbol: "ToolObservationBudgetRuntime.enforceRound",
      compile_survives: true,
      frozen_patch_sha256: mutationFingerprint(
        "const limit = Math.max(1, this.policy.maxRoundChars - errorReserve);",
        "const limit = Number.MAX_SAFE_INTEGER;",
      ),
      expected_killer_test_ids: ["e01.mutation.observation-budget-enforces-cross-result-limit"],
    },
    {
      schema_version: "3.0",
      execution_id: "E01",
      record_type: "mutation",
      mutation_id: "e01-mut-046-observation-budget-snapshot-checksum",
      mutation_operator: "disable-guard",
      semantic_risk: "provider-observation-restore",
      target_path: "packages/runtime/claude-runtime/src/loop/tool-observation-budget-runtime.ts",
      target_symbol: "ToolObservationBudgetRuntime.restore",
      compile_survives: true,
      frozen_patch_sha256: mutationFingerprint(
        "if (expectedChecksum !== snapshot.checksum) {",
        "if (false) {",
      ),
      expected_killer_test_ids: ["e01.mutation.observation-budget-restore-rejects-tampering"],
    },
  ];
  const byId = new Map(records.map((record) => [String(record.mutation_id), record]));
  for (const record of additions) byId.set(String(record.mutation_id), record);
  return [...byId.values()].sort((left, right) => String(left.mutation_id).localeCompare(String(right.mutation_id)));
};

const replaceScriptVersion = (value: unknown): unknown => {
  if (typeof value === "string") {
    return value
      .replaceAll("m1_r01_e01_v3.ts", "m1_r01_e01_v4.ts")
      .replaceAll("verify_m1_r01_e01_v3.ts", "verify_m1_r01_e01_v4.ts");
  }
  if (Array.isArray(value)) return value.map(replaceScriptVersion);
  if (value && typeof value === "object") {
    const output: JsonRecord = {};
    for (const [key, entry] of Object.entries(value as JsonRecord)) output[key] = replaceScriptVersion(entry);
    return output;
  }
  return value;
};

await import("./m1_r01_e01_v3.ts");

const implementationHead = gitText(repoRoot, ["rev-parse", "HEAD"]);
const sourceRecords = readJsonLines(sourceManifestPath);
const sourceById = new Map<string, JsonRecord>();
const frozenBlobs = new Map<string, Buffer>();
for (const record of sourceRecords) {
  const path = String(record.source_path);
  let blob = frozenBlobs.get(path);
  if (!blob) {
    blob = gitBytes(sourceRoot, ["show", `${SOURCE_SNAPSHOT}:${path}`]);
    frozenBlobs.set(path, blob);
  }
  record.source_snapshot = SOURCE_SNAPSHOT;
  record.source_sha256 = sha256(blob);
  const reason = rejectionReason(record);
  if (reason) {
    record.accepted = false;
    record.migration_mode = "rejected";
    record.exclusion_reason = reason;
  } else if (
    String(record.mapping_id).startsWith("e01-rej-") &&
    String(record.exclusion_reason).startsWith("statement tail is outside")
  ) {
    record.accepted = true;
    record.migration_mode = "adapted";
    record.source_role = "supplementary";
    record.supplementary_gap =
      "continues the same accepted primary source symbol beyond the original parser range so its executable tail is not omitted";
    record.exclusion_reason = null;
    record.continuation_of_source_symbol = record.source_symbol;
  }
  sourceById.set(String(record.mapping_id), record);
}
const orderedSources = [...sourceById.values()].sort((left, right) =>
  String(left.mapping_id).localeCompare(String(right.mapping_id)),
);

const routedTargets = readJsonLines(targetManifestPath)
  .filter((target) => sourceById.get(String(target.mapping_id))?.accepted === true)
  .map((target) => {
    const source = sourceById.get(String(target.mapping_id));
    if (!source) throw new Error(`target ${String(target.mapping_id)} has no source record`);
    const sourcePath = String(source.source_path).replaceAll("\\", "/");
    target.default_entry_id = DEFAULT_ENTRY_ID;
    target.source_symbol = source.source_symbol;
    target.source_behavior_claim ??= `${String(source.source_symbol)} contributes executable ${String(source.semantic_domain)} behavior`;
    target.target_behavior_claim ??= `${String(target.target_symbol)} owns the corresponding Zyra runtime state transition`;
    target.semantic_equivalence ??=
      "The source mechanism is adapted to Zyra contracts while preserving its externally observable runtime effect.";
    if (sourcePath.endsWith("/toolResultStorage.ts")) routeToolResult(target, source);
    else if (sourcePath.endsWith("/history.ts") || sourcePath.endsWith("/sessionState.ts")) {
      routeSession(target, source);
    } else if (sourcePath.endsWith("/sessionRestore.ts")) {
      routeSessionRestore(target, source);
    } else if (sourcePath.endsWith("/tokenBudget.ts")) routeContextBudget(target, source);
    const targetPath = String(target.target_path);
    target.target_sha256 = sha256(gitBytes(repoRoot, ["show", `${implementationHead}:${targetPath}`]));
    return target;
  });
const targetById = new Map(routedTargets.map((target) => [String(target.mapping_id), target]));
for (const source of orderedSources.filter((record) => record.accepted === true)) {
  const id = String(source.mapping_id);
  if (targetById.has(id)) continue;
  const template = routedTargets.find(
    (target) => String(target.source_symbol) === String(source.source_symbol),
  );
  if (!template) {
    throw new Error(`accepted continuation ${id} has no target template for ${String(source.source_symbol)}`);
  }
  const target = JSON.parse(JSON.stringify(template)) as JsonRecord;
  target.mapping_id = id;
  target.source_symbol = source.source_symbol;
  target.source_behavior_claim = `${String(source.source_symbol)} continuation lines complete the same executable source behavior as its accepted leading range`;
  target.state_effect_assertion = `assert.${id}.${String(target.state_effect_kind)}`;
  targetById.set(id, target);
}
const targetRecords = [...targetById.values()].sort((left, right) =>
  String(left.mapping_id).localeCompare(String(right.mapping_id)),
);

const mutationRecords = addStrictMutations(readJsonLines(mutationManifestPath));
writeJsonLines(sourceManifestPath, orderedSources);
writeJsonLines(targetManifestPath, targetRecords);
writeJsonLines(mutationManifestPath, mutationRecords);

const originalProfile = readJson(gateProfilePath);
const profile = replaceScriptVersion(originalProfile) as JsonRecord;
profile.allowed_control_plane_commits = (Array.isArray(profile.allowed_control_plane_commits)
  ? profile.allowed_control_plane_commits
  : []
).filter((value) => String(value) !== IMPLEMENTATION_DIFF_BASELINE);
profile.implementation_diff_baseline = IMPLEMENTATION_DIFF_BASELINE;
profile.preexisting_non_e01_baseline = {
  commit: IMPLEMENTATION_DIFF_BASELINE,
  required_parent: VERIFIED_BASELINE,
  e01_effective_line_credit: 0,
  reason: "pre-E01 runtime event checkpoint retained in ancestry but excluded from E01 implementation credit",
};
profile.default_entries = [
  {
    default_entry_id: DEFAULT_ENTRY_ID,
    path: "apps/code-worker/src/main.ts",
    symbol: "main",
    runtime_owner: "typescript",
    command: ["bun", "apps/code-worker/src/main.ts"],
  },
];
profile.manifest_generator = GENERATOR;
profile.schema_verifier = SCHEMA_VERIFIER;
profile.checker_source_paths = [
  ...new Set([
    ...(Array.isArray(profile.checker_source_paths) ? profile.checker_source_paths.map(String) : []),
    GENERATOR,
    SCHEMA_VERIFIER,
  ]),
].sort();
writeJson(gateProfilePath, profile);

const receipt = readJson(receiptPath);
receipt.current_control_plane_head = implementationHead;
receipt.implementation_diff_baseline = IMPLEMENTATION_DIFF_BASELINE;
receipt.manifest_generator_command = ["npx", "--yes", "bun@1.2.15", GENERATOR];
receipt.schema_validator_command = ["npx", "--yes", "bun@1.2.15", SCHEMA_VERIFIER];
receipt.source_hash_semantics = "git-blob-bytes-at-declared-source-snapshot";
receipt.input_sha256 = {
  "execution-01-source-manifest.jsonl": sha256(readFileSync(sourceManifestPath)),
  "execution-01-python-owner-baseline.jsonl": sha256(
    readFileSync(join(manifestRoot, "execution-01-python-owner-baseline.jsonl")),
  ),
  "execution-01-target-custody-map.jsonl": sha256(readFileSync(targetManifestPath)),
  "execution-01-mutation-manifest.jsonl": sha256(readFileSync(mutationManifestPath)),
  "execution-01-gate-profile.json": sha256(readFileSync(gateProfilePath)),
};
writeJson(receiptPath, receipt);

writeJson(candidateMetadataPath, {
  schema_version: "3.0",
  execution_id: "E01",
  verified_baseline: VERIFIED_BASELINE,
  implementation_diff_baseline: IMPLEMENTATION_DIFF_BASELINE,
  implementation_candidate: implementationHead,
  cleanroom_target: null,
  candidate_evidence_commit: null,
  candidate_status: "implementation_generated_pending_validation",
  verified_complete: false,
  independent_review_target: null,
  independent_review_verdict: "PENDING",
  root_manifest_commit_boundary:
    "G:/agent-zoo/docs/remediations is outside the Zyra Git repository and is delivered as an explicit workspace boundary.",
  notes: [
    "The implementation diff starts after the pre-E01 0cd21bff5e2d checkpoint; that checkpoint receives zero E01 line credit.",
    "Source hashes bind immutable Git blob bytes at the declared Claude snapshot, not checkout line endings.",
    "Evidence and independent-review commits are populated only after validation and candidate freeze.",
  ],
});

process.stdout.write(
  `${JSON.stringify({
    ok: true,
    generator: "v4",
    implementation_candidate: implementationHead,
    accepted_sources: orderedSources.filter((record) => record.accepted === true).length,
    rejected_sources: orderedSources.filter((record) => record.accepted !== true).length,
    custody_mappings: targetRecords.length,
    mutations: mutationRecords.length,
  })}\n`,
);
