import { describe, expect, it } from 'vitest'

import { auditQuery, emptyFilters, exportQuery, filterProblem, visibleWindow } from './auditLogic'

describe('auditQuery', () => {
  it('leaves empty filters out and always sends a limit', () => {
    expect(auditQuery(emptyFilters)).toBe('limit=50')
  })

  it('sends set filters, the cursor and ISO dates', () => {
    const query = new URLSearchParams(
      auditQuery({ ...emptyFilters, action: 'login', since: '2026-10-01T00:00' }, 120, 25),
    )
    expect(query.get('action')).toBe('login')
    expect(query.get('cursor')).toBe('120')
    expect(query.get('limit')).toBe('25')
    expect(query.get('since')).toMatch(/^\d{4}-\d{2}-\d{2}T.*Z$/)
  })

  it('drops paging from the export query', () => {
    const query = new URLSearchParams(exportQuery({ ...emptyFilters, action: 'login' }))
    expect(query.get('action')).toBe('login')
    expect(query.has('limit')).toBe(false)
  })
})

describe('visibleWindow', () => {
  it('renders only the slice in view plus overscan', () => {
    expect(visibleWindow(10_000, 0, 400, 40, 2)).toEqual({ start: 0, end: 12 })
    expect(visibleWindow(10_000, 4000, 400, 40, 2)).toEqual({ start: 98, end: 112 })
  })

  it('never goes past the end of the data', () => {
    expect(visibleWindow(20, 700, 400, 40, 5).end).toBe(20)
  })
})

describe('filterProblem', () => {
  it('rejects an inverted date range and a non-UUID actor', () => {
    expect(filterProblem({ ...emptyFilters, since: '2026-10-02T00:00', until: '2026-10-01T00:00' })).toMatch(/earlier/)
    expect(filterProblem({ ...emptyFilters, actor: 'bob' })).toMatch(/UUID/)
  })

  it('accepts valid filters', () => {
    expect(filterProblem({ ...emptyFilters, actor: '3f2b1c0e-0000-4000-8000-000000000001' })).toBeNull()
  })
})
