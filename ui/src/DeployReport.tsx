import { useEffect, useState } from 'react'

import { describeFailure, type ApiRequest } from './modelTypes'

type Share = { described: number; total: number; percent: number }
type Report = {
  revision: number
  models: Share
  columns: Share
  needsReview: { model: string; modelDescribed: boolean; undescribedColumns: string[] }[]
  checks: { kind: string; name: string; ok: boolean; errors: { code: string; message: string }[] }[]
  deployable: boolean
}

export function DeployReport({
  apiRequest,
  projectId,
  revision,
  disabledReason,
  onDeploy,
  busy,
}: {
  apiRequest: ApiRequest
  projectId: string
  revision: number | undefined
  disabledReason: string | null
  onDeploy: () => void
  busy: boolean
}) {
  const [loaded, setLoaded] = useState<{ revision: number; report?: Report; error?: string } | null>(null)

  useEffect(() => {
    if (!revision) return
    let current = true
    void apiRequest(`/api/v1/projects/${projectId}/deploy-report?revision=${revision}`)
      .then(async (response) => {
        const body = await response.json().catch(() => null)
        if (!response.ok) throw new Error(describeFailure(body, `Could not load the deploy report (${response.status})`))
        if (current) setLoaded({ revision, report: body as Report })
      })
      .catch((reason: unknown) => {
        if (current) setLoaded({ revision, error: reason instanceof Error ? reason.message : 'Could not load the deploy report' })
      })
    return () => {
      current = false
    }
  }, [apiRequest, projectId, revision])

  if (!revision) return <p className="side-panel-hint">Save the model to generate a deploy report.</p>
  const current = loaded?.revision === revision ? loaded : null
  const error = current?.error
  const report = current?.report
  if (error) return <p className="model-message error" role="alert">{error}</p>
  if (!report) return <p className="side-panel-hint" role="status">Loading the deploy report…</p>

  const failing = report.checks.filter((check) => !check.ok)
  const blocked = disabledReason ?? (report.deployable ? null : 'Fix the failing compiler checks first.')
  return (
    <section className="deploy-report" aria-label="Deploy report">
      <h2>Deploy report · revision {report.revision}</h2>
      <div className="coverage">
        <div>
          <strong>{report.models.percent}%</strong>
          <span>models described ({report.models.described}/{report.models.total})</span>
        </div>
        <div>
          <strong>{report.columns.percent}%</strong>
          <span>columns described ({report.columns.described}/{report.columns.total})</span>
        </div>
        <div>
          <strong>{report.checks.length - failing.length}/{report.checks.length}</strong>
          <span>compiler checks pass</span>
        </div>
      </div>
      <h3>Compiler checks</h3>
      {report.checks.length === 0 && <p className="side-panel-hint">No calculated fields, metrics or relationships to check.</p>}
      <ul className="check-list">
        {report.checks.map((check) => (
          <li key={`${check.kind}-${check.name}`} className={check.ok ? 'ok' : 'failed'}>
            <span aria-hidden="true">{check.ok ? '✓' : '✕'}</span>
            <strong>{check.name}</strong>
            <small>{check.kind.replaceAll('_', ' ')}</small>
            {check.errors.map((item) => <em key={item.message}>{item.code}: {item.message}</em>)}
          </li>
        ))}
      </ul>
      <h3>Needs review</h3>
      {report.needsReview.length === 0 && <p className="side-panel-hint">Every model and column has a description.</p>}
      <ul className="check-list">
        {report.needsReview.map((item) => (
          <li key={item.model}>
            <strong>{item.model}</strong>
            {!item.modelDescribed && <em>no model description</em>}
            {item.undescribedColumns.length > 0 && <em>{item.undescribedColumns.length} undescribed column(s)</em>}
          </li>
        ))}
      </ul>
      <p className="side-panel-hint">Gaps are flagged for review and do not block deployment.</p>
      {blocked && <p className="model-message error" role="alert">{blocked}</p>}
      <button type="button" disabled={busy || blocked !== null} onClick={onDeploy}>
        {busy ? 'Working…' : 'Deploy model'}
      </button>
    </section>
  )
}
