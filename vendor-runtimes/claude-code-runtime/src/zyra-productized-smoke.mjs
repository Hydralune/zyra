import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { zyraClaudeCodeProductizedHealth, zyraClaudeCodeProductizedManifest } from "./zyra-productized-manifest.mjs";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const runtimeRoot = path.resolve(__dirname, "..");
const projectRoot = path.resolve(runtimeRoot, "..", "..");
const crosswalkPath = path.resolve(runtimeRoot, "metadata", "reference_crosswalk.json");

function fileExists(projectRelativePath) {
  return fs.existsSync(path.resolve(projectRoot, projectRelativePath));
}

function readCrosswalk() {
  if (!fs.existsSync(crosswalkPath)) {
    return { ok: false, entryCount: 0, missingTargetCount: 0, referenceOnlyRepos: [] };
  }
  const payload = JSON.parse(fs.readFileSync(crosswalkPath, "utf8"));
  return {
    ok: payload.ok === true,
    entryCount: payload.summary?.entry_count ?? 0,
    missingTargetCount: payload.summary?.missing_target_count ?? 0,
    referenceOnlyRepos: payload.summary?.reference_only_repos ?? [],
  };
}

const missing = zyraClaudeCodeProductizedManifest.files.filter((item) => !fileExists(item.targetPath));
const crosswalk = readCrosswalk();
const health = {
  ...zyraClaudeCodeProductizedHealth(),
  runtimeRoot,
  productizedRoot: path.resolve(runtimeRoot, "productized", "claude-code-best"),
  missingTargetCount: missing.length,
  missingTargets: missing.map((item) => item.targetPath),
  referenceCrosswalk: crosswalk,
};

const ok = health.ok && missing.length === 0 && crosswalk.ok;
if (process.argv.includes("--inventory")) {
  process.stdout.write(`${JSON.stringify({ ok, manifest: zyraClaudeCodeProductizedManifest, health })}\n`);
} else {
  process.stdout.write(`${JSON.stringify({ ok, health })}\n`);
}

if (!ok) {
  process.exitCode = 1;
}
