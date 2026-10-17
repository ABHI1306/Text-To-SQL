import { useEffect } from 'react'

import { useResource } from './adminApi'
import type { ApiRequest } from './modelTypes'
import { ScreenState } from './ScreenState'

type Usage = {
  questions: number
  llm_calls: number
  llm_errors: number
  prompt_tokens: number
  completion_tokens: number
  gpu_ms: number
  avg_tokens_per_question: number
  avg_gpu_ms_per_question: number
  recent: { tokens: number; gpu_ms: number; llm_calls: number; latency_ms: number }[]
}

type SchedulerHealth = {
  status: 'ok' | 'degraded'
  checked_at: string
  active: number
  paused: number
  overdue: number
  last_failed: number
  running: number
  stale: number
  failed_24h: number
  succeeded_24h: number
  next_slot: string | null
  last_run_at: string | null
}

const REFRESH_MS = 15_000
const number = new Intl.NumberFormat()
const seconds = (ms: number) => `${(ms / 1000).toFixed(1)} s`
const when = (value: string | null) => (value ? new Date(value).toLocaleString() : '—')

/** Model usage per question and scheduler health: what the local GPU and the worker are doing. */
export function OperationsScreen({ apiRequest }: { apiRequest: ApiRequest }) {
  const usage = useResource<Usage>(apiRequest, '/api/v1/ops/usage')
  const scheduler = useResource<SchedulerHealth>(apiRequest, '/api/v1/ops/scheduler')
  const reloadUsage = usage.reload
  const reloadScheduler = scheduler.reload

  useEffect(() => {
    const timer = window.setInterval(() => {
      reloadUsage()
      reloadScheduler()
    }, REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [reloadUsage, reloadScheduler])

  const u = usage.data
  const s = scheduler.data
  return (
    <section aria-label="Operations" className="admin-screen">
      <header className="admin-header">
        <div>
          <span className="eyebrow">SYSTEM</span>
          <h1>Operations</h1>
          <p>Tokens and GPU time per question, and whether the scheduler is keeping up. Counts start at the last API restart.</p>
        </div>
      </header>

      <h2>Model usage</h2>
      <ScreenState resource={usage} label="model usage" />
      {u && (
        <>
          <dl className="admin-facts" aria-label="Model usage totals">
            <div><dt>Questions answered</dt><dd>{number.format(u.questions)}</dd></div>
            <div><dt>Model calls</dt><dd>{number.format(u.llm_calls)}</dd></div>
            <div><dt>Model errors</dt><dd>{number.format(u.llm_errors)}</dd></div>
            <div><dt>Tokens (prompt / answer)</dt><dd>{number.format(u.prompt_tokens)} / {number.format(u.completion_tokens)}</dd></div>
            <div><dt>GPU time</dt><dd>{seconds(u.gpu_ms)}</dd></div>
            <div><dt>Average tokens per question</dt><dd>{number.format(u.avg_tokens_per_question)}</dd></div>
            <div><dt>Average GPU time per question</dt><dd>{seconds(u.avg_gpu_ms_per_question)}</dd></div>
          </dl>
          {u.recent.length === 0 ? (
            <p className="admin-empty">No questions answered since the last restart.</p>
          ) : (
            <table className="admin-table" aria-label="Recent questions">
              <thead>
                <tr><th>Latest questions</th><th>Tokens</th><th>GPU time</th><th>Model calls</th><th>Latency</th></tr>
              </thead>
              <tbody>
                {[...u.recent].reverse().map((row, index) => (
                  <tr key={index}>
                    <td>{index === 0 ? 'Most recent' : `${index + 1} back`}</td>
                    <td>{number.format(row.tokens)}</td>
                    <td>{seconds(row.gpu_ms)}</td>
                    <td>{row.llm_calls}</td>
                    <td>{seconds(row.latency_ms)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}

      <h2>Scheduler health</h2>
      <ScreenState resource={scheduler} label="scheduler health" />
      {s && (
        <>
          <p
            className={s.status === 'ok' ? 'admin-state' : 'admin-state error'}
            role={s.status === 'ok' ? 'status' : 'alert'}
          >
            {s.status === 'ok'
              ? 'The scheduler is keeping up.'
              : `The scheduler is behind: ${s.overdue} overdue and ${s.stale} stalled. Check that the worker is running.`}
          </p>
          <dl className="admin-facts" aria-label="Scheduler counts">
            <div><dt>Active jobs</dt><dd>{s.active}</dd></div>
            <div><dt>Paused jobs</dt><dd>{s.paused}</dd></div>
            <div><dt>Running now</dt><dd>{s.running}</dd></div>
            <div><dt>Overdue</dt><dd>{s.overdue}</dd></div>
            <div><dt>Stalled</dt><dd>{s.stale}</dd></div>
            <div><dt>Succeeded (24 h)</dt><dd>{s.succeeded_24h}</dd></div>
            <div><dt>Failed (24 h)</dt><dd>{s.failed_24h}</dd></div>
            <div><dt>Next slot</dt><dd>{when(s.next_slot)}</dd></div>
            <div><dt>Last run</dt><dd>{when(s.last_run_at)}</dd></div>
          </dl>
        </>
      )}
    </section>
  )
}
