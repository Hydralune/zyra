export interface GatewaySourceRecord {
  readonly path: string;
  readonly language: "python" | "typescript" | "javascript" | "rust";
  readonly content: string;
}

export interface GatewayAuditFinding {
  readonly code: string;
  readonly path: string;
  readonly line: number;
  readonly reason: string;
  readonly blocking: boolean;
}

export interface GatewayAuditResult {
  readonly passed: boolean;
  readonly scannedFiles: number;
  readonly scannedLines: number;
  readonly findings: readonly GatewayAuditFinding[];
}

const RULES: readonly {
  code: string;
  pattern: RegExp;
  reason: string;
  allow: (path: string) => boolean;
}[] = [
  {
    code: "raw_process_execution",
    pattern: /\b(?:execSync|spawnSync|exec)\s*\(/,
    reason: "process execution must use the fixed gateway host/backend port",
    allow: (path) =>
      path.endsWith("integration_host.py") || path.endsWith("backends.py"),
  },
  {
    code: "raw_filesystem_write",
    pattern: /\b(?:writeFileSync|appendFileSync|write_text|write_bytes)\s*\(/,
    reason: "workspace mutations must use GatewayFileArtifactPort",
    allow: (path) =>
      path.endsWith("state_store.py") ||
      path.endsWith("integration_events.py") ||
      path.endsWith("integration_remote.py"),
  },
  {
    code: "raw_network_connector",
    pattern: /\b(?:fetch|urlopen|requests\.(?:get|post)|httpx\.(?:get|post))\s*\(/,
    reason: "network access must use BrowserGatewayBoundary",
    allow: (path) => path.endsWith("integration_browser.py"),
  },
  {
    code: "dynamic_module_loading",
    pattern: /\b(?:createRequire|importlib\.import_module|import)\s*\(/,
    reason: "dynamic module loading cannot provide a gateway fallback",
    allow: () => false,
  },
  {
    code: "root_source_repository_dependency",
    pattern:
      /\.\.[/\\](?:claude-code-best|OpenHands|browser-use|opencode|OpenClaw|oh-my-pi)/i,
    reason: "Zyra cannot depend on a root-level source repository at runtime",
    allow: () => false,
  },
  {
    code: "vendor_runtime_dependency",
    pattern: /(?:vendor-runtimes|runtime-sources|source-pool)/i,
    reason: "gateway production code cannot depend on a source pool",
    allow: () => false,
  },
];

export function auditGatewaySources(
  records: readonly GatewaySourceRecord[],
): GatewayAuditResult {
  const findings: GatewayAuditFinding[] = [];
  let scannedLines = 0;
  for (const record of records) {
    const path = record.path.replaceAll("\\", "/");
    const lines = record.content.split(/\r?\n/);
    scannedLines += lines.length;
    for (const [index, line] of lines.entries()) {
      for (const rule of RULES) {
        rule.pattern.lastIndex = 0;
        if (!rule.pattern.test(line) || rule.allow(path)) continue;
        findings.push(
          Object.freeze({
            code: rule.code,
            path,
            line: index + 1,
            reason: rule.reason,
            blocking: true,
          }),
        );
      }
    }
  }
  const markers = new Set<string>();
  for (const record of records) {
    for (const marker of [
      "sandbox_gateway_required",
      "sandbox_gateway_router",
      "sandbox_gateway_unavailable",
    ]) {
      if (record.content.includes(marker)) markers.add(marker);
    }
  }
  if (markers.size !== 3) {
    findings.push(
      Object.freeze({
        code: "gateway_disable_probe_missing",
        path: "<integration>",
        line: 0,
        reason: "gateway disconnect does not have an observable fail-closed path",
        blocking: true,
      }),
    );
  }
  return Object.freeze({
    passed: findings.every((item) => !item.blocking),
    scannedFiles: records.length,
    scannedLines,
    findings: Object.freeze(findings),
  });
}
