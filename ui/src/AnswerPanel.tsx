import { useMemo, useState, type ReactNode } from 'react'

import { chartModel, seconds, type AnswerRun, type ChartModel } from './askStream'

type ChartKind = 'bar' | 'line'

const COLORS = ['#4f46e5', '#0e9f6e', '#d97706']

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return 'NULL'
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value)
  return JSON.stringify(value) ?? String(value)
}

function Chart({ model, kind }: { model: ChartModel; kind: 'bar' | 'line' }) {
  const width = 640
  const height = 240
  const pad = { left: 44, right: 12, top: 12, bottom: 56 }
  const max = Math.max(...model.series.flatMap((series) => series.values), 0) || 1
  const min = Math.min(...model.series.flatMap((series) => series.values), 0)
  const plotW = width - pad.left - pad.right
  const plotH = height - pad.top - pad.bottom
  const y = (value: number) => pad.top + plotH - ((value - min) / (max - min || 1)) * plotH
  const step = plotW / model.labels.length
  return (
    <figure className="chart" aria-label={`${kind === 'bar' ? 'Bar' : 'Line'} chart`}>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={model.series.map((s) => s.name).join(', ')}>
        <line x1={pad.left} x2={pad.left} y1={pad.top} y2={pad.top + plotH} stroke="currentColor" opacity="0.3" />
        <line x1={pad.left} x2={width - pad.right} y1={y(0)} y2={y(0)} stroke="currentColor" opacity="0.3" />
        <text x={pad.left - 6} y={pad.top + 4} textAnchor="end" fontSize="10" fill="currentColor">{max}</text>
        <text x={pad.left - 6} y={pad.top + plotH} textAnchor="end" fontSize="10" fill="currentColor">{min}</text>
        {model.labels.map((label, index) => (
          <text
            key={`${label}-${index}`}
            x={pad.left + step * index + step / 2}
            y={height - pad.bottom + 14}
            fontSize="10"
            fill="currentColor"
            textAnchor="end"
            transform={`rotate(-30 ${pad.left + step * index + step / 2} ${height - pad.bottom + 14})`}
          >
            {label.length > 14 ? `${label.slice(0, 13)}…` : label}
          </text>
        ))}
        {model.series.map((series, seriesIndex) => {
          const color = COLORS[seriesIndex % COLORS.length]
          if (kind === 'line') {
            const points = series.values.map((value, index) => `${pad.left + step * index + step / 2},${y(value)}`)
            return (
              <g key={series.name}>
                <polyline fill="none" stroke={color} strokeWidth="2" points={points.join(' ')} />
                {series.values.map((value, index) => (
                  <circle key={index} cx={pad.left + step * index + step / 2} cy={y(value)} r="3" fill={color}>
                    <title>{`${model.labels[index]}: ${value}`}</title>
                  </circle>
                ))}
              </g>
            )
          }
          const barW = (step * 0.7) / model.series.length
          return (
            <g key={series.name}>
              {series.values.map((value, index) => (
                <rect
                  key={index}
                  x={pad.left + step * index + step * 0.15 + barW * seriesIndex}
                  y={Math.min(y(value), y(0))}
                  width={barW}
                  height={Math.abs(y(0) - y(value))}
                  fill={color}
                >
                  <title>{`${model.labels[index]} · ${series.name}: ${value}`}</title>
                </rect>
              ))}
            </g>
          )
        })}
      </svg>
      <figcaption>
        {model.series.map((series, index) => (
          <span key={series.name}><i style={{ background: COLORS[index % COLORS.length] }} />{series.name}</span>
        ))}
      </figcaption>
    </figure>
  )
}

const SQL_KEYWORDS = new Set([
  'SELECT', 'FROM', 'WHERE', 'GROUP', 'BY', 'ORDER', 'LIMIT', 'JOIN', 'LEFT', 'RIGHT', 'INNER', 'OUTER',
  'ON', 'AND', 'OR', 'NOT', 'AS', 'WITH', 'SUM', 'COUNT', 'AVG', 'MIN', 'MAX', 'DESC', 'ASC', 'HAVING',
  'DISTINCT', 'IN', 'IS', 'NULL', 'CASE', 'WHEN', 'THEN', 'ELSE', 'END', 'BETWEEN', 'LIKE', 'UNION',
])

/** Keywords, strings and comments get a colour; everything else stays plain. */
function SqlText({ sql }: { sql: string }) {
  return (
    <>
      {sql.split('\n').map((line, lineIndex) => (
        <div key={lineIndex}>
          {line.split(/(--.*$|'[^']*'|\b[A-Za-z_]+\b)/).map((part, index) => {
            if (part.startsWith('--')) return <span className="sql-comment" key={index}>{part}</span>
            if (part.startsWith("'")) return <span className="sql-string" key={index}>{part}</span>
            if (SQL_KEYWORDS.has(part.toUpperCase())) return <span className="sql-keyword" key={index}>{part}</span>
            return <span key={index}>{part}</span>
          })}
        </div>
      ))}
    </>
  )
}

export type AnswerActions = {
  pin?: () => Promise<string>
  schedule?: (preset: 'hourly' | 'daily' | 'weekly') => Promise<string>
  shareDialog?: (onClose: () => void) => ReactNode
}

export function AnswerPanel({
  run,
  onRerun,
  onAsk,
  onLoadRows,
  onFeedback,
  onRunSql,
  actions,
}: {
  run: AnswerRun
  onRerun: () => void
  onAsk: (question: string) => void
  /** Recompute the rows of a saved answer (they are not stored with the chat). */
  onLoadRows?: () => Promise<void>
  /** Save a thumbs up or down for this answer. */
  onFeedback?: (rating: 'up' | 'down') => Promise<void>
  /** Run edited (or the same) logical SQL again; only offered to accounts that may. */
  onRunSql?: (sql: string) => Promise<void>
  actions?: AnswerActions
}) {
  const [stepsOpen, setStepsOpen] = useState(true)
  const [chartKind, setChartKind] = useState<ChartKind>('bar')
  const [editing, setEditing] = useState(false)
  const [draftSql, setDraftSql] = useState('')
  const [sqlTab, setSqlTab] = useState<'logical' | 'compiled'>('logical')
  const [copied, setCopied] = useState(false)
  const [feedback, setFeedback] = useState<'up' | 'down' | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [note, setNote] = useState<{ text: string; error: boolean } | null>(null)
  const [panel, setPanel] = useState<'schedule' | 'share' | null>(null)
  const chart = useMemo(() => chartModel(run.columns, run.rows), [run.columns, run.rows])
  const sum = run.steps.reduce((total, step) => total + step.ms, 0)
  const hasRows = run.columns.length > 0
  const shownSql = sqlTab === 'compiled' && run.compiledSql ? run.compiledSql : (run.logicalSql ?? '')
  const rowsPending = run.status === 'done' && !hasRows && Boolean(run.logicalSql) && Boolean(run.messageId)

  async function attempt(name: string, work: () => Promise<string | void>) {
    setBusy(name)
    setNote(null)
    try {
      const text = await work()
      if (text) setNote({ text, error: false })
    } catch (reason: unknown) {
      setNote({ text: reason instanceof Error ? reason.message : 'That did not work', error: true })
    } finally {
      setBusy(null)
    }
  }

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1600)
    } catch {
      setNote({ text: 'Copy is blocked here. Select the SQL and copy it.', error: true })
    }
  }

  async function rate(rating: 'up' | 'down') {
    setFeedback(rating)
    if (!onFeedback) return
    try {
      await onFeedback(rating)
    } catch {
      setFeedback(null)
    }
  }

  return (
    <div className="result-content">
      {run.status === 'done' && (hasRows || rowsPending) && (
        <div className="result-labels">
          <span className="computed-label">Computed for you</span>
          {(run.retrievedModels.length > 0 || run.modelVersion !== null) && (
            <span className="model-label">
              {[...run.retrievedModels, ...(run.modelVersion !== null ? [`model v${run.modelVersion}`] : [])].join(' · ')}
            </span>
          )}
        </div>
      )}

      {(run.steps.length > 0 || run.status === 'running') && (
        <section className="trace-card" aria-label="Question execution steps">
          <button
            aria-expanded={stepsOpen}
            className="trace-heading"
            onClick={() => setStepsOpen((open) => !open)}
            type="button"
          >
            <span>
              <strong>Steps</strong>
              <span className="trace-meta">
                {run.status === 'running' ? ' · working…' : ` · ${run.steps.length} steps · ${seconds(sum)}`}
              </span>
            </span>
            <span className="trace-toggle">{stepsOpen ? 'Hide' : 'Show'}</span>
          </button>
          {stepsOpen && (
            <ol className="trace-steps">
              {run.steps.map((step) => (
                <li className={`trace-step ${step.ok ? 'done' : 'failed'}`} key={step.id}>
                  <span className="step-status" aria-hidden="true">{step.ok ? '✓' : '✕'}</span>
                  <div className="step-body">
                    <div className="step-main">
                      <strong>{step.title}</strong>
                      <span>{step.detail}</span>
                      <time>{seconds(step.ms)}</time>
                    </div>
                    {step.title === 'Retrieval' && run.retrievedModels.length > 0 && (
                      <div className="step-tags">
                        {run.retrievedModels.map((model) => <span key={model}>{model}</span>)}
                      </div>
                    )}
                  </div>
                </li>
              ))}
              {run.status === 'running' && (
                <li className="trace-step running" role="status">
                  <span className="step-status" aria-hidden="true">…</span>
                  <div className="step-body"><div className="step-main"><strong>Working</strong></div></div>
                </li>
              )}
            </ol>
          )}
        </section>
      )}

      {run.error && (
        <div className="query-error" role="alert">
          <strong>E_{run.error.code}</strong> {run.error.message}
          {run.error.retryable && <button type="button" onClick={onRerun}>Try again</button>}
        </div>
      )}

      {run.clarification && (
        <section className="clarification" aria-label="Clarifying question">
          <p>{run.clarification.question}</p>
          <div className="suggestion-list">
            {run.clarification.options.map((option) => (
              <button key={option} type="button" onClick={() => onAsk(option)}>{option}</button>
            ))}
          </div>
        </section>
      )}

      {rowsPending && (
        <section className="data-card" aria-label="Saved answer">
          <div className="data-card-heading">
            <strong>{run.question}</strong>
            <div>
              {onLoadRows && (
                <button disabled={busy === 'rows'} onClick={() => void attempt('rows', async () => { await onLoadRows() })} type="button">
                  {busy === 'rows' ? 'Loading…' : 'Show results'}
                </button>
              )}
            </div>
          </div>
        </section>
      )}

      {hasRows && (
        <section className="data-card">
          <div className="data-card-heading">
            <strong>{run.question}</strong>
            <div>
              <span>{run.rows.length} rows</span>
              {run.truncated && <span className="truncate-label">Truncated: showing {run.rows.length} rows</span>}
            </div>
          </div>
          <div className="table-scroll">
            <table>
              <thead><tr>{run.columns.map((column, index) => <th key={`${column}-${index}`}>{column}</th>)}</tr></thead>
              <tbody>
                {run.rows.map((row, rowIndex) => (
                  <tr key={rowIndex}>
                    {run.columns.map((column, columnIndex) => (
                      <td key={`${column}-${columnIndex}`}>{formatCell(row[columnIndex])}</td>
                    ))}
                  </tr>
                ))}
                {run.rows.length === 0 && (
                  <tr><td colSpan={run.columns.length}>No rows matched this question.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {run.summary && run.status === 'done' && <p className="answer-summary">{run.summary}</p>}

      {hasRows && chart && (
        <section className="data-card chart-card" aria-label="Chart">
          <div className="data-card-heading">
            <strong>Chart</strong>
            <div className="sql-actions" role="group" aria-label="Chart type">
              <button className={chartKind === 'bar' ? 'selected' : ''} onClick={() => setChartKind('bar')} type="button">Bar</button>
              <button className={chartKind === 'line' ? 'selected' : ''} onClick={() => setChartKind('line')} type="button">Line</button>
            </div>
          </div>
          <Chart model={chart} kind={chartKind} />
        </section>
      )}

      {run.logicalSql && run.status === 'done' && (
        <section className="data-card sql-card" aria-label="SQL">
          <div className="sql-card-heading">
            <div className="sql-tabs" role="tablist">
              <button aria-selected={sqlTab === 'logical'} className={sqlTab === 'logical' ? 'selected' : ''} onClick={() => setSqlTab('logical')} role="tab" type="button">Logical SQL</button>
              {run.compiledSql && (
                <button aria-selected={sqlTab === 'compiled'} className={sqlTab === 'compiled' ? 'selected' : ''} onClick={() => { setSqlTab('compiled'); setEditing(false) }} role="tab" type="button">Compiled SQL</button>
              )}
            </div>
            <div className="sql-actions">
              {onRunSql && sqlTab === 'logical' && (
                <>
                  <button
                    onClick={() => {
                      // Editing changes the logical SQL; the compiled form is rebuilt from it.
                      setDraftSql(run.logicalSql ?? '')
                      setEditing((value) => !value)
                    }}
                    type="button"
                  >
                    {editing ? 'Cancel edit' : 'Edit'}
                  </button>
                  <button
                    disabled={busy === 'run'}
                    onClick={() => void attempt('run', async () => { await onRunSql(editing ? draftSql : run.logicalSql ?? ''); setEditing(false) })}
                    type="button"
                  >
                    {busy === 'run' ? 'Running…' : 'Re-run'}
                  </button>
                </>
              )}
              <button aria-label={copied ? 'Copied' : sqlTab === 'compiled' ? 'Copy compiled SQL' : 'Copy logical SQL'} onClick={() => void copy(shownSql)} type="button">
                {copied ? 'Copied' : 'Copy'}
              </button>
            </div>
          </div>
          {editing && sqlTab === 'logical' ? (
            <div className="sql-edit">
              <textarea aria-label="Edit SQL" onChange={(event) => setDraftSql(event.target.value)} rows={7} value={draftSql} />
              <p>Edited SQL is validated and policy-checked again before it runs.</p>
            </div>
          ) : (
            <pre className="sql-viewer"><code><SqlText sql={shownSql} /></code></pre>
          )}
          {sqlTab === 'compiled' && (
            <p className="sql-note">Built from the logical SQL with your row policies applied. To change it, edit the Logical SQL tab.</p>
          )}
        </section>
      )}

      {run.status === 'done' && run.messageId && (hasRows || rowsPending) && (
        <>
          <div className="result-actions">
            <button aria-label="Good answer" className={feedback === 'up' ? 'feedback-selected' : ''} onClick={() => void rate('up')} title="Proposes this as a SQL pair for DBA review" type="button">👍</button>
            <button aria-label="Poor answer" className={feedback === 'down' ? 'feedback-selected' : ''} onClick={() => void rate('down')} type="button">👎</button>
            {actions?.pin && (
              <button disabled={busy === 'pin'} onClick={() => void attempt('pin', actions.pin!)} type="button">Pin to dashboard</button>
            )}
            {actions?.schedule && (
              <button aria-expanded={panel === 'schedule'} onClick={() => setPanel(panel === 'schedule' ? null : 'schedule')} type="button">Schedule this question</button>
            )}
            {actions?.shareDialog && (
              <button aria-expanded={panel === 'share'} onClick={() => setPanel(panel === 'share' ? null : 'share')} type="button">Share</button>
            )}
          </div>
          {panel === 'schedule' && actions?.schedule && (
            <div className="action-panel" role="group" aria-label="Schedule this question">
              <span>Run it</span>
              {(['hourly', 'daily', 'weekly'] as const).map((preset) => (
                <button disabled={busy === 'schedule'} key={preset} onClick={() => void attempt('schedule', () => actions.schedule!(preset))} type="button">{preset}</button>
              ))}
            </div>
          )}
          {panel === 'share' && actions?.shareDialog && actions.shareDialog(() => setPanel(null))}
          {note && <p className={`action-note${note.error ? ' error' : ''}`} role={note.error ? 'alert' : 'status'}>{note.text}</p>}
        </>
      )}
      {(!run.messageId || !(hasRows || rowsPending)) && note && (
        <p className={`action-note${note.error ? ' error' : ''}`} role={note.error ? 'alert' : 'status'}>{note.text}</p>
      )}

      {run.status === 'done' && run.suggestions.length > 0 && (
        <div className="suggestion-list" aria-label="Follow-up suggestions">
          {run.suggestions.map((item) => (
            <button key={item} type="button" onClick={() => onAsk(item)}>{item}</button>
          ))}
        </div>
      )}
    </div>
  )
}
