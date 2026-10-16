import { useCallback, useEffect, useRef, useState } from 'react'

import { callApi } from './adminApi'
import type { ApiRequest } from './modelTypes'

type Summary = {
  scored: number
  correct: number
  execution_accuracy: number | null
  unmeasured_no_reference: number
  failures_by_stage: Record<string, number>
  latency_seconds: { p50: number | null; p95: number }
}
type CaseResult = {
  case_id: string
  question: string
  outcome: string
  stage: string | null
  failure_code: string | null
  latency_ms: number | null
}
type Report = {
  run: { id: string; status: 'running' | 'completed' | 'failed'; summary: Summary | null; error: string | null }
  results: CaseResult[]
  diff: {
    previous_run_id: string | null
    regressions: { case_id: string }[]
    fixes: { case_id: string }[]
    unchanged: number
  }
}

const POLL_MS = 3000

export function EvaluationScreen({ apiRequest, projectId }: { apiRequest: ApiRequest; projectId: string | undefined }) {
  const [report, setReport] = useState<Report | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const runId = useRef<string | null>(null)

  const refresh = useCallback(async () => {
    if (!runId.current) return
    try {
      setReport(await callApi<Report>(apiRequest, `/api/v1/eval/${runId.current}`))
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : 'Could not read the report')
    }
  }, [apiRequest])

  useEffect(() => {
    if (report?.run.status !== 'running') return
    const timer = window.setTimeout(() => void refresh(), POLL_MS)
    return () => window.clearTimeout(timer)
  }, [report, refresh])

  if (!projectId) {
    return (
      <section className="placeholder-content">
        <h1>Evaluation</h1>
        <p>Create a project in Setup before running an evaluation.</p>
      </section>
    )
  }

  async function start() {
    setStarting(true)
    setError(null)
    try {
      // A fresh key per click: a double click on this button is one run only because the button
      // is disabled while the request is in flight.
      const response = await apiRequest(`/api/v1/projects/${projectId}/eval/run`, {
        method: 'POST',
        headers: { 'idempotency-key': crypto.randomUUID() },
      })
      const body = (await response.json()) as { run_id?: string; detail?: unknown }
      if (!response.ok || !body.run_id) throw new Error('Could not start the evaluation')
      runId.current = body.run_id
      await refresh()
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : 'Could not start the evaluation')
    } finally {
      setStarting(false)
    }
  }

  async function downloadMarkdown() {
    if (!runId.current) return
    const response = await apiRequest(`/api/v1/eval/${runId.current}?format=markdown`)
    if (!response.ok) return setError('Could not download the report')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = `evaluation-${runId.current}.md`
    link.click()
    URL.revokeObjectURL(url)
  }

  const summary = report?.run.summary

  return (
    <section className="admin-screen" aria-label="Evaluation">
      <header className="admin-header">
        <div>
          <span className="eyebrow">SYSTEM</span>
          <h1>Evaluation</h1>
          <p>Runs the golden questions through the full pipeline and compares the rows with reference queries.</p>
        </div>
        <button className="primary" disabled={starting || report?.run.status === 'running'} onClick={() => void start()} type="button">
          {report?.run.status === 'running' ? 'Running…' : 'Run evaluation'}
        </button>
      </header>
      {error && <p className="admin-state error" role="alert">{error}</p>}
      {report?.run.status === 'failed' && (
        <p className="admin-state error" role="alert">The run failed: {report.run.error}</p>
      )}
      {summary && (
        <div className="admin-panel">
          <dl className="eval-summary">
            <div><dt>Execution accuracy</dt><dd>{summary.execution_accuracy === null ? '—' : `${Math.round(summary.execution_accuracy * 100)}%`}</dd></div>
            <div><dt>Correct / scored</dt><dd>{summary.correct} / {summary.scored}</dd></div>
            <div><dt>Not measurable</dt><dd>{summary.unmeasured_no_reference}</dd></div>
            <div><dt>Latency p50 / p95</dt><dd>{summary.latency_seconds.p50 ?? '—'}s / {summary.latency_seconds.p95}s</dd></div>
          </dl>
          <p className="muted">
            Failures by stage:{' '}
            {Object.keys(summary.failures_by_stage).length === 0
              ? 'none'
              : Object.entries(summary.failures_by_stage).map(([stage, n]) => `${stage} ${n}`).join(' · ')}
          </p>
          {report && (
            <p className="muted">
              {report.diff.previous_run_id === null
                ? 'No earlier run to compare with.'
                : `Against the previous run: ${report.diff.regressions.length} regressions, ${report.diff.fixes.length} fixed, ${report.diff.unchanged} unchanged.`}
              {report.diff.regressions.length > 0 && ` Regressed: ${report.diff.regressions.map((r) => r.case_id).join(', ')}.`}
            </p>
          )}
          <div className="admin-actions"><button onClick={() => void downloadMarkdown()} type="button">Download report (Markdown)</button></div>
        </div>
      )}
      {report && report.results.length > 0 && (
        <table className="admin-table" aria-label="Evaluation cases">
          <thead><tr><th>Case</th><th>Question</th><th>Outcome</th><th>Stage</th><th>Failure</th><th>ms</th></tr></thead>
          <tbody>
            {report.results.map((result) => (
              <tr key={result.case_id}>
                <td>{result.case_id}</td><td>{result.question}</td><td>{result.outcome}</td>
                <td>{result.stage ?? ''}</td><td>{result.failure_code ? `E_${result.failure_code}` : ''}</td>
                <td>{result.latency_ms}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {!report && <p className="admin-empty">No run yet. Start one to measure accuracy on this project.</p>}
    </section>
  )
}
