import { spawnSync } from "node:child_process"
import { resolve } from "node:path"
import { writeFileSync } from "node:fs"

const ROOT = resolve(import.meta.dirname, "..")
const BASELINE =
  process.argv.find((value) => value.startsWith("--baseline="))
    ?.slice("--baseline=".length)
  || "af6f6dc0c66a846c739b73e8c6006c67de219ba8"
const TARGET =
  process.argv.find((value) => value.startsWith("--target="))
    ?.slice("--target=".length)
  || "9cb8272"
const MINIMUM = Number(
  process.argv.find((value) => value.startsWith("--minimum="))
    ?.slice("--minimum=".length)
  || 8_000,
)
const OUTPUT =
  process.argv.find((value) => value.startsWith("--output="))
    ?.slice("--output=".length)

function sourceDecision(path) {
  if (
    path.startsWith("apps/web/src/features/session/") ||
    path.startsWith("packages/commands/src/")
  ) {
    return {
      source_role: "primary_implementation",
      source_repo: "claude-code-best",
      source_language: "typescript/tsx",
      target_language: "typescript/tsx",
      migration_mode:
        "retained_control_flow_adapt/cropped_migration/same_language_component_integration",
    }
  }
  if (
    path === "apps/web/src/features/memory/projection.ts" ||
    path === "apps/web/src/features/memory/retrieval-session.ts"
  ) {
    return {
      source_role: "primary_implementation",
      source_repo: "claude-code-best",
      source_language: "typescript",
      target_language: "typescript",
      migration_mode:
        "cropped_migration/same_language_component_integration",
    }
  }
  if (path.startsWith("apps/web/src/features/memory/")) {
    return {
      source_role: "supplementary_implementation",
      source_repo: "hermes-agent",
      source_language: "python/typescript",
      target_language: "typescript",
      migration_mode:
        "bounded_cross_language_typed_supplement/cropped_migration",
    }
  }
  if (
    path.startsWith("apps/web/src/features/providers/") ||
    path.startsWith("apps/web/src/features/placement/")
  ) {
    return {
      source_role: "supplementary_implementation",
      source_repo: "opencode",
      source_language: "typescript/tsx",
      target_language: "typescript/tsx",
      migration_mode:
        "cropped_migration/same_language_component_integration",
    }
  }
  if (path.startsWith("apps/web/src/")) {
    return {
      source_role: "zyra_owner_integration",
      source_repo: "zyra",
      source_language: "typescript/tsx",
      target_language: "typescript/tsx",
      migration_mode: "canonical_projection_component_integration",
    }
  }
  if (path.startsWith("apps/web/test/")) {
    return {
      source_role: "behavior_evidence",
      source_repo: "zyra",
      source_language: "typescript",
      target_language: "typescript",
      migration_mode: "test_or_conformance",
    }
  }
  return {
    source_role: "evidence_or_build",
    source_repo: "zyra",
    source_language: "n/a",
    target_language: "n/a",
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

function move(path, from, to, requested = "all") {
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

// Composition bindings connect the already-owned projection/runtime to the
// application but cannot satisfy the slice's implementation quota.
move("apps/web/src/app/runtime.ts", "ui_behavior", "adapter_only")

// Command descriptors and result labels are required typed control metadata.
// They are deliberately excluded even though the shared AST scanner sees
// executable object initializers.
move("packages/commands/src/registry.ts", "production_runtime", "schema_dto_data")
move("packages/commands/src/result-model.ts", "production_runtime", "schema_dto_data")
move("apps/web/src/features/commands/runtime.ts", "production_runtime", "schema_dto_data")

result.audit_scope = "M2-S04B-01"
result.baseline_commit = BASELINE
result.implementation_commit = TARGET
result.interval = `${BASELINE}..${TARGET}`
result.methodology =
  "Exact Git added-line sets from the frozen baseline to the latest implementation commit. The shared TypeScript compiler-AST scanner excludes imports/exports, interfaces, type aliases, declaration-only signatures, comments/blanks and complete JSX presentation ranges. This slice additionally excludes app composition bindings as adapter-only and all new command descriptors/result labels as schema/DTO metadata. Tests, fixtures, docs, static presentation, generated material, ledger data and vendor/source-pool content receive zero credit. Effective production is executable runtime plus non-JSX UI behavior only."
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
    entry.adapter_only > entry.raw_additions * 0.3 ||
    entry.test_mock_fixture > entry.raw_additions * 0.3 ||
    entry.ui_presentation > entry.raw_additions * 0.3)
  .map((entry) => entry.path)

const serialized = `${JSON.stringify(result, null, 2)}\n`
if (OUTPUT) writeFileSync(resolve(ROOT, OUTPUT), serialized)
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
