import { spawnSync } from "node:child_process"
import { resolve } from "node:path"
import { writeFileSync } from "node:fs"

const ROOT = resolve(import.meta.dirname, "..")
const BASELINE =
  process.argv.find((value) => value.startsWith("--baseline="))
    ?.slice("--baseline=".length)
  || "53b002bac1e97eab23b7553d344da068dd8dd3c9"
const TARGET =
  process.argv.find((value) => value.startsWith("--target="))
    ?.slice("--target=".length)
  || "6e365ea1e5a28619a052d5d0f8d6a5ccd880f976"
const MINIMUM = Number(
  process.argv.find((value) => value.startsWith("--minimum="))
    ?.slice("--minimum=".length)
  || 7_500,
)
const OUTPUT =
  process.argv.find((value) => value.startsWith("--output="))
    ?.slice("--output=".length)

function sourceDecision(path) {
  if (
    path.startsWith("packages/commands/src/") &&
    [
      "coordinator.ts",
      "history.ts",
      "input-engine.ts",
      "palette.ts",
      "parser.ts",
      "policy.ts",
      "queue-actions.ts",
      "queue-recovery.ts",
      "queue.ts",
      "request.ts",
      "result-store.ts",
      "side-question.ts",
    ].some((name) => path.endsWith(name))
  ) {
    return {
      source_role: "primary_implementation",
      source_repo: "claude-code-best",
      source_language: "typescript/tsx",
      migration_mode:
        "retained_control_flow_adapt/cropped_migration/same_language_component_integration",
    }
  }
  if (
    path.startsWith("packages/commands/src/") &&
    [
      "diagnostics.ts",
      "identity.ts",
      "receipt-ledger.ts",
      "receipts.ts",
      "registry.ts",
      "result-model.ts",
    ].some((name) => path.endsWith(name))
  ) {
    return {
      source_role: "supplementary_implementation",
      source_repo: "opencode",
      source_language: "typescript/tsx",
      migration_mode:
        "cropped_migration/same_language_component_integration",
    }
  }
  if (
    path.endsWith("packages/commands/src/correlation.ts") ||
    path.endsWith("packages/commands/src/projection-index.ts")
  ) {
    return {
      source_role: "zyra_owner_integration",
      source_repo: "zyra/M2-01B",
      source_language: "typescript/tsx",
      migration_mode: "canonical_projection_component_integration",
    }
  }
  if (
    path.startsWith("apps/web/src/") ||
    path.startsWith("packages/core/typed-api-client/")
  ) {
    return {
      source_role: "zyra_owner_integration",
      source_repo: "zyra",
      source_language: "typescript/tsx",
      migration_mode: "typed_main_path_integration",
    }
  }
  if (path.startsWith("apps/api/")) {
    return {
      source_role: "zyra_canonical_owner_adapter",
      source_repo: "zyra/M1-control-runtime",
      source_language: "python",
      migration_mode: "canonical_owner_route_adapter",
    }
  }
  if (path.startsWith("tests/") || path.includes("/test/")) {
    return {
      source_role: "behavior_evidence",
      source_repo: "zyra",
      source_language:
        path.endsWith(".py") ? "python" : "typescript/tsx",
      migration_mode: "test_or_conformance",
    }
  }
  return {
    source_role: "evidence_or_build",
    source_repo: "zyra",
    source_language: "n/a",
    migration_mode: "excluded_support",
  }
}

const child = spawnSync(
  resolve(ROOT, "node_modules", ".bin", "bun.exe"),
  [
    resolve(ROOT, "scripts", "audit_m2_s02b_01_effective_lines.mjs"),
    `--baseline=${BASELINE}`,
    `--target=${TARGET}`,
    "--minimum=0",
  ],
  {
    cwd: ROOT,
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  },
)

if (child.status !== 0 || !child.stdout.trim()) {
  process.stderr.write(child.stderr)
  process.exit(child.status ?? 1)
}

const result = JSON.parse(child.stdout)
const file = (path) => {
  const selected = result.files.find((entry) => entry.path === path)
  if (!selected) throw new Error(`Effective-line audit is missing ${path}`)
  return selected
}

function move(path, from, to, requested) {
  const selected = file(path)
  const count =
    requested === "all"
      ? selected[from]
      : Math.min(Number(requested), selected[from])
  selected[from] -= count
  selected[to] += count
  selected.effective_production =
    selected.production_runtime + selected.ui_behavior
}

// Static command catalog records are executable configuration but the global
// gate requires them to remain schema/DTO/data rather than production credit.
move(
  "apps/web/src/command/catalog.ts",
  "production_runtime",
  "schema_dto_data",
  "all",
)
move(
  "packages/commands/src/registry.ts",
  "production_runtime",
  "schema_dto_data",
  383,
)

// Typed wire declarations and direct HTTP field mapping are useful delivery
// support, but cannot satisfy the primary/supplementary implementation quota.
move(
  "apps/web/src/api/task-api.ts",
  "production_runtime",
  "adapter_only",
  "all",
)
for (const path of [
  "packages/core/typed-api-client/src/constants.ts",
  "packages/core/typed-api-client/src/normalizers.ts",
  "packages/core/typed-api-client/src/protocol.ts",
]) {
  move(path, "production_runtime", "generated", "all")
}
move(
  "apps/api/zyra_api/main.py",
  "generated",
  "adapter_only",
  "all",
)

// The Workbench route binding and direct TaskApi bridge are deliberately
// excluded. CommandSurfaceRuntime's reconciliation, recovery, action,
// fail-closed and overlay orchestration remains production runtime.
move(
  "apps/web/src/app/runtime.ts",
  "ui_behavior",
  "adapter_only",
  "all",
)
for (const count of [43, 13, 18]) {
  move(
    "apps/web/src/features/commands/runtime.ts",
    "production_runtime",
    "adapter_only",
    count,
  )
}
move(
  "apps/web/src/features/commands/runtime.ts",
  "production_runtime",
  "schema_dto_data",
  18,
)

// Small frozen name lists and title maps remain data even though they are
// inside otherwise executable validators/render-model builders.
move(
  "packages/commands/src/diagnostics.ts",
  "production_runtime",
  "schema_dto_data",
  13,
)
move(
  "packages/commands/src/result-model.ts",
  "production_runtime",
  "schema_dto_data",
  15,
)

result.audit_scope = "M2-S04A-01"
result.baseline_commit = BASELINE
result.implementation_commit = TARGET
result.interval = `${BASELINE}..${TARGET}`
result.methodology =
  "Exact Git added-line sets from the frozen baseline to implementation commit. The shared TypeScript compiler-AST scanner excludes imports/exports, interfaces, type aliases, declaration-only signatures, blank/comment lines and complete JSX presentation ranges. This slice applies additional conservative deductions for the 11-command static catalogs, typed API wire declarations, Python/TaskApi route adapters, Workbench route binding, direct transport/payload maps and small static title/name tables. Tests, docs, lock/manifests and generated/data material receive zero credit. Effective production is production runtime plus non-JSX UI behavior only."
result.files = result.files.map((entry) => ({
  ...entry,
  ...sourceDecision(entry.path),
}))

const numericKeys = [
  "raw_additions",
  "raw_deletions",
  "production_runtime",
  "ui_behavior",
  "ui_presentation",
  "type_declaration",
  "schema_dto_data",
  "adapter_only",
  "generated",
  "test_mock_fixture",
  "docs_comments_blank",
  "vendor_like_source_pool",
  "effective_production",
]
result.totals = Object.fromEntries(
  numericKeys.map((key) => [
    key,
    result.files.reduce((total, entry) => total + Number(entry[key] || 0), 0),
  ]),
)
result.minimum_effective_production = MINIMUM
result.line_count_ok = result.totals.effective_production >= MINIMUM
result.large_file_triggers = result.files
  .filter((entry) =>
    entry.raw_additions > 500 ||
    entry.effective_production > result.totals.effective_production * 0.2 ||
    entry.type_declaration > entry.raw_additions * 0.3 ||
    entry.schema_dto_data > entry.raw_additions * 0.3 ||
    entry.generated > entry.raw_additions * 0.3 ||
    entry.adapter_only > entry.raw_additions * 0.3)
  .map((entry) => entry.path)

const serialized = `${JSON.stringify(result, null, 2)}\n`
if (OUTPUT) {
  writeFileSync(resolve(ROOT, OUTPUT), serialized)
}
if (process.argv.includes("--summary")) {
  process.stdout.write(
    [
      `scope=${result.audit_scope}`,
      `interval=${result.interval}`,
      ...numericKeys.map((key) => `${key}=${result.totals[key]}`),
      `minimum=${MINIMUM}`,
      `line_count_ok=${result.line_count_ok}`,
      `large_file_triggers=${result.large_file_triggers.join(",")}`,
    ].join("\n") + "\n",
  )
} else {
  process.stdout.write(serialized)
}

if (!result.line_count_ok) process.exitCode = 1
