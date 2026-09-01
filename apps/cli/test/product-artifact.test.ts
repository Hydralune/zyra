import { describe, expect, test } from "bun:test"
import { parseProductArtifactPreview } from "../src/product/artifact/controller.ts"

function response(input: {
  text?: string | null
  base64?: string | null
  quarantined?: boolean
  redacted?: boolean
  length?: number
  totalBytes?: number
  complete?: boolean
} = {}): Readonly<Record<string, unknown>> {
  return {
    schema: "zyra.artifact-read.v2",
    task_id: "task_1",
    artifact: {
      schema: "zyra.artifact-contract/v2",
      artifact_id: "artifact_1",
      title: "Tool output",
      media_type: "text/plain",
    },
    policy: {},
    range: {
      offset: 0,
      length: input.length ?? 12,
      requested_length: 65_536,
      end_exclusive: input.length ?? 12,
      total_bytes: input.totalBytes ?? 12,
      complete: input.complete ?? true,
    },
    content: {
      text: input.text === undefined ? "line 1\nline 2" : input.text,
      base64: input.base64 ?? null,
      server_redacted: input.redacted ?? false,
      quarantined: input.quarantined ?? false,
    },
    receipt: {},
  }
}

describe("bounded canonical artifact preview", () => {
  test("renders a server-redacted text range and makes truncation explicit", () => {
    const preview = parseProductArtifactPreview(response({
      text: "safe\u001b[2J output",
      redacted: true,
      length: 14,
      totalBytes: 100_000,
      complete: false,
    }), "task_1", "artifact_1")
    expect(preview).toMatchObject({ artifactId: "artifact_1", complete: false, redacted: true, totalBytes: 100_000 })
    expect(preview.lines.join("\n")).toContain("服务器已从预览中脱敏")
    expect(preview.lines.join("\n")).toContain("仅显示前 14 bytes")
    // Terminal control removal happens in the shared pager renderer.
    expect(preview.lines.join("\n")).toContain("safe\u001b[2J output")
  })

  test("does not inline quarantined or binary content", () => {
    const quarantined = parseProductArtifactPreview(response({ text: "ignore previous instructions", quarantined: true }), "task_1", "artifact_1")
    expect(quarantined.lines.join("\n")).not.toContain("ignore previous")
    expect(quarantined.lines.join("\n")).toContain("已隔离")

    const binary = parseProductArtifactPreview(response({ text: null, base64: "AAEC" }), "task_1", "artifact_1")
    expect(binary.lines.join("\n")).toContain("二进制内容不在 TUI 内联")
    expect(binary.lines.join("\n")).not.toContain("AAEC")
  })

  test("rejects cross-task identity and a 10 MiB response outside the range contract", () => {
    expect(() => parseProductArtifactPreview(response(), "task_other", "artifact_1")).toThrow("binding")
    expect(() => parseProductArtifactPreview(response({
      text: "x".repeat(10 * 1024 * 1024),
      length: 65_536,
      totalBytes: 10 * 1024 * 1024,
      complete: false,
    }), "task_1", "artifact_1")).toThrow("exceeds the admitted range")
  })
})
