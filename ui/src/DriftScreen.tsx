import { useState } from 'react'

import { callApi, useResource } from './adminApi'
import type { ApiRequest } from './modelTypes'
import { ScreenState } from './ScreenState'

type Finding = { kind?: string; model?: string; column?: string; message?: string; [key: string]: unknown }
type Report = {
  id: string
  mdlVersion: number
  hasDrift: boolean
  summary: Record<string, number>
  findings: Finding[]
  draftMdlVersion: number | null
  createdAt: string
}
type ReportPage = { items: Report[]; nextCursor: string | null }

export function describeFinding(finding: Finding): string {
  if (finding.message) return finding.message
  const target = [finding.model, finding.column].filter(Boolean).join('.')
  return `${finding.kind ?? 'change'}${target ? ` · ${target}` : ''}`
}

export function DriftScreen({
  apiRequest,
  projectId,
  canEditModel,
  onOpenModel,
}: {
  apiRequest: ApiRequest
  projectId: string | undefined
  canEditModel: boolean
  onOpenModel: () => void
}) {
  const reports = useResource<ReportPage>(
    apiRequest,
    projectId ? `/api/v1/projects/${projectId}/drift-reports?limit=20` : null,
  )
  const [openId, setOpenId] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null)

  if (!projectId) {
    return (
      <section className="placeholder-content">
        <h1>Drift and profiling</h1>
        <p>Create a project in Setup before profiling it.</p>
      </section>
    )
  }

  async function createDraft(report: Report) {
    setBusy(report.id)
    setMessage(null)
    try {
      const draft = await callApi<{ revision: number }>(
        apiRequest,
        `/api/v1/projects/${projectId}/drift-reports/${report.id}/draft`,
        'POST',
      )
      setMessage({ text: `Draft revision ${draft.revision} created from this report.`, error: false })
      reports.reload()
    } catch (reason: unknown) {
      setMessage({ text: reason instanceof Error ? reason.message : 'Could not create the draft', error: true })
    } finally {
      setBusy(null)
    }
  }

  const items = reports.data?.items

  return (
    <section className="admin-screen" aria-label="Drift and profiling">
      <header className="admin-header">
        <div>
          <span className="eyebrow">PROJECT HEALTH</span>
          <h1>Drift and profiling</h1>
          <p>Scheduled profiling compares the live database with the deployed model and reports differences.</p>
        </div>
      </header>
      {message && (
        <p className={`admin-state${message.error ? ' error' : ''}`} role={message.error ? 'alert' : 'status'}>
          {message.text}{' '}
          {!message.error && <button onClick={onOpenModel} type="button">Open the model</button>}
        </p>
      )}
      <ScreenState label="drift reports" resource={reports} />
      {items && items.length === 0 && (
        <p className="admin-empty">No reports yet. A report appears after the first scheduled profiling run.</p>
      )}
      {items && items.length > 0 && (
        <ul className="admin-cards" aria-label="Drift reports">
          {items.map((report) => (
            <li key={report.id}>
              <div className="admin-toolbar">
                <strong>{new Date(report.createdAt).toLocaleString()}</strong>
                <span className={`status-pill ${report.hasDrift ? 'warn' : 'ok'}`}>
                  {report.hasDrift ? 'Drift found' : 'No drift'}
                </span>
                <span className="muted">compared with model revision {report.mdlVersion}</span>
                <span className="muted">
                  {Object.entries(report.summary).map(([key, count]) => `${count} ${key}`).join(' · ')}
                </span>
                {report.hasDrift && (
                  <button onClick={() => setOpenId(openId === report.id ? null : report.id)} type="button">
                    {openId === report.id ? 'Hide findings' : 'Show findings'}
                  </button>
                )}
                {report.hasDrift && canEditModel && report.draftMdlVersion === null && (
                  <button className="primary" disabled={busy === report.id} onClick={() => void createDraft(report)} type="button">
                    {busy === report.id ? 'Creating…' : 'Create draft from drift'}
                  </button>
                )}
                {report.draftMdlVersion !== null && (
                  <span className="muted">Draft revision {report.draftMdlVersion} created</span>
                )}
              </div>
              {openId === report.id && (
                <ul className="drift-findings">
                  {report.findings.map((finding, index) => (
                    <li key={index}>{describeFinding(finding)}</li>
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
