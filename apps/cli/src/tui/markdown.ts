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

function stableStreamingPrefix(source: string): string {
  let inFence = false
  let fenceMarker = ""
  for (const line of source.split("\n")) {
    const marker = line.match(/^\s*(```|~~~)/u)?.[1]
    if (!marker) continue
    if (!inFence) {
      inFence = true
      fenceMarker = marker
    } else if (marker === fenceMarker) {
      inFence = false
      fenceMarker = ""
    }
  }
  // Open fenced blocks are safe to render incrementally as literal code.
  if (inFence) return source
  let cutoff = source.length
  const ticks = [...source.matchAll(/(?<!\\)`/gu)]
  if (ticks.length % 2 === 1) cutoff = Math.min(cutoff, ticks.at(-1)!.index ?? cutoff)
  const linkStart = source.lastIndexOf("](")
  if (linkStart >= 0 && source.indexOf(")", linkStart + 2) < 0) {
    const labelStart = source.lastIndexOf("[", linkStart)
    cutoff = Math.min(cutoff, labelStart >= 0 ? labelStart : linkStart)
  } else {
    const openLabel = source.lastIndexOf("[")
    const closeLabel = source.lastIndexOf("]")
    if (openLabel > closeLabel) cutoff = Math.min(cutoff, Math.max(0, openLabel - (source[openLabel - 1] === "!" ? 1 : 0)))
  }
  return cutoff < source.length ? source.slice(0, cutoff).trimEnd() : source
}

export function renderMarkdown(value: string, width: number, options: { streaming?: boolean } = {}): string[] {
  const sanitized = sanitizeTerminalText(value)
  const source = options.streaming ? stableStreamingPrefix(sanitized) : sanitized
  const output: string[] = []
  let fence = false
  let fenceMarker = ""
  let language = ""
  for (const raw of source.split("\n")) {
    const fenceMatch = raw.match(/^\s*(```|~~~)\s*([^\s`]*)/u)
    if (fenceMatch) {
      if (!fence) {
        fence = true
        fenceMarker = fenceMatch[1]!
        language = fenceMatch[2] ?? ""
        const label = language ? ` ${language} ` : ""
        output.push(`  ┌─${label}${"─".repeat(Math.max(0, width - 4 - displayWidth(label)))}`)
      } else if (fenceMatch[1] === fenceMarker) {
        fence = false
        fenceMarker = ""
        output.push(`  └${"─".repeat(Math.max(0, width - 3))}`)
        language = ""
      } else {
        output.push(...wrapDisplay(raw, Math.max(8, width - 4)).map((line) => `  │ ${line}`))
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
    if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/u.test(raw)) {
      output.push(`  ${"─".repeat(Math.max(1, width - 2))}`)
      continue
    }
    const list = raw.match(/^(\s*)([-+*]|\d+[.)])\s+(.+)$/u)
    if (list) {
      const indent = " ".repeat(Math.min(8, list[1]!.replaceAll("\t", "  ").length))
      const task = list[3]!.match(/^\[([ xX])\]\s+(.+)$/u)
      const marker = task ? (task[1]!.toLowerCase() === "x" ? "☑ " : "☐ ") : /^\d/u.test(list[2]!) ? `${list[2]} ` : "• "
      output.push(...prefixed(task?.[2] ?? list[3]!, `  ${indent}${marker}`, width))
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
