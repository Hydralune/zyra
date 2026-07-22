import type { TemporalConstraint } from "./types.ts";
import { uniqueStrings } from "./types.ts";

// Cropped from oh-my-pi Mnemopi core/temporal-parser.ts at c6b83c1d. Zyra
// converts point signals into explicit half-open UTC windows for index filters.

const DAY_MILLISECONDS = 86_400_000;

const DAY_MAP: Readonly<Record<string, number>> = {
  monday: 0,
  tuesday: 1,
  wednesday: 2,
  thursday: 3,
  friday: 4,
  saturday: 5,
  sunday: 6,
  mon: 0,
  tue: 1,
  wed: 2,
  thu: 3,
  fri: 4,
  sat: 5,
  sun: 6,
};

const MONTH_MAP: Readonly<Record<string, number>> = {
  january: 1,
  february: 2,
  march: 3,
  april: 4,
  may: 5,
  june: 6,
  july: 7,
  august: 8,
  september: 9,
  october: 10,
  november: 11,
  december: 12,
  jan: 1,
  feb: 2,
  mar: 3,
  apr: 4,
  jun: 6,
  jul: 7,
  aug: 8,
  sep: 9,
  oct: 10,
  nov: 11,
  dec: 12,
};

const EMPTY_TEMPORAL: TemporalConstraint = {
  startAt: "",
  endAt: "",
  tags: [],
  precision: "unknown",
  sourceText: "",
};

function referenceDate(value: string): Date {
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) throw new Error("requestTime must be a valid timestamp");
  return parsed;
}

function dateUtc(year: number, month: number, day: number): Date | null {
  const value = new Date(Date.UTC(year, month - 1, day));
  if (value.getUTCFullYear() !== year) return null;
  if (value.getUTCMonth() !== month - 1) return null;
  if (value.getUTCDate() !== day) return null;
  return value;
}

function dayStart(value: Date): Date {
  return new Date(Date.UTC(value.getUTCFullYear(), value.getUTCMonth(), value.getUTCDate()));
}

function addDays(value: Date, days: number): Date {
  return new Date(dayStart(value).getTime() + days * DAY_MILLISECONDS);
}

function addSeconds(value: Date, seconds: number): Date {
  return new Date(value.getTime() + seconds * 1000);
}

function isoDay(value: Date): string {
  return value.toISOString().slice(0, 10);
}

function weekday(value: Date): number {
  return (value.getUTCDay() + 6) % 7;
}

function isoWeek(value: Date): number {
  const cursor = dayStart(value);
  cursor.setUTCDate(cursor.getUTCDate() + 4 - (cursor.getUTCDay() || 7));
  const yearStart = new Date(Date.UTC(cursor.getUTCFullYear(), 0, 1));
  return Math.ceil(((cursor.getTime() - yearStart.getTime()) / DAY_MILLISECONDS + 1) / 7);
}

function weekStart(value: Date): Date {
  return addDays(value, -weekday(value));
}

function monthStart(value: Date): Date {
  return new Date(Date.UTC(value.getUTCFullYear(), value.getUTCMonth(), 1));
}

function nextMonth(value: Date): Date {
  return new Date(Date.UTC(value.getUTCFullYear(), value.getUTCMonth() + 1, 1));
}

function yearStart(value: Date): Date {
  return new Date(Date.UTC(value.getUTCFullYear(), 0, 1));
}

function nextYear(value: Date): Date {
  return new Date(Date.UTC(value.getUTCFullYear() + 1, 0, 1));
}

function dayName(value: Date): string {
  const names = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"];
  return names[value.getUTCDay()] ?? "";
}

function dayTags(value: Date, ...extra: string[]): string[] {
  return uniqueStrings([
    isoDay(value),
    `week-${isoWeek(value)}-${value.getUTCFullYear()}`,
    dayName(value),
    ...extra,
  ]);
}

function constraint(
  start: Date,
  end: Date,
  precision: TemporalConstraint["precision"],
  sourceText: string,
  tags: readonly string[],
): TemporalConstraint {
  return {
    startAt: start.toISOString(),
    endAt: end.toISOString(),
    tags: uniqueStrings([...tags]),
    precision,
    sourceText,
  };
}

function dayConstraint(value: Date, sourceText: string, tags: readonly string[] = []): TemporalConstraint {
  const start = dayStart(value);
  return constraint(start, addDays(start, 1), "day", sourceText, dayTags(start, ...tags));
}

function monthShift(reference: Date, offset: number): Date {
  return new Date(Date.UTC(reference.getUTCFullYear(), reference.getUTCMonth() + offset, 1));
}

function resolveWeekday(reference: Date, target: number, qualifier: string): Date {
  const current = weekday(reference);
  if (qualifier === "last") return addDays(reference, -(((current - target + 7) % 7) + 7));
  if (qualifier === "next") return addDays(reference, ((target - current + 7) % 7) || 7);
  return addDays(reference, -((current - target + 7) % 7));
}

function relativeDate(reference: Date, count: number, unit: string, direction: -1 | 1): Date | null {
  const seconds: Readonly<Record<string, number>> = {
    second: 1,
    minute: 60,
    hour: 3_600,
    day: 86_400,
    week: 604_800,
    month: 2_592_000,
    year: 31_536_000,
  };
  const multiplier = seconds[unit];
  if (multiplier === undefined || !Number.isSafeInteger(count)) return null;
  const value = addSeconds(reference, direction * count * multiplier);
  return Number.isFinite(value.getTime()) ? value : null;
}

export function parseTemporalConstraint(text: string, requestTime: string): TemporalConstraint {
  const reference = referenceDate(requestTime);
  const normalized = text.normalize("NFKC").replace(/\0/g, " ").replace(/\s+/g, " ").trim();
  const lower = normalized.toLowerCase();
  let match = /\b(\d{4})-(\d{2})-(\d{2})\b/.exec(normalized);
  if (match) {
    const value = dateUtc(Number(match[1]), Number(match[2]), Number(match[3]));
    if (value) return dayConstraint(value, match[0]);
  }
  match = /\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b/.exec(normalized);
  if (match) {
    const first = Number(match[1]);
    const second = Number(match[2]);
    const year = Number(match[3]) + (Number(match[3]) < 100 ? 2000 : 0);
    const value = first > 12 ? dateUtc(year, second, first) : dateUtc(year, first, second);
    if (value) return dayConstraint(value, match[0]);
  }
  match = /\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?\b/.exec(lower);
  if (match) {
    const month = MONTH_MAP[match[1] ?? ""] ?? 0;
    const day = Number(match[2]);
    const year = match[3] ? Number(match[3]) : reference.getUTCFullYear();
    const value = dateUtc(year, month, day);
    if (value) return dayConstraint(value, match[0]);
  }
  if (/\bday\s+before\s+yesterday\b/.test(lower)) {
    return dayConstraint(addDays(reference, -2), "day before yesterday", ["day-before-yesterday"]);
  }
  if (/\byesterday\b/.test(lower)) return dayConstraint(addDays(reference, -1), "yesterday", ["yesterday"]);
  if (/\btoday\b/.test(lower)) return dayConstraint(reference, "today", ["today"]);
  if (/\btomorrow\b/.test(lower)) return dayConstraint(addDays(reference, 1), "tomorrow", ["tomorrow"]);
  match = /\b(last|this|next)\s+(week|month|year)\b/.exec(lower);
  if (match) {
    const qualifier = match[1] ?? "this";
    const unit = match[2] ?? "week";
    const shift = qualifier === "last" ? -1 : qualifier === "next" ? 1 : 0;
    if (unit === "week") {
      const start = weekStart(addDays(reference, shift * 7));
      return constraint(start, addDays(start, 7), "week", match[0], [`${qualifier}-week`, `week-${isoWeek(start)}-${start.getUTCFullYear()}`]);
    }
    if (unit === "month") {
      const start = monthShift(reference, shift);
      return constraint(start, nextMonth(start), "month", match[0], [`${qualifier}-month`, start.toISOString().slice(0, 7)]);
    }
    const start = new Date(Date.UTC(reference.getUTCFullYear() + shift, 0, 1));
    return constraint(start, nextYear(start), "year", match[0], [`${qualifier}-year`, String(start.getUTCFullYear())]);
  }
  match = /\b(?:(last|this|next)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)\b/.exec(lower);
  if (match) {
    const qualifier = match[1] ?? "this";
    const name = match[2] ?? "monday";
    const target = DAY_MAP[name];
    if (target !== undefined) return dayConstraint(resolveWeekday(reference, target, qualifier), match[0], [name, qualifier]);
  }
  match = /\b(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+(ago|before|earlier|back|later|from\s+now)\b/.exec(lower);
  if (match) {
    const count = Number(match[1]);
    const unit = match[2] ?? "day";
    const direction = /later|from\s+now/.test(match[3] ?? "") ? 1 : -1;
    const value = relativeDate(reference, count, unit, direction);
    if (value) return dayConstraint(value, match[0], [`${count}-${unit}-${direction < 0 ? "ago" : "ahead"}`]);
  }
  match = /\bin\s+(\d+)\s+(second|minute|hour|day|week|month|year)s?\b/.exec(lower);
  if (match) {
    const count = Number(match[1]);
    const unit = match[2] ?? "day";
    const value = relativeDate(reference, count, unit, 1);
    if (value) return dayConstraint(value, match[0], [`in-${count}-${unit}`]);
  }
  if (/\b(recent|recently|latest|lately|not\s+long\s+ago)\b/.test(lower)) {
    return constraint(addDays(reference, -7), new Date(reference.getTime() + 1000), "relative", "recent", ["recent"]);
  }
  if (/\b(a\s+while\s+ago|some\s+time\s+ago|long\s+ago)\b/.test(lower)) {
    return constraint(addDays(reference, -365), new Date(reference.getTime() + 1000), "relative", "vague past", ["vague"]);
  }
  return EMPTY_TEMPORAL;
}

export function temporalActive(value: TemporalConstraint): boolean {
  return Boolean(value.startAt || value.endAt || value.tags.length > 0);
}

export function temporalMatches(eventAt: string, constraintValue: TemporalConstraint): boolean {
  if (!temporalActive(constraintValue)) return true;
  if (!eventAt) return false;
  const timestamp = Date.parse(eventAt);
  if (!Number.isFinite(timestamp)) return false;
  if (constraintValue.startAt && timestamp < Date.parse(constraintValue.startAt)) return false;
  if (constraintValue.endAt && timestamp >= Date.parse(constraintValue.endAt)) return false;
  return true;
}

export function temporalRecency(eventAt: string, requestTime: string, halfLifeDays = 30): number {
  const event = Date.parse(eventAt);
  const reference = Date.parse(requestTime);
  if (!Number.isFinite(event) || !Number.isFinite(reference) || halfLifeDays <= 0) return 0;
  const age = Math.max(0, reference - event);
  return Math.exp((-Math.log(2) * age) / (halfLifeDays * DAY_MILLISECONDS));
}
