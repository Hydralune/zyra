import { spawnSync } from "node:child_process"
import { resolve } from "node:path"

const ROOT = resolve(import.meta.dirname, "..")
const BASELINE =
  process.argv
    .find((value) => value.startsWith("--baseline="))
    ?.slice("--baseline=".length) ||
  "f54446ec7bcbec16f189c8f50d957bd67e76e94b"
const TARGET =
  process.argv
    .find((value) => value.startsWith("--target="))
    ?.slice("--target=".length) ||
  "1bb50e7cf3f5f784ba11a4d107f1ec7730b7b9d8"
const MINIMUM = Number(
  process.argv
    .find((value) => value.startsWith("--minimum="))
    ?.slice("--minimum=".length) || 6_000,
)

function sourceDecision(path) {
  if (
    path.endsWith("/terminal/input.ts") ||
    path.endsWith("/terminal/writer.ts") ||
    path.endsWith("/terminal/backpressure.ts") ||
    path.endsWith("/terminal/screen.ts")
  ) {
    return {
      source_role: "supplementary_implementation",
      source_repo: "oh-my-pi",
      migration_mode: "bounded_same_language_semantic_integration",
    }
  }
  if (
    path.endsWith("/terminal/structured.ts") ||
    path.endsWith("/terminal/view/terminal-workbench.tsx") ||
    path.endsWith("/terminal/render.ts")
  ) {
    return {
      source_role: "supplementary_implementation",
      source_repo: "OpenHands",
      migration_mode:
        "cropped_migration/same_language_component_integration",
    }
  }
  if (path.startsWith("apps/web/src/features/terminal/")) {
    return {
      source_role: "primary_implementation",
      source_repo: "opencode",
      migration_mode:
        "cropped_migration/same_language_component_integration",
    }
  }
  if (
    path.startsWith("apps/web/src/") ||
    path.startsWith("packages/core/typed-api-client/")
  ) {
    return {
      source_role: "zyra_owner_integration",
      source_repo: "zyra",
      migration_mode: "typed_protocol_and_component_integration",
    }
  }
  if (
    path.startsWith("apps/api/") ||
    path.startsWith("packages/workers/") ||
    path.startsWith("packages/core/zyra_core/")
  ) {
    return {
      source_role: "zyra_owned_new_owner",
      source_repo: "zyra",
      migration_mode: "platform_and_canonical_owner_integration",
    }
  }
  if (path.startsWith("tests/") || path.includes("/test/")) {
    return {
      source_role: "behavior_evidence",
      source_repo: "zyra",
      migration_mode: "test_or_conformance",
    }
  }
  return {
    source_role: "evidence_tooling",
    source_repo: "zyra",
    migration_mode: "audit_or_documentation",
  }
}

const child = spawnSync(
  resolve(ROOT, "node_modules", ".bin", "bun.exe"),
  [
    resolve(ROOT, "scripts", "audit_m2_s02b_01_effective_lines.mjs"),
    `--baseline=${BASELINE}`,
    `--target=${TARGET}`,
    `--minimum=${MINIMUM}`,
  ],
  {
    cwd: ROOT,
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  },
)

if (!child.stdout.trim()) {
  process.stderr.write(child.stderr)
  process.exit(child.status ?? 1)
}

const result = JSON.parse(child.stdout)
result.audit_scope = "M2-S03B-01"
result.baseline_commit = BASELINE
result.implementation_commit = TARGET
result.interval = `${BASELINE}..${TARGET}`
result.methodology =
  "Exact Git added-line sets using the frozen TypeScript compiler AST and scanner classifier. Imports, comments, blanks, interfaces, type aliases, declaration-only signatures, repeated wire/schema declarations, CSS/static JSX/SVG, tests, fixtures, Python PTY owner code, docs, data, generated material, adapters and vendor/source-pool content receive zero TypeScript/React production credit. Active React handlers and executable runtime statements retain credit. Every row is rebound to the frozen source role and migration mode."
result.files = result.files.map((file) => ({
  ...file,
  ...sourceDecision(file.path),
}))

if (process.argv.includes("--summary")) {
  const totals = result.totals
  process.stdout.write(
    [
      `scope=${result.audit_scope}`,
      `interval=${result.interval}`,
      `raw_additions=${totals.raw_additions}`,
      `raw_deletions=${totals.raw_deletions}`,
      `production_runtime=${totals.production_runtime}`,
      `ui_behavior=${totals.ui_behavior}`,
      `ui_presentation=${totals.ui_presentation}`,
      `type_declaration=${totals.type_declaration}`,
      `schema_dto_data=${totals.schema_dto_data}`,
      `adapter_only=${totals.adapter_only}`,
      `generated=${totals.generated}`,
      `test_mock_fixture=${totals.test_mock_fixture}`,
      `docs_comments_blank=${totals.docs_comments_blank}`,
      `vendor_like_source_pool=${totals.vendor_like_source_pool}`,
      `effective_production=${totals.effective_production}`,
      `minimum=${MINIMUM}`,
      `line_count_ok=${result.line_count_ok}`,
    ].join("\n") + "\n",
  )
} else {
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`)
}

if (!result.line_count_ok) process.exitCode = 1
