import { describe, expect, test } from "bun:test"

import { resolveWorkbenchClientOptions } from "../src/app/runtime.ts"

describe("workbench API configuration", () => {
  test("uses the API origin embedded in the production page", async () => {
    const index = await Bun.file(new URL("../index.html", import.meta.url)).text()
    expect(index).toContain('data-api-base-url="http://127.0.0.1:8000"')

    expect(resolveWorkbenchClientOptions({
      apiBaseUrl: "http://127.0.0.1:8000",
      pageUrl: "http://127.0.0.1:5173/",
    })).toMatchObject({
      baseUrl: "http://127.0.0.1:8000",
    })
  })

  test("allows the documented api query parameter to override the embedded origin", () => {
    expect(resolveWorkbenchClientOptions({
      apiBaseUrl: "http://127.0.0.1:8000",
      apiToken: " token ",
      pageUrl: "http://127.0.0.1:5174/?api=http://127.0.0.1:8010",
    })).toMatchObject({
      baseUrl: "http://127.0.0.1:8010",
      token: "token",
    })
  })
})
