import { lstat, readFile, readdir, realpath, stat } from "node:fs/promises";
import { extname, relative, resolve, sep } from "node:path";

import type { JsonObject } from "../contracts.ts";
import { cloneJson, deterministicId, digest, monotonicNow } from "../e02/index.ts";
import type { PluginManifest } from "./contracts.ts";

export interface PluginScannedFile {
  fileId: string;
  relativePath: string;
  realPathDigest: string;
  sizeBytes: number;
  modifiedAtMs: number;
  mode: number;
  extension: string;
  contentDigest: string;
  executable: boolean;
  symbolicLink: boolean;
  mediaKind: "source" | "markdown" | "config" | "binary" | "archive" | "unknown";
  metadata: JsonObject;
}

export interface PluginSupplyChainFinding {
  findingId: string;
  severity: "info" | "warning" | "error" | "critical";
  code: string;
  path: string | null;
  message: string;
  blocking: boolean;
  details: JsonObject;
}

export interface PluginSupplyChainReceipt {
  receiptId: string;
  pluginId: string;
  pluginVersion: string;
  manifestDigest: string;
  rootDigest: string;
  files: PluginScannedFile[];
  findings: PluginSupplyChainFinding[];
  allowed: boolean;
  totalBytes: number;
  scannedAt: string;
  policyDigest: string;
  digest: string;
  metadata: JsonObject;
}

export interface PluginSupplyChainPolicy {
  workspaceRoot: string;
  allowedExtensions: string[];
  deniedExtensions: string[];
  maximumFiles: number;
  maximumFileBytes: number;
  maximumTotalBytes: number;
  allowSymlinks: boolean;
  allowNativeBinaries: boolean;
  allowPackageScripts: boolean;
  requireLicense: boolean;
  forbiddenPathFragments: string[];
  forbiddenContentPatterns: string[];
  metadata: JsonObject;
}

export interface PluginSupplyChainSnapshot {
  version: "zyra.plugin-supply-chain/v1";
  revision: number;
  policy: PluginSupplyChainPolicy;
  receipts: PluginSupplyChainReceipt[];
  digest: string;
  capturedAt: string;
}

export class PluginSupplyChainRuntime {
  private readonly policy: PluginSupplyChainPolicy;
  private readonly receipts = new Map<string, PluginSupplyChainReceipt>();
  private readonly now: () => Date;
  private readonly maximumReceipts: number;
  private revision = 0;
  private lastTimestamp: string | null = null;

  constructor(options: { policy: PluginSupplyChainPolicy; now?: () => Date; maximumReceipts?: number; snapshot?: PluginSupplyChainSnapshot | null }) {
    this.policy = normalizePolicy(options.policy);
    this.now = options.now ?? (() => new Date());
    this.maximumReceipts = options.maximumReceipts ?? 1_000;
    if (options.snapshot) this.restore(options.snapshot);
  }

  async scan(manifestValue: PluginManifest, metadata: JsonObject = {}): Promise<PluginSupplyChainReceipt> {
    const manifest = cloneJson(manifestValue);
    const workspaceRoot = resolve(this.policy.workspaceRoot);
    const rootPath = resolve(manifest.rootPath);
    if (!contains(workspaceRoot, rootPath)) throw supplyError("plugin_root_outside_workspace", `plugin ${manifest.pluginId} root is outside workspace`);
    const rootRealPath = await realpath(rootPath);
    if (!contains(workspaceRoot, rootRealPath)) throw supplyError("plugin_root_symlink_escape", `plugin ${manifest.pluginId} root resolves outside workspace`);
    const files: PluginScannedFile[] = [];
    const findings: PluginSupplyChainFinding[] = [];
    const queue = [rootRealPath];
    let totalBytes = 0;
    while (queue.length) {
      const directory = queue.shift()!;
      const entries = await readdir(directory, { withFileTypes: true });
      entries.sort((left, right) => left.name.localeCompare(right.name));
      for (const entry of entries) {
        const path = resolve(directory, entry.name);
        const relativePath = relative(rootRealPath, path).replace(/\\/g, "/");
        if (this.policy.forbiddenPathFragments.some((fragment) => relativePath.toLowerCase().includes(fragment.toLowerCase()))) {
          findings.push(finding("error", "forbidden_plugin_path", relativePath, `plugin path contains forbidden fragment`, true, { fragments: this.policy.forbiddenPathFragments }));
          continue;
        }
        if (entry.isDirectory()) {
          queue.push(path);
          continue;
        }
        if (entry.isSymbolicLink()) {
          if (!this.policy.allowSymlinks) {
            findings.push(finding("error", "plugin_symlink_denied", relativePath, "plugin symbolic link is denied", true));
            continue;
          }
          const target = await realpath(path);
          if (!contains(rootRealPath, target)) {
            findings.push(finding("critical", "plugin_symlink_escape", relativePath, "plugin symbolic link escapes plugin root", true));
            continue;
          }
        }
        const file = await this.scanFile(rootRealPath, path, entry.isSymbolicLink(), findings);
        if (!file) continue;
        files.push(file);
        totalBytes += file.sizeBytes;
        if (files.length > this.policy.maximumFiles) throw supplyError("plugin_file_count_exceeded", `plugin ${manifest.pluginId} exceeds ${this.policy.maximumFiles} files`);
        if (totalBytes > this.policy.maximumTotalBytes) throw supplyError("plugin_total_size_exceeded", `plugin ${manifest.pluginId} exceeds ${this.policy.maximumTotalBytes} bytes`);
      }
    }
    await this.inspectPackage(manifest, rootRealPath, findings);
    if (this.policy.requireLicense && !manifest.license) findings.push(finding("error", "plugin_license_missing", null, "plugin manifest lacks license", true));
    const scannedAt = this.timestamp();
    const policyDigest = digest(this.policy);
    const rootDigest = digest(files.map((file) => ({ path: file.relativePath, digest: file.contentDigest, size: file.sizeBytes, mode: file.mode })));
    const base = {
      pluginId: manifest.pluginId,
      pluginVersion: manifest.version,
      manifestDigest: manifest.manifestDigest,
      rootDigest,
      files: files.sort((left, right) => left.relativePath.localeCompare(right.relativePath)),
      findings,
      allowed: !findings.some((value) => value.blocking),
      totalBytes,
      scannedAt,
      policyDigest,
      metadata: cloneJson(metadata),
    };
    const receiptId = deterministicId("plugin-supply-chain-receipt", base, 40);
    const receipt: PluginSupplyChainReceipt = { receiptId, ...base, digest: digest({ receiptId, ...base }) };
    this.receipts.set(receiptId, receipt);
    this.revision += 1;
    this.trim();
    return cloneJson(receipt);
  }

  requireAllowed(receiptId: string, manifestDigest: string): PluginSupplyChainReceipt {
    const receipt = this.receipts.get(receiptId);
    if (!receipt) throw supplyError("supply_receipt_not_found", `plugin supply receipt ${receiptId} was not found`);
    if (receipt.manifestDigest !== manifestDigest) throw supplyError("supply_manifest_mismatch", `supply receipt ${receiptId} belongs to another manifest`);
    if (!receipt.allowed) throw supplyError("plugin_supply_chain_blocked", `plugin ${receipt.pluginId} failed supply-chain policy`, { findings: cloneJson(receipt.findings) as unknown as JsonObject });
    return cloneJson(receipt);
  }

  list(pluginId?: string): PluginSupplyChainReceipt[] {
    return [...this.receipts.values()].filter((receipt) => !pluginId || receipt.pluginId === pluginId).sort((left, right) => left.scannedAt.localeCompare(right.scannedAt)).map(cloneJson);
  }

  snapshot(): PluginSupplyChainSnapshot {
    const withoutDigest = {
      version: "zyra.plugin-supply-chain/v1" as const,
      revision: this.revision,
      policy: cloneJson(this.policy),
      receipts: this.list(),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: digest(withoutDigest) };
  }

  restore(snapshot: PluginSupplyChainSnapshot): void {
    if (snapshot.version !== "zyra.plugin-supply-chain/v1") throw supplyError("unsupported_supply_snapshot", "unsupported plugin supply-chain snapshot version");
    const { digest: expected, ...withoutDigest } = snapshot;
    if (digest(withoutDigest) !== expected) throw supplyError("supply_snapshot_digest_mismatch", "plugin supply-chain snapshot digest mismatch");
    if (
      digest(snapshot.policy) !== digest(this.policy)
      && !isLegacyEmptySupplyPolicy(snapshot, this.policy)
    ) {
      throw supplyError("supply_policy_mismatch", "plugin supply-chain restore policy changed");
    }
    this.receipts.clear();
    this.revision = snapshot.revision;
    for (const receipt of snapshot.receipts) this.receipts.set(receipt.receiptId, cloneJson(receipt));
  }

  private async scanFile(root: string, path: string, symbolicLink: boolean, findings: PluginSupplyChainFinding[]): Promise<PluginScannedFile | null> {
    const metadata = await lstat(path);
    const actual = symbolicLink ? await stat(path) : metadata;
    if (!actual.isFile()) return null;
    const relativePath = relative(root, path).replace(/\\/g, "/");
    const extension = extname(path).toLowerCase();
    if (this.policy.deniedExtensions.includes(extension)) findings.push(finding("critical", "plugin_extension_denied", relativePath, `plugin extension ${extension} is denied`, true));
    if (this.policy.allowedExtensions.length && !this.policy.allowedExtensions.includes(extension)) findings.push(finding("warning", "plugin_extension_unknown", relativePath, `plugin extension ${extension} is not allowlisted`, false));
    if (actual.size > this.policy.maximumFileBytes) {
      findings.push(finding("error", "plugin_file_oversized", relativePath, `plugin file exceeds ${this.policy.maximumFileBytes} bytes`, true, { size: actual.size }));
      return null;
    }
    const bytes = await readFile(path);
    const kind = mediaKind(extension);
    if ((kind === "binary" || kind === "archive") && !this.policy.allowNativeBinaries) findings.push(finding("critical", "plugin_binary_denied", relativePath, "native binary or archive is denied", true));
    if (["source", "markdown", "config"].includes(kind)) {
      const text = new TextDecoder("utf-8", { fatal: false }).decode(bytes);
      for (const pattern of this.policy.forbiddenContentPatterns) {
        let matched = false;
        try { matched = new RegExp(pattern, "iu").test(text); } catch { throw supplyError("supply_pattern_invalid", `invalid forbidden content pattern ${pattern}`); }
        if (matched) findings.push(finding("error", "forbidden_plugin_content", relativePath, `plugin content matches forbidden pattern`, true, { pattern_digest: digest(pattern) }));
      }
    }
    const base = {
      relativePath,
      realPathDigest: digest(await realpath(path)),
      sizeBytes: actual.size,
      modifiedAtMs: actual.mtimeMs,
      mode: actual.mode,
      extension,
      contentDigest: digest(bytes.toString("base64")),
      executable: (actual.mode & 0o111) !== 0,
      symbolicLink,
      mediaKind: kind,
      metadata: {},
    };
    return { fileId: deterministicId("plugin-scanned-file", base, 32), ...base };
  }

  private async inspectPackage(manifest: PluginManifest, root: string, findings: PluginSupplyChainFinding[]): Promise<void> {
    const packagePath = resolve(root, "package.json");
    try {
      const content = JSON.parse(await readFile(packagePath, "utf8")) as JsonObject;
      const scripts = content.scripts && typeof content.scripts === "object" && !Array.isArray(content.scripts) ? content.scripts as JsonObject : {};
      if (!this.policy.allowPackageScripts && Object.keys(scripts).length) findings.push(finding("error", "plugin_package_scripts_denied", "package.json", "plugin package scripts are denied", true, { script_names: Object.keys(scripts).sort() }));
      if (content.name !== undefined && typeof content.name !== "string") findings.push(finding("warning", "plugin_package_name_invalid", "package.json", "package name is not a string", false));
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") findings.push(finding("error", "plugin_package_invalid", "package.json", error instanceof Error ? error.message : String(error), true));
    }
    if (manifest.rootPath.includes("node_modules")) findings.push(finding("critical", "plugin_node_modules_root_denied", null, "plugin root cannot be node_modules", true));
  }

  private trim(): void {
    while (this.receipts.size > this.maximumReceipts) {
      const first = this.receipts.keys().next().value as string | undefined;
      if (!first) break;
      this.receipts.delete(first);
    }
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function normalizePolicy(value: PluginSupplyChainPolicy): PluginSupplyChainPolicy {
  const policy = cloneJson(value);
  policy.workspaceRoot = resolve(policy.workspaceRoot);
  policy.allowedExtensions = [...new Set(policy.allowedExtensions.map((item) => item.toLowerCase()))].sort();
  policy.deniedExtensions = [...new Set(policy.deniedExtensions.map((item) => item.toLowerCase()))].sort();
  if (policy.maximumFiles < 1 || policy.maximumFileBytes < 1 || policy.maximumTotalBytes < 1) throw supplyError("supply_limits_invalid", "plugin supply-chain limits must be positive");
  return policy;
}

function isLegacyEmptySupplyPolicy(
  snapshot: PluginSupplyChainSnapshot,
  currentPolicy: PluginSupplyChainPolicy,
): boolean {
  if (snapshot.revision !== 0 || snapshot.receipts.length !== 0) return false;
  const legacyPolicy = normalizePolicy(snapshot.policy);
  const legacyPattern = "child_process.execSync(";
  const currentPattern = "child_process\\.execSync\\(";
  if (
    !legacyPolicy.forbiddenContentPatterns.includes(legacyPattern)
    || !currentPolicy.forbiddenContentPatterns.includes(currentPattern)
  ) return false;
  legacyPolicy.forbiddenContentPatterns = legacyPolicy.forbiddenContentPatterns
    .map((pattern) => pattern === legacyPattern ? currentPattern : pattern);
  return digest(legacyPolicy) === digest(currentPolicy);
}

function mediaKind(extension: string): PluginScannedFile["mediaKind"] {
  if ([".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".rs", ".go"].includes(extension)) return "source";
  if ([".md", ".mdx", ".txt"].includes(extension)) return "markdown";
  if ([".json", ".yaml", ".yml", ".toml"].includes(extension)) return "config";
  if ([".exe", ".dll", ".so", ".dylib", ".node", ".wasm"].includes(extension)) return "binary";
  if ([".zip", ".tar", ".gz", ".tgz", ".7z"].includes(extension)) return "archive";
  return "unknown";
}

function finding(severity: PluginSupplyChainFinding["severity"], code: string, path: string | null, message: string, blocking: boolean, details: JsonObject = {}): PluginSupplyChainFinding {
  const base = { severity, code, path, message, blocking, details };
  return { findingId: deterministicId("plugin-supply-finding", base, 32), ...base };
}

function contains(parent: string, child: string): boolean {
  const rel = relative(parent, child);
  return rel === "" || (!rel.startsWith("..") && !resolve(rel).startsWith(sep));
}

function supplyError(code: string, message: string, details: JsonObject = {}): Error {
  return Object.assign(new Error(message), { name: "PluginSupplyChainError", code, details });
}
