import {
  booleanValue,
  firstDefined,
  integerValue,
  isRecord,
  optionalRecord,
  optionalString,
  recordArray,
  safeObservedUrl,
  stableDigest,
  type BrowserDomNodeSummary,
  type BrowserDomSummary,
  type BrowserScope,
} from "../contracts.ts"
import {
  firstDeep,
  valuesDeep,
  type BrowserRecord,
} from "./reader.ts"

const INJECTION_PATTERNS = Object.freeze([
  /\bignore (?:all |any )?(?:previous|prior|system|developer) instructions?\b/i,
  /\breveal (?:the )?(?:system prompt|developer message|secret|token|password|credential)\b/i,
  /\b(?:disable|bypass|override) (?:the )?(?:permission|policy|guard|sandbox|security)\b/i,
  /\bcall (?:the )?(?:tool|shell|terminal|browser control)\b/i,
  /\b(?:exfiltrate|upload|send) (?:all |the )?(?:secrets?|credentials?|tokens?|files?)\b/i,
  /\byou are now\b/i,
  /\bact as (?:the )?(?:system|developer|administrator|root)\b/i,
])

const SECRET_ATTRIBUTE = /(?:authorization|cookie|credential|password|secret|token|api[-_]?key)/i
const INTERACTIVE_ROLES = new Set([
  "button",
  "checkbox",
  "combobox",
  "gridcell",
  "link",
  "listbox",
  "menuitem",
  "option",
  "radio",
  "searchbox",
  "slider",
  "spinbutton",
  "switch",
  "tab",
  "textbox",
  "treeitem",
])
const EDITABLE_ROLES = new Set([
  "combobox",
  "searchbox",
  "spinbutton",
  "textbox",
])
const INTERACTIVE_TAGS = new Set([
  "a",
  "button",
  "input",
  "option",
  "select",
  "textarea",
])

interface DomNodeDraft {
  nodeId: string
  backendNodeId?: number
  parentNodeId?: string
  role?: string
  name?: string
  tag?: string
  text?: string
  value?: string
  description?: string
  selector?: string
  clickable: boolean
  editable: boolean
  disabled: boolean
  hidden: boolean
  focused: boolean
  attributes: Record<string, string>
}

function likelyDomRecord(record: BrowserRecord): boolean {
  const kind = record.kind.toLowerCase()
  if (
    kind.includes("dom")
    || kind.includes("accessibility")
    || kind.includes("browser_state")
    || kind.includes("page_state")
    || kind.includes("snapshot")
  ) return true
  return Boolean(
    firstDeep(record.payload, [
      "selector_map",
      "dom_state",
      "accessibility_tree",
      "nodes",
    ]),
  )
}

function candidateNodes(record: BrowserRecord): readonly Readonly<Record<string, unknown>>[] {
  const output: Readonly<Record<string, unknown>>[] = []
  const seen = new Set<string>()
  const accept = (candidate: unknown) => {
    if (!isRecord(candidate)) return
    const marker = String(
      firstDefined(
        candidate,
        "node_id",
        "nodeId",
        "backend_node_id",
        "backendNodeId",
        "tag",
        "tag_name",
        "role",
      ) ?? "",
    )
    if (!marker) return
    const key = `${marker}:${optionalString(
      firstDefined(candidate, "selector", "xpath", "name", "text"),
      "$.node.key",
      4_096,
    ) ?? ""}`
    if (seen.has(key)) return
    seen.add(key)
    output.push(candidate)
  }
  for (const key of [
    "nodes",
    "elements",
    "selector_map",
    "selectorMap",
    "clickable_elements",
    "accessibility_nodes",
    "ax_nodes",
  ]) {
    const value = record.payload[key]
    if (Array.isArray(value)) {
      for (const item of value.slice(0, 20_000)) accept(item)
    } else if (isRecord(value)) {
      for (const [index, item] of Object.entries(value).slice(0, 20_000)) {
        if (isRecord(item)) accept({ index, ...item })
      }
    }
  }
  for (const value of valuesDeep(
    record.payload,
    [
      "nodes",
      "elements",
      "selector_map",
      "selectorMap",
      "accessibility_nodes",
    ],
    7,
    20_000,
  )) {
    if (Array.isArray(value)) {
      for (const item of value.slice(0, 20_000)) accept(item)
    } else if (isRecord(value)) {
      for (const item of Object.values(value).slice(0, 20_000)) accept(item)
    }
  }
  return Object.freeze(output)
}

function safeAttributes(
  value: unknown,
): Readonly<Record<string, string>> {
  if (!isRecord(value)) return Object.freeze({})
  const output: Record<string, string> = {}
  for (const [rawName, rawValue] of Object.entries(value).slice(0, 128)) {
    const name = String(rawName).trim().toLowerCase().slice(0, 256)
    if (!name || SECRET_ATTRIBUTE.test(name)) continue
    if (
      rawValue === null
      || rawValue === undefined
      || typeof rawValue === "object"
    ) continue
    const text = String(rawValue).trim().slice(0, 4_096)
    if (!text) continue
    output[name] = text
  }
  return Object.freeze(output)
}

function nodeText(
  candidate: Readonly<Record<string, unknown>>,
  ...keys: readonly string[]
): string | undefined {
  return optionalString(
    firstDefined(candidate, ...keys),
    `$.node.${keys[0]}`,
    16 * 1024,
  )
}

function integerOrUndefined(value: unknown): number | undefined {
  if (value === undefined || value === null || value === "") return undefined
  const result = Number(value)
  return Number.isSafeInteger(result) && result >= 0 ? result : undefined
}

function domNode(
  candidate: Readonly<Record<string, unknown>>,
  record: BrowserRecord,
  index: number,
): DomNodeDraft {
  const backendNodeId = integerOrUndefined(
    firstDefined(candidate, "backend_node_id", "backendNodeId"),
  )
  const nodeId =
    nodeText(candidate, "node_id", "nodeId", "id", "index")
    ?? (backendNodeId === undefined ? undefined : String(backendNodeId))
    ?? `dom-node:${stableDigest({
      record: record.recordId,
      index,
      selector: candidate.selector,
      role: candidate.role,
    })}`
  const tag = nodeText(candidate, "tag", "tag_name", "tagName", "node_name")
    ?.toLowerCase()
  const role = nodeText(candidate, "role", "ax_role", "aria_role")?.toLowerCase()
  const attributes = {
    ...safeAttributes(candidate.attributes),
    ...safeAttributes(candidate.attrs),
  }
  const disabled =
    booleanValue(firstDefined(candidate, "disabled", "is_disabled"))
    || attributes.disabled === "true"
    || attributes["aria-disabled"] === "true"
  const hidden =
    booleanValue(firstDefined(candidate, "hidden", "is_hidden"))
    || attributes.hidden === "true"
    || attributes["aria-hidden"] === "true"
  const editable =
    !disabled
    && (
      booleanValue(firstDefined(candidate, "editable", "is_editable"))
      || (role ? EDITABLE_ROLES.has(role) : false)
      || ["input", "select", "textarea"].includes(tag ?? "")
      || attributes.contenteditable === "true"
    )
  const clickable =
    !disabled
    && !hidden
    && (
      booleanValue(firstDefined(candidate, "clickable", "is_clickable"))
      || (role ? INTERACTIVE_ROLES.has(role) : false)
      || (tag ? INTERACTIVE_TAGS.has(tag) : false)
      || Boolean(attributes.onclick)
      || attributes.tabindex === "0"
    )
  return {
    nodeId,
    backendNodeId,
    parentNodeId: nodeText(
      candidate,
      "parent_node_id",
      "parentNodeId",
      "parent_id",
    ),
    role,
    name: nodeText(candidate, "name", "accessible_name", "aria_label"),
    tag,
    text: nodeText(
      candidate,
      "text",
      "text_content",
      "inner_text",
      "node_value",
    ),
    value: nodeText(candidate, "value", "input_value"),
    description: nodeText(
      candidate,
      "description",
      "accessible_description",
      "title",
    ),
    selector: nodeText(
      candidate,
      "selector",
      "xpath",
      "css_selector",
      "enhanced_css_selector",
    ),
    clickable,
    editable,
    disabled,
    hidden,
    focused: booleanValue(firstDefined(candidate, "focused", "is_focused")),
    attributes,
  }
}

function mergeNode(left: DomNodeDraft, right: DomNodeDraft): DomNodeDraft {
  return {
    nodeId: left.nodeId,
    backendNodeId: right.backendNodeId ?? left.backendNodeId,
    parentNodeId: right.parentNodeId ?? left.parentNodeId,
    role: right.role ?? left.role,
    name: right.name ?? left.name,
    tag: right.tag ?? left.tag,
    text: right.text ?? left.text,
    value: right.value ?? left.value,
    description: right.description ?? left.description,
    selector: right.selector ?? left.selector,
    clickable: right.clickable || left.clickable,
    editable: right.editable || left.editable,
    disabled: right.disabled,
    hidden: right.hidden,
    focused: right.focused || left.focused,
    attributes: { ...left.attributes, ...right.attributes },
  }
}

function injectionFindings(nodes: readonly DomNodeDraft[]): string[] {
  const findings = new Set<string>()
  for (const node of nodes) {
    const text = [
      node.name,
      node.text,
      node.value,
      node.description,
      ...Object.values(node.attributes),
    ]
      .filter(Boolean)
      .join(" ")
      .slice(0, 128 * 1024)
    for (const pattern of INJECTION_PATTERNS) {
      if (pattern.test(text)) {
        findings.add(
          `untrusted_page_instruction:${node.nodeId}:${pattern.source.slice(0, 64)}`,
        )
      }
    }
  }
  return [...findings]
}

function observedNodeCount(
  record: BrowserRecord,
  admitted: number,
): number {
  const candidate = firstDeep(record.payload, [
    "node_count",
    "element_count",
    "dom_node_count",
  ])
  if (candidate === undefined || candidate === null || candidate === "") {
    return admitted
  }
  try {
    return integerValue(candidate, "$.dom.node_count", {
      minimum: admitted,
      maximum: 10_000_000,
    })
  } catch {
    return admitted
  }
}

function targetIdentity(record: BrowserRecord): string | undefined {
  return optionalString(
    firstDeep(record.payload, ["target_id", "targetId", "tab_id"]),
    "$.dom.target_id",
    512,
  )
}

function frameIdentity(record: BrowserRecord): string | undefined {
  return optionalString(
    firstDeep(record.payload, ["frame_id", "frameId"]),
    "$.dom.frame_id",
    512,
  )
}

function stepIdentity(record: BrowserRecord): string | undefined {
  return optionalString(
    firstDeep(record.payload, [
      "browser_step_id",
      "step_id",
      "stepId",
      "action_id",
    ]),
    "$.dom.step_id",
    512,
  )
}

function buildSummary(
  scope: BrowserScope,
  record: BrowserRecord,
): BrowserDomSummary {
  const drafts = new Map<string, DomNodeDraft>()
  for (const [index, candidate] of candidateNodes(record).entries()) {
    const node = domNode(candidate, record, index)
    const existing = drafts.get(node.nodeId)
    drafts.set(node.nodeId, existing ? mergeNode(existing, node) : node)
  }
  const nodes = [...drafts.values()]
  const admittedNodes = nodes.slice(0, 5_000)
  const nodeCount = observedNodeCount(record, admittedNodes.length)
  const promptInjectionFindings = injectionFindings(admittedNodes)
  const source = record.payload
  const textDigest =
    optionalString(
      firstDeep(source, ["text_digest", "text_hash", "dom_digest"]),
      "$.dom.text_digest",
      512,
    )
    ?? stableDigest(
      admittedNodes.map((node) => [
        node.nodeId,
        node.role,
        node.tag,
        node.text,
        node.name,
      ]),
    )
  const accessibilityDigest =
    optionalString(
      firstDeep(source, [
        "accessibility_digest",
        "ax_digest",
        "accessibility_hash",
      ]),
      "$.dom.accessibility_digest",
      512,
    )
    ?? stableDigest(
      admittedNodes.map((node) => [
        node.nodeId,
        node.role,
        node.name,
        node.description,
      ]),
    )
  const url = safeObservedUrl(
    firstDeep(source, ["url", "current_url", "page_url"]),
  )
  return Object.freeze({
    browserSessionId: scope.browserSessionId,
    stepId: stepIdentity(record),
    targetId: targetIdentity(record),
    frameId: frameIdentity(record),
    url: url || undefined,
    title: optionalString(
      firstDeep(source, ["title", "page_title"]),
      "$.dom.title",
      16 * 1024,
    ),
    textDigest,
    accessibilityDigest,
    nodeCount,
    interactiveCount: admittedNodes.filter(
      (node) => node.clickable || node.editable,
    ).length,
    visibleCount: admittedNodes.filter((node) => !node.hidden).length,
    truncated: nodeCount > admittedNodes.length || nodes.length > admittedNodes.length,
    promptInjectionFindings: Object.freeze(promptInjectionFindings),
    nodes: Object.freeze(
      admittedNodes.map((node) =>
        Object.freeze({
          ...node,
          attributes: Object.freeze({ ...node.attributes }),
          sourceRecordId: record.recordId,
        } satisfies BrowserDomNodeSummary),
      ),
    ),
    sourceRecordIds: Object.freeze([record.recordId]),
    sequence: record.sequence,
  })
}

function dedupeSummaries(
  summaries: readonly BrowserDomSummary[],
): BrowserDomSummary[] {
  const output: BrowserDomSummary[] = []
  const seen = new Set<string>()
  for (const summary of summaries) {
    const key = [
      summary.browserSessionId,
      summary.stepId,
      summary.targetId,
      summary.frameId,
      summary.textDigest,
      summary.accessibilityDigest,
    ].join("\u0000")
    if (seen.has(key)) continue
    seen.add(key)
    output.push(summary)
  }
  return output.sort(
    (left, right) =>
      left.sequence - right.sequence
      || (left.stepId ?? "").localeCompare(right.stepId ?? ""),
  )
}

export function projectBrowserDom(
  scope: BrowserScope,
  input: readonly BrowserRecord[],
): readonly BrowserDomSummary[] {
  const summaries: BrowserDomSummary[] = []
  for (const record of input) {
    if (
      record.scope.browserSessionId !== scope.browserSessionId
      || record.scope.workerRequestId !== scope.workerRequestId
      || !likelyDomRecord(record)
    ) continue
    const summary = buildSummary(scope, record)
    if (
      summary.nodes.length
      || summary.nodeCount
      || summary.url
      || summary.title
    ) summaries.push(summary)
  }
  return Object.freeze(dedupeSummaries(summaries))
}

export function currentDomSummary(
  summaries: readonly BrowserDomSummary[],
  targetId?: string,
): BrowserDomSummary | undefined {
  return [...summaries]
    .filter((summary) => !targetId || !summary.targetId || summary.targetId === targetId)
    .sort(
      (left, right) =>
        right.sequence - left.sequence
        || right.nodeCount - left.nodeCount,
    )[0]
}

export function searchableDomText(summary: BrowserDomSummary): string {
  const parts: string[] = []
  for (const node of summary.nodes) {
    parts.push(
      [
        node.role,
        node.tag,
        node.name,
        node.text,
        node.value,
        node.description,
        node.selector,
      ]
        .filter(Boolean)
        .join(" "),
    )
  }
  return parts.join("\n")
}

export function domNodeByIdentity(
  summary: BrowserDomSummary,
  identity: string | number,
): BrowserDomNodeSummary | undefined {
  const wanted = String(identity)
  return summary.nodes.find(
    (node) =>
      node.nodeId === wanted
      || String(node.backendNodeId ?? "") === wanted
      || node.selector === wanted,
  )
}
