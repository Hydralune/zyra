import { resolve } from "node:path"
import { admitPolicyEvidencePage } from "../apps/web/src/api/policy-api.ts"

const selected = process.argv[2]
if (!selected) throw new Error("policy evidence page path is required")
const path = resolve(selected)
const raw = JSON.parse(await Bun.file(path).text())
const page = await admitPolicyEvidencePage(raw)
if (page.metric_report.status !== "verified") {
  throw new Error("production metric report is not Web-admitted as verified")
}
if (!page.transitions.some((item) => (
  item.contract_kind === "physical_dispatch_receipt"
  && item.execution === "real"
  && item.integrity === "verified"
))) {
  throw new Error("production physical receipt is absent from Web admission")
}
if (!page.transitions.some((item) => (
  item.contract_kind === "memory_continuity_receipt"
  && item.integrity === "verified"
))) {
  throw new Error("production continuity receipt is absent from Web admission")
}
if (!page.transitions.some((item) => (
  item.contract_kind === "neuro_symbolic_evidence_bundle"
  && item.integrity === "verified"
))) {
  throw new Error("production symbolic bundle is absent from Web admission")
}
console.log(JSON.stringify({
  status: "passed",
  transition_count: page.transition_count,
  evidence_digest: page.evidence_digest,
  report_digest: String(page.metric_report.digest || ""),
}))
