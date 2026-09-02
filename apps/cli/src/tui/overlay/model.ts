export interface ProductOverlayRow {
  id: string
  label: string
  detail?: string
  tone?: "default" | "secondary" | "success" | "error" | "accent"
}

export interface ProductOverlay {
  kind?: "completion" | "picker" | "menu" | "approval" | "question" | "pager"
  title: string
  description?: readonly string[]
  query?: string
  rows: readonly ProductOverlayRow[]
  selected: number
  footer?: string
}

export interface ProductPickerItem extends ProductOverlayRow {
  keywords?: readonly string[]
}
