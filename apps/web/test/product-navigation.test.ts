import { expect, test } from "bun:test"
import { parseWorkbenchRoute, routeHref } from "../src/shell/router.ts"

test("artifact and evidence destinations survive a copied URL or reload", () => {
  const artifacts = parseWorkbenchRoute({ pathname: "/tasks/task_navigation", search: "?view=artifacts" })
  expect(artifacts.kind).toBe("task")
  expect(routeHref(artifacts)).toBe("/tasks/task_navigation?view=artifacts")
  const skills = parseWorkbenchRoute({ pathname: "/tasks/task_navigation/evidence", search: "?section=skill-runtime-panel" })
  expect(skills.query.section).toBe("skill-runtime-panel")
  expect(routeHref(skills)).toContain("section=skill-runtime-panel")
  expect(parseWorkbenchRoute({ pathname: "/tasks/task_navigation", search: "?section=%3Cscript%3E&view=unknown" }).query).toEqual({})
})
