import type { Insets, Point, Rect, Size, Transform } from "./contracts.ts"

const EPSILON = 1e-9

export function finite(value: number, fallback = 0): number {
  return Number.isFinite(value) ? value : fallback
}
export function clamp(value: number, minimum: number, maximum: number): number {
  if (minimum > maximum) return clamp(value, maximum, minimum)
  return Math.min(maximum, Math.max(minimum, finite(value, minimum)))
}

export function lerp(start: number, end: number, amount: number): number {
  const t = clamp(amount, 0, 1)
  return start + (end - start) * t
}

export function roundTo(value: number, precision = 3): number {
  if (!Number.isFinite(value)) return 0
  const factor = 10 ** clamp(Math.floor(precision), 0, 12)
  return Math.round(value * factor) / factor
}

export function point(x = 0, y = 0): Point {
  return Object.freeze({ x: finite(x), y: finite(y) })
}

export function size(width = 0, height = 0): Size {
  return Object.freeze({
    width: Math.max(0, finite(width)),
    height: Math.max(0, finite(height)),
  })
}

export function rect(x = 0, y = 0, width = 0, height = 0): Rect {
  return Object.freeze({
    x: finite(x),
    y: finite(y),
    width: Math.max(0, finite(width)),
    height: Math.max(0, finite(height)),
  })
}

export function normalizeRect(value: Rect): Rect {
  const x1 = Math.min(value.x, value.x + value.width)
  const x2 = Math.max(value.x, value.x + value.width)
  const y1 = Math.min(value.y, value.y + value.height)
  const y2 = Math.max(value.y, value.y + value.height)
  return rect(x1, y1, x2 - x1, y2 - y1)
}

export function centerOf(value: Rect): Point {
  return point(value.x + value.width / 2, value.y + value.height / 2)
}

export function rectFromCenter(center: Point, dimensions: Size): Rect {
  return rect(
    center.x - dimensions.width / 2,
    center.y - dimensions.height / 2,
    dimensions.width,
    dimensions.height,
  )
}

export function rectFromPoints(values: readonly Point[], padding = 0): Rect {
  if (values.length === 0) return rect()
  let minimumX = Number.POSITIVE_INFINITY
  let minimumY = Number.POSITIVE_INFINITY
  let maximumX = Number.NEGATIVE_INFINITY
  let maximumY = Number.NEGATIVE_INFINITY
  for (const value of values) {
    minimumX = Math.min(minimumX, finite(value.x))
    minimumY = Math.min(minimumY, finite(value.y))
    maximumX = Math.max(maximumX, finite(value.x))
    maximumY = Math.max(maximumY, finite(value.y))
  }
  const inset = Math.max(0, finite(padding))
  return rect(
    minimumX - inset,
    minimumY - inset,
    maximumX - minimumX + inset * 2,
    maximumY - minimumY + inset * 2,
  )
}

export function unionRect(left: Rect, right: Rect): Rect {
  if (left.width === 0 && left.height === 0) return normalizeRect(right)
  if (right.width === 0 && right.height === 0) return normalizeRect(left)
  const a = normalizeRect(left)
  const b = normalizeRect(right)
  const x = Math.min(a.x, b.x)
  const y = Math.min(a.y, b.y)
  return rect(
    x,
    y,
    Math.max(a.x + a.width, b.x + b.width) - x,
    Math.max(a.y + a.height, b.y + b.height) - y,
  )
}

export function unionRects(values: readonly Rect[]): Rect {
  if (values.length === 0) return rect()
  let result = normalizeRect(values[0]!)
  for (const value of values.slice(1)) result = unionRect(result, value)
  return result
}

export function expandRect(value: Rect, amount: number | Partial<Insets>): Rect {
  const normalized = normalizeRect(value)
  const insets =
    typeof amount === "number"
      ? {
          top: Math.max(0, finite(amount)),
          right: Math.max(0, finite(amount)),
          bottom: Math.max(0, finite(amount)),
          left: Math.max(0, finite(amount)),
        }
      : {
          top: Math.max(0, finite(amount.top ?? 0)),
          right: Math.max(0, finite(amount.right ?? 0)),
          bottom: Math.max(0, finite(amount.bottom ?? 0)),
          left: Math.max(0, finite(amount.left ?? 0)),
        }
  return rect(
    normalized.x - insets.left,
    normalized.y - insets.top,
    normalized.width + insets.left + insets.right,
    normalized.height + insets.top + insets.bottom,
  )
}

export function insetRect(value: Rect, amount: number | Partial<Insets>): Rect {
  const normalized = normalizeRect(value)
  const insets =
    typeof amount === "number"
      ? { top: amount, right: amount, bottom: amount, left: amount }
      : {
          top: amount.top ?? 0,
          right: amount.right ?? 0,
          bottom: amount.bottom ?? 0,
          left: amount.left ?? 0,
        }
  const left = clamp(finite(insets.left), 0, normalized.width / 2)
  const right = clamp(finite(insets.right), 0, normalized.width / 2)
  const top = clamp(finite(insets.top), 0, normalized.height / 2)
  const bottom = clamp(finite(insets.bottom), 0, normalized.height / 2)
  return rect(
    normalized.x + left,
    normalized.y + top,
    Math.max(0, normalized.width - left - right),
    Math.max(0, normalized.height - top - bottom),
  )
}

export function translateRect(value: Rect, delta: Point): Rect {
  return rect(value.x + delta.x, value.y + delta.y, value.width, value.height)
}

export function scaleRect(value: Rect, scale: number, origin = centerOf(value)): Rect {
  const amount = Math.max(EPSILON, finite(scale, 1))
  const start = subtract(point(value.x, value.y), origin)
  return rect(
    origin.x + start.x * amount,
    origin.y + start.y * amount,
    value.width * amount,
    value.height * amount,
  )
}

export function containsPoint(value: Rect, target: Point): boolean {
  const normalized = normalizeRect(value)
  return (
    target.x >= normalized.x &&
    target.x <= normalized.x + normalized.width &&
    target.y >= normalized.y &&
    target.y <= normalized.y + normalized.height
  )
}

export function containsRect(outer: Rect, inner: Rect): boolean {
  const a = normalizeRect(outer)
  const b = normalizeRect(inner)
  return (
    b.x >= a.x &&
    b.y >= a.y &&
    b.x + b.width <= a.x + a.width &&
    b.y + b.height <= a.y + a.height
  )
}

export function intersectsRect(left: Rect, right: Rect): boolean {
  const a = normalizeRect(left)
  const b = normalizeRect(right)
  return !(
    a.x + a.width < b.x ||
    b.x + b.width < a.x ||
    a.y + a.height < b.y ||
    b.y + b.height < a.y
  )
}

export function intersectionRect(left: Rect, right: Rect): Rect | undefined {
  if (!intersectsRect(left, right)) return undefined
  const a = normalizeRect(left)
  const b = normalizeRect(right)
  const x = Math.max(a.x, b.x)
  const y = Math.max(a.y, b.y)
  return rect(
    x,
    y,
    Math.max(0, Math.min(a.x + a.width, b.x + b.width) - x),
    Math.max(0, Math.min(a.y + a.height, b.y + b.height) - y),
  )
}

export function rectArea(value: Rect): number {
  const normalized = normalizeRect(value)
  return normalized.width * normalized.height
}

export function intersectionArea(left: Rect, right: Rect): number {
  const intersection = intersectionRect(left, right)
  return intersection ? rectArea(intersection) : 0
}

export function overlapRatio(left: Rect, right: Rect): number {
  const overlap = intersectionArea(left, right)
  if (overlap <= 0) return 0
  const smallest = Math.min(rectArea(left), rectArea(right))
  return smallest <= EPSILON ? 0 : overlap / smallest
}

export function add(left: Point, right: Point): Point {
  return point(left.x + right.x, left.y + right.y)
}

export function subtract(left: Point, right: Point): Point {
  return point(left.x - right.x, left.y - right.y)
}

export function multiply(value: Point, amount: number): Point {
  const scale = finite(amount)
  return point(value.x * scale, value.y * scale)
}

export function divide(value: Point, amount: number): Point {
  const divisor = Math.abs(amount) <= EPSILON ? 1 : finite(amount, 1)
  return point(value.x / divisor, value.y / divisor)
}

export function dot(left: Point, right: Point): number {
  return left.x * right.x + left.y * right.y
}

export function cross(left: Point, right: Point): number {
  return left.x * right.y - left.y * right.x
}

export function magnitudeSquared(value: Point): number {
  return value.x * value.x + value.y * value.y
}

export function magnitude(value: Point): number {
  return Math.sqrt(magnitudeSquared(value))
}

export function distanceSquared(left: Point, right: Point): number {
  return magnitudeSquared(subtract(left, right))
}

export function distance(left: Point, right: Point): number {
  return Math.sqrt(distanceSquared(left, right))
}

export function normalizeVector(value: Point): Point {
  const length = magnitude(value)
  return length <= EPSILON ? point() : divide(value, length)
}

export function perpendicular(value: Point): Point {
  return point(-value.y, value.x)
}

export function angle(value: Point): number {
  return Math.atan2(value.y, value.x)
}

export function angleBetween(left: Point, right: Point): number {
  const denominator = magnitude(left) * magnitude(right)
  if (denominator <= EPSILON) return 0
  return Math.acos(clamp(dot(left, right) / denominator, -1, 1))
}

export function rotate(value: Point, radians: number, origin: Point = point()): Point {
  const delta = subtract(value, origin)
  const cosine = Math.cos(finite(radians))
  const sine = Math.sin(finite(radians))
  return point(
    origin.x + delta.x * cosine - delta.y * sine,
    origin.y + delta.x * sine + delta.y * cosine,
  )
}

export function midpoint(left: Point, right: Point): Point {
  return point((left.x + right.x) / 2, (left.y + right.y) / 2)
}

export function interpolatePoint(left: Point, right: Point, amount: number): Point {
  return point(lerp(left.x, right.x, amount), lerp(left.y, right.y, amount))
}

export function closestPointOnSegment(target: Point, start: Point, end: Point): Point {
  const segment = subtract(end, start)
  const length = magnitudeSquared(segment)
  if (length <= EPSILON) return start
  const amount = clamp(dot(subtract(target, start), segment) / length, 0, 1)
  return add(start, multiply(segment, amount))
}

export function distanceToSegment(target: Point, start: Point, end: Point): number {
  return distance(target, closestPointOnSegment(target, start, end))
}

export function polylineLength(values: readonly Point[]): number {
  let result = 0
  for (let index = 1; index < values.length; index += 1) {
    result += distance(values[index - 1]!, values[index]!)
  }
  return result
}

export function polylineBounds(values: readonly Point[], width = 0): Rect {
  return rectFromPoints(values, Math.max(0, width) / 2)
}

export function closestPointOnPolyline(
  target: Point,
  values: readonly Point[],
): { point: Point; distance: number; segment: number } {
  if (values.length === 0) return { point: target, distance: Number.POSITIVE_INFINITY, segment: -1 }
  if (values.length === 1) return { point: values[0]!, distance: distance(target, values[0]!), segment: 0 }
  let bestPoint = values[0]!
  let bestDistance = Number.POSITIVE_INFINITY
  let bestSegment = 0
  for (let index = 1; index < values.length; index += 1) {
    const candidate = closestPointOnSegment(target, values[index - 1]!, values[index]!)
    const candidateDistance = distanceSquared(target, candidate)
    if (candidateDistance < bestDistance) {
      bestPoint = candidate
      bestDistance = candidateDistance
      bestSegment = index - 1
    }
  }
  return { point: bestPoint, distance: Math.sqrt(bestDistance), segment: bestSegment }
}

export function screenToGraph(value: Point, transform: Transform): Point {
  const scale = Math.max(EPSILON, finite(transform.scale, 1))
  return point((value.x - transform.x) / scale, (value.y - transform.y) / scale)
}

export function graphToScreen(value: Point, transform: Transform): Point {
  const scale = Math.max(EPSILON, finite(transform.scale, 1))
  return point(value.x * scale + transform.x, value.y * scale + transform.y)
}

export function screenRectToGraph(value: Rect, transform: Transform): Rect {
  const start = screenToGraph(point(value.x, value.y), transform)
  const end = screenToGraph(point(value.x + value.width, value.y + value.height), transform)
  return rect(start.x, start.y, end.x - start.x, end.y - start.y)
}

export function graphRectToScreen(value: Rect, transform: Transform): Rect {
  const start = graphToScreen(point(value.x, value.y), transform)
  const end = graphToScreen(point(value.x + value.width, value.y + value.height), transform)
  return rect(start.x, start.y, end.x - start.x, end.y - start.y)
}

export function fitTransform(
  graphBounds: Rect,
  viewport: Rect,
  padding = 32,
  minimumScale = 0.05,
  maximumScale = 4,
): Transform {
  const available = insetRect(viewport, padding)
  const bounds = normalizeRect(graphBounds)
  if (bounds.width <= EPSILON || bounds.height <= EPSILON) {
    return Object.freeze({ x: available.x, y: available.y, scale: 1 })
  }
  const scale = clamp(
    Math.min(available.width / bounds.width, available.height / bounds.height),
    minimumScale,
    maximumScale,
  )
  return Object.freeze({
    x: available.x + (available.width - bounds.width * scale) / 2 - bounds.x * scale,
    y: available.y + (available.height - bounds.height * scale) / 2 - bounds.y * scale,
    scale,
  })
}

export function zoomTransformAt(
  transform: Transform,
  screenAnchor: Point,
  nextScale: number,
  minimumScale: number,
  maximumScale: number,
): Transform {
  const clamped = clamp(nextScale, minimumScale, maximumScale)
  const graphAnchor = screenToGraph(screenAnchor, transform)
  return Object.freeze({
    x: screenAnchor.x - graphAnchor.x * clamped,
    y: screenAnchor.y - graphAnchor.y * clamped,
    scale: clamped,
  })
}

export function panTransform(transform: Transform, delta: Point): Transform {
  return Object.freeze({
    x: finite(transform.x + delta.x),
    y: finite(transform.y + delta.y),
    scale: Math.max(EPSILON, finite(transform.scale, 1)),
  })
}

export function clampTransformToBounds(
  transform: Transform,
  graphBounds: Rect,
  viewport: Rect,
  padding = 80,
): Transform {
  const screen = graphRectToScreen(graphBounds, transform)
  const expandedViewport = expandRect(viewport, padding)
  let x = transform.x
  let y = transform.y
  if (screen.width <= viewport.width) {
    x += viewport.x + viewport.width / 2 - (screen.x + screen.width / 2)
  } else {
    if (screen.x > expandedViewport.x) x -= screen.x - expandedViewport.x
    if (screen.x + screen.width < expandedViewport.x + expandedViewport.width) {
      x += expandedViewport.x + expandedViewport.width - (screen.x + screen.width)
    }
  }
  if (screen.height <= viewport.height) {
    y += viewport.y + viewport.height / 2 - (screen.y + screen.height / 2)
  } else {
    if (screen.y > expandedViewport.y) y -= screen.y - expandedViewport.y
    if (screen.y + screen.height < expandedViewport.y + expandedViewport.height) {
      y += expandedViewport.y + expandedViewport.height - (screen.y + screen.height)
    }
  }
  return Object.freeze({ x: finite(x), y: finite(y), scale: transform.scale })
}

export function snap(value: number, grid: number): number {
  const step = Math.max(EPSILON, Math.abs(finite(grid, 1)))
  return Math.round(value / step) * step
}

export function snapPoint(value: Point, grid: number): Point {
  return point(snap(value.x, grid), snap(value.y, grid))
}

export function stableHash(value: string): number {
  let hash = 2166136261
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return hash >>> 0
}

export function deterministicUnit(value: string): number {
  return stableHash(value) / 0xffffffff
}

export function deterministicJitter(value: string, radius: number): Point {
  const x = deterministicUnit(`${value}:x`) * 2 - 1
  const y = deterministicUnit(`${value}:y`) * 2 - 1
  const direction = normalizeVector(point(x, y))
  const scale = Math.max(0, finite(radius)) * (0.35 + deterministicUnit(`${value}:r`) * 0.65)
  return multiply(direction, scale)
}

export function spiralPoint(index: number, spacing = 32, origin = point()): Point {
  const safeIndex = Math.max(0, Math.floor(finite(index)))
  const goldenAngle = Math.PI * (3 - Math.sqrt(5))
  const radius = Math.sqrt(safeIndex) * Math.max(1, finite(spacing, 32))
  const theta = safeIndex * goldenAngle
  return point(
    origin.x + Math.cos(theta) * radius,
    origin.y + Math.sin(theta) * radius,
  )
}

export function gridCell(value: Point, cellSize: number): { column: number; row: number } {
  const scale = Math.max(EPSILON, Math.abs(finite(cellSize, 1)))
  return {
    column: Math.floor(value.x / scale),
    row: Math.floor(value.y / scale),
  }
}

export function rectCells(
  value: Rect,
  cellSize: number,
  maximum = 100_000,
): readonly string[] {
  const normalized = normalizeRect(value)
  const start = gridCell(point(normalized.x, normalized.y), cellSize)
  const end = gridCell(
    point(normalized.x + normalized.width, normalized.y + normalized.height),
    cellSize,
  )
  const result: string[] = []
  for (let row = start.row; row <= end.row; row += 1) {
    for (let column = start.column; column <= end.column; column += 1) {
      result.push(`${column}:${row}`)
      if (result.length >= maximum) return Object.freeze(result)
    }
  }
  return Object.freeze(result)
}

export function comparePoints(left: Point, right: Point): number {
  return left.y - right.y || left.x - right.x
}

export function approximatelyEqual(left: number, right: number, epsilon = 0.001): boolean {
  return Math.abs(left - right) <= Math.max(EPSILON, Math.abs(epsilon))
}

export function samePoint(left: Point, right: Point, epsilon = 0.001): boolean {
  return approximatelyEqual(left.x, right.x, epsilon) && approximatelyEqual(left.y, right.y, epsilon)
}

export function sameRect(left: Rect, right: Rect, epsilon = 0.001): boolean {
  return (
    samePoint(left, right, epsilon) &&
    approximatelyEqual(left.width, right.width, epsilon) &&
    approximatelyEqual(left.height, right.height, epsilon)
  )
}

export function quantizeTransform(value: Transform): Transform {
  return Object.freeze({
    x: roundTo(value.x, 2),
    y: roundTo(value.y, 2),
    scale: roundTo(value.scale, 4),
  })
}
