import { createRoot } from "react-dom/client"
import { WorkbenchApp } from "./app/workbench-app.tsx"
import { createWorkbenchRuntime } from "./app/runtime.ts"

const rootElement = document.getElementById("root")
if (!rootElement) throw new TypeError("Zyra workbench root element is missing.")

const runtime = createWorkbenchRuntime()
createRoot(rootElement).render(
  <WorkbenchApp runtime={runtime} />,
)
