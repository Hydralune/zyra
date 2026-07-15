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

const targets: Record<string, Obj> = {
  query: {
    path: "packages/runtime/claude-runtime/src/query-engine.ts",
    symbol: "ClaudeRuntimeCore.run",
    defaultCallsitePath: "packages/runtime/claude-runtime/src/query-engine.ts",
    defaultCallsiteSymbol: "ClaudeRuntimeCore.run",
    store: "E01DurableRuntimeState.query",
    effect: "query-transition",
    tests: ["e01.query.default-loop", "e01.query.failure-revise"],
  },
  "provider-model": {
    path: "packages/runtime/claude-runtime/src/model-stream.ts",
    symbol: "resolveModelTurns",
    defaultCallsitePath: "packages/runtime/claude-runtime/src/query-engine.ts",
    defaultCallsiteSymbol: "ClaudeRuntimeCore.run",
    store: "E01DurableRuntimeState.provider",
    effect: "provider-request-stream",
    tests: ["e01.provider.stream", "e01.provider.transport-failure"],
  },
  "provider-recovery": {
    path: "packages/runtime/claude-runtime/src/provider/recovery-runtime.ts",
    symbol: "ProviderRecoveryRuntime.plan",
    defaultCallsitePath: "packages/runtime/claude-runtime/src/e01/coordinator.ts",
    defaultCallsiteSymbol: "E01RuntimeCoordinator.recordProvider",
    store: "E01DurableRuntimeState.recovery",
    effect: "retry-replan",
    tests: ["e01.provider.retry", "e01.provider.non-retryable"],
  },
  "provider-telemetry": {
    path: "packages/runtime/claude-runtime/src/provider/telemetry-runtime.ts",
    symbol: "ProviderTelemetryRuntime.recordUsage",
    defaultCallsitePath: "packages/runtime/claude-runtime/src/e01/coordinator.ts",
    defaultCallsiteSymbol: "E01RuntimeCoordinator.recordProvider",
    store: "E01DurableRuntimeState.usage",
    effect: "usage-cache-causality",
    tests: ["e01.provider.usage", "e01.provider.cache-break"],
  },
  compact: {
    path: "packages/runtime/claude-runtime/src/compact/context-runtime.ts",
    symbol: "ContextCompactionRuntime.compactConversation",
    defaultCallsitePath: "packages/runtime/claude-runtime/src/query-engine.ts",
    defaultCallsiteSymbol: "ClaudeRuntimeCore.run",
    store: "E01DurableRuntimeState.compact",
    effect: "context-compact-restore",
    tests: ["e01.compact.default-path", "e01.compact.restore"],
  },
  "context-token": {
    path: "packages/runtime/claude-runtime/src/context/token-runtime.ts",
    symbol: "ContextTokenRuntime.estimate",
    defaultCallsitePath: "packages/runtime/claude-runtime/src/e01/coordinator.ts",
    defaultCallsiteSymbol: "E01RuntimeCoordinator.decideContext",
    store: "E01DurableRuntimeState.budget",
    effect: "token-budget-decision",
    tests: ["e01.context.token-budget", "e01.context.diminishing"],
  },
};

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

function methodName(record: Obj): string {
  const parts = String(record.source_symbol).split("::");
  const stem = basename(parts[0], ".ts").replace(/[^A-Za-z0-9]+/g, "_");
  return (stem + "_module").replace(/[^A-Za-z0-9_]+/g, "_");
}

function targetManifest(sources: Obj[]): Obj[] {
  return sources.map((source) => {
    const profile = { ...targets[source.semantic_domain] };
    if (source.source_path.endsWith("services/api/withRetry.ts")) {
      profile.path = "packages/runtime/claude-runtime/src/model-stream.ts";
      profile.symbol = "resolveModelTurns";
      profile.defaultCallsitePath = "packages/runtime/claude-runtime/src/query-engine.ts";
      profile.defaultCallsiteSymbol = "ClaudeRuntimeCore.run";
    }
    if (source.source_path.endsWith("services/api/errors.ts")) profile.symbol = "ProviderRecoveryRuntime.plan";
    if (source.source_path.endsWith("cost-tracker.ts")) profile.symbol = "ProviderTelemetryRuntime.recordUsage";
    if (source.source_path.endsWith("promptCacheBreakDetection.ts")) profile.symbol = "ProviderTelemetryRuntime.recordPromptState";
    if (source.source_path.endsWith("services/api/logging.ts")) profile.symbol = "ProviderTelemetryRuntime.logging_module";
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
      default_callsite_path: profile.defaultCallsitePath,
      default_callsite_symbol: profile.defaultCallsiteSymbol,
      state_store: profile.store,
      state_effect_kind: profile.effect,
      state_effect_assertion: "assert." + source.mapping_id + "." + profile.effect,
      success_test_ids: [profile.tests[0]],
      failure_test_ids: [profile.tests[1]],
      disable_test_ids: ["e01.owner.disable"],
      mutation_ids: [],
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
      expected_killer_test_ids: ["e01.mutation." + name],
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
