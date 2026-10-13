import { describe, expect, it } from 'vitest'

import { applyEvent, chartModel, emptyRun, parseSse } from './askStream'

describe('parseSse', () => {
  it('parses complete events and keeps the unfinished tail', () => {
    const { events, rest } = parseSse('event: retrieval\ndata: {"models":["orders"]}\n\nevent: rows\ndata: {"row')
    expect(events).toEqual([{ type: 'retrieval', data: { models: ['orders'] } }])
    expect(rest).toBe('event: rows\ndata: {"row')
  })

  it('handles CRLF framing, multi-line data and skips malformed frames', () => {
    const { events } = parseSse('event: a\r\ndata: {"x":\r\ndata: 1}\r\n\r\nevent: b\r\ndata: not json\r\n\r\nevent: c\r\ndata: {}\r\n\r\n')
    expect(events.map((event) => event.type)).toEqual(['a', 'c'])
    expect(events[0].data).toEqual({ x: 1 })
  })
})

describe('applyEvent', () => {
  it('builds the step timeline with per-step milliseconds and the final state', () => {
    let run = emptyRun('How many orders?')
    const feed = (type: string, data: Record<string, unknown>) => { run = applyEvent(run, { type, data }) }
    feed('retrieval', { models: ['orders', 'customers'], ms: 700 })
    feed('sql_candidate', { logical_sql: 'SELECT 1', attempt: 1, ms: 9000 })
    feed('compilation', { ok: false, errors: [{ code: 'UNKNOWN_COLUMN', message: 'x' }], ms: 4 })
    feed('correction', { attempt: 1, reason: 'bad column', ms: 0 })
    feed('sql_candidate', { logical_sql: 'SELECT 2', attempt: 2, ms: 8000 })
    feed('compilation', { ok: true, errors: [], sql: 'WITH m AS (SELECT 1) SELECT 2', ms: 5 })
    feed('validation', { ok: true, errors: [], ms: 11 })
    feed('rows', { columns: ['n'], rows: [[2]], truncated: false, row_count: 1, ms: 12 })
    feed('done', { latency_ms: 17800 })
    expect(run.status).toBe('done')
    expect(run.logicalSql).toBe('SELECT 2')
    expect(run.compiledSql).toBe('WITH m AS (SELECT 1) SELECT 2')
    expect(run.steps.map((step) => step.title)).toEqual([
      'Retrieval', 'SQL candidate', 'Compile', 'Correction', 'SQL candidate', 'Compile', 'Validate', 'PostgreSQL',
    ])
    expect(run.steps[2]).toMatchObject({ ok: false, detail: 'E_UNKNOWN_COLUMN x' })
    expect(run.steps.reduce((sum, step) => sum + step.ms, 0)).toBe(700 + 9000 + 4 + 0 + 8000 + 5 + 11 + 12)
    expect(run.rows).toEqual([[2]])
    expect(run.totalMs).toBe(17800)
  })

  it('never keeps compiled SQL the server did not send', () => {
    let run = emptyRun('q')
    run = applyEvent(run, { type: 'compilation', data: { ok: true, errors: [] } })
    expect(run.compiledSql).toBeNull()
  })

  it('turns an error event into a typed failure and a clarification into quick replies', () => {
    const failed = applyEvent(emptyRun('q'), { type: 'error', data: { code: 'TIMEOUT', message: 'slow', retryable: true } })
    expect(failed).toMatchObject({ status: 'error', error: { code: 'TIMEOUT', retryable: true } })
    const asked = applyEvent(emptyRun('q'), { type: 'clarification', data: { question: 'Which year?', options: ['2025', '2026'] } })
    expect(asked.clarification).toEqual({ question: 'Which year?', options: ['2025', '2026'] })
  })
})

describe('chartModel', () => {
  it('uses the first non-numeric column as labels and numeric columns as series', () => {
    expect(chartModel(['status', 'orders', 'revenue'], [['paid', 2, '10.5'], ['shipped', 1, '3']])).toEqual({
      labels: ['paid', 'shipped'],
      series: [{ name: 'orders', values: [2, 1] }, { name: 'revenue', values: [10.5, 3] }],
    })
  })

  it('refuses results that cannot be charted', () => {
    expect(chartModel(['n'], [[1]])).toBeNull()
    expect(chartModel(['a', 'b'], [['x', 'y']])).toBeNull()
    expect(chartModel(['a', 'b'], [])).toBeNull()
    expect(chartModel(['a', 'b'], [[1, 2]])).toBeNull()
    expect(chartModel(['a', 'b'], Array.from({ length: 201 }, (_, i) => [`k${i}`, i]))).toBeNull()
  })
})
