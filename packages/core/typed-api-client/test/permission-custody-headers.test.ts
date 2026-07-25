import { describe, expect, test } from "bun:test"

import {
  HeaderPolicy,
  redactHeaders,
} from "../src/headers.ts"

function context() {
  return {
    operation: "permission.requests.query",
    contract: "zyra.permission-control.v2",
    requestId: "request_permission_header_test",
    attempt: 1,
    deadlineMs: Date.now() + 10_000,
    hasBody: false,
  }
}

describe("permission session custody headers", () => {
  test("explicit session custody takes precedence over ambient API auth", async () => {
    const policy = new HeaderPolicy({
      version: { requested: "1.0" },
      auth: async () => "ambient-api-token",
    })
    const headers = await policy.build(
      context(),
      { Authorization: "Bearer permission-session-custody" },
    )
    expect(headers.get("Authorization")).toBe(
      "Bearer permission-session-custody",
    )
  })

  test("ambient API auth still applies when permission custody is absent", async () => {
    const policy = new HeaderPolicy({
      version: { requested: "1.0" },
      auth: async () => "ambient-api-token",
    })
    const headers = await policy.build(context())
    expect(headers.get("Authorization")).toBe("Bearer ambient-api-token")
  })

  test("diagnostic header projection never exposes either token", async () => {
    const policy = new HeaderPolicy({
      version: { requested: "1.0" },
      auth: async () => "ambient-api-token",
      defaults: { "X-Console": "permission" },
    })
    const headers = await policy.build(
      context(),
      { Authorization: "Bearer permission-session-custody" },
    )
    expect(redactHeaders(headers).authorization).toBe("[redacted]")
    expect(JSON.stringify(redactHeaders(headers))).not.toContain(
      "permission-session-custody",
    )
  })
})
