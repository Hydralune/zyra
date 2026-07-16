import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  mutationPatchFingerprint,
  mutationSpecs,
} from "./run_m1_r01_e01_mutations.ts";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "../..");
const workspaceRoot = resolve(repoRoot, "..");
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

const VERIFIED_BASELINE = "c34535a783e88f9481387ced89cba4fbc333dc74";
const IMPLEMENTATION_DIFF_BASELINE = "0cd21bff5e2d160476f2ce3cef766bf53aab1239";
const DEFAULT_ENTRY_ID = "e01.default-code-worker";
const VERIFICATION_CONTRACT_VERSION = "zyra.e01-verification/v5";
const GENERATOR = "scripts/remediation/m1_r01_e01_v6.ts";
const SCHEMA_VERIFIER = "scripts/remediation/verify_m1_r01_e01_v4.ts";
const MAIN_PATH = "apps/code-worker/src/main.ts";
const MAIN_SYMBOL = "main";
const QUERY_PATH = "packages/runtime/claude-runtime/src/query-engine.ts";
const QUERY_SYMBOL = "ClaudeRuntimeCore.run";
const COORDINATOR_PATH = "packages/runtime/claude-runtime/src/e01/coordinator.ts";
const SEMANTIC_TEST_PATH =
  "packages/runtime/claude-runtime/test/e01/semantic-custody.behavior.test.ts";

type JsonRecord = Record<string, unknown>;

const sha256 = (value: Uint8Array | string): string =>
  createHash("sha256").update(value).digest("hex");

const gitText = (args: readonly string[]): string =>
  execFileSync("git", [...args], {
    cwd: repoRoot,
    encoding: "utf8",
    maxBuffer: 128 * 1024 * 1024,
  }).trim();

const gitBytes = (args: readonly string[]): Buffer =>
  execFileSync("git", [...args], {
    cwd: repoRoot,
    encoding: "buffer",
    maxBuffer: 128 * 1024 * 1024,
  });

const readJson = (path: string): JsonRecord =>
  JSON.parse(readFileSync(path, "utf8")) as JsonRecord;

const readJsonLines = (path: string): JsonRecord[] =>
  readFileSync(path, "utf8")
    .split(/\r?\n/)
    .filter((line) => line.trim().length > 0)
    .map((line) => JSON.parse(line) as JsonRecord);

const stable = (value: unknown): unknown => {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === "object") {
    const output: JsonRecord = {};
    for (const key of Object.keys(value as JsonRecord).sort()) {
      output[key] = stable((value as JsonRecord)[key]);
    }
    return output;
  }
  return value;
};

const writeJson = (path: string, value: unknown): void => {
  writeFileSync(path, `${JSON.stringify(stable(value), null, 2)}\n`, "utf8");
};

const writeJsonLines = (path: string, values: readonly JsonRecord[]): void => {
  writeFileSync(
    path,
    `${values.map((value) => JSON.stringify(stable(value))).join("\n")}\n`,
    "utf8",
  );
};

const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

const sourceName = (record: JsonRecord): string => {
  const symbol = String(record.source_symbol ?? "");
  return symbol.slice(symbol.lastIndexOf("::") + 2);
};

const behaviorTest = (
  name: string,
  path: string,
  anchor: string,
  assertionTokens: readonly string[],
): JsonRecord => ({ name, path, anchor, assertion_tokens: assertionTokens });

const edge = (
  callerPath: string,
  callerSymbol: string,
  calleePath: string,
  calleeSymbol: string,
  kind: string,
): JsonRecord => ({
  caller_path: callerPath,
  caller_symbol: callerSymbol,
  callee_path: calleePath,
  callee_symbol: calleeSymbol,
  kind,
});

const directEntryEdges = (
  callsitePath: string,
  callsiteSymbol: string,
  targetPath: string,
  targetSymbol: string,
): JsonRecord[] => {
  const output = [edge(MAIN_PATH, MAIN_SYMBOL, QUERY_PATH, QUERY_SYMBOL, "default-entry")];
  if (targetPath === QUERY_PATH && targetSymbol === QUERY_SYMBOL) return output;
  if (callsitePath === QUERY_PATH && callsiteSymbol === QUERY_SYMBOL) {
    output.push(edge(QUERY_PATH, QUERY_SYMBOL, targetPath, targetSymbol, "runtime-target"));
    return output;
  }
  output.push(edge(QUERY_PATH, QUERY_SYMBOL, callsitePath, callsiteSymbol, "runtime-call"));
  if (callsitePath !== targetPath || callsiteSymbol !== targetSymbol) {
    output.push(edge(callsitePath, callsiteSymbol, targetPath, targetSymbol, "owner-call"));
  }
  return output;
};

const setRoute = (
  target: JsonRecord,
  source: JsonRecord,
  input: {
    targetPath: string;
    targetSymbol: string;
    callsitePath: string;
    callsiteSymbol: string;
    edges?: JsonRecord[];
    owner: string;
    stateStore: string;
    stateProperty: string;
    stateKind: string;
    stateObservation: string;
    adaptation: string;
    sourceClaim: string;
    targetClaim: string;
    equivalence: string;
    tests: JsonRecord[];
    mutations: string[];
  },
): void => {
  Object.assign(target, {
    default_entry_id: DEFAULT_ENTRY_ID,
    source_symbol: source.source_symbol,
    target_path: input.targetPath,
    target_symbol: input.targetSymbol,
    planned_method: input.targetSymbol.slice(input.targetSymbol.lastIndexOf(".") + 1),
    default_callsite_path: input.callsitePath,
    default_callsite_symbol: input.callsiteSymbol,
    default_entry_edges:
      input.edges ??
      directEntryEdges(
        input.callsitePath,
        input.callsiteSymbol,
        input.targetPath,
        input.targetSymbol,
      ),
    canonical_owner_id: input.owner,
    state_store: input.stateStore,
    state_snapshot_property: input.stateProperty,
    state_effect_kind: input.stateKind,
    state_observation: input.stateObservation,
    state_effect_assertion: `assert.${String(source.mapping_id)}.${input.stateKind}`,
    adaptation: input.adaptation,
    source_behavior_claim: input.sourceClaim,
    target_behavior_claim: input.targetClaim,
    semantic_equivalence: input.equivalence,
    behavior_tests: input.tests,
    success_test_ids: input.tests.map((test) => String(test.name)),
    failure_test_ids: input.tests.map((test) => String(test.name)),
    mutation_ids: input.mutations,
  });
};

const forcedRejections = new Map<string, string>([
  [
    "skillPrefetch",
    "conditional skill-search module loading is not owned by E01; SkillRuntime activation is a later execution-unit boundary",
  ],
  [
    "jobClassifier",
    "conditional template job-classifier loading has no E01 runtime-core state owner or default-path invocation",
  ],
  [
    "taskSummaryModule",
    "background-session task-summary module loading is not migrated into the E01 query loop",
  ],
  [
    "autoModeStateModule",
    "process-local auto-mode module loading is not equivalent to model stream resolution and is not claimed by E01",
  ],
  [
    "createStderrLogger",
    "the Anthropic SDK console logger is intentionally replaced by structured Zyra telemetry; its stderr side effect is not migrated",
  ],
  [
    "verifyApiKey",
    "E01 registers and resolves credentials but does not issue the source one-token remote verification request; registration is not claimed as verification",
  ],
  [
    "sessionTranscriptModule",
    "conditional transcript-module loading has no migrated module-loading or transcript lookup effect in ContextCompactionRuntime",
  ],
  [
    "getCachedMCModule",
    "dynamic cached-microcompact module loading is deliberately removed; Zyra directly owns microcompact state without a lazy module cache",
  ],
  [
    "isToolResultContentEmpty",
    "the source empty-content predicate is not a distinct ToolObservationBudgetRuntime transition and is excluded rather than mapped to candidate lookup",
  ],
  [
    "resolveStoredPastedContent",
    "history restore does not read source pasted-content files; attachment content resolution remains owned by the separate restore attachment runtime",
  ],
  [
    "createCompactCanUseTool",
    "the source compact-summary tool permission helper is not migrated; E01 uses an injected summary provider without granting tools during compaction",
  ],
]);

const newlyAcceptedPostCutoff = new Set([
  "getDefaultMaxRetries",
  "getMaxRetries",
  "getRetryAfterMs",
  "getRateLimitResetDelayMs",
  "categorizeRetryableAPIError",
  "getErrorMessageIfRefusal",
]);

const specificPostCutoffReason = (record: JsonRecord): string => {
  const name = sourceName(record);
  const path = String(record.source_path ?? "").replaceAll("\\", "/");
  if (name === "adjustParamsForNonStreaming") {
    return "the compatibility helper is not invoked by E01's default provider execution path, so its non-streaming parameter mutation receives no main-path custody credit";
  }
  if (name === "resetPromptCacheBreakDetection") {
    return "process-wide prompt-cache reset is intentionally absent; E01 only deletes session/agent-scoped telemetry through cleanupAgentTracking";
  }
  if (name === "writeCacheBreakDiff") {
    return "filesystem cache-diff emission is intentionally replaced by structured telemetry events and is not migrated as a write side effect";
  }
  if (path.endsWith("/claude.ts")) {
    return `${name} is not invoked by the frozen E01 provider path and therefore receives no provider execution custody claim`;
  }
  if (path.endsWith("/withRetry.ts")) {
    return `${name} is superseded by ProviderRecoveryRuntime policy and has no separately reachable state transition in E01`;
  }
  if (path.endsWith("/promptCacheBreakDetection.ts")) {
    return `${name} retains source filesystem/process-global cache tracking not owned by E01 structured telemetry`;
  }
  if (path.endsWith("/compact.ts")) {
    return `${name} depends on source worktree or skill-restore policy that is not part of E01 compaction custody`;
  }
  return `${name} has no independently reachable E01 target behavior after source-role裁决`;
};

const recoveryEdges = (targetPath: string, targetSymbol: string): JsonRecord[] => {
  const decide = "E01RuntimeCoordinator.decideProviderRecovery";
  const planPath = "packages/runtime/claude-runtime/src/provider/recovery-runtime.ts";
  const plan = "ProviderRecoveryRuntime.plan";
  const output = [
    edge(MAIN_PATH, MAIN_SYMBOL, QUERY_PATH, QUERY_SYMBOL, "default-entry"),
    edge(QUERY_PATH, QUERY_SYMBOL, COORDINATOR_PATH, decide, "recovery-callback"),
  ];
  if (targetSymbol === "ProviderRecoveryRuntime.createContext") {
    output.push(edge(COORDINATOR_PATH, decide, targetPath, targetSymbol, "recovery-context"));
    return output;
  }
  output.push(edge(COORDINATOR_PATH, decide, planPath, plan, "recovery-plan"));
  if (targetSymbol !== plan) {
    output.push(edge(planPath, plan, targetPath, targetSymbol, "recovery-classification"));
  }
  return output;
};

const routeRecovery = (
  target: JsonRecord,
  source: JsonRecord,
  mode: "context" | "classify" | "plan",
): void => {
  const targetPath = "packages/runtime/claude-runtime/src/provider/recovery-runtime.ts";
  const targetSymbol =
    mode === "context"
      ? "ProviderRecoveryRuntime.createContext"
      : mode === "classify"
        ? "ProviderRecoveryRuntime.classify"
        : "ProviderRecoveryRuntime.plan";
  const test =
    mode === "context"
      ? behaviorTest(
          "e01.mutation.max-retries",
          "packages/runtime/claude-runtime/test/e01/mutation-contract.behavior.test.ts",
          "retryContext(0)",
          ["plan.action", "retry_limit_exhausted"],
        )
      : behaviorTest(
          "e01.mutation.retry-class",
          "packages/runtime/claude-runtime/test/e01/mutation-contract.behavior.test.ts",
          "runtime.plan",
          ["plan.action", 'toBe("stop")'],
        );
  const callsitePath =
    mode === "classify" ? targetPath : COORDINATOR_PATH;
  const callsiteSymbol =
    mode === "classify"
      ? "ProviderRecoveryRuntime.plan"
      : "E01RuntimeCoordinator.decideProviderRecovery";
  setRoute(target, source, {
    targetPath,
    targetSymbol,
    callsitePath,
    callsiteSymbol,
    edges: recoveryEdges(targetPath, targetSymbol),
    owner: "e01.provider-recovery",
    stateStore: "E01RuntimeSnapshot.recovery",
    stateProperty: "recovery",
    stateKind: "provider-recovery-policy",
    stateObservation: "recovery.contexts[].attempt/maxRetries/outputTokenLimit",
    adaptation:
      "Claude retry and error helpers are consolidated into the deterministic ProviderRecoveryRuntime state machine used by the default model loop.",
    sourceClaim: `${sourceName(source)} classifies provider failure or computes retry limits/delay`,
    targetClaim: `${targetSymbol} persists the corresponding retry context, classification, or terminal plan`,
    equivalence:
      "Both mechanisms bound retry and classify provider failure; Zyra makes attempts, cooldowns, token limits and decisions checksum-restorable.",
    tests: [test],
    mutations: mode === "context" ? ["e01-mut-023-max-retries"] : ["e01-mut-022-retry-class"],
  });
};

const routeOutputRecovery = (target: JsonRecord, source: JsonRecord): void => {
  const targetPath = "packages/runtime/claude-runtime/src/provider/recovery-runtime.ts";
  const targetSymbol = "ProviderRecoveryRuntime.plan";
  setRoute(target, source, {
    targetPath,
    targetSymbol,
    callsitePath: COORDINATOR_PATH,
    callsiteSymbol: "E01RuntimeCoordinator.decideProviderRecovery",
    edges: recoveryEdges(targetPath, targetSymbol),
    owner: "e01.provider-output-recovery",
    stateStore: "E01RuntimeSnapshot.recovery",
    stateProperty: "recovery",
    stateKind: "bounded-output-token-recovery",
    stateObservation: "recovery.contexts[].attempt/outputTokenLimit",
    adaptation:
      "Claude's withheld max-output retry counter is adapted to a durable recovery context that reduces output budget without leaking an intermediate terminal error.",
    sourceClaim: `${sourceName(source)} bounds or recognizes a recoverable max-output-token failure`,
    targetClaim:
      "ProviderRecoveryRuntime.plan records the attempt and deterministically reduces outputTokenLimit above the safety floor",
    equivalence:
      "Both paths withhold terminal failure while a bounded lower-output retry remains possible; Zyra stores the bound and next limit in canonical recovery state.",
    tests: [
      behaviorTest(
        "e01.semantic.output-token-recovery-is-bounded",
        SEMANTIC_TEST_PATH,
        "recovery.plan",
        ["plan.action", "plan.nextOutputTokenLimit", "originalOutputTokenLimit"],
      ),
    ],
    mutations: ["e01-mut-047-output-token-reduction"],
  });
};

const routeCredentialFailure = (target: JsonRecord, source: JsonRecord): void => {
  const targetPath = "packages/runtime/claude-runtime/src/provider/credential-runtime.ts";
  const targetSymbol = "ProviderCredentialRuntime.recordFailure";
  setRoute(target, source, {
    targetPath,
    targetSymbol,
    callsitePath: COORDINATOR_PATH,
    callsiteSymbol: "E01RuntimeCoordinator.executePreparedProvider",
    owner: "e01.provider-credentials",
    stateStore: "E01RuntimeSnapshot.providerCredentials",
    stateProperty: "providerCredentials",
    stateKind: "credential-failure-invalidation",
    stateObservation: "providerCredentials.records[].status/consecutiveFailures",
    adaptation:
      "Source SDK credential-cache clearing is replaced by Zyra credential cooldown/quarantine state and secret-vault isolation.",
    sourceClaim: `${sourceName(source)} invalidates cached cloud credentials after an authentication failure`,
    targetClaim:
      "ProviderCredentialRuntime.recordFailure removes the active credential from selection through cooldown or quarantine",
    equivalence:
      "Both paths prevent reuse of failed credentials before retry; Zyra records the failure durably instead of clearing an SDK-global cache.",
    tests: [
      behaviorTest(
        "quarantines after the configured failure threshold",
        "packages/runtime/claude-runtime/test/e01/provider-policy.behavior.test.ts",
        "runtime.recordFailure",
        ["quarantined.status", "no_eligible_credential"],
      ),
    ],
    mutations: ["e01-mut-050-provider-credential-failure"],
  });
};

const routeToolPartition = (target: JsonRecord, source: JsonRecord): void => {
  const targetSymbol = "E01RuntimeCoordinator.planToolBatches";
  setRoute(target, source, {
    targetPath: COORDINATOR_PATH,
    targetSymbol,
    callsitePath: QUERY_PATH,
    callsiteSymbol: QUERY_SYMBOL,
    owner: "e01.tool-invocation-scheduler",
    stateStore: "E01RuntimeSnapshot.tools/custody",
    stateProperty: "tools",
    stateKind: "read-write-tool-partition",
    stateObservation: "tools.batches[].readOnly/callIds + custody.tools[].batchId",
    adaptation:
      "Claude's pure read/write partition is adapted to the Zyra-owned invocation scheduler, which also persists call identity and custody before execution.",
    sourceClaim: `${sourceName(source)} partitions concurrent read-only calls from serialized mutating calls`,
    targetClaim:
      "E01RuntimeCoordinator.planToolBatches schedules concurrent read-only batches and one-at-a-time mutating batches while binding canonical custody",
    equivalence:
      "Both preserve read concurrency without permitting write concurrency; Zyra adds durable invocation, permission and batch ownership.",
    tests: [
      behaviorTest(
        "e01.semantic.tool-planning-partitions-read-only-and-write-calls",
        SEMANTIC_TEST_PATH,
        "runtime.planToolBatches",
        ["concurrent_read_only", "serial_non_read_only", "runtime.snapshot().tools.calls"],
      ),
    ],
    mutations: ["e01-mut-049-owned-tool-partition"],
  });
};

const routeHistory = (target: JsonRecord, source: JsonRecord): void => {
  const name = sourceName(source);
  const append = name === "addToHistory" || name === "addToPromptHistory";
  const restore = [
    "deserializeLogEntry",
    "makeLogEntryReader",
    "makeHistoryReader",
    "logEntryToHistoryEntry",
  ].includes(name);
  const targetPath = "packages/runtime/claude-runtime/src/session/history-runtime.ts";
  const targetSymbol = append
    ? "SessionHistoryRuntime.append"
    : restore
      ? "SessionHistoryRuntime.restore"
      : "SessionHistoryRuntime.snapshot";
  const callsiteSymbol = append
    ? "E01RuntimeCoordinator.beginCanonicalTurn"
    : restore
      ? "E01RuntimeCoordinator.restore"
      : "E01RuntimeCoordinator.snapshot";
  const tests = append
    ? [
        behaviorTest(
          "appends a parent-linked main transcript with monotonic sequence",
          "packages/runtime/claude-runtime/test/e01/session-recovery.behavior.test.ts",
          "appendHistory",
          ["history.transcript()", "[1, 2, 3]"],
        ),
      ]
    : [
        behaviorTest(
          "round-trips branch history and resumes the sequence",
          "packages/runtime/claude-runtime/test/e01/session-recovery.behavior.test.ts",
          "restored.restore(snapshot)",
          ["restored.transcript()", "snapshot.revision"],
        ),
        behaviorTest(
          "rejects a checksum-tampered history snapshot",
          "packages/runtime/claude-runtime/test/e01/session-recovery.behavior.test.ts",
          "new SessionHistoryRuntime().restore(snapshot)",
          ["session history snapshot checksum mismatch"],
        ),
      ];
  setRoute(target, source, {
    targetPath,
    targetSymbol,
    callsitePath: COORDINATOR_PATH,
    callsiteSymbol,
    owner: "e01.session-history",
    stateStore: "E01RuntimeSnapshot.history",
    stateProperty: "history",
    stateKind: restore ? "history-log-restore" : append ? "history-append" : "history-checkpoint",
    stateObservation: "history.nodes/branches/boundaries/sequence",
    adaptation:
      "Claude JSONL history reading, conversion and flush are adapted to checksum-bound history nodes and coordinator-owned checkpoint restore; pasted-file expansion is excluded.",
    sourceClaim: `${name} reads, converts, appends, or flushes query-visible history`,
    targetClaim: `${targetSymbol} appends or serializes the canonical branch history restored by the default run path`,
    equivalence:
      "Both preserve visible role/content/order across resume; Zyra replaces process-global flush queues and raw JSONL with a checksummed branch snapshot.",
    tests,
    mutations: ["e01-mut-048-history-restore-checksum"],
  });
};

const routeTokenBudget = (target: JsonRecord, source: JsonRecord): void => {
  const targetPath = "packages/runtime/claude-runtime/src/context/token-runtime.ts";
  const targetSymbol = "ContextTokenRuntime.decide";
  setRoute(target, source, {
    targetPath,
    targetSymbol,
    callsitePath: COORDINATOR_PATH,
    callsiteSymbol: "E01RuntimeCoordinator.decideContext",
    owner: "e01.context-token-budget",
    stateStore: "E01RuntimeSnapshot.tokens",
    stateProperty: "tokens",
    stateKind: "context-budget-decision",
    stateObservation: "tokens.remainingTokens/action",
    adaptation:
      "Claude completion and diminishing thresholds are adapted to the durable ContextTokenRuntime tracker used by coordinator context decisions.",
    sourceClaim: `${sourceName(source)} computes continue, compact, or stop from remaining context budget`,
    targetClaim:
      "ContextTokenRuntime.decide mutates and returns the canonical context budget action",
    equivalence:
      "Both enforce diminishing context budget; Zyra snapshots consumption and decision history under the coordinator tokens owner.",
    tests: Array.isArray(target.behavior_tests)
      ? (target.behavior_tests as JsonRecord[]).slice(0, 2)
      : [],
    mutations: ["e01-mut-016-compact-threshold"],
  });
};

const routeToolResult = (target: JsonRecord, source: JsonRecord): void => {
  const name = sourceName(source);
  const external = new Set([
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
  ]).has(name);
  if (external) {
    const targetPath = "packages/runtime/claude-runtime/src/tools/result-runtime.ts";
    const targetSymbol = "ToolResultRuntime.deliver";
    setRoute(target, source, {
      targetPath,
      targetSymbol,
      callsitePath: COORDINATOR_PATH,
      callsiteSymbol: "E01RuntimeCoordinator.completeToolExecution",
      owner: "e01.tool-result-receipt",
      stateStore: "E01RuntimeSnapshot.toolResults",
      stateProperty: "toolResults",
      stateKind: "tool-result-externalization",
      stateObservation: "toolResults.deliveries[].deliveryDigest/truncated",
      adaptation:
        "Source filesystem persistence is adapted to ToolResultRuntime delivery plus the Zyra artifact boundary; path helpers remain rejected.",
      sourceClaim: `${name} contributes threshold-driven durable replacement of oversized tool output`,
      targetClaim:
        "ToolResultRuntime.deliver budgets sealed result blocks and persists a canonical delivery receipt",
      equivalence:
        "Both replace oversized inline output with bounded durable delivery; Zyra owns paths through its artifact port.",
      tests: Array.isArray(target.behavior_tests)
        ? (target.behavior_tests as JsonRecord[]).slice(0, 2)
        : [],
      mutations: ["e01-mut-030-result-budget"],
    });
    return;
  }

  const snapshotNames = new Set([
    "createContentReplacementState",
    "cloneContentReplacementState",
  ]);
  const restoreNames = new Set([
    "reconstructContentReplacementState",
    "reconstructForSubagentResume",
  ]);
  const registerNames = new Set([
    "generatePreview",
    "provisionContentReplacementState",
    "hasImageBlock",
    "contentSize",
  ]);
  const contentNames = new Set(["replaceToolResultContents", "buildReplacement"]);
  let targetPath = "packages/runtime/claude-runtime/src/loop/tool-observation-budget-runtime.ts";
  let targetSymbol = "ToolObservationBudgetRuntime.enforceRound";
  let callsitePath = "packages/runtime/claude-runtime/src/loop/model-iteration-runtime.ts";
  let callsiteSymbol = "ModelIterationRuntime.buildRevisionMessages";
  let mutations = ["e01-mut-045-observation-budget-enforcement"];
  if (snapshotNames.has(name)) {
    targetPath = callsitePath;
    targetSymbol = "ModelIterationRuntime.snapshot";
    callsitePath = QUERY_PATH;
    callsiteSymbol = QUERY_SYMBOL;
    mutations = ["e01-mut-046-observation-budget-snapshot-checksum"];
  } else if (restoreNames.has(name)) {
    targetPath = callsitePath;
    targetSymbol = "ModelIterationRuntime.restore";
    callsitePath = QUERY_PATH;
    callsiteSymbol = QUERY_SYMBOL;
    mutations = ["e01-mut-046-observation-budget-snapshot-checksum"];
  } else if (registerNames.has(name)) {
    targetSymbol = "ToolObservationBudgetRuntime.register";
  } else if (contentNames.has(name)) {
    targetSymbol = "ToolObservationBudgetRuntime.contentFor";
  }
  const integration = behaviorTest(
    "e01.integration.model-iteration-applies-observation-budget",
    "packages/runtime/claude-runtime/test/e01/tool-observation-budget.behavior.test.ts",
    "ModelIterationRuntime.buildRevisionMessages",
    [
      "observation_budget_plan_digest",
      "snapshot.toolObservationBudget.plans.length",
      "tool_observation_omitted",
    ],
  );
  const restoreTest = behaviorTest(
    "e01.mutation.observation-budget-restore-rejects-tampering",
    "packages/runtime/claude-runtime/test/e01/tool-observation-budget.behavior.test.ts",
    "ToolObservationBudgetRuntime.restore",
    ["restored.restore(snapshot)", "snapshot checksum mismatch"],
  );
  const tests = restoreNames.has(name) || snapshotNames.has(name)
    ? [restoreTest, integration]
    : [integration];
  setRoute(target, source, {
    targetPath,
    targetSymbol,
    callsitePath,
    callsiteSymbol,
    owner: "e01.tool-observation-budget",
    stateStore: "RuntimeRunResult.sessionSnapshot.modelIteration",
    stateProperty: "modelIteration",
    stateKind: "provider-observation-budget",
    stateObservation: "modelIteration.toolObservationBudget.plans[].digest",
    adaptation:
      "Claude tool-result compaction is adapted to the checksum-restorable observation budget nested in ModelIterationRuntime.",
    sourceClaim: `${name} selects, replaces, previews, or reconstructs provider-visible tool observations`,
    targetClaim: `${targetSymbol} performs the corresponding operation before the next provider round or during resume`,
    equivalence:
      "Both constrain accumulated tool observations while retaining recent/error context; Zyra stores each decision with model-iteration custody.",
    tests,
    mutations,
  });
};

const routeSessionState = (target: JsonRecord, source: JsonRecord): void => {
  const targetPath = "packages/runtime/claude-runtime/src/session/durable-runtime.ts";
  const targetSymbol = "DurableSessionRuntime.snapshot";
  setRoute(target, source, {
    targetPath,
    targetSymbol,
    callsitePath: COORDINATOR_PATH,
    callsiteSymbol: "E01RuntimeCoordinator.snapshot",
    owner: "e01.durable-session",
    stateStore: "E01RuntimeSnapshot.session",
    stateProperty: "session",
    stateKind: "session-state-projection",
    stateObservation: "session.status/effects/outbox/checkpoints",
    adaptation:
      "Process-global session getters are adapted to an immutable durable session snapshot with explicit pending effect and outbox state.",
    sourceClaim: `${sourceName(source)} exposes pending or current session state`,
    targetClaim: "DurableSessionRuntime.snapshot exposes the canonical resumable session state",
    equivalence:
      "Both expose query-visible session status; Zyra removes listeners and makes the projection checksum-restorable.",
    tests: Array.isArray(target.behavior_tests)
      ? (target.behavior_tests as JsonRecord[]).slice(0, 2)
      : [],
    mutations: ["e01-mut-001-restore-before-bootstrap"],
  });
};

const routeSessionRestore = (target: JsonRecord, source: JsonRecord): void => {
  const targetSymbol = "E01RuntimeCoordinator.restore";
  setRoute(target, source, {
    targetPath: COORDINATOR_PATH,
    targetSymbol,
    callsitePath: QUERY_PATH,
    callsiteSymbol: QUERY_SYMBOL,
    owner: "e01.session-restore-coordinator",
    stateStore: "E01RuntimeSnapshot",
    stateProperty: "session",
    stateKind: "same-session-resume",
    stateObservation: "journal.transitions[].transitionId + session.restartEpoch",
    adaptation:
      "Claude resume choreography is adapted to checksum-verified restoration of every E01 canonical owner before bootstrap.",
    sourceClaim: `${sourceName(source)} reconstructs a resumable conversation from durable evidence`,
    targetClaim:
      "E01RuntimeCoordinator.restore restores journal, session, history, provider, tool and custody owners before the default loop resumes",
    equivalence:
      "Both resume the same conversation without replaying completed effects; Zyra uses explicit owner snapshots rather than process globals.",
    tests: Array.isArray(target.behavior_tests)
      ? (target.behavior_tests as JsonRecord[]).slice(0, 2)
      : [],
    mutations: ["e01-mut-001-restore-before-bootstrap", "e01-mut-007-lost-ack"],
  });
};

const routeTarget = (target: JsonRecord, source: JsonRecord): void => {
  const path = String(source.source_path ?? "").replaceAll("\\", "/");
  const name = sourceName(source);
  if (path.endsWith("/tokenBudget.ts")) routeTokenBudget(target, source);
  else if (path.endsWith("/toolResultStorage.ts")) routeToolResult(target, source);
  else if (path.endsWith("/history.ts")) routeHistory(target, source);
  else if (path.endsWith("/sessionState.ts")) routeSessionState(target, source);
  else if (path.endsWith("/sessionRestore.ts")) routeSessionRestore(target, source);
  else if (path.endsWith("/query.ts") && ["MAX_OUTPUT_TOKENS_RECOVERY_LIMIT", "isWithheldMaxOutputTokens"].includes(name)) {
    routeOutputRecovery(target, source);
  } else if (path.endsWith("/query.ts") && name === "yieldMissingToolResultBlocks") {
    const targetPath = "packages/runtime/claude-runtime/src/loop/model-iteration-runtime.ts";
    const targetSymbol = "ModelIterationRuntime.buildRevisionMessages";
    setRoute(target, source, {
      targetPath,
      targetSymbol,
      callsitePath: QUERY_PATH,
      callsiteSymbol: QUERY_SYMBOL,
      owner: "e01.model-iteration",
      stateStore: "RuntimeRunResult.sessionSnapshot.modelIteration",
      stateProperty: "modelIteration",
      stateKind: "missing-tool-result-repair",
      stateObservation: "modelIteration.transcript[].content[type=tool_result]",
      adaptation:
        "Source interruption tool-result synthesis is adapted to ModelIterationRuntime's terminal observation projection before the next provider round.",
      sourceClaim: "yieldMissingToolResultBlocks emits error observations for unresolved tool uses",
      targetClaim:
        "ModelIterationRuntime.buildRevisionMessages emits one correlated tool_result block for every settled tool call",
      equivalence:
        "Both repair provider transcript invariants by pairing tool uses with terminal observations.",
      tests: [
        behaviorTest(
          "e01.integration.model-iteration-applies-observation-budget",
          "packages/runtime/claude-runtime/test/e01/tool-observation-budget.behavior.test.ts",
          "ModelIterationRuntime.buildRevisionMessages",
          ["tool_observation_omitted", "observation_budget_plan_digest"],
        ),
      ],
      mutations: ["e01-mut-045-observation-budget-enforcement"],
    });
  } else if (path.endsWith("/withRetry.ts") && ["handleAwsCredentialError", "handleGcpCredentialError"].includes(name)) {
    routeCredentialFailure(target, source);
  } else if (path.endsWith("/withRetry.ts") && newlyAcceptedPostCutoff.has(name)) {
    routeRecovery(target, source, ["getDefaultMaxRetries", "getMaxRetries"].includes(name) ? "context" : "classify");
  } else if (path.endsWith("/errors.ts") && newlyAcceptedPostCutoff.has(name)) {
    routeRecovery(target, source, "classify");
  } else if (path.endsWith("/toolOrchestration.ts") && ["partitionToolCalls", "getMaxToolUseConcurrency"].includes(name)) {
    routeToolPartition(target, source);
  }
};

const addMutations = (records: JsonRecord[]): JsonRecord[] => {
  const additions: Array<[string, string, string, string, string[]]> = [
    [
      "e01-mut-047-output-token-reduction",
      "packages/runtime/claude-runtime/src/provider/recovery-runtime.ts",
      "ProviderRecoveryRuntime.plan",
      "bounded-output-token-recovery",
      ["e01.semantic.output-token-recovery-is-bounded"],
    ],
    [
      "e01-mut-048-history-restore-checksum",
      "packages/runtime/claude-runtime/src/session/history-runtime.ts",
      "SessionHistoryRuntime.restore",
      "history-restore-integrity",
      ["rejects a checksum-tampered history snapshot"],
    ],
    [
      "e01-mut-049-owned-tool-partition",
      COORDINATOR_PATH,
      "E01RuntimeCoordinator.planToolBatches",
      "read-write-tool-partition",
      ["e01.semantic.tool-planning-partitions-read-only-and-write-calls"],
    ],
    [
      "e01-mut-050-provider-credential-failure",
      "packages/runtime/claude-runtime/src/provider/credential-runtime.ts",
      "ProviderCredentialRuntime.recordFailure",
      "credential-failure-invalidation",
      ["quarantines after the configured failure threshold"],
    ],
  ];
  const byId = new Map(records.map((record) => [String(record.mutation_id), record]));
  for (const [id, targetPath, targetSymbol, risk, tests] of additions) {
    const spec = mutationSpecs[id];
    if (!spec) throw new Error(`missing executable mutation ${id}`);
    byId.set(id, {
      schema_version: "3.0",
      execution_id: "E01",
      record_type: "mutation",
      mutation_id: id,
      mutation_operator: "disable-guard",
      semantic_risk: risk,
      target_path: targetPath,
      target_symbol: targetSymbol,
      compile_survives: true,
      frozen_patch_sha256: mutationPatchFingerprint(spec, "\n"),
      expected_killer_test_ids: tests,
    });
  }
  return [...byId.values()].sort((left, right) =>
    String(left.mutation_id).localeCompare(String(right.mutation_id)),
  );
};

await import("./m1_r01_e01_v4.ts");

const implementationHead = gitText(["rev-parse", "HEAD"]);
const sourceRecords = readJsonLines(sourceManifestPath);
for (const record of sourceRecords) {
  const name = sourceName(record);
  const rejection = forcedRejections.get(name);
  if (rejection) {
    record.accepted = false;
    record.migration_mode = "rejected";
    record.source_role = "reference_only";
    record.exclusion_reason = rejection;
    delete record.supplementary_gap;
    continue;
  }
  if (newlyAcceptedPostCutoff.has(name)) {
    record.accepted = true;
    record.migration_mode = "adapted";
    record.source_role = "supplementary";
    record.supplementary_gap =
      "closes a concrete retry classification, limit, or delay mechanism already owned by ProviderRecoveryRuntime but omitted by the original source cutoff";
    record.exclusion_reason = null;
    continue;
  }
  if (
    record.accepted !== true &&
    String(record.exclusion_reason ?? "").startsWith("outside curated executable source boundary")
  ) {
    record.migration_mode = "rejected";
    record.source_role = "reference_only";
    record.exclusion_reason = specificPostCutoffReason(record);
  }
}
sourceRecords.sort((left, right) =>
  String(left.mapping_id).localeCompare(String(right.mapping_id)),
);
const sourceById = new Map(sourceRecords.map((record) => [String(record.mapping_id), record]));

const originalTargets = readJsonLines(targetManifestPath);
const originalTargetById = new Map(
  originalTargets.map((target) => [String(target.mapping_id), target]),
);
const accepted = sourceRecords.filter((record) => record.accepted === true);
const targetRecords: JsonRecord[] = [];
for (const source of accepted) {
  const id = String(source.mapping_id);
  let target = originalTargetById.get(id);
  if (!target) {
    const template = originalTargets.find((candidate) => {
      const templateSource = sourceById.get(String(candidate.mapping_id));
      return templateSource?.source_path === source.source_path;
    });
    if (!template) throw new Error(`no target template for ${id} ${String(source.source_path)}`);
    target = clone(template);
    target.mapping_id = id;
  } else {
    target = clone(target);
  }
  target.source_symbol = source.source_symbol;
  target.default_entry_id = DEFAULT_ENTRY_ID;
  routeTarget(target, source);
  target.target_sha256 = sha256(
    gitBytes(["show", `${implementationHead}:${String(target.target_path)}`]),
  );
  targetRecords.push(target);
}
targetRecords.sort((left, right) =>
  String(left.mapping_id).localeCompare(String(right.mapping_id)),
);

const mutationRecords = addMutations(readJsonLines(mutationManifestPath));
writeJsonLines(sourceManifestPath, sourceRecords);
writeJsonLines(targetManifestPath, targetRecords);
writeJsonLines(mutationManifestPath, mutationRecords);

const profile = readJson(gateProfilePath);
profile.manifest_generator = GENERATOR;
profile.schema_verifier = SCHEMA_VERIFIER;
profile.candidate_scope_paths = [
  ...new Set([
    ...((profile.candidate_scope_paths as unknown[] | undefined) ?? []).map(String),
    "packages/runtime/runtime-event-spine",
  ]),
].sort();
profile.checker_source_paths = [
  ...new Set([
    ...((profile.checker_source_paths as unknown[] | undefined) ?? []).map(String),
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
  verification_contract_version: VERIFICATION_CONTRACT_VERSION,
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
    "V6 rejects source symbols without a material target effect instead of preserving generic domain templates.",
    "V6 records direct default-entry, immediate-callsite, target, state, behavior-test and mutation links.",
    "The pre-E01 runtime-event-spine checkpoint remains zero-credit but is explicitly included in candidate-scope auditing.",
  ],
});

process.stdout.write(
  `${JSON.stringify({
    ok: true,
    generator: "v6",
    implementation_candidate: implementationHead,
    accepted_sources: accepted.length,
    rejected_sources: sourceRecords.length - accepted.length,
    custody_mappings: targetRecords.length,
    mutations: mutationRecords.length,
  })}\n`,
);
