import { describe, expect, it } from 'vitest'

import { diffCapabilities, isLocked, normalizeProposals, parseAttributes, splitList } from './adminLogic'

describe('normalizeProposals', () => {
  it('reads flat items and nested items with a null or present nearest pair', () => {
    const pair = { id: 'p1', question: 'q', logical_sql: 'select 1' }
    const result = normalizeProposals([
      { id: 'a', question: 'How many?', logical_sql: 'select 2', nearest_approved: pair },
      { proposal: { id: 'b', question: 'Who?', logical_sql: 'select 3' }, nearest_approved: null },
    ])
    expect(result.map((item) => item.id)).toEqual(['a', 'b'])
    expect(result[0].nearest_approved).toEqual(pair)
    expect(result[1].nearest_approved).toBeNull()
    expect(result[1].question).toBe('Who?')
  })

  it('returns an empty list for anything that is not a list', () => {
    expect(normalizeProposals(undefined)).toEqual([])
  })
})

describe('diffCapabilities', () => {
  it('reports gained and lost capabilities', () => {
    expect(diffCapabilities(['a', 'b'], ['b', 'c'])).toEqual({ gained: ['c'], lost: ['a'] })
  })
})

describe('parseAttributes', () => {
  it('accepts objects and blank input, rejects arrays and bad JSON', () => {
    expect(parseAttributes('{"region":"EU"}').value).toEqual({ region: 'EU' })
    expect(parseAttributes('  ').value).toEqual({})
    expect(parseAttributes('[1]').error).toMatch(/object/)
    expect(parseAttributes('{').error).toMatch(/valid JSON/)
  })
})

describe('helpers', () => {
  it('splits comma lists without blanks or duplicates', () => {
    expect(splitList('a, b,,a ')).toEqual(['a', 'b'])
  })
  it('detects an active lock only', () => {
    const now = Date.parse('2026-01-01T00:00:00Z')
    expect(isLocked('2026-01-01T01:00:00Z', now)).toBe(true)
    expect(isLocked('2025-12-31T23:00:00Z', now)).toBe(false)
    expect(isLocked(null, now)).toBe(false)
  })
})
