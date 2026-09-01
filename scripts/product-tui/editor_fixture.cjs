const { appendFileSync } = require("node:fs")

if (process.env.ZYRA_EDITOR_FIXTURE_FAIL === "1") process.exit(7)

const path = process.argv.at(-1)
if (!path || path === __filename) {
  process.stderr.write("draft path missing\n")
  process.exit(2)
}

appendFileSync(path, "\n外部编辑器完成 ✅", "utf8")
