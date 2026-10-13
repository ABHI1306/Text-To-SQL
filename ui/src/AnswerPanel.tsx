import { useMemo, useState } from 'react'

import { chartModel, type AnswerRun, type ChartModel } from './askStream'

type View = 'table' | 'bar' | 'line' | 'sql'

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

export function AnswerPanel({
  run,
  onRerun,
  onAsk,
}: {
  run: AnswerRun
  onRerun: () => void
  onAsk: (question: string) => void
}) {
  const [view, setView] = useState<View>('table')
  const [copied, setCopied] = useState(false)
  const [feedback, setFeedback] = useState<'up' | 'down' | null>(null)
  const chart = useMemo(() => chartModel(run.columns, run.rows), [run.columns, run.rows])
  const shown: View = (view === 'bar' || view === 'line') && !chart ? 'table' : view
  const sum = run.steps.reduce((total, step) => total + step.ms, 0)
  const hasRows = run.columns.length > 0

  async function copy(text: string) {
    await navigator.clipboard.writeText(text)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1600)
  }

  return (
    <div className="result-content">
      {run.retrievedModels.length > 0 && (
        <div className="result-labels">
          <span className="computed-label">PostgreSQL results</span>
          <span className="model-label">{run.retrievedModels.join(' · ')}</span>
        </div>
      )}
      <section className="trace-card" aria-label="Question execution steps">
        <div className="trace-heading">
          <strong>
            {run.status === 'running' ? 'Working…' : run.status === 'error' ? 'Stopped' : 'Query completed'}
            {sum > 0 && ` · ${sum} ms in steps`}
            {run.status === 'done' && run.totalMs > 0 && ` · ${run.totalMs} ms total`}
          </strong>
        </div>
        <ol className="trace-steps">
          {run.steps.map((step) => (
            <li className={`trace-step ${step.ok ? 'done' : 'failed'}`} key={step.id}>
              <span className="step-status" aria-hidden="true">{step.ok ? '✓' : '✕'}</span>
              <div className="step-body">
                <div className="step-main">
                  <strong>{step.title}</strong>
                  <span>{step.detail}</span>
                  <time>{step.ms} ms</time>
                </div>
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
      </section>

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

      {hasRows && (
        <section className="data-card">
          <div className="data-card-heading">
            <strong>Query results</strong>
            <div>
              <span>{run.rows.length} rows</span>
              {run.truncated && <span className="truncate-label">Showing first {run.rows.length} rows</span>}
            </div>
          </div>
          {shown === 'sql' ? (
            <div className="sql-views">
              <h4>Logical SQL</h4>
              <pre className="sql-viewer"><code>{run.logicalSql}</code></pre>
              {run.compiledSql && (
                <>
                  <h4>Compiled PostgreSQL (policies applied)</h4>
                  <pre className="sql-viewer"><code>{run.compiledSql}</code></pre>
                </>
              )}
            </div>
          ) : (shown === 'bar' || shown === 'line') && chart ? (
            <Chart model={chart} kind={shown} />
          ) : (
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
          )}
        </section>
      )}

      {hasRows && (
        <>
          <div className="answer-toolbar">
            <div className="sql-actions" role="group" aria-label="Result view">
              <button className={shown === 'table' ? 'selected' : ''} onClick={() => setView('table')} type="button">Table</button>
              <button className={shown === 'bar' ? 'selected' : ''} disabled={!chart} onClick={() => setView('bar')} type="button">Bar</button>
              <button className={shown === 'line' ? 'selected' : ''} disabled={!chart} onClick={() => setView('line')} type="button">Line</button>
              <button className={shown === 'sql' ? 'selected' : ''} onClick={() => setView('sql')} type="button">SQL</button>
            </div>
          </div>
          <div className="result-actions">
            <button onClick={onRerun} type="button">Re-run</button>
            <button
              disabled={!run.logicalSql}
              onClick={() => void copy(run.compiledSql ?? run.logicalSql ?? '')}
              type="button"
            >
              {copied ? 'Copied' : run.compiledSql ? 'Copy compiled SQL' : 'Copy SQL'}
            </button>
            <span className="action-divider" />
            <button aria-label="Good answer" className={feedback === 'up' ? 'feedback-selected' : ''} onClick={() => setFeedback('up')} type="button">👍</button>
            <button aria-label="Poor answer" className={feedback === 'down' ? 'feedback-selected' : ''} onClick={() => setFeedback('down')} type="button">👎</button>
          </div>
        </>
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
