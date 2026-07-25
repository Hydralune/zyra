import { spawnSync } from "node:child_process"
import { resolve } from "node:path"
import { writeFileSync } from "node:fs"

const ROOT = resolve(import.meta.dirname, "..")
const BASELINE =
  process.argv.find((value) => value.startsWith("--baseline="))
    ?.slice("--baseline=".length)
  || "33327d49ef1fab62d314709e169eebc781eb2a9c"
const TARGET =
  process.argv.find((value) => value.startsWith("--target="))
    ?.slice("--target=".length)
  || "cc09f054ad491877eeed5951574a518d57e157a0"
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
    path ===
      "packages/runtime/claude-runtime/src/permission/response-proof.ts"
    || path ===
      "packages/runtime/claude-runtime/src/e02/api-port-runtime.ts"
    || [
      "canonical.ts",
      "projection.ts",
      "redaction.ts",
      "response-proof.ts",
      "response-race.ts",
      "runtime.ts",
      "store.ts",
      "view-model.ts",
      "warning-policy.ts",
    ].some((name) =>
      path === `apps/web/src/features/permissions/${name}`)
    || path.startsWith("apps/web/src/features/permissions/view/")
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
    [
      "event-reconciler.ts",
      "reconnect-supervisor.ts",
      "session-roster.ts",
    ].some((name) =>
      path === `apps/web/src/features/permissions/${name}`)
  ) {
    return {
      source_role: "supplementary_implementation",
      source_repo: "opencode",
      source_language: "typescript/tsx",
      migration_mode:
        "cropped_migration/same_language_component_integration",
    }
  }
  if (path.startsWith("apps/web/src/features/permissions/")) {
    return {
      source_role: "zyra_owner_integration",
      source_repo: "zyra/M1-permission-runtime",
      source_language: "typescript/tsx",
      migration_mode: "canonical_permission_component_integration",
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
      source_repo: "zyra/M1-permission-runtime",
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

// HTTP field mapping, the Web permission transport, and composition-only
// bindings are required delivery glue but cannot satisfy the implementation
// quota.
move(
  "apps/web/src/api/permission-api.ts",
  "production_runtime",
  "adapter_only",
  "all",
)
move(
  "apps/web/src/api/index.ts",
  "production_runtime",
  "adapter_only",
  "all",
)
move(
  "apps/web/src/app/runtime.ts",
  "ui_behavior",
  "adapter_only",
  "all",
)
move(
  "apps/api/zyra_api/main.py",
  "generated",
  "adapter_only",
  "all",
)

// Typed wire declarations and normalizers are generated/schema delivery
// support. The custody-header precedence implementation remains executable
// behavior and is not deducted.
for (const path of [
  "packages/core/typed-api-client/src/constants.ts",
  "packages/core/typed-api-client/src/normalizers.ts",
  "packages/core/typed-api-client/src/protocol.ts",
]) {
  move(path, "production_runtime", "generated", "all")
}

result.audit_scope = "M2-S04A-02"
result.baseline_commit = BASELINE
result.implementation_commit = TARGET
result.interval = `${BASELINE}..${TARGET}`
result.methodology =
  "Exact Git added-line sets from the frozen baseline to implementation commit. The shared TypeScript compiler-AST scanner excludes imports/exports, interfaces, type aliases, declaration-only signatures, comments/blanks and complete JSX presentation ranges. This slice additionally assigns the full Web permission HTTP client, API/index and Workbench composition bindings to adapter-only; Python response forwarding to adapter-only; and typed constants, protocol declarations and normalizers to generated wire support. Tests, fixtures, docs, CSS/static presentation, evidence, ledger and vendor/source-pool material receive zero credit. Effective production is executable production runtime plus non-JSX UI behavior only."
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
