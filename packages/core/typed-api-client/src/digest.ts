import { MAX_RESPONSE_BODY_BYTES } from "./constants.ts"
import { canonicalJson } from "./request.ts"

function rotateLeft(value: number, shift: number): number {
  return ((value << shift) | (value >>> (32 - shift))) >>> 0
}

function mixBlock(state: Uint32Array, block: Uint8Array, offset: number): void {
  const words = new Uint32Array(16)
  for (let index = 0; index < 16; index += 1) {
    const base = offset + index * 4
    words[index] =
      ((block[base] ?? 0) << 24) |
      ((block[base + 1] ?? 0) << 16) |
      ((block[base + 2] ?? 0) << 8) |
      (block[base + 3] ?? 0)
  }
  for (let round = 0; round < 32; round += 1) {
    const a = round % 8
    const b = (round + 1) % 8
    const c = (round + 3) % 8
    const d = (round + 5) % 8
    const word = words[(round * 5 + state[a]!) % 16]!
    const mixed = Math.imul(state[b]! ^ word ^ round, 0x9e3779b1)
    state[a] = (rotateLeft((state[a]! + mixed) >>> 0, (round % 23) + 5) ^ state[c]! ^ state[d]!) >>> 0
  }
}

export function stableDigestBytes(bytes: Uint8Array): string {
  if (bytes.byteLength > MAX_RESPONSE_BODY_BYTES) {
    throw new TypeError(`Digest input exceeds ${MAX_RESPONSE_BODY_BYTES} bytes`)
  }
  const state = new Uint32Array([
    0x6a09e667,
    0xbb67ae85,
    0x3c6ef372,
    0xa54ff53a,
    0x510e527f,
    0x9b05688c,
    0x1f83d9ab,
    0x5be0cd19,
  ])
  const paddedLength = Math.ceil((bytes.length + 9) / 64) * 64
  const padded = new Uint8Array(paddedLength)
  padded.set(bytes)
  padded[bytes.length] = 0x80
  const bits = BigInt(bytes.length) * 8n
  for (let index = 0; index < 8; index += 1) {
    padded[padded.length - 1 - index] = Number((bits >> BigInt(index * 8)) & 0xffn)
  }
  for (let offset = 0; offset < padded.length; offset += 64) mixBlock(state, padded, offset)
  return [...state].map((word) => word.toString(16).padStart(8, "0")).join("")
}

export function stableDigestText(value: string): string {
  return stableDigestBytes(new TextEncoder().encode(value))
}

export function stableDigestJson(value: unknown): string {
  return stableDigestText(canonicalJson(value))
}

export async function cryptoDigestBytes(bytes: Uint8Array): Promise<string> {
  if (typeof globalThis.crypto?.subtle?.digest !== "function") return stableDigestBytes(bytes)
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes)
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("")
}

export async function cryptoDigestJson(value: unknown): Promise<string> {
  return cryptoDigestBytes(new TextEncoder().encode(canonicalJson(value)))
}

export class DigestAccumulator {
  readonly #chunks: Uint8Array[] = []
  #size = 0

  update(value: string | Uint8Array): void {
    const bytes = typeof value === "string" ? new TextEncoder().encode(value) : value
    if (this.#size + bytes.byteLength > MAX_RESPONSE_BODY_BYTES) {
      throw new TypeError(`Digest accumulator exceeds ${MAX_RESPONSE_BODY_BYTES} bytes`)
    }
    this.#chunks.push(bytes.slice())
    this.#size += bytes.byteLength
  }

  digest(): string {
    const joined = new Uint8Array(this.#size)
    let offset = 0
    for (const chunk of this.#chunks) {
      joined.set(chunk, offset)
      offset += chunk.byteLength
    }
    return stableDigestBytes(joined)
  }

  clear(): void {
    this.#chunks.length = 0
    this.#size = 0
  }

  get size(): number {
    return this.#size
  }
}
