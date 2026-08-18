// Helpers for the duplicated-bot pairing field in the strategy side panel.
// The field stores internal strategy ids separated by spaces (commas are
// accepted on input for convenience).  Values are unique positive integers.

export function parsePairedIds(raw: string): number[] {
  const seen = new Set<number>()
  for (const part of raw.split(/[\s,]+/)) {
    if (!/^\d+$/.test(part)) continue
    const value = Number(part)
    if (value > 0 && !seen.has(value)) seen.add(value)
  }
  return [...seen].sort((left, right) => left - right)
}

export function formatPairedIds(ids: number[]): string {
  return [...new Set(ids.filter(value => Number.isInteger(value) && value > 0))]
    .sort((left, right) => left - right)
    .join(' ')
}
