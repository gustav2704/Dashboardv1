/**
 * A response may arrive after the user has changed a Monitoring filter. Keep
 * prior payloads for other views, but never render them under a new scope.
 */
export function isCurrentOverviewScope(appliedQuery: string, activeQuery: string) {
  return appliedQuery.length > 0 && appliedQuery === activeQuery
}
