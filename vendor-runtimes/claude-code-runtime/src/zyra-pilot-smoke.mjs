import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { zyraClaudeCodePilotHealth, zyraClaudeCodePilotManifest } from "./zyra-pilot-manifest.mjs";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const runtimeRoot = path.resolve(__dirname, "..");

function fileExists(projectRelativePath) {
  const projectRoot = path.resolve(runtimeRoot, "..", "..");
  return fs.existsSync(path.resolve(projectRoot, projectRelativePath));
}

const missing = zyraClaudeCodePilotManifest.files.filter((item) => !fileExists(item.targetPath));
const health = {
  ...zyraClaudeCodePilotHealth(),
  runtimeRoot,
  missingTargetCount: missing.length,
  missingTargets: missing.map((item) => item.targetPath),
};

if (process.argv.includes("--inventory")) {
  process.stdout.write(`${JSON.stringify({ ok: health.ok && missing.length === 0, manifest: zyraClaudeCodePilotManifest, health })}\n`);
} else {
  process.stdout.write(`${JSON.stringify({ ok: health.ok && missing.length === 0, health })}\n`);
}

if (!health.ok || missing.length > 0) {
  process.exitCode = 1;
}
