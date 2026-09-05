import { describe, expect, test } from "bun:test"

import { resolveWorkbenchClientOptions } from "../src/app/runtime.ts"

describe("workbench API configuration", () => {
  test("managed Web uses its fixed same-origin proxy after navigation and refresh", () => {
    for (const pageUrl of [
      "http://127.0.0.1:43127/tasks?api=http://127.0.0.1:8010",
      "http://127.0.0.1:43127/tasks/task_demo_001?view=artifacts",
    ]) {
      expect(resolveWorkbenchClientOptions({ apiProxy: true, pageUrl })).toMatchObject({
        baseUrl: "http://127.0.0.1:43127/api",
      })
    }
  })
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
