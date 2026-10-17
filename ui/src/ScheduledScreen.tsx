import { useState } from 'react'

import { callApi, useResource } from './adminApi'
import type { ApiRequest } from './modelTypes'
import { ScreenState } from './ScreenState'
import { ShareDialog } from './ShareDialog'

type SavedQuestion = { id: string; question: string }
type Job = {
  id: string
  saved_question_id: string
  schedule: string
  timezone: string
  status: string
  paused_reason: string | null
  next_run_at: string | null
  last_run_at: string | null
  last_status: string | null
}
type Run = {
  id: string
  started_at: string
  status: 'succeeded' | 'failed'
  row_count: number | null
  error: string | null
  trigger: string
}

const PRESETS = [
  { value: 'hourly', label: 'Every hour' },
  { value: 'daily', label: 'Every day at 06:00' },
  { value: 'weekly', label: 'Every Monday at 06:00' },
  { value: 'custom', label: 'Custom (cron)…' },
]

const when = (value: string | null) => (value ? new Date(value).toLocaleString() : '—')

/** Scheduled questions: create from a saved question, pause, run now and read the run history. */
export function ScheduledScreen({ apiRequest, projectId }: { apiRequest: ApiRequest; projectId: string | undefined }) {
  const jobs = useResource<Job[]>(apiRequest, '/api/v1/scheduled-questions')
  const questions = useResource<SavedQuestion[]>(
    apiRequest,
    projectId ? `/api/v1/projects/${projectId}/saved-questions` : null,
  )
  const [questionId, setQuestionId] = useState('')
  const [preset, setPreset] = useState('daily')
  const [cron, setCron] = useState('')
  const [timezone, setTimezone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC')
  const [error, setError] = useState<string | null>(null)
  const [openId, setOpenId] = useState<string | null>(null)

  const titles = new Map(questions.data?.map((item) => [item.id, item.question]))
  const schedule = preset === 'custom' ? cron.trim() : preset

  async function create() {
    setError(null)
    try {
      await callApi(apiRequest, '/api/v1/scheduled-questions', 'POST', {
        saved_question_id: questionId,
        schedule,
        timezone,
      })
      setQuestionId('')
      jobs.reload()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not create the schedule')
    }
  }

  return (
    <section aria-label="Scheduled questions" className="admin-screen">
      <header className="admin-header">
        <div>
          <span className="eyebrow">WORK</span>
          <h1>Scheduled questions</h1>
          <p>Saved questions that run on a schedule, as you and with your own data access. A failed run sends you a notice.</p>
        </div>
      </header>
      <form
        className="admin-toolbar"
        onSubmit={(event) => {
          event.preventDefault()
          void create()
        }}
      >
        <select aria-label="Question to schedule" onChange={(event) => setQuestionId(event.target.value)} value={questionId}>
          <option value="">Choose a saved question…</option>
          {questions.data?.map((item) => <option key={item.id} value={item.id}>{item.question}</option>)}
        </select>
        <select aria-label="Schedule" onChange={(event) => setPreset(event.target.value)} value={preset}>
          {PRESETS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
        </select>
        {preset === 'custom' && (
          <input aria-label="Cron expression" onChange={(event) => setCron(event.target.value)} placeholder="0 6 * * 1-5" value={cron} />
        )}
        <input aria-label="Time zone" onChange={(event) => setTimezone(event.target.value)} value={timezone} />
        <button disabled={!questionId || !schedule} type="submit">Schedule</button>
      </form>
      {questions.data && questions.data.length === 0 && (
        <p className="admin-empty">No saved questions yet. Use “Schedule this question” on an answer, or pin one first.</p>
      )}
      {error && <p className="admin-state error" role="alert">{error}</p>}
      <ScreenState label="schedules" resource={jobs} />
      {jobs.data && jobs.data.length === 0 && <p className="admin-empty">Nothing is scheduled.</p>}
      {jobs.data && jobs.data.length > 0 && (
        <ul aria-label="Schedules" className="admin-cards">
          {jobs.data.map((job) => (
            <JobCard
              apiRequest={apiRequest}
              job={job}
              key={job.id}
              onChanged={jobs.reload}
              onToggle={() => setOpenId(openId === job.id ? null : job.id)}
              open={openId === job.id}
              title={titles.get(job.saved_question_id) ?? 'Saved question'}
            />
          ))}
        </ul>
      )}
    </section>
  )
}

function JobCard({
  apiRequest,
  job,
  title,
  open,
  onToggle,
  onChanged,
}: {
  apiRequest: ApiRequest
  job: Job
  title: string
  open: boolean
  onToggle: () => void
  onChanged: () => void
}) {
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [sharing, setSharing] = useState(false)
  const paused = job.status !== 'active'

  async function act(action: () => Promise<string | void>) {
    setError(null)
    setNotice(null)
    try {
      const message = await action()
      if (message) setNotice(message)
      onChanged()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Request failed')
      onChanged()
    }
  }

  return (
    <li>
      <div className="admin-toolbar">
        <strong>{title}</strong>
        <span className="status-pill">{paused ? 'Paused' : 'Active'}</span>
        {job.last_status === 'failed' && <span className="status-pill error">Last run failed</span>}
        <code>{job.schedule}</code>
        <span className="muted">{job.timezone} · next {when(job.next_run_at)} · last {when(job.last_run_at)}</span>
      </div>
      {paused && job.paused_reason && <p className="muted">Paused: {job.paused_reason.replaceAll('_', ' ')}</p>}
      <div className="admin-toolbar">
        <button
          onClick={() => void act(async () => {
            await callApi(apiRequest, `/api/v1/scheduled-questions/${job.id}/run-now`, 'POST')
            return 'Ran now. See the history below.'
          })}
          type="button"
        >
          Run now
        </button>
        <button
          onClick={() => void act(async () => {
            await callApi(apiRequest, `/api/v1/scheduled-questions/${job.id}/${paused ? 'resume' : 'pause'}`, 'POST')
          })}
          type="button"
        >
          {paused ? 'Resume' : 'Pause'}
        </button>
        <button onClick={onToggle} type="button">{open ? 'Hide history' : 'Run history'}</button>
        <button onClick={() => setSharing((value) => !value)} type="button">Share question</button>
        <button
          onClick={() => void act(async () => {
            if (!window.confirm('Delete this schedule?')) return
            await callApi(apiRequest, `/api/v1/scheduled-questions/${job.id}`, 'DELETE')
          })}
          type="button"
        >
          Delete
        </button>
      </div>
      {notice && <p className="admin-state" role="status">{notice}</p>}
      {error && <p className="admin-state error" role="alert">{error}</p>}
      {sharing && (
        <ShareDialog
          apiRequest={apiRequest}
          onClose={() => setSharing(false)}
          resource={{ type: 'saved_question', id: job.saved_question_id, title }}
        />
      )}
      {open && <RunHistory apiRequest={apiRequest} jobId={job.id} />}
    </li>
  )
}

function RunHistory({ apiRequest, jobId }: { apiRequest: ApiRequest; jobId: string }) {
  const runs = useResource<Run[]>(apiRequest, `/api/v1/scheduled-questions/${jobId}/runs`)
  if (runs.error || runs.loading) return <ScreenState label="history" resource={runs} />
  if (!runs.data || runs.data.length === 0) return <p className="admin-empty">No runs yet.</p>
  return (
    <table aria-label="Run history" className="admin-table">
      <thead><tr><th>Started</th><th>How</th><th>Result</th><th>Rows</th></tr></thead>
      <tbody>
        {runs.data.map((run) => (
          <tr key={run.id}>
            <td>{when(run.started_at)}</td>
            <td>{run.trigger === 'manual' ? 'Run now' : 'Schedule'}</td>
            <td>{run.status === 'succeeded' ? 'Succeeded' : `Failed${run.error ? `: ${run.error}` : ''}`}</td>
            <td>{run.row_count ?? '—'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
