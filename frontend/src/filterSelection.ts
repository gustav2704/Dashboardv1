export function sameOptionalSet(left: Set<string> | null, right: Set<string> | null) {
  if (left === null || right === null) return left === right
  return left.size === right.size && [...left].every(value => right.has(value))
}

export function normalizeFilterSelection(values: string[], selection: Set<string>) {
  return values.length > 0 && values.every(value => selection.has(value))
    ? null
    : new Set(selection)
}
