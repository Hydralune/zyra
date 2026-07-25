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
  || "cc09f054ad491877eeed5951574a518d57e157a0"
const MINIMUM = Number(
  process.argv.find((value) => value.startsWith("--minimum="))
    ?.slice("--minimum=".length)
  || 15_000,
)
const OUTPUT =
  process.argv.find((value) => value.startsWith("--output="))
    ?.slice("--output=".length)

// Start with the exact parent Git interval and the S04A-01 conservative
// classifier, then apply the S04A-02 adapter/wire deductions. This is a direct
// parent audit; it does not sum slice self-reported totals.
const child = spawnSync(
  resolve(ROOT, "node_modules", ".bin", "bun.exe"),
  [
    resolve(ROOT, "scripts", "audit_m2_s04a_01_effective_lines.mjs"),
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
  if (!selected) throw new Error(`Parent audit is missing ${path}`)
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
// S04A-01 already assigns the entire Workbench composition binding and Python
// route adapter to non-effective buckets over this exact interval.
for (const path of [
  "packages/core/typed-api-client/src/constants.ts",
  "packages/core/typed-api-client/src/normalizers.ts",
  "packages/core/typed-api-client/src/protocol.ts",
]) {
  move(path, "production_runtime", "generated", "all")
}

const permissionPrimary = new Set([
  "canonical.ts",
  "projection.ts",
  "redaction.ts",
  "response-proof.ts",
  "response-race.ts",
  "runtime.ts",
  "store.ts",
  "view-model.ts",
  "warning-policy.ts",
])
const permissionSupplementary = new Set([
  "event-reconciler.ts",
  "reconnect-supervisor.ts",
  "session-roster.ts",
])
result.files = result.files.map((entry) => {
  if (
    entry.path ===
      "packages/runtime/claude-runtime/src/permission/response-proof.ts"
    || entry.path ===
      "packages/runtime/claude-runtime/src/e02/api-port-runtime.ts"
    || entry.path.startsWith("apps/web/src/features/permissions/view/")
    || permissionPrimary.has(
      entry.path.replace("apps/web/src/features/permissions/", ""),
    )
  ) {
    return {
      ...entry,
      source_role: "primary_implementation",
      source_repo: "claude-code-best",
      source_language: "typescript/tsx",
      migration_mode:
        "retained_control_flow_adapt/cropped_migration/same_language_component_integration",
    }
  }
  if (
    permissionSupplementary.has(
      entry.path.replace("apps/web/src/features/permissions/", ""),
    )
  ) {
    return {
      ...entry,
      source_role: "supplementary_implementation",
      source_repo: "opencode",
      source_language: "typescript/tsx",
      migration_mode:
        "cropped_migration/same_language_component_integration",
    }
  }
  return entry
})

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
result.audit_scope = "M2-04A-parent"
result.baseline_commit = BASELINE
result.implementation_commit = TARGET
result.interval = `${BASELINE}..${TARGET}`
result.methodology =
  "Direct exact Git parent interval from the frozen M2-04A baseline to the S04A-02 implementation commit. The S04A-01 AST and conservative catalog/adapter/wire deductions are applied first, then the S04A-02 Web permission transport, export bridge and remaining typed wire deductions. This report does not sum slice self-reported totals. Tests, docs, fixtures, CSS/static JSX presentation, schema/data, generated wire, adapter-only, ledger, evidence and vendor/source-pool material receive zero credit."
result.totals = Object.fromEntries(
  numericKeys.map((key) => [
    key,
    result.files.reduce((total, entry) => total + Number(entry[key] || 0), 0),
  ]),
)
result.minimum_effective_production = MINIMUM
result.line_count_ok = result.totals.effective_production >= MINIMUM

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
    ].join("\n") + "\n",
  )
} else {
  process.stdout.write(serialized)
}

if (!result.line_count_ok) process.exitCode = 1
