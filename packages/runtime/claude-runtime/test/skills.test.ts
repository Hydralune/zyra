import assert from "node:assert/strict";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { parseSkillRoots, TypeScriptSkillRuntime } from "../src/skills/index.ts";

test("TypeScript skill runtime owns discovery, invocation, resources, and commands", async () => {
  const root = await mkdtemp(join(tmpdir(), "zyra-ts-skills-"));
  try {
    const skillDirectory = join(root, "skills", "reviewer");
    const commandDirectory = join(root, ".claude", "commands");
    await mkdir(skillDirectory, { recursive: true });
    await mkdir(commandDirectory, { recursive: true });
    await writeFile(join(skillDirectory, "SKILL.md"), [
      "---",
      "name: reviewer",
      "description: Review a change precisely",
      "allowed-tools: [file_read]",
      "---",
      "Inspect the requested change and report concrete findings.",
    ].join("\n"));
    await writeFile(join(skillDirectory, "checklist.txt"), "correctness\nsecurity\n");
    await writeFile(join(commandDirectory, "audit.md"), [
      "---",
      "description: Run the audit procedure",
      "---",
      "Apply the repository audit procedure to the supplied target.",
    ].join("\n"));
    const runtime = new TypeScriptSkillRuntime(parseSkillRoots([
      { path: root, kind: "workspace", precedence: 0 },
    ], []));
    await runtime.open();

    const listed = await runtime.execute("list_skills", {});
    assert.equal(listed.metadata.canonical_runtime_owner, "typescript");
    assert.equal((listed.output.skills as unknown[]).length, 1);

    const invoked = await runtime.execute("skill", {
      name: "reviewer",
      arguments: { target: "src/main.ts" },
    });
    assert.match(String(invoked.output.instructions), /concrete findings/);
    assert.deepEqual(invoked.output.allowed_tools, ["file_read"]);

    const resource = await runtime.execute("read_skill_resource", {
      name: "reviewer",
      path: "checklist.txt",
    });
    assert.match(String(resource.output.content), /security/);

    const commands = await runtime.execute("list_commands", {});
    assert.equal((commands.output.commands as unknown[]).length, 1);
    const command = await runtime.execute("command", { name: "audit" });
    assert.match(String(command.output.instructions), /audit procedure/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("skill resources cannot escape the selected skill directory", async () => {
  const root = await mkdtemp(join(tmpdir(), "zyra-ts-skill-path-"));
  try {
    const skillDirectory = join(root, "skill-a");
    await mkdir(skillDirectory, { recursive: true });
    await writeFile(join(skillDirectory, "SKILL.md"), "---\nname: skill-a\n---\nSafe skill");
    await writeFile(join(root, "secret.txt"), "secret");
    const runtime = new TypeScriptSkillRuntime(parseSkillRoots([root], []));
    await runtime.open();
    await assert.rejects(
      runtime.execute("read_skill_resource", { name: "skill-a", path: "../secret.txt" }),
      /escapes/,
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
