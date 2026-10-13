import type { ApiRequest } from './modelTypes'

export type AskEvent = { type: string; data: Record<string, unknown> }

export type RunStep = {
  id: string
  title: string
  detail: string
  ok: boolean
  ms: number
}

export type AnswerRun = {
  question: string
  status: 'running' | 'done' | 'error'
  steps: RunStep[]
  columns: string[]
  rows: unknown[][]
  truncated: boolean
  logicalSql: string | null
  compiledSql: string | null
  retrievedModels: string[]
  totalMs: number
  error?: { code: string; message: string; retryable: boolean }
  clarification?: { question: string; options: string[] }
  suggestions: string[]
}

export const emptyRun = (question: string): AnswerRun => ({
  question,
  status: 'running',
  steps: [],
  columns: [],
  rows: [],
  truncated: false,
  logicalSql: null,
  compiledSql: null,
  retrievedModels: [],
  totalMs: 0,
  suggestions: [],
})

/** Split a buffer of server-sent events into parsed events and the unfinished remainder. */
export function parseSse(buffer: string): { events: AskEvent[]; rest: string } {
  const blocks = buffer.replaceAll('\r\n', '\n').split('\n\n')
  const rest = blocks.pop() ?? ''
  const events: AskEvent[] = []
  for (const block of blocks) {
    let type = 'message'
    const data: string[] = []
    for (const line of block.split('\n')) {
      if (line.startsWith('event:')) type = line.slice(6).trim()
      else if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
    }
    if (data.length === 0) continue
    try {
      events.push({ type, data: JSON.parse(data.join('\n')) as Record<string, unknown> })
    } catch {
      // A malformed frame is skipped; the stream still ends in done or error.
    }
  }
  return { events, rest }
}

type Issue = { code?: string; message?: string }

function issues(list: unknown): string {
  return (Array.isArray(list) ? list as Issue[] : [])
    .map((item) => `${item.code ? `E_${item.code}` : ''} ${item.message ?? ''}`.trim())
    .join('; ')
}

/** Pure reducer: one server event onto the run. */
export function applyEvent(run: AnswerRun, event: AskEvent): AnswerRun {
  const { type, data } = event
  const ms = typeof data.ms === 'number' ? data.ms : 0
  const step = (title: string, detail: string, ok = true): RunStep[] => [
    ...run.steps,
    { id: `${run.steps.length}-${type}`, title, detail, ok, ms },
  ]
  switch (type) {
    case 'retrieval': {
      const models = Array.isArray(data.models) ? data.models.map(String) : []
      return { ...run, retrievedModels: models, steps: step('Retrieval', models.join(', ') || 'No models') }
    }
    case 'sql_candidate':
      return {
        ...run,
        logicalSql: String(data.logical_sql ?? ''),
        steps: step('SQL candidate', `Attempt ${String(data.attempt ?? 1)}`),
      }
    case 'compilation': {
      const ok = data.ok === true
      return {
        ...run,
        compiledSql: ok && typeof data.sql === 'string' ? data.sql : run.compiledSql,
        steps: step('Compile', ok ? 'Row policies applied' : issues(data.errors), ok),
      }
    }
    case 'validation': {
      const ok = data.ok === true
      return { ...run, steps: step('Validate', ok ? 'Single read-only query; dry run passed' : issues(data.errors), ok) }
    }
    case 'correction':
      return { ...run, steps: step('Correction', `Attempt ${String(data.attempt ?? '')}: ${String(data.reason ?? '')}`, false) }
    case 'rows': {
      const rows = Array.isArray(data.rows) ? data.rows as unknown[][] : []
      return {
        ...run,
        columns: Array.isArray(data.columns) ? data.columns.map(String) : [],
        rows,
        truncated: data.truncated === true,
        steps: step('PostgreSQL', `${rows.length} row${rows.length === 1 ? '' : 's'} returned${data.truncated === true ? ' (results capped)' : ''}`),
      }
    }
    case 'suggestions':
      return { ...run, suggestions: Array.isArray(data.items) ? data.items.map(String) : [] }
    case 'clarification':
      return {
        ...run,
        status: 'done',
        clarification: {
          question: String(data.question ?? ''),
          options: Array.isArray(data.options) ? data.options.map(String) : [],
        },
      }
    case 'done':
      return { ...run, status: 'done', totalMs: typeof data.latency_ms === 'number' ? data.latency_ms : run.totalMs }
    case 'error':
      return {
        ...run,
        status: 'error',
        error: {
          code: String(data.code ?? 'ERROR'),
          message: String(data.message ?? 'The question failed'),
          retryable: data.retryable === true,
        },
      }
    default:
      return run
  }
}

/** POST the question and feed each event to `onRun` as it arrives. */
export async function streamQuestion(
  request: ApiRequest,
  projectId: string,
  question: string,
  onRun: (run: AnswerRun) => void,
  signal?: AbortSignal,
): Promise<void> {
  let run = emptyRun(question)
  onRun(run)
  const response = await request(`/api/v1/projects/${projectId}/query/stream`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ question }),
    signal,
  })
  if (!response.ok || !response.body) {
    const failure = await response.json().catch(() => null) as { detail?: unknown } | null
    const detail = failure?.detail
    const message = typeof detail === 'string'
      ? detail
      : (detail as { message?: string } | undefined)?.message ?? `Question failed (${response.status})`
    onRun({ ...run, status: 'error', error: { code: String(response.status), message, retryable: response.status >= 500 } })
    return
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const parsed = parseSse(buffer)
    buffer = parsed.rest
    for (const event of parsed.events) {
      run = applyEvent(run, event)
      onRun(run)
    }
  }
  if (run.status === 'running') {
    onRun({ ...run, status: 'error', error: { code: 'STREAM_ENDED', message: 'The connection closed before the answer finished.', retryable: true } })
  }
}

export type ChartModel = {
  labels: string[]
  series: { name: string; values: number[] }[]
}

const asNumber = (value: unknown): number | null => {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value))) return Number(value)
  return null
}

/** A label column plus numeric columns, or null when the result is not chartable. */
export function chartModel(columns: string[], rows: unknown[][]): ChartModel | null {
  if (columns.length < 2 || rows.length === 0 || rows.length > 200) return null
  const numeric = columns.map((_, index) => rows.every((row) => asNumber(row[index]) !== null))
  const labelIndex = numeric.findIndex((isNumeric) => !isNumeric)
  const valueIndexes = columns.map((_, index) => index).filter((index) => numeric[index] && index !== labelIndex)
  if (labelIndex === -1 || valueIndexes.length === 0) return null
  return {
    labels: rows.map((row) => String(row[labelIndex] ?? '')),
    series: valueIndexes.slice(0, 3).map((index) => ({
      name: columns[index],
      values: rows.map((row) => asNumber(row[index]) ?? 0),
    })),
  }
}
