import { describe, expect, test } from "bun:test"
import type { PlanNodeProjection } from "../../../packages/core/typed-api-client/src/index.ts"
import {
  extractArtifactText,
  productTaskProgress,
} from "../src/components/tasks/product-task-detail.tsx"

function node(nodeId: string, status: string): PlanNodeProjection {
  return {
    nodeId,
    title: nodeId,
    description: `${nodeId} description`,
    status,
    dependsOn: [],
    artifactIds: [],
    metadata: {},
  }
}

describe("product frontstage", () => {
  test("projects plan completion without inventing runtime progress", () => {
    expect(productTaskProgress({
      terminal: false,
      planNodes: [
        node("understand", "completed"),
        node("implement", "running"),
        node("verify", "pending"),
      ],
    })).toEqual({ completed: 1, total: 3, percentage: 33 })

    expect(productTaskProgress({ terminal: true, planNodes: [] })).toEqual({
      completed: 0,
      total: 0,
      percentage: 100,
    })
  })

  test("previews only real text returned by the artifact content envelope", () => {
    expect(extractArtifactText({ content: { text: "verified result" } })).toBe("verified result")
    expect(extractArtifactText({ content: { bytes: "ZmFrZQ==" } })).toBeUndefined()
    expect(extractArtifactText({ text: "not the API envelope" })).toBeUndefined()
  })

  test("keeps engineering evidence behind an explicit advanced surface", async () => {
    const app = await Bun.file(new URL("../src/app/workbench-app.tsx", import.meta.url)).text()
    const detail = await Bun.file(new URL("../src/components/tasks/product-task-detail.tsx", import.meta.url)).text()
    const command = await Bun.file(new URL("../src/components/command-input/command-input.tsx", import.meta.url)).text()
    const styles = await Bun.file(new URL("../src/styles.css", import.meta.url)).text()

    expect(app).toContain('className="app-shell product-shell"')
    expect(app).toContain("今天想让 Zyra 完成什么？")
    expect(app).toContain("高级 Workbench")
    expect(app).toContain("<ProductTaskDetail")
    expect(detail).toContain("<TaskDetail runtime={runtime} state={state} />")
    expect(detail).toContain('className="advanced-drawer"')
    expect(detail).toContain('className="product-final-answer"')
    expect(detail).toContain("没有生成有效回答")
    expect(command).toContain("描述一个任务，或向 Zyra 提问")
    expect(command).toContain("const selectedTask = taskContext")
    expect(app).toContain('taskContext={route.kind === "task" ? state.detail.task : undefined}')
    expect(styles).toContain(".product-starter-grid")
    expect(styles).toContain(".advanced-drawer-panel")
  })
})
