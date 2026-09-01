import { displayWidth, sanitizeTerminalText, wrapDisplay } from "./text.ts"

function inline(value: string): string {
  return value
    .replace(/!\[([^\]]*)\]\([^\s)]+(?:\s+"[^"]*")?\)/g, "$1 [image]")
    .replace(/\[([^\]]+)\]\(([^\s)]+)(?:\s+"[^"]*")?\)/g, "$1 <$2>")
    .replace(/`([^`]+)`/g, "‹$1›")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/__([^_]+)__/g, "$1")
    .replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, "$1")
    .replace(/(?<!_)_([^_]+)_(?!_)/g, "$1")
}

function prefixed(value: string, prefix: string, width: number, continuation = ""): string[] {
  const available = Math.max(8, width - displayWidth(prefix))
  const rest = continuation || " ".repeat(displayWidth(prefix))
  return wrapDisplay(inline(value), available).map((line, index) => `${index ? rest : prefix}${line}`)
}

export function renderMarkdown(value: string, width: number): string[] {
  const source = sanitizeTerminalText(value)
  const output: string[] = []
  let fence = false
  let language = ""
  for (const raw of source.split("\n")) {
    const fenceMatch = raw.match(/^\s*```\s*([^\s`]*)/)
    if (fenceMatch) {
      if (!fence) {
        fence = true
        language = fenceMatch[1] ?? ""
        output.push(`  ┌─${language ? ` ${language} ` : ""}${"─".repeat(Math.max(0, width - 5 - displayWidth(language)))}`)
      } else {
        fence = false
        output.push(`  └${"─".repeat(Math.max(0, width - 3))}`)
        language = ""
      }
      continue
    }
    if (fence) {
      output.push(...wrapDisplay(raw.replaceAll("\t", "    "), Math.max(8, width - 4)).map((line) => `  │ ${line}`))
      continue
    }
    if (!raw.trim()) {
      if (output.at(-1) !== "") output.push("")
      continue
    }
    const heading = raw.match(/^\s*(#{1,6})\s+(.+)$/)
    if (heading) {
      const marker = heading[1]!.length <= 2 ? "◆ " : "◇ "
      output.push(...prefixed(heading[2]!, marker, width))
      continue
    }
    const quote = raw.match(/^\s*>\s?(.*)$/)
    if (quote) {
      output.push(...prefixed(quote[1]!, "│ ", width, "│ "))
      continue
    }
    const list = raw.match(/^\s*([-+*]|\d+[.)])\s+(.+)$/)
    if (list) {
      const marker = /^\d/u.test(list[1]!) ? `${list[1]} ` : "• "
      output.push(...prefixed(list[2]!, `  ${marker}`, width))
      continue
    }
    const table = raw.trim().startsWith("|") && raw.trim().endsWith("|")
    if (table) {
      if (/^\|?[\s:|-]+\|?$/u.test(raw.trim())) continue
      const cells = raw.trim().slice(1, -1).split("|").map((cell) => inline(cell.trim()))
      output.push(...prefixed(cells.join(" │ "), "  ", width))
      continue
    }
    output.push(...wrapDisplay(inline(raw), width))
  }
  if (fence) output.push(`  └${"─".repeat(Math.max(0, width - 3))}`)
  while (output.at(-1) === "") output.pop()
  return output
}
