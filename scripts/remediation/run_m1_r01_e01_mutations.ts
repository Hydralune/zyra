#!/usr/bin/env bun

import { createHash } from "node:crypto";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";

type ManifestRow = {
  mutation_id: string;
  target_path: string;
  target_symbol: string;
  mutation_operator: string;
  expected_killer_test_ids: string[];
  compile_survives: boolean;
  frozen_patch_sha256: string;
};

type Edit = { search: string; replacement: string };
type MutationSpec = { edits: Edit[]; tests: string[] };
type CommandResult = {
  command: string[];
  exitCode: number;
  durationMs: number;
  stdout: string;
  stderr: string;
};

const zyra = resolve(import.meta.dir, "..", "..");
const workspace = resolve(zyra, "..");
const manifestPath = join(
  workspace,
  "docs",
  "remediations",
  "M1-R01-claude-source-custody",
  "manifests",
  "execution-01-mutation-manifest.jsonl",
);
const evidencePath = join(
  zyra,
  "docs",
  "reviews",
  "evidence",
  "M1-R01-v3",
  "execution-01",
  "mutation-results.json",
);
const buildOutput = join(zyra, ".tmp", "m1-r01-e01-mutant-build.js");
const testRoot = "packages/runtime/claude-runtime/test/e01";
const mutationContract = `${testRoot}/mutation-contract.behavior.test.ts`;
const kernelTests = [
  `${testRoot}/durable-session.behavior.test.ts`,
  `${testRoot}/coordinator-cutover.behavior.test.ts`,
  mutationContract,
];
const queryTests = [`${testRoot}/query-context.behavior.test.ts`, mutationContract];
const providerTests = [
  `${testRoot}/provider-policy.behavior.test.ts`,
  `${testRoot}/provider-request-response.behavior.test.ts`,
  mutationContract,
];
const toolTests = [`${testRoot}/tool-protocol.behavior.test.ts`, mutationContract];
const runtimeTests = ["packages/runtime/claude-runtime/test/runtime.test.ts"];

function spec(tests: string[], search: string, replacement: string): MutationSpec {
  return { tests, edits: [{ search, replacement }] };
}

const specs: Record<string, MutationSpec> = {
  "e01-mut-001-restore-before-bootstrap": {
    tests: kernelTests,
    edits: [
      {
        search: "  restore(snapshot: JournalSnapshot, nextRunId: string): void {\n",
        replacement: "  restore(snapshot: JournalSnapshot, nextRunId: string): void {\n    this.runId = nextRunId;\n",
      },
      {
        search: "    this.runId = nextRunId;\n    this.epochValue = snapshot.restartEpoch + 1;",
        replacement: "    this.epochValue = snapshot.restartEpoch + 1;",
      },
    ],
  },
  "e01-mut-002-revision-monotonic": spec(
    kernelTests,
    "    this.revisionValue += 1;\n    pending.phase = \"commit\";",
    "    pending.phase = \"commit\";",
  ),
  "e01-mut-003-transition-unique": spec(
    kernelTests,
    "    \"revision\" + String(nextRevision),\n    \"sequence\" + String(transitionSequence),\n    randomUUID(),",
    "    \"reused-transition\",",
  ),
  "e01-mut-004-payload-digest": spec(
    kernelTests,
    "    if (value.payloadDigest !== digest(value.payload)) throw new InvariantError(\"payload_digest\", \"payload digest mismatch\");",
    "    if (false && value.payloadDigest !== digest(value.payload)) throw new InvariantError(\"payload_digest\", \"payload digest mismatch\");",
  ),
  "e01-mut-005-pending-committed": spec(
    kernelTests,
    "    this.pendingById.delete(value.identity.transitionId);",
    "    void value.identity.transitionId;",
  ),
  "e01-mut-006-effect-fence": spec(
    kernelTests,
    "    if (previous?.terminal) return clone(previous);",
    "    if (false && previous?.terminal) return clone(previous);",
  ),
  "e01-mut-007-lost-ack": spec(
    kernelTests,
    "    receipt.phase = \"ack\";\n    receipt.status = \"acked\";",
    "    this.committedById.set(transitionId + \":ack\", clone(receipt));\n    receipt.phase = \"ack\";\n    receipt.status = \"acked\";",
  ),
  "e01-mut-008-corrupt-snapshot": spec(
    kernelTests,
    "    if (digest(unsigned) !== snapshot.checksum) {",
    "    if (false && digest(unsigned) !== snapshot.checksum) {",
  ),
  "e01-mut-009-session-scope": spec(
    kernelTests,
    "    if (snapshot.sessionId !== this.sessionId) {",
    "    if (false && snapshot.sessionId !== this.sessionId) {",
  ),
  "e01-mut-010-outbox-once": spec(
    kernelTests,
    "    if (!record.delivered) {",
    "    if (true) {",
  ),
  "e01-mut-011-turn-limit": spec(
    queryTests,
    "    if (this.budget.maximumTurns !== null && this.budget.consumedTurns >= this.budget.maximumTurns) {",
    "    if (false && this.budget.maximumTurns !== null && this.budget.consumedTurns >= this.budget.maximumTurns) {",
  ),
  "e01-mut-012-cancel": spec(
    queryTests,
    "    this.control.abortRequested = true;",
    "    this.control.abortRequested = false;",
  ),
  "e01-mut-013-empty-turn": spec(
    queryTests,
    "    turn.messageDigestAfter = required(input.messageDigest, \"message digest\");",
    "    turn.messageDigestAfter = input.messageDigest;",
  ),
  "e01-mut-014-query-revision": spec(
    queryTests,
    "    const from = this.status;\n    this.status = next;\n    this.revision += 1;",
    "    const from = this.status;\n    this.status = next;",
  ),
  "e01-mut-015-failure-route": spec(
    queryTests,
    "    if (terminal) this.fail(\"model_error\", turn.error);",
    "    if (false && terminal) this.fail(\"model_error\", turn.error);",
  ),
  "e01-mut-016-compact-threshold": spec(
    queryTests,
    "      shouldAutoCompact: estimatedTokens >= autoCompactThreshold,",
    "      shouldAutoCompact: estimatedTokens < autoCompactThreshold,",
  ),
  "e01-mut-017-compact-tool-pair": spec(
    queryTests,
    "export function adjustIndexToPreserveApiInvariants(\n  messages: readonly CompactMessage[],\n  proposedIndex: number,\n): number {\n  let index = Math.max(0, Math.min(messages.length, proposedIndex));",
    "export function adjustIndexToPreserveApiInvariants(\n  messages: readonly CompactMessage[],\n  proposedIndex: number,\n): number {\n  return Math.max(0, Math.min(messages.length, proposedIndex));\n  let index = Math.max(0, Math.min(messages.length, proposedIndex));",
  ),
  "e01-mut-018-compact-suffix": spec(
    queryTests,
    "      messages: [...plan.preserve.map((item) => structuredClone(item))],",
    "      messages: [],",
  ),
  "e01-mut-019-compact-restore": spec(
    queryTests,
    "    this.tokens.restore(snapshot.tokenRuntime);",
    "    void snapshot.tokenRuntime;",
  ),
  "e01-mut-020-compact-cleanup": spec(
    queryTests,
    "    this.runPostCompactCleanup(options.querySource);",
    "    void options.querySource;",
  ),
  "e01-mut-021-retry-delay": spec(
    providerTests,
    "      ? this.delay(context, error, nowMs)\n      : 0;",
    "      ? 0\n      : 0;",
  ),
  "e01-mut-022-retry-class": spec(
    providerTests,
    "    } else if (error.retryable && context.attempt <= context.maxRetries) {",
    "    } else if ((error.retryable || error.category === \"authentication\") && context.attempt <= context.maxRetries) {",
  ),
  "e01-mut-023-max-retries": spec(
    providerTests,
    "    } else if (error.retryable && context.attempt <= context.maxRetries) {",
    "    } else if (error.retryable) {",
  ),
  "e01-mut-024-fallback": spec(
    providerTests,
    "    } else if (context.consecutiveCapacityErrors >= MAX_CAPACITY_RETRIES && context.fallbackDepth < fallbackModels.length) {",
    "    } else if (context.fallbackDepth < fallbackModels.length) {",
  ),
  "e01-mut-025-stream-final": spec(
    providerTests,
    "  if (!state.closed) throw new ProviderProtocolError(\"stream_end\", \"provider stream ended before message_stop\");",
    "  if (false && !state.closed) throw new ProviderProtocolError(\"stream_end\", \"provider stream ended before message_stop\");",
  ),
  "e01-mut-026-usage": spec(
    providerTests,
    "    this.mergeSession(sample);\n    this.observe(\"provider.request.duration_ms\", sample.durationMs);",
    "    this.mergeSession(sample);\n    this.mergeSession(sample);\n    this.observe(\"provider.request.duration_ms\", sample.durationMs);",
  ),
  "e01-mut-027-cache-lineage": spec(
    providerTests,
    "    this.prompts.set(key, snapshot);",
    "    this.prompts.set(key, previous ?? snapshot);",
  ),
  "e01-mut-028-tool-schema": spec(
    toolTests,
    "    return validateSchema(spec.input_schema, step.arguments, \"$\");",
    "    return [];",
  ),
  "e01-mut-029-write-serialization": spec(
    toolTests,
    "      && registry.readOnly(steps[cursor].tool_name)\n      && selected.length < maxReadOnlyConcurrency",
    "      && (registry.readOnly(steps[cursor].tool_name) || !registry.readOnly(steps[cursor].tool_name))\n      && selected.length < maxReadOnlyConcurrency",
  ),
  "e01-mut-030-result-budget": spec(
    toolTests,
    "  if (originalChars <= maxChars) {",
    "  if (true || originalChars <= maxChars) {",
  ),
  "e01-mut-031-recovery-planner-custody": spec(
    runtimeTests,
    "        (observation) => e01.decideProviderRecovery(observation),\n        e01.journal.restartEpoch,",
    "        undefined,\n        e01.journal.restartEpoch,",
  ),
};

function hash(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

function materialize(value: string, newline: string): string {
  return value.replaceAll("\n", newline);
}

function occurrenceCount(source: string, needle: string): number {
  if (!needle) return 0;
  let count = 0;
  let offset = 0;
  while ((offset = source.indexOf(needle, offset)) !== -1) {
    count += 1;
    offset += needle.length;
  }
  return count;
}

async function run(command: string[], cwd = zyra): Promise<CommandResult> {
  const started = performance.now();
  const child = Bun.spawn({
    cmd: command,
    cwd,
    env: { ...process.env, NO_COLOR: "1", FORCE_COLOR: "0" },
    stdin: "ignore",
    stdout: "pipe",
    stderr: "pipe",
  });
  const stdoutPromise = new Response(child.stdout).text();
  const stderrPromise = new Response(child.stderr).text();
  const exitCode = await child.exited;
  return {
    command,
    exitCode,
    durationMs: Math.round(performance.now() - started),
    stdout: await stdoutPromise,
    stderr: await stderrPromise,
  };
}

function excerpt(value: string): string {
  const normalized = value.replace(/\x1b\[[0-9;]*m/g, "").trim();
  return normalized.length <= 4_000 ? normalized : normalized.slice(-4_000);
}

async function main(): Promise<void> {
  const rows = (await readFile(manifestPath, "utf8"))
    .split(/\r?\n/)
    .filter(Boolean)
    .map((line) => JSON.parse(line) as ManifestRow);
  if (rows.length < 30) throw new Error(`expected at least 30 frozen mutations, received ${rows.length}`);
  const unknown = rows.filter((row) => !specs[row.mutation_id]).map((row) => row.mutation_id);
  const extra = Object.keys(specs).filter((id) => !rows.some((row) => row.mutation_id === id));
  if (unknown.length || extra.length) throw new Error(`mutation spec mismatch unknown=${unknown.join(",")} extra=${extra.join(",")}`);
  await mkdir(dirname(evidencePath), { recursive: true });
  await mkdir(dirname(buildOutput), { recursive: true });
  const baseline = await run([process.execPath, "test", testRoot]);
  if (baseline.exitCode !== 0) {
    throw new Error(`baseline E01 behavior tests failed before mutation run\n${excerpt(baseline.stdout + "\n" + baseline.stderr)}`);
  }
  const results: Record<string, unknown>[] = [];
  for (const row of rows) {
    const mutation = specs[row.mutation_id];
    const target = join(zyra, row.target_path);
    const original = await readFile(target, "utf8");
    const originalSha256 = hash(original);
    const newline = original.includes("\r\n") ? "\r\n" : "\n";
    let mutated = original;
    const actualEdits: Array<Record<string, unknown>> = [];
    let compile: CommandResult | null = null;
    let test: CommandResult | null = null;
    let failure: string | null = null;
    try {
      for (const edit of mutation.edits) {
        const search = materialize(edit.search, newline);
        const replacement = materialize(edit.replacement, newline);
        const matches = occurrenceCount(mutated, search);
        if (matches !== 1) throw new Error(`patch must match exactly once; matched ${matches}`);
        mutated = mutated.replace(search, replacement);
        actualEdits.push({
          search_sha256: hash(search),
          replacement_sha256: hash(replacement),
          removed_chars: search.length,
          added_chars: replacement.length,
        });
      }
      if (mutated === original) throw new Error("mutation produced no source change");
      await writeFile(target, mutated, "utf8");
      compile = await run([process.execPath, "build", row.target_path, "--target", "bun", "--outfile", buildOutput]);
      if (compile.exitCode === 0) test = await run([process.execPath, "test", ...mutation.tests]);
    } catch (error) {
      failure = error instanceof Error ? error.message : String(error);
    } finally {
      await writeFile(target, original, "utf8");
      await rm(buildOutput, { force: true });
    }
    const restored = await readFile(target, "utf8");
    const restoredSha256 = hash(restored);
    if (restoredSha256 !== originalSha256) throw new Error(`source restoration failed for ${row.mutation_id}`);
    const compileSurvived = compile?.exitCode === 0;
    const killed = failure === null && compileSurvived && test !== null && test.exitCode !== 0;
    results.push({
      mutation_id: row.mutation_id,
      target_path: row.target_path,
      target_symbol: row.target_symbol,
      mutation_operator: row.mutation_operator,
      expected_killer_test_ids: row.expected_killer_test_ids,
      frozen_patch_sha256: row.frozen_patch_sha256,
      original_sha256: originalSha256,
      mutant_sha256: hash(mutated),
      restored_sha256: restoredSha256,
      actual_patch_sha256: hash(JSON.stringify(actualEdits)),
      edits: actualEdits,
      compile_survives_required: row.compile_survives,
      compile_survived: compileSurvived,
      compile_command: compile?.command ?? null,
      compile_exit_code: compile?.exitCode ?? null,
      compile_duration_ms: compile?.durationMs ?? null,
      test_command: test?.command ?? null,
      test_exit_code: test?.exitCode ?? null,
      test_duration_ms: test?.durationMs ?? null,
      output_sha256: hash((test?.stdout ?? "") + "\n" + (test?.stderr ?? "")),
      failure_excerpt: excerpt((test?.stdout ?? "") + "\n" + (test?.stderr ?? "")),
      runner_error: failure,
      killed,
    });
    process.stdout.write(`${row.mutation_id}: ${killed ? "KILLED" : "SURVIVED_OR_INVALID"}\n`);
  }
  const killed = results.filter((item) => item.killed === true).length;
  const invalid = results.filter((item) => item.compile_survived !== true || item.runner_error !== null).length;
  const output = {
    schema_version: "3.0",
    execution_id: "E01",
    generated_at_utc: new Date().toISOString(),
    runner: "scripts/remediation/run_m1_r01_e01_mutations.ts",
    baseline: {
      command: baseline.command,
      exit_code: baseline.exitCode,
      duration_ms: baseline.durationMs,
      output_sha256: hash(baseline.stdout + "\n" + baseline.stderr),
    },
    summary: {
      declared: rows.length,
      applied: results.filter((item) => item.mutant_sha256 !== item.original_sha256).length,
      compile_survived: results.filter((item) => item.compile_survived === true).length,
      killed,
      survived: rows.length - killed - invalid,
      invalid,
      kill_rate: killed / rows.length,
      restored_sha_match: results.every((item) => item.original_sha256 === item.restored_sha256),
    },
    results,
  };
  await writeFile(evidencePath, JSON.stringify(output, null, 2) + "\n", "utf8");
  if (killed !== rows.length || invalid !== 0) {
    process.exitCode = 1;
    process.stderr.write(`mutation gate failed: killed=${killed}/${rows.length}, invalid=${invalid}\n`);
  }
}

await main();
