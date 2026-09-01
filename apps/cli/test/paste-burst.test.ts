import { describe, expect, test } from "bun:test"
import {
  PASTE_BURST_CHAR_INTERVAL_MS,
  PASTE_BURST_ENTER_WINDOW_MS,
  PASTE_BURST_IDLE_MS,
  PasteBurstDetector,
} from "../src/tui/paste-burst.ts"

describe("Windows paste burst detector", () => {
  test("holds one ASCII character briefly and then releases normal typing", () => {
    const detector = new PasteBurstDetector(1024)
    expect(detector.onCharacter("a", 10)).toEqual({ kind: "hold" })
    expect(detector.flushIfDue(10 + PASTE_BURST_CHAR_INTERVAL_MS)).toEqual({ kind: "none" })
    expect(detector.flushIfDue(11 + PASTE_BURST_CHAR_INTERVAL_MS)).toEqual({ kind: "typed", text: "a" })
  })

  test("buffers a rapid ASCII stream and prevents its Enter from submitting", () => {
    const detector = new PasteBurstDetector(1024)
    expect(detector.onCharacter("a", 0)).toEqual({ kind: "hold" })
    expect(detector.onCharacter("b", 1)).toEqual({ kind: "buffer" })
    expect(detector.onCharacter("c", 2)).toEqual({ kind: "buffer" })
    expect(detector.onNewline(3)).toBe("buffer")
    expect(detector.flushIfDue(3 + PASTE_BURST_IDLE_MS + 1)).toEqual({ kind: "paste", text: "abc\n" })
    expect(detector.onNewline(3 + PASTE_BURST_ENTER_WINDOW_MS)).toBe("insert")
    expect(detector.onNewline(4 + PASTE_BURST_ENTER_WINDOW_MS * 2)).toBe("submit")
  })

  test("does not hold the first IME character and exposes retro capture only after a burst", () => {
    const detector = new PasteBurstDetector(1024)
    expect(detector.onCharacter("中", 0)).toEqual({ kind: "insert" })
    expect(detector.onCharacter("文", 1)).toEqual({ kind: "insert" })
    expect(detector.onCharacter("输", 2)).toEqual({ kind: "retro", characters: 2 })
    detector.acceptRetroactive("中文", "输", 2)
    expect(detector.onCharacter("入", 3)).toEqual({ kind: "buffer" })
    expect(detector.flushIfDue(3 + PASTE_BURST_IDLE_MS + 1)).toEqual({ kind: "paste", text: "中文输入" })
  })

  test("bounds a detected paste before returning it to the composer", () => {
    const detector = new PasteBurstDetector(4)
    detector.onCharacter("a", 0)
    detector.onCharacter("b", 1)
    detector.onCharacter("c", 2)
    detector.onCharacter("d", 3)
    detector.onCharacter("e", 4)
    expect(detector.flushIfDue(4 + PASTE_BURST_IDLE_MS + 1)).toEqual({ kind: "overflow" })
  })
})
