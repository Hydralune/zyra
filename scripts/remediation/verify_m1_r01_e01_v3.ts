#!/usr/bin/env bun

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

type Obj = Record<string, any>;
type Finding = { code: string; location: string; message: string };

const VERIFIED = "c34535a783e88f9481387ced89cba4fbc333dc74";
const script = fileURLToPath(import.meta.url);
const zyra = resolve(dirname(script), "..", "..");
const workspace = resolve(zyra, "..");
const sourceRoot = join(workspace, "claude-code-best");
const root = join(workspace, "docs", "remediations", "M1-R01-claude-source-custody", "manifests");
const requireTargets = process.argv.includes("--require-targets");

function hash(value: string | Uint8Array): string {
  return createHash("sha256").update(value).digest("hex");
}

function lines(path: string): Obj[] {
  return readFileSync(path, "utf8").split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
}

function gitBytes(cwd: string, args: string[]): Buffer {
  return execFileSync("git", args, { cwd, encoding: "buffer" }) as Buffer;
}

function need(record: Obj, fields: string[], at: string, findings: Finding[]): void {
  for (const field of fields) {
    if (!(field in record)) findings.push({ code: "field_missing", location: at, message: field });
  }
}

function overlaps(records: Obj[], pathKey: string, findings: Finding[]): void {
  const groups = new Map<string, Obj[]>();
  for (const record of records) groups.set(record[pathKey], (groups.get(record[pathKey]) ?? []).concat(record));
  for (const [path, values] of groups) {
    values.sort((a, b) => a.start_line - b.start_line);
    let end = 0;
    for (const value of values) {
      if (value.start_line <= end) findings.push({ code: "range_overlap", location: path + ":" + value.start_line, message: "overlap" });
      end = Math.max(end, value.end_line);
    }
  }
}

function main(): number {
  const findings: Finding[] = [];
  const paths = {
    source: join(root, "execution-01-source-manifest.jsonl"),
    python: join(root, "execution-01-python-owner-baseline.jsonl"),
    target: join(root, "execution-01-target-custody-map.jsonl"),
    mutation: join(root, "execution-01-mutation-manifest.jsonl"),
    profile: join(root, "execution-01-gate-profile.json"),
    receipt: join(root, "execution-01-baseline-receipt.json"),
  };
  for (const [name, path] of Object.entries(paths)) {
    if (!existsSync(path)) findings.push({ code: "file_missing", location: name, message: path });
  }
  if (findings.length) {
    console.log(JSON.stringify({ ok: false, findings }, null, 2));
    return 1;
  }
  const source = lines(paths.source);
  const python = lines(paths.python);
  const target = lines(paths.target);
  const mutation = lines(paths.mutation);
  const profile = JSON.parse(readFileSync(paths.profile, "utf8"));
  const receipt = JSON.parse(readFileSync(paths.receipt, "utf8"));
  const state = readFileSync(join(workspace, "docs", "milestones", "execution-state.yaml"), "utf8");
  const stateHead = /verified_zyra_head:\s*"([0-9a-f]{40})"/.exec(state)?.[1] ?? "";
  if (stateHead !== VERIFIED) findings.push({ code: "verified_head", location: "execution-state.yaml", message: stateHead });
  const snapshot = execFileSync("git", ["rev-parse", "HEAD"], { cwd: sourceRoot, encoding: "utf8" }).trim();
  const ids = new Set<string>();
  const acceptedIds = new Set<string>();
  const rejectedIds = new Set<string>();
  for (const record of source) {
    need(record, ["mapping_id", "source_snapshot", "source_path", "source_sha256", "start_line", "end_line", "source_symbol", "semantic_domain", "accepted", "exclusion_reason"], record.mapping_id, findings);
    if (ids.has(record.mapping_id)) findings.push({ code: "mapping_duplicate", location: record.mapping_id, message: "duplicate" });
    ids.add(record.mapping_id);
    if (record.accepted === true) {
      acceptedIds.add(record.mapping_id);
      if (record.exclusion_reason !== null) findings.push({ code: "accepted_exclusion", location: record.mapping_id, message: "accepted range has exclusion" });
    } else if (record.accepted === false) {
      rejectedIds.add(record.mapping_id);
      if (typeof record.exclusion_reason !== "string" || !record.exclusion_reason.trim()) {
        findings.push({ code: "rejected_reason", location: record.mapping_id, message: "rejected range requires reason" });
      }
    } else {
      findings.push({ code: "accepted_flag", location: record.mapping_id, message: String(record.accepted) });
    }
    const path = join(sourceRoot, record.source_path);
    if (!existsSync(path)) {
      findings.push({ code: "source_missing", location: record.mapping_id, message: record.source_path });
      continue;
    }
    const bytes = readFileSync(path);
    const text = bytes.toString("utf8");
    if (record.source_snapshot !== snapshot) findings.push({ code: "source_snapshot", location: record.mapping_id, message: "drift" });
    if (record.source_sha256 !== hash(bytes)) findings.push({ code: "source_hash", location: record.mapping_id, message: "drift" });
    const count = text.split(/\r?\n/).length;
    if (record.start_line < 1 || record.end_line < record.start_line || record.end_line > count) findings.push({ code: "source_range", location: record.mapping_id, message: "invalid" });
    const symbol = String(record.source_symbol).split("::").at(-1)?.split("+")[0] ?? "";
    if (!symbol || (!symbol.startsWith("#statement_") && !text.includes(symbol))) findings.push({ code: "source_symbol", location: record.mapping_id, message: symbol });
  }
  overlaps(source, "source_path", findings);
  const targetIds = new Set(target.map((record) => record.mapping_id));
  for (const id of acceptedIds) if (!targetIds.has(id)) findings.push({ code: "target_mapping", location: id, message: "missing accepted target" });
  for (const id of rejectedIds) if (targetIds.has(id)) findings.push({ code: "rejected_target_mapping", location: id, message: "rejected source has target" });
  if (target.length !== acceptedIds.size) findings.push({ code: "target_cardinality", location: paths.target, message: `${target.length}/${acceptedIds.size}` });
  for (const record of target) {
    need(record, ["target_path", "target_symbol", "canonical_owner_id", "default_callsite_path", "default_callsite_symbol", "state_store", "state_effect_kind", "success_test_ids", "failure_test_ids", "disable_test_ids", "runtime_origin_probe_id", "write_path_probe_id", "restore_probe_id"], record.mapping_id, findings);
    const path = join(zyra, record.target_path);
    if (requireTargets && !existsSync(path)) findings.push({ code: "target_missing", location: record.mapping_id, message: record.target_path });
    if (requireTargets && (typeof record.target_sha256 !== "string" || record.target_sha256.length !== 64)) {
      findings.push({ code: "target_hash_missing", location: record.mapping_id, message: String(record.target_sha256) });
    }
    if (existsSync(path) && record.target_sha256 && hash(readFileSync(path)) !== record.target_sha256) findings.push({ code: "target_hash", location: record.mapping_id, message: "drift" });
    if (JSON.stringify(record.success_test_ids) === JSON.stringify(record.failure_test_ids)) {
      findings.push({ code: "test_polarity", location: record.mapping_id, message: "success and failure tests are identical" });
    }
  }
  for (const record of python) {
    need(record, ["owner_id", "verified_zyra_head", "python_path", "python_sha256", "start_line", "end_line", "python_symbol", "disposition", "deletion_test_id"], record.owner_id, findings);
    const bytes = gitBytes(zyra, ["show", VERIFIED + ":" + record.python_path]);
    if (record.python_sha256 !== hash(bytes)) findings.push({ code: "python_hash", location: record.owner_id, message: "drift" });
  }
  overlaps(python, "python_path", findings);
  if (mutation.length < 30) findings.push({ code: "mutation_floor", location: paths.mutation, message: String(mutation.length) });
  if (new Set(mutation.map((record) => record.mutation_id)).size !== mutation.length) findings.push({ code: "mutation_duplicate", location: paths.mutation, message: "duplicate" });
  if (profile.verified_zyra_head !== VERIFIED || receipt.verified_zyra_head !== VERIFIED) findings.push({ code: "baseline_drift", location: "profile/receipt", message: "verified head mismatch" });
  if (receipt.clean_worktree !== true || (receipt.dirty_paths_at_capture ?? []).length !== 0) {
    findings.push({ code: "receipt_worktree", location: paths.receipt, message: "G0 capture did not begin from a clean worktree" });
  }
  if (profile.toolchain?.bun_version !== "1.2.15") findings.push({ code: "bun_profile", location: paths.profile, message: "not pinned" });
  const bunVersion = (globalThis as any).Bun?.version ?? "not-bun";
  if (bunVersion !== "1.2.15") findings.push({ code: "bun_runtime", location: "toolchain", message: bunVersion });
  for (const key of ["source", "python", "target", "mutation", "profile"] as const) {
    const actual = hash(readFileSync(paths[key]));
    if (receipt.input_sha256?.[basename(paths[key])] !== actual) findings.push({ code: "receipt_hash", location: key, message: "drift" });
  }
  const report = {
    ok: findings.length === 0,
    execution_id: "E01",
    verified_zyra_head: stateHead,
    toolchain: { bun: bunVersion },
    counts: {
      source_ranges: source.length,
      accepted_source_ranges: acceptedIds.size,
      rejected_source_ranges: rejectedIds.size,
      accepted_source_physical_range_sloc: source.filter((item) => item.accepted === true).reduce((sum, item) => sum + item.end_line - item.start_line + 1, 0),
      source_physical_range_sloc: source.reduce((sum, item) => sum + item.end_line - item.start_line + 1, 0),
      python_symbols: python.length,
      python_physical_range_sloc: python.reduce((sum, item) => sum + item.end_line - item.start_line + 1, 0),
      target_mappings: target.length,
      mutations: mutation.length,
    },
    require_targets: requireTargets,
    findings,
  };
  console.log(JSON.stringify(report, null, 2));
  return report.ok ? 0 : 1;
}

process.exitCode = main();
