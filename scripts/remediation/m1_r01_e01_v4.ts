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
const IMPLEMENTATION_DIFF_BASELINE = "0cd21bff5171607680c11f2652c46e55a8a5a983";
const DEFAULT_ENTRY_ID = "e01.default-code-worker";
const GENERATOR = "scripts/remediation/m1_r01_e01_v4.ts";
const SCHEMA_VERIFIER = "scripts/remediation/verify_m1_r01_e01_v4.ts";

type JsonRecord = Record<string, unknown>;

const sha256 = (value: Uint8Array | string): string =>
  createHash("sha256").update(value).digest("hex");

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

const historyAccepted = new Set(["getTimestampedHistory", "getHistory", "addToHistory"]);
const sessionStateAccepted = new Set(["hasPendingAction", "getSessionState"]);

const rejectionReason = (record: JsonRecord): string | null => {
  const path = String(record.source_path ?? "").replaceAll("\\", "/");
  const name = symbolName(record);
  if (path.endsWith("context/tokenBudget.ts") && name === "createBudgetTracker") {
    return "factory shape is not migrated; Zyra owns budget construction inside ContextTokenRuntime";
  }
  if (path.endsWith("tools/toolResultStorage.ts") && !toolResultAccepted.has(name)) {
    return "filesystem persistence and process-global replacement state are deliberately not migrated";
  }
  if (path.endsWith("session/sessionRestore.ts")) {
    return "Claude process-global restore choreography is rejected in favor of Zyra durable session restore";
  }
  if (path.endsWith("session/history.ts") && !historyAccepted.has(name)) {
    return "terminal formatting, global flush promises and image reference rewriting are outside E01 custody";
  }
  if (path.endsWith("session/sessionState.ts") && !sessionStateAccepted.has(name)) {
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
    ],
    success_test_ids: ["e01.mutation.observation-budget-enforces-cross-result-limit"],
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
  if (path.endsWith("session/history.ts")) {
    targetPath = "packages/runtime/claude-runtime/src/session/history-runtime.ts";
    targetSymbol = name === "addToHistory" ? "SessionHistoryRuntime.append" : "SessionHistoryRuntime.transcript";
    callsiteSymbol = "E01RuntimeCoordinator.appendHistory";
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
    canonical_owner_id: path.endsWith("session/history.ts") ? "e01.session-history" : "e01.durable-session",
    state_store: path.endsWith("session/history.ts")
      ? "E01RuntimeSnapshot.history"
      : "E01RuntimeSnapshot.session",
    state_snapshot_property: path.endsWith("session/history.ts") ? "history" : "session",
    state_effect_kind: path.endsWith("session/history.ts") ? "history-mutation" : "session-projection",
    state_observation: path.endsWith("session/history.ts") ? "history.entries" : "session.phase",
    state_effect_assertion: `assert.${String(source.mapping_id)}.session-state`,
    adaptation:
      "Claude history/session observation is adapted to Zyra durable session and append-only history owners; global listeners and flush state are excluded.",
    source_behavior_claim: `${name} reads or appends session history/state used by a resumed query`,
    target_behavior_claim: `${targetSymbol} changes or exposes the canonical session snapshot restored by the coordinator`,
    semantic_equivalence: "Both sides preserve query-visible session continuity; Zyra supplies explicit snapshot custody and checksums.",
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
      frozen_patch_sha256: sha256("round limit => Number.MAX_SAFE_INTEGER"),
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
      frozen_patch_sha256: sha256("if (expectedChecksum !== snapshot.checksum) => if (false)"),
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
      .replaceAll("verify_m1_r01_e01_v3.ts", "verify_m1_r01_e01_v4.ts")
      .replaceAll("run_m1_r01_e01_mutations.ts", "run_m1_r01_e01_mutations_v4.ts");
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
  }
  sourceById.set(String(record.mapping_id), record);
}
const orderedSources = [...sourceById.values()].sort((left, right) =>
  String(left.mapping_id).localeCompare(String(right.mapping_id)),
);

const targetRecords = readJsonLines(targetManifestPath)
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
    if (sourcePath.endsWith("tools/toolResultStorage.ts")) routeToolResult(target, source);
    else if (sourcePath.endsWith("session/history.ts") || sourcePath.endsWith("session/sessionState.ts")) {
      routeSession(target, source);
    } else if (sourcePath.endsWith("context/tokenBudget.ts")) routeContextBudget(target, source);
    const targetPath = String(target.target_path);
    target.target_sha256 = sha256(gitBytes(repoRoot, ["show", `${implementationHead}:${targetPath}`]));
    return target;
  })
  .sort((left, right) => String(left.mapping_id).localeCompare(String(right.mapping_id)));

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
    "scripts/remediation/run_m1_r01_e01_mutations_v4.ts",
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
    "The implementation diff starts after the pre-E01 0cd21b checkpoint; that checkpoint receives zero E01 line credit.",
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
