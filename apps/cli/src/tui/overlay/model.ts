export interface ProductOverlayRow {
  id: string
  label: string
  detail?: string
}

export interface ProductOverlay {
  title: string
  query?: string
  rows: readonly ProductOverlayRow[]
  selected: number
  footer?: string
}

export interface ProductPickerItem extends ProductOverlayRow {
  keywords?: readonly string[]
}
