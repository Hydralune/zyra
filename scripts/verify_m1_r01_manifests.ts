#!/usr/bin/env bun

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { basename, dirname, join, relative, resolve } from "node:path";
import * as ts from "typescript";

type Obj = Record<string, any>;
type Unit = { hash: string; lines: number; path: string; symbol: string };

const VERIFIED = "c34535a783e88f9481387ced89cba4fbc333dc74";
const zyra = resolve(import.meta.dir, "..");
const workspace = resolve(zyra, "..");
const manifestRoot = join(workspace, "docs", "remediations", "M1-R01-claude-source-custody", "manifests");
const evidenceRoot = join(zyra, "docs", "reviews", "evidence", "M1-R01-v3", "execution-01");
const productionRoots = [
  "apps/code-worker/src",
  "packages/runtime/claude-runtime/src",
  "packages/runtime/runtime-event-spine/src",
];
const testRoot = "packages/runtime/claude-runtime/test/e01";

function hash(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

function jsonl(path: string): Obj[] {
  return readFileSync(path, "utf8").split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
}

function writeJson(path: string, value: unknown): void {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(value, null, 2) + "\n", "utf8");
}

function writeJsonl(path: string, values: unknown[]): void {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, values.map((value) => JSON.stringify(value)).join("\n") + "\n", "utf8");
}

function walk(relativeRoot: string): string[] {
  const absolute = join(zyra, relativeRoot);
  if (!existsSync(absolute)) return [];
  const output: string[] = [];
  for (const entry of readdirSync(absolute, { withFileTypes: true })) {
    const relativePath = join(relativeRoot, entry.name).replaceAll("\\", "/");
    if (entry.isDirectory()) output.push(...walk(relativePath));
    else output.push(relativePath);
  }
  return output;
}

function physicalLines(text: string): number {
  if (!text) return 0;
  const lines = text.replaceAll("\r", "").split("\n");
  if (lines.at(-1) === "") lines.pop();
  return lines.length;
}

function normalized(value: string): string {
  return value
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/(^|\n)\s*\/\/[^\n]*/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

function astUnits(path: string, text: string): Unit[] {
  const source = ts.createSourceFile(path, text, ts.ScriptTarget.ESNext, true, ts.ScriptKind.TS);
  const output: Unit[] = [];
  const add = (node: ts.Node, symbol: string): void => {
    const start = node.getStart(source);
    const end = node.end;
    const raw = text.slice(start, end);
    const canonical = normalized(raw);
    if (!canonical) return;
    const startLine = source.getLineAndCharacterOfPosition(start).line;
    const endLine = source.getLineAndCharacterOfPosition(Math.max(start, end - 1)).line;
    output.push({ hash: hash(canonical), lines: endLine - startLine + 1, path, symbol });
  };
  source.statements.forEach((statement, index) => {
    if (ts.isImportDeclaration(statement) || ts.isExportDeclaration(statement)) return;
    if (ts.isClassDeclaration(statement)) {
      const className = statement.name?.text ?? `anonymous_class_${index}`;
      if (statement.members.length === 0) add(statement, className);
      else statement.members.forEach((member, memberIndex) => {
        const name = member.name && "getText" in member.name ? member.name.getText(source) : `member_${memberIndex}`;
        add(member, `${className}.${name}`);
      });
      return;
    }
    const named = (statement as ts.NamedDeclaration).name;
    add(statement, named && "getText" in named ? named.getText(source) : `statement_${index}`);
  });
  return output;
}

function git(args: string[], encoding: "utf8" | "buffer" = "utf8"): string | Buffer {
  return execFileSync("git", args, {
    cwd: zyra,
    encoding: encoding === "utf8" ? "utf8" : "buffer",
    stdio: ["ignore", "pipe", "ignore"],
  }) as string | Buffer;
}

function baselineText(path: string): string | null {
  try {
    return (git(["show", `${VERIFIED}:${path}`], "buffer") as Buffer).toString("utf8");
  } catch {
    return null;
  }
}

function deduplicatedLines(units: Unit[], excludedHashes = new Set<string>()): { lines: number; units: Unit[] } {
  const seen = new Set<string>();
  const accepted: Unit[] = [];
  for (const unit of units) {
    if (excludedHashes.has(unit.hash) || seen.has(unit.hash)) continue;
    seen.add(unit.hash);
    accepted.push(unit);
  }
  return { lines: accepted.reduce((sum, unit) => sum + unit.lines, 0), units: accepted };
}

function testNames(path: string): string[] {
  const text = readFileSync(join(zyra, path), "utf8");
  const names: string[] = [];
  for (const match of text.matchAll(/(?:test|it)\s*\(\s*["'`]([^"'`]+)["'`]/g)) names.push(match[1]);
  return names;
}

function testsForDomain(domain: string): string[] {
  if (domain.startsWith("provider-")) {
    return [
      `${testRoot}/provider-policy.behavior.test.ts`,
      `${testRoot}/provider-request-response.behavior.test.ts`,
    ];
  }
  if (domain === "tool" || domain === "result-budget") return [`${testRoot}/tool-protocol.behavior.test.ts`];
  return [`${testRoot}/query-context.behavior.test.ts`];
}

function symbolExists(text: string, symbol: string): boolean {
  const parts = symbol.split(".");
  if (parts.length === 2) {
    return text.includes(`class ${parts[0]}`) && new RegExp(`\\b${parts[1]}\\s*\\(`).test(text);
  }
  return new RegExp(`(?:function|class)\\s+${parts[0]}\\b`).test(text);
}

function callsiteInvokes(text: string, targetSymbol: string, callsiteSymbol: string, samePath: boolean): boolean {
  const targetMethod = targetSymbol.split(".").at(-1) ?? targetSymbol;
  const callsiteMethod = callsiteSymbol.split(".").at(-1) ?? callsiteSymbol;
  const callsitePresent = new RegExp(`\\b${callsiteMethod}\\s*\\(`).test(text);
  if (!callsitePresent) return false;
  if (samePath && targetSymbol === callsiteSymbol) return true;
  return new RegExp(`(?:\\.|\\b)${targetMethod}\\s*\\(`).test(text);
}

function mutationPathsForDomain(domain: string): string[] {
  if (domain === "query") return ["/query/lifecycle-runtime.ts"];
  if (domain === "provider-model") return ["/provider/model-runtime.ts"];
  if (domain === "provider-recovery") return ["/provider/recovery-runtime.ts"];
  if (domain === "provider-telemetry") return ["/provider/telemetry-runtime.ts"];
  if (domain === "compact") return ["/compact/context-runtime.ts"];
  return [];
}

function readEvidence(name: string): Obj | null {
  const path = join(evidenceRoot, `${name}-result.json`);
  return existsSync(path) ? JSON.parse(readFileSync(path, "utf8")) : null;
}

function main(): void {
  const enforce = process.argv.includes("--enforce-gates");
  const failures: string[] = [];
  const source = jsonl(join(manifestRoot, "execution-01-source-manifest.jsonl"));
  const python = jsonl(join(manifestRoot, "execution-01-python-owner-baseline.jsonl"));
  const targets = jsonl(join(manifestRoot, "execution-01-target-custody-map.jsonl"));
  const mutations = jsonl(join(manifestRoot, "execution-01-mutation-manifest.jsonl"));
  const profile = JSON.parse(readFileSync(join(manifestRoot, "execution-01-gate-profile.json"), "utf8"));
  const thresholds = profile.thresholds as Obj;
  if (profile.verified_zyra_head !== VERIFIED) failures.push("verified baseline mismatch");
  if (Bun.version !== profile.toolchain.bun_version) failures.push(`Bun ${Bun.version} does not match ${profile.toolchain.bun_version}`);
  const packageJson = JSON.parse(readFileSync(join(zyra, "package.json"), "utf8"));
  if (packageJson.devDependencies.typescript !== profile.toolchain.typescript_version) failures.push("TypeScript pin mismatch");

  const productionFiles = productionRoots.flatMap(walk).filter((path) => path.endsWith(".ts") && !/\.(?:test|spec)\.ts$/.test(path));
  const testFiles = walk(testRoot).filter((path) => path.endsWith(".behavior.test.ts"));
  const productionText = new Map(productionFiles.map((path) => [path, readFileSync(join(zyra, path), "utf8")]));
  const finalPhysical = [...productionText.values()].reduce((sum, text) => sum + physicalLines(text), 0);
  const baselineHashes = new Set<string>();
  for (const path of productionFiles) {
    const previous = baselineText(path);
    if (previous !== null) for (const unit of astUnits(path, previous)) baselineHashes.add(unit.hash);
  }
  const currentUnits = productionFiles.flatMap((path) => astUnits(path, productionText.get(path) ?? ""));
  const changed = deduplicatedLines(currentUnits, baselineHashes);
  const testUnits = testFiles.flatMap((path) => astUnits(path, readFileSync(join(zyra, path), "utf8")));
  const effectiveTests = deduplicatedLines(testUnits);
  const adapterFiles = productionFiles.filter((path) => /(?:adapter|bridge|gateway)/i.test(basename(path)));
  const adapterLines = adapterFiles.reduce((sum, path) => sum + physicalLines(productionText.get(path) ?? ""), 0);
  const adapterRatio = finalPhysical === 0 ? 1 : adapterLines / finalPhysical;

  if (changed.lines < thresholds.effective_changed_typescript_sloc) failures.push(`effective changed TypeScript SLOC ${changed.lines} < ${thresholds.effective_changed_typescript_sloc}`);
  if (finalPhysical < thresholds.final_non_test_typescript_sloc) failures.push(`final non-test TypeScript SLOC ${finalPhysical} < ${thresholds.final_non_test_typescript_sloc}`);
  if (effectiveTests.lines < thresholds.effective_behavior_test_sloc) failures.push(`effective behavior test SLOC ${effectiveTests.lines} < ${thresholds.effective_behavior_test_sloc}`);
  if (adapterRatio > thresholds.adapter_ratio_maximum) failures.push(`adapter ratio ${adapterRatio} > ${thresholds.adapter_ratio_maximum}`);

  const sourcePhysical = source.filter((item) => item.accepted).reduce((sum, item) => sum + item.end_line - item.start_line + 1, 0);
  if (sourcePhysical < thresholds.accepted_source_executable_sloc) failures.push("accepted source range threshold failed");
  if (mutations.length < thresholds.mutation_points_minimum) failures.push("mutation manifest threshold failed");
  const undeletedPython = [...new Set(python.map((item) => item.python_path))].filter((path) => existsSync(join(zyra, path)));
  if (undeletedPython.length) failures.push(`Python canonical owner paths still exist: ${undeletedPython.join(",")}`);

  const coordinatorPath = "packages/runtime/claude-runtime/src/e01/coordinator.ts";
  const coordinatorText = readFileSync(join(zyra, coordinatorPath), "utf8");
  const rootCallsiteText = readFileSync(join(zyra, "packages/runtime/claude-runtime/src/query-engine.ts"), "utf8");
  if (!rootCallsiteText.includes("E01RuntimeCoordinator") || !rootCallsiteText.includes("await e01.bootstrap()")) failures.push("default E01 callsite is not wired");
  const sourceById = new Map(source.map((item) => [item.mapping_id, item]));
  const chains = targets.map((target) => {
    const sourceItem = sourceById.get(target.mapping_id);
    const targetAbsolute = join(zyra, target.target_path);
    const targetText = existsSync(targetAbsolute) ? readFileSync(targetAbsolute, "utf8") : "";
    const targetSymbol = String(target.target_symbol);
    const targetSymbolExists = symbolExists(targetText, targetSymbol);
    const callsitePath = String(target.default_callsite_path);
    const callsiteAbsolute = join(zyra, callsitePath);
    const callsiteText = existsSync(callsiteAbsolute) ? readFileSync(callsiteAbsolute, "utf8") : "";
    const invokesTarget = callsiteInvokes(callsiteText, targetSymbol, String(target.default_callsite_symbol), callsitePath === target.target_path);
    const domain = String(sourceItem?.semantic_domain ?? "unknown");
    const behaviorFiles = testsForDomain(domain);
    const behaviorTests = behaviorFiles.flatMap((path) => testNames(path).map((name) => ({ path, test_name: name })));
    const mutationSuffixes = mutationPathsForDomain(domain);
    const domainMutationIds = mutations.filter((item) => mutationSuffixes.some((suffix) => item.target_path.endsWith(suffix))).map((item) => item.mutation_id);
    const ownerProperty = domain === "compact" ? "compact"
      : domain === "context-token" ? "tokens"
      : domain === "provider-model" ? "provider"
      : domain === "provider-recovery" ? "recovery"
      : domain === "provider-telemetry" ? "telemetry"
      : "query";
    const stateEffectReachable = coordinatorText.includes(`this.${ownerProperty}.snapshot()`);
    const complete = Boolean(sourceItem) && targetSymbolExists && invokesTarget && stateEffectReachable && behaviorTests.length > 0;
    if (!complete) failures.push(`incomplete five-hop chain ${target.mapping_id}`);
    return {
      mapping_id: target.mapping_id,
      complete,
      source: sourceItem ? {
        repo: sourceItem.source_repo,
        snapshot: sourceItem.source_snapshot,
        path: sourceItem.source_path,
        symbol: sourceItem.source_symbol,
        range: [sourceItem.start_line, sourceItem.end_line],
        sha256: sourceItem.source_sha256,
      } : null,
      target: {
        path: target.target_path,
        symbol: target.target_symbol,
        sha256: targetText ? hash(targetText) : null,
        symbol_exists: targetSymbolExists,
      },
      default_callsite: {
        path: callsitePath,
        symbol: target.default_callsite_symbol,
        coordinator_path: coordinatorPath,
        coordinator_symbol: "E01RuntimeCoordinator",
        invokes_target: invokesTarget,
      },
      state_effect: {
        store: target.state_store,
        kind: target.state_effect_kind,
        coordinator_snapshot_property: ownerProperty,
        reachable: stateEffectReachable,
      },
      behavior_tests: behaviorTests,
      mutation_ids: domainMutationIds,
    };
  });

  const mutationEvidencePath = join(evidenceRoot, "mutation-results.json");
  const mutationEvidence = existsSync(mutationEvidencePath) ? JSON.parse(readFileSync(mutationEvidencePath, "utf8")) : null;
  if (enforce && (!mutationEvidence || mutationEvidence.summary.killed !== mutations.length || mutationEvidence.summary.invalid !== 0)) {
    failures.push("30/30 executable mutation evidence missing or failed");
  }
  const requiredEvidence = ["runtime-origin", "write-path", "same-session-resume", "lost-ack", "disable", "dependencies", "toolchain"];
  const evidenceStatus = Object.fromEntries(requiredEvidence.map((name) => [name, readEvidence(name)]));
  if (enforce) for (const [name, value] of Object.entries(evidenceStatus)) if (!value?.ok) failures.push(`required evidence missing or failed: ${name}`);

  const locReport = {
    schema_version: "3.0",
    execution_id: "E01",
    verified_baseline: VERIFIED,
    production_roots: productionRoots,
    production_file_count: productionFiles.length,
    final_non_test_typescript_physical_sloc: finalPhysical,
    baseline_ast_unit_hash_count: baselineHashes.size,
    current_ast_unit_count: currentUnits.length,
    effective_changed_ast_unit_count: changed.units.length,
    effective_changed_typescript_sloc: changed.lines,
    behavior_test_files: testFiles,
    behavior_test_ast_unit_count: testUnits.length,
    effective_behavior_test_ast_unit_count: effectiveTests.units.length,
    effective_behavior_test_sloc: effectiveTests.lines,
    adapter_files: adapterFiles,
    adapter_sloc: adapterLines,
    adapter_ratio: adapterRatio,
    algorithm: "TypeScript AST declaration/member units; whitespace/comment normalized exact-clone hashes; baseline and repeated-unit hashes excluded",
  };
  writeJson(join(evidenceRoot, "effective-loc-report.json"), locReport);
  writeJsonl(join(evidenceRoot, "source-to-target-five-hop.jsonl"), chains);
  const result = {
    schema_version: "3.0",
    execution_id: "E01",
    generated_at_utc: new Date().toISOString(),
    verified_baseline: VERIFIED,
    enforce_gates: enforce,
    pass: failures.length === 0,
    failures,
    thresholds,
    actual: {
      accepted_source_physical_sloc: sourcePhysical,
      effective_changed_typescript_sloc: changed.lines,
      final_non_test_typescript_sloc: finalPhysical,
      effective_behavior_test_sloc: effectiveTests.lines,
      adapter_ratio: adapterRatio,
      source_to_target_chain_count: chains.length,
      complete_source_to_target_chain_count: chains.filter((item) => item.complete).length,
      mutation_declared: mutations.length,
      mutation_killed: mutationEvidence?.summary?.killed ?? 0,
      python_owner_paths_remaining: undeletedPython,
    },
    evidence: Object.fromEntries(Object.entries(evidenceStatus).map(([name, value]) => [name, value ? { ok: value.ok, probe_id: value.probe_id } : null])),
  };
  writeJson(join(evidenceRoot, "candidate-gate-result.json"), result);
  process.stdout.write(JSON.stringify(result, null, 2) + "\n");
  if (failures.length) process.exitCode = 1;
}

if (process.argv[2] !== "verify") throw new Error("usage: verify_m1_r01_manifests.ts verify [--enforce-gates]");
main();
