import { useCallback, useEffect, useState, type FormEvent } from 'react'

import type { components } from './api/schema'
import { PasswordField } from './PasswordField'

type Project = components['schemas']['Project']
type CatalogTable = components['schemas']['CatalogTable']
type Mdl = components['schemas']['Mdl']
type LoadedCatalog = {
  step: SetupStep
  selectedTables: { schema: string; table: string }[]
  items: CatalogTable[]
  page: number
  total: number
  mdl?: Mdl
}
type ConnectionDraft = Pick<
  components['schemas']['ConnectionRequest'],
  'displayName' | 'host' | 'port' | 'username' | 'database' | 'ssl'
>
type SetupStep = 'connect' | 'tables' | 'relationships' | 'complete'
type ApiRequest = (path: string, init?: RequestInit) => Promise<Response>

const defaultDraft: ConnectionDraft = {
  displayName: 'PostgreSQL demo',
  host: 'demo-db',
  port: 5432,
  username: 'warehouse_reader',
  database: 'demo',
  ssl: false,
}

async function responseError(response: Response): Promise<Error> {
  try {
    const body = (await response.json()) as { detail?: unknown }
    if (typeof body.detail === 'string') return new Error(body.detail)
    if (body.detail !== undefined) return new Error(JSON.stringify(body.detail))
  } catch {
    return new Error(`Request failed (${response.status})`)
  }
  return new Error(`Request failed (${response.status})`)
}

const STEP_TITLES = {
  connect: {
    title: 'Connect the data source',
    lede: 'Use a read-only database account. The password is stored encrypted on this server.',
  },
  tables: {
    title: 'Select tables to create data models',
    lede: 'Models are built from the selected tables so the assistant understands your data.',
  },
  relationships: {
    title: 'Define relationships',
    lede: 'Foreign keys are added automatically. Review the generated model before saving.',
  },
  complete: {
    title: 'Review and save the model',
    lede: 'Your semantic model is saved. You can keep editing it or deploy it from Connect and model.',
  },
} as const

export function SetupWizard({
  apiRequest,
  existingProject,
  draft,
  onDraftChange,
  onProjectCreated,
  onModelSaved,
}: {
  apiRequest: ApiRequest
  existingProject?: Project
  draft: Partial<ConnectionDraft>
  onDraftChange: (draft: Partial<ConnectionDraft>) => void
  onProjectCreated: () => void
  /** Called after the draft model is saved, so the app can open the model editor. */
  onModelSaved?: () => void
}) {
  const [projectId, setProjectId] = useState(existingProject?.id ?? '')
  const [step, setStep] = useState<SetupStep>('connect')
  const [tables, setTables] = useState<CatalogTable[]>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [search, setSearch] = useState('')
  const [appliedSearch, setAppliedSearch] = useState('')
  const [tablePage, setTablePage] = useState(1)
  const [tableTotal, setTableTotal] = useState(0)
  const [password, setPassword] = useState('')
  const [caCertificate, setCaCertificate] = useState('')
  const [draftJson, setDraftJson] = useState('')
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const form = { ...defaultDraft, ...draft }

  const existingProjectId = existingProject?.id
  const loadCatalog = useCallback(async (
    id: string,
    page: number,
    query: string,
  ): Promise<LoadedCatalog> => {
    const tableQuery = new URLSearchParams({
      search: query,
      page: String(page),
      page_size: '50',
    })
    const [setupResponse, tableResponse] = await Promise.all([
      apiRequest(`/api/v1/projects/${id}/setup`),
      apiRequest(`/api/v1/projects/${id}/catalog/tables?${tableQuery}`),
    ])
    if (!setupResponse.ok) throw await responseError(setupResponse)
    if (!tableResponse.ok) throw await responseError(tableResponse)
    const setup = (await setupResponse.json()) as {
      step: SetupStep
      selected_tables: { schema: string; table: string }[]
    }
    const catalog = (await tableResponse.json()) as {
      items: CatalogTable[]
      page: number
      total: number
    }
    let mdl: Mdl | undefined
    if (setup.step === 'complete') {
      const mdlResponse = await apiRequest(`/api/v1/projects/${id}/mdl`)
      if (mdlResponse.ok) {
        mdl = (await mdlResponse.json()) as Mdl
      }
    }
    return {
      step: setup.step,
      selectedTables: setup.selected_tables,
      items: catalog.items,
      page: catalog.page,
      total: catalog.total,
      mdl,
    }
  }, [apiRequest])

  const applyCatalog = useCallback((catalog: LoadedCatalog) => {
    setStep(catalog.step)
    setSelected(
      new Set(catalog.selectedTables.map(({ schema, table }) => `${schema}.${table}`)),
    )
    setTables(catalog.items)
    setTablePage(catalog.page)
    setTableTotal(catalog.total)
    if (catalog.mdl) setDraftJson(JSON.stringify(catalog.mdl, null, 2))
  }, [])

  function changeCatalogPage(page: number) {
    void loadCatalog(projectId, page, appliedSearch)
      .then(applyCatalog)
      .catch((reason: unknown) => {
        setError(reason instanceof Error ? reason.message : 'Could not load this page')
      })
  }

  useEffect(() => {
    if (!existingProjectId) return
    let active = true
    void loadCatalog(existingProjectId, 1, '')
      .then((catalog) => {
        if (active) applyCatalog(catalog)
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : 'Could not load project setup')
      })
    return () => {
      active = false
    }
  }, [applyCatalog, existingProjectId, loadCatalog])

  async function testConnection() {
    setBusy(true)
    setNotice(null)
    setError(null)
    try {
      const response = await apiRequest('/api/v1/connections/test', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          ...form,
          password,
          sslCaCertificate: caCertificate || null,
        }),
      })
      if (!response.ok) throw await responseError(response)
      const result = (await response.json()) as { server_version: string; latency_ms: number }
      setNotice(`Connected to PostgreSQL ${result.server_version} in ${result.latency_ms} ms.`)
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : 'Connection test failed')
    } finally {
      setBusy(false)
    }
  }

  async function createProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setBusy(true)
    setNotice(null)
    setError(null)
    try {
      const response = await apiRequest('/api/v1/projects', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          ...form,
          password,
          sslCaCertificate: caCertificate || null,
        }),
      })
      if (!response.ok) throw await responseError(response)
      const project = (await response.json()) as Project
      setProjectId(project.id)
      applyCatalog(await loadCatalog(project.id, 1, ''))
      onProjectCreated()
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : 'Could not create the project')
    } finally {
      setBusy(false)
    }
  }

  async function profileTables() {
    setBusy(true)
    setNotice(null)
    setError(null)
    try {
      const selections = Array.from(selected, (key) => {
        const separator = key.indexOf('.')
        return { schema: key.slice(0, separator), table: key.slice(separator + 1) }
      })
      const saved = await apiRequest(`/api/v1/projects/${projectId}/selected-tables`, {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ tables: selections }),
      })
      if (!saved.ok) throw await responseError(saved)
      const response = await apiRequest(`/api/v1/projects/${projectId}/introspect`, {
        method: 'POST',
      })
      if (!response.ok) throw await responseError(response)
      const result = (await response.json()) as { mdl: Mdl; warnings?: string[] }
      setDraftJson(JSON.stringify(result.mdl, null, 2))
      setStep('relationships')
      setNotice(
        ['Draft created. Review descriptions and inferred relationships before saving.', ...(result.warnings ?? [])].join(' '),
      )
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : 'Could not profile the selected tables')
    } finally {
      setBusy(false)
    }
  }

  async function saveModel() {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const mdl = JSON.parse(draftJson) as Mdl
      const document = { ...mdl }
      delete document.project_id
      delete document.revision
      const response = await apiRequest(`/api/v1/projects/${projectId}/mdl`, {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(document),
      })
      if (!response.ok) throw await responseError(response)
      const saved = (await response.json()) as Mdl
      setDraftJson(JSON.stringify(saved, null, 2))
      setStep('complete')
      setNotice(`Semantic model revision ${saved.revision ?? 'saved'} is ready for review.`)
      onProjectCreated()
      onModelSaved?.()
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : 'Could not save the semantic model')
    } finally {
      setBusy(false)
    }
  }

  async function loadSavedModel() {
    setBusy(true)
    setError(null)
    try {
      const response = await apiRequest(`/api/v1/projects/${projectId}/mdl`)
      if (!response.ok) throw await responseError(response)
      const saved = (await response.json()) as Mdl
      setDraftJson(JSON.stringify(saved, null, 2))
      setStep('relationships')
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : 'Could not load the saved model')
    } finally {
      setBusy(false)
    }
  }

  function updateDraft<K extends keyof ConnectionDraft>(
    field: K,
    value: ConnectionDraft[K],
  ) {
    onDraftChange({ ...form, [field]: value })
  }

  const filteredTables = tables

  return (
    <section className="setup-content" aria-label="Connect and model setup">
      <ol className="setup-progress">
        {(['connect', 'tables', 'relationships', 'complete'] as const).map((item, index) => (
          <li className={step === item ? 'active' : ''} key={item}>
            <span>{index + 1}</span>{item === 'complete' ? 'Review' : item[0].toUpperCase() + item.slice(1)}
          </li>
        ))}
      </ol>
      <h1>{STEP_TITLES[step].title}</h1>
      <p className="setup-lede">{STEP_TITLES[step].lede}</p>
      {error && <p className="setup-error" role="alert">{error}</p>}
      {notice && <p className="setup-notice" role="status">{notice}</p>}
      {step === 'connect' && (
        <form className="setup-card setup-form" onSubmit={createProject}>
          <div className="setup-form-grid">
            <label>Project name
              <input onChange={(event) => updateDraft('displayName', event.target.value)} required value={form.displayName} />
            </label>
            <label>Host
              <input onChange={(event) => updateDraft('host', event.target.value)} required value={form.host} />
            </label>
            <label>Port
              <input
                max={65535}
                min={1}
                onChange={(event) => updateDraft('port', Number(event.target.value))}
                required
                type="number"
                value={form.port}
              />
            </label>
            <label>Database
              <input onChange={(event) => updateDraft('database', event.target.value)} required value={form.database} />
            </label>
            <label>Read-only username
              <input onChange={(event) => updateDraft('username', event.target.value)} required value={form.username} />
            </label>
            <PasswordField label="Password"
                autoComplete="new-password"
                onChange={(event) => setPassword(event.target.value)}
                required
                value={password}
              />
          </div>
          <label className="setup-checkbox">
            <input checked={form.ssl} onChange={(event) => updateDraft('ssl', event.target.checked)} type="checkbox" />
            Use TLS
          </label>
          {form.ssl && (
            <label>CA certificate (PEM, optional)
              <textarea onChange={(event) => setCaCertificate(event.target.value)} value={caCertificate} />
            </label>
          )}
          <div className="setup-actions">
            <button
              className="secondary-action"
              disabled={busy}
              onClick={(event) => {
                if (event.currentTarget.form?.reportValidity()) void testConnection()
              }}
              type="button"
            >
              Test connection
            </button>
            <button className="primary-action" disabled={busy} type="submit">
              {busy ? 'Connecting…' : 'Connect and continue'}
            </button>
          </div>
        </form>
      )}
      {step === 'tables' && (
        <section className="setup-card">
          <form
            className="table-search"
            onSubmit={(event) => {
              event.preventDefault()
              setAppliedSearch(search)
              setTablePage(1)
              void loadCatalog(projectId, 1, search)
                .then(applyCatalog)
                .catch((reason: unknown) => {
                  setError(reason instanceof Error ? reason.message : 'Could not search tables')
                })
            }}
          >
            <label>Search tables
              <input onChange={(event) => setSearch(event.target.value)} placeholder="schema.table" value={search} />
            </label>
            <button className="secondary-action" type="submit">Search</button>
          </form>
          <div className="catalog-list">
            <table className="catalog-table">
              <thead>
                <tr><th scope="col" /><th scope="col">Table</th><th className="num" scope="col">Approx. rows</th><th className="num" scope="col">Columns</th></tr>
              </thead>
              <tbody>
                {filteredTables.map((table) => {
                  const key = `${table.schema}.${table.table}`
                  return (
                    <tr className={selected.has(key) ? 'selected' : undefined} key={key}>
                      <td>
                        <input
                          aria-label={`Select ${key}`}
                          checked={selected.has(key)}
                          onChange={(event) => setSelected((current) => {
                            const updated = new Set(current)
                            if (event.target.checked) updated.add(key)
                            else updated.delete(key)
                            return updated
                          })}
                          type="checkbox"
                        />
                      </td>
                      <td className="mono">{key}</td>
                      <td className="num mono">{table.approx_rows.toLocaleString()}</td>
                      <td className="num mono">{table.column_count}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
            {filteredTables.length === 0 && <p className="setup-empty">No matching tables found.</p>}
          </div>
          <div className="table-pagination">
            <span>{tableTotal} tables · Page {tablePage} of {Math.max(1, Math.ceil(tableTotal / 50))}</span>
            <div>
              <button
                className="secondary-action"
                disabled={busy || tablePage <= 1}
                onClick={() => changeCatalogPage(tablePage - 1)}
                type="button"
              >Previous</button>
              <button
                className="secondary-action"
                disabled={busy || tablePage * 50 >= tableTotal}
                onClick={() => changeCatalogPage(tablePage + 1)}
                type="button"
              >Next</button>
            </div>
          </div>
          <button className="primary-action" disabled={busy || selected.size === 0} onClick={() => void profileTables()} type="button">
            {busy ? 'Profiling…' : `Continue with ${selected.size} table${selected.size === 1 ? '' : 's'}`}
          </button>
          {busy && (
            <p className="side-panel-hint" role="status">
              Profiling the tables and asking the local model for descriptions. This can take a few
              minutes on CPU; tables it cannot finish are left for you to describe.
            </p>
          )}
        </section>
      )}
      {(step === 'relationships' || step === 'complete') && (
        <section className="setup-card">
          <div className="setup-actions">
            {step === 'complete' && (
              <button className="secondary-action" disabled={busy} onClick={() => void loadSavedModel()} type="button">
                Load saved model
              </button>
            )}
            {step === 'relationships' && (
              <button className="secondary-action" disabled={busy} onClick={() => setStep('tables')} type="button">
                Change selected tables
              </button>
            )}
            {draftJson && (
              <button className="primary-action" disabled={busy} onClick={() => void saveModel()} type="button">
                {busy ? 'Saving…' : 'Validate and save model'}
              </button>
            )}
          </div>
          {draftJson ? (
            <label>{step === 'complete'
              ? `Saved semantic model — edit and save again to create a new revision`
              : 'Semantic model JSON — edit descriptions or relationships, then validate and save'}
              <textarea
                className="mdl-editor"
                onChange={(event) => setDraftJson(event.target.value)}
                spellCheck={false}
                value={draftJson}
              />
            </label>
          ) : (
            <div className="setup-empty">
              <p>Choose the tables to profile and generate a reviewable semantic-model draft.</p>
              <button className="primary-action" onClick={() => setStep('tables')} type="button">Select tables</button>
            </div>
          )}
        </section>
      )}
    </section>
  )
}
