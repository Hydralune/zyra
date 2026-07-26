import assert from "node:assert/strict";
import { join } from "node:path";

import {
  parseSkillRoots,
  TypeScriptSkillRuntime,
} from "../packages/runtime/claude-runtime/src/skills/index.ts";

const root = join(import.meta.dir, "..");
const runtime = new TypeScriptSkillRuntime(parseSkillRoots([
  {
    path: join(root, "skills", "builtin"),
    kind: "builtin",
    precedence: 0,
  },
], []));

await runtime.open();
const listed = await runtime.execute("list_skills", {});
assert.equal(listed.metadata.canonical_runtime_owner, "typescript");

const skills = Array.isArray(listed.output.skills) ? listed.output.skills : [];
const names = new Set(
  skills
    .map((skill) => (
      skill && typeof skill === "object" && "name" in skill
        ? String(skill.name)
        : ""
    ))
    .filter(Boolean),
);
assert.ok(names.has("requirement-change"));
assert.ok(names.has("failure-recovery"));

const invoked = await runtime.execute("skill", {
  name: "requirement-change",
  arguments: { change: "preserve causal verification" },
});
assert.match(String(invoked.output.instructions), /requirement|change/i);

console.log("canonical TypeScript skill owner verification passed");
