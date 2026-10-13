import { useCallback, useEffect, useMemo, useState } from 'react'

import { CanvasEditor } from './CanvasEditor'
import { DeployReport } from './DeployReport'
import { MetricsEditor } from './MetricsEditor'
import { describeFailure, layoutFor, type ApiRequest, type Mdl } from './modelTypes'

type Tab = 'Canvas' | 'Metrics' | 'Deploy report'
const TABS: Tab[] = ['Canvas', 'Metrics', 'Deploy report']

function withoutServerFields(mdl: Mdl): Omit<Mdl, 'project_id' | 'revision' | 'status'> {
  const { project_id: _p, revision: _r, status: _s, ...rest } = mdl
  void _p
  void _r
  void _s
  return rest
}

function normalised(document: Mdl): Mdl {
  return { ...document, layout: layoutFor(document) }
}

export function ModelCanvas({
  apiRequest,
  projectId,
}: {
  apiRequest: ApiRequest
  projectId: string | undefined
}) {
  const [draft, setDraft] = useState<Mdl | null>(null)
  const [baseline, setBaseline] = useState('')
  const [tab, setTab] = useState<Tab>('Canvas')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback((document: Mdl) => {
    const next = normalised(document)
    setDraft(next)
    setBaseline(JSON.stringify(withoutServerFields(next)))
  }, [])

  useEffect(() => {
    if (!projectId) return
    let current = true
    void apiRequest(`/api/v1/projects/${projectId}/mdl`)
      .then(async (response) => {
        if (!response.ok) throw new Error(`Could not load semantic model (${response.status})`)
        const document = await response.json() as Mdl
        if (current) load(document)
      })
      .catch((reason: unknown) => {
        if (current) setError(reason instanceof Error ? reason.message : 'Could not load model')
      })
    return () => {
      current = false
    }
  }, [apiRequest, load, projectId])

  const dirty = useMemo(
    () => draft !== null && JSON.stringify(withoutServerFields(draft)) !== baseline,
    [draft, baseline],
  )
  const incomplete = draft?.relationships.some((relationship) => (relationship.condition ?? '').trim() === '') ?? false

  async function save() {
    if (!draft || !projectId) return
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const response = await apiRequest(`/api/v1/projects/${projectId}/mdl`, {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(withoutServerFields(draft)),
      })
      const body = await response.json().catch(() => null)
      if (!response.ok) throw new Error(describeFailure(body, `Could not save the model (${response.status})`))
      load(body as Mdl)
      setNotice('Model saved.')
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : 'Could not save the model')
    } finally {
      setBusy(false)
    }
  }

  async function deploy() {
    if (!draft || !projectId) return
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const revision = draft.revision ? `?revision=${draft.revision}` : ''
      const response = await apiRequest(`/api/v1/projects/${projectId}/deploy${revision}`, { method: 'POST' })
      const body = await response.json().catch(() => null)
      if (!response.ok) throw new Error(describeFailure(body, `Could not deploy model (${response.status})`))
      setNotice(`Model revision ${(body as { version: number }).version} is deployed and ready for questions.`)
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : 'Could not deploy model')
    } finally {
      setBusy(false)
    }
  }

  if (!projectId) {
    return (
      <section className="placeholder-content">
        <h1>Connect and model</h1>
        <p>Create a project in Setup before editing its semantic model.</p>
      </section>
    )
  }

  const deployBlocked = dirty ? 'Save your changes before deploying.' : null
  return (
    <section className="model-workspace" aria-label="Semantic model editor">
      <header className="model-toolbar">
        <div>
          <h1>Connect and model</h1>
          <p>
            {draft ? `${draft.models.length} models · ` : ''}Drag cards to arrange them, drag between cards to relate them, and select anything to edit it.
            {dirty && ' You have unsaved changes.'}
          </p>
        </div>
        <div className="model-toolbar-actions">
          <button className="primary" disabled={!draft || busy || !dirty || incomplete} onClick={() => void save()} type="button">
            {busy ? 'Saving…' : 'Save model'}
          </button>
        </div>
      </header>
      {error && <p className="model-message error" role="alert">{error}</p>}
      {notice && <p className="model-message" role="status">{notice}</p>}
      {incomplete && dirty && (
        <p className="model-message error" role="alert">A relationship has no join condition yet.</p>
      )}
      {!draft ? (
        <div className="model-empty">No saved semantic model is available for this project.</div>
      ) : (
        <>
          <div className="tab-bar" role="tablist" aria-label="Model sections">
            {TABS.map((name) => (
              <button
                key={name}
                role="tab"
                type="button"
                aria-selected={tab === name}
                className={tab === name ? 'active' : undefined}
                onClick={() => setTab(name)}
              >
                {name}
              </button>
            ))}
          </div>
          {tab === 'Canvas' && (
            <CanvasEditor apiRequest={apiRequest} projectId={projectId} mdl={draft} onChange={setDraft} />
          )}
          {tab === 'Metrics' && (
            <MetricsEditor apiRequest={apiRequest} projectId={projectId} mdl={draft} onChange={setDraft} />
          )}
          {tab === 'Deploy report' && (
            <DeployReport
              apiRequest={apiRequest}
              projectId={projectId}
              revision={draft.revision}
              disabledReason={deployBlocked}
              onDeploy={() => void deploy()}
              busy={busy}
            />
          )}
          <table className="model-node-table" aria-label="Models in this project">
            <thead>
              <tr><th scope="col">Model</th><th scope="col">Table</th><th className="num" scope="col">Columns</th></tr>
            </thead>
            <tbody>
              {draft.models.map((model) => (
                <tr key={model.name}>
                  <td className="mono"><strong>{model.name}</strong></td>
                  <td className="mono muted">{model.tableReference}</td>
                  <td className="num mono">{model.columns.length}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  )
}
