export type AuditFilters = {
  actor: string
  action: string
  resourceType: string
  since: string
  until: string
}

export const emptyFilters: AuditFilters = { actor: '', action: '', resourceType: '', since: '', until: '' }

export type AuditEvent = {
  id: number
  actorId: string | null
  action: string
  resourceType: string
  resourceId: string
  details: Record<string, unknown>
  createdAt: string
}

/** Query string for /audit-events; empty filters are left out, dates become ISO instants. */
export function auditQuery(filters: AuditFilters, cursor?: number | null, limit = 50): string {
  const params = new URLSearchParams()
  if (filters.actor.trim()) params.set('actor_id', filters.actor.trim())
  if (filters.action.trim()) params.set('action', filters.action.trim())
  if (filters.resourceType.trim()) params.set('resource_type', filters.resourceType.trim())
  if (filters.since) params.set('since', new Date(filters.since).toISOString())
  if (filters.until) params.set('until', new Date(filters.until).toISOString())
  if (cursor) params.set('cursor', String(cursor))
  params.set('limit', String(limit))
  return params.toString()
}

/** The same filters without paging, for the CSV export. */
export function exportQuery(filters: AuditFilters): string {
  const params = new URLSearchParams(auditQuery(filters))
  params.delete('limit')
  return params.toString()
}

/** Rows to render for a scroll position: the visible slice plus a little overscan. */
export function visibleWindow(
  total: number,
  scrollTop: number,
  viewportHeight: number,
  rowHeight: number,
  overscan = 6,
): { start: number; end: number } {
  const first = Math.floor(Math.max(0, scrollTop) / rowHeight)
  const count = Math.ceil(viewportHeight / rowHeight)
  return {
    start: Math.max(0, first - overscan),
    end: Math.min(total, first + count + overscan),
  }
}

/** Why a filter combination cannot be sent, or null when it is fine. */
export function filterProblem(filters: AuditFilters): string | null {
  if (filters.since && filters.until && new Date(filters.since) >= new Date(filters.until)) {
    return '"From" must be earlier than "To".'
  }
  const actor = filters.actor.trim()
  if (actor && !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(actor)) {
    return 'The actor filter takes an account id (a UUID).'
  }
  return null
}
