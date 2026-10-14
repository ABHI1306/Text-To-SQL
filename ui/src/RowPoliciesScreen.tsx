import { useState, type FormEvent } from 'react'

import { callApi, useResource } from './adminApi'
import { ScreenState } from './ScreenState'
import { splitList } from './adminLogic'
import type { ApiRequest } from './modelTypes'

type Policy = {
  id: string
  name: string
  model: string
  predicate: string
  appliesTo: { roles?: string[]; exemptRoles?: string[] }
  enabled: boolean
}
type Compilation = {
  ok: boolean
  sql: string
  parameters?: Record<string, unknown>
  appliedPolicies?: string[]
  missingAttributes?: string[]
  errors: { code: string; message: string }[]
  preview?: { labeled?: boolean }
}
type SaveResult = Policy & { validation?: { warnings: string[]; testCompilation: Compilation | null } }
type User = { id: string; username: string }
type Draft = {
  id: string | null
  name: string
  model: string
  predicate: string
  roles: string
  exemptRoles: string
  enabled: boolean
}

const blank: Draft = { id: null, name: '', model: '', predicate: '', roles: '', exemptRoles: '', enabled: true }

export function RowPoliciesScreen({
  apiRequest,
  projectId,
  self,
}: {
  apiRequest: ApiRequest
  projectId: string | undefined
  self: { id: string; username: string }
}) {
  const base = projectId ? `/api/v1/projects/${projectId}` : null
  const policies = useResource<{ items: Policy[] }>(apiRequest, base && `${base}/row-policies`)
  const mdl = useResource<{ models: { name: string }[] }>(apiRequest, base && `${base}/mdl`)
  const users = useResource<{ items: User[] }>(apiRequest, '/api/v1/users')
  const [draft, setDraft] = useState<Draft | null>(null)
  const [testUserId, setTestUserId] = useState(self.id)
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null)
  const [saved, setSaved] = useState<SaveResult | null>(null)
  const [previewModel, setPreviewModel] = useState('')
  const [preview, setPreview] = useState<{ user: string; model: string; result: Compilation } | { error: string } | null>(null)
  const [busy, setBusy] = useState(false)

  if (!projectId || !base) {
    return (
      <section className="placeholder-content">
        <h1>Row policies</h1>
        <p>Create a project in Setup before adding row policies.</p>
      </section>
    )
  }

  // /users needs manage_users; policy managers without it can still test as themselves.
  const testUsers: User[] = users.data?.items.length ? users.data.items : [self]
  const modelNames = mdl.data?.models.map((model) => model.name) ?? []
  const items = policies.data?.items
  const noModel = mdl.error !== null && !mdl.data

  async function save(event: FormEvent) {
    event.preventDefault()
    if (!draft) return
    setBusy(true)
    setMessage(null)
    setSaved(null)
    const body = {
      name: draft.name.trim(),
      model: draft.model,
      predicate: draft.predicate.trim(),
      appliesTo: { roles: splitList(draft.roles), exemptRoles: splitList(draft.exemptRoles) },
      enabled: draft.enabled,
      testUserId,
    }
    try {
      const result = draft.id
        ? await callApi<SaveResult>(apiRequest, `${base}/row-policies/${draft.id}`, 'PUT', body)
        : await callApi<SaveResult>(apiRequest, `${base}/row-policies`, 'POST', body)
      setSaved(result)
      setDraft(null)
      policies.reload()
      setMessage({ text: `Policy ${result.name} saved.`, error: false })
    } catch (reason: unknown) {
      setMessage({ text: reason instanceof Error ? reason.message : 'Could not save the policy', error: true })
    } finally {
      setBusy(false)
    }
  }

  async function remove(policy: Policy) {
    setMessage(null)
    try {
      await callApi(apiRequest, `${base}/row-policies/${policy.id}`, 'DELETE')
      policies.reload()
      setMessage({ text: `Policy ${policy.name} deleted.`, error: false })
    } catch (reason: unknown) {
      setMessage({ text: reason instanceof Error ? reason.message : 'Could not delete the policy', error: true })
    }
  }

  async function runPreview(model: string) {
    setBusy(true)
    setPreview(null)
    try {
      const result = await callApi<Compilation>(apiRequest, `${base}/compile-preview`, 'POST', {
        logicalSql: `SELECT * FROM ${model}`,
        previewAsUserId: testUserId,
      })
      const user = testUsers.find((candidate) => candidate.id === testUserId)?.username ?? testUserId
      setPreview({ user, model, result })
    } catch (reason: unknown) {
      setPreview({ error: reason instanceof Error ? reason.message : 'Preview failed' })
    } finally {
      setBusy(false)
    }
  }

  const effectiveModel = previewModel || modelNames[0] || ''

  return (
    <section className="admin-screen" aria-label="Row policies">
      <header className="admin-header">
        <div>
          <span className="eyebrow">GOVERNANCE</span>
          <h1>Row policies</h1>
          <p>The compiler adds these predicates to every query on the model, whoever asks. Test them as a chosen user before relying on them.</p>
        </div>
        <button className="primary" onClick={() => { setSaved(null); setDraft({ ...blank, model: modelNames[0] ?? '' }) }} type="button">New policy</button>
      </header>
      {message && <p className={`admin-state${message.error ? ' error' : ''}`} role={message.error ? 'alert' : 'status'}>{message.text}</p>}
      <ScreenState label="row policies" resource={policies} />
      {items && items.length === 0 && <p className="admin-empty">No row policies yet. Without a policy every permitted user sees every row of a model.</p>}
      {items && items.length > 0 && (
        <ul className="admin-cards" aria-label="Row policies list">
          {items.map((policy) => (
            <li key={policy.id}>
              <div>
                <strong>{policy.name}</strong> <span className="pill">{policy.enabled ? 'Enabled' : 'Disabled'}</span>
                <p className="muted">Model <code>{policy.model}</code></p>
                <pre className="admin-sql">{policy.predicate}</pre>
                <p className="muted">
                  Applies to {policy.appliesTo.roles?.length ? policy.appliesTo.roles.join(', ') : 'all roles'}
                  {policy.appliesTo.exemptRoles?.length ? `; exempt: ${policy.appliesTo.exemptRoles.join(', ')}` : ''}
                </p>
              </div>
              <div className="admin-actions">
                <button
                  aria-label={`Edit policy ${policy.name}`}
                  onClick={() => {
                    setSaved(null)
                    setDraft({
                      id: policy.id,
                      name: policy.name,
                      model: policy.model,
                      predicate: policy.predicate,
                      roles: (policy.appliesTo.roles ?? []).join(', '),
                      exemptRoles: (policy.appliesTo.exemptRoles ?? []).join(', '),
                      enabled: policy.enabled,
                    })
                  }}
                  type="button"
                >Edit</button>
                <button aria-label={`Delete policy ${policy.name}`} onClick={() => void remove(policy)} type="button">Delete</button>
                <button
                  aria-label={`Preview policy ${policy.name} as test user`}
                  onClick={() => { setPreviewModel(policy.model); void runPreview(policy.model) }}
                  type="button"
                >Preview</button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {draft && (
        <form aria-label="Policy editor" className="admin-form" onSubmit={(event) => void save(event)}>
          <h2>{draft.id ? `Edit policy ${draft.name}` : 'New policy'}</h2>
          {noModel && <p className="admin-state error" role="alert">Save a semantic model before adding policies.</p>}
          <label>Policy name
            <input maxLength={64} onChange={(event) => setDraft({ ...draft, name: event.target.value })} required value={draft.name} />
          </label>
          <label>Model
            <select onChange={(event) => setDraft({ ...draft, model: event.target.value })} required value={draft.model}>
              {[...new Set([draft.model, ...modelNames])].filter(Boolean).map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
          </label>
          <label>Predicate (SQL over the model's columns; {'{{ user.<attribute> }}'} becomes a bound parameter)
            <textarea className="mono" onChange={(event) => setDraft({ ...draft, predicate: event.target.value })} placeholder="region = {{ user.region }}" required rows={3} value={draft.predicate} />
          </label>
          <label>Applies to roles (comma separated, empty means all)
            <input onChange={(event) => setDraft({ ...draft, roles: event.target.value })} value={draft.roles} />
          </label>
          <label>Exempt roles (comma separated)
            <input onChange={(event) => setDraft({ ...draft, exemptRoles: event.target.value })} value={draft.exemptRoles} />
          </label>
          <label className="check">
            <input checked={draft.enabled} onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })} type="checkbox" />
            Enabled
          </label>
          <p className="muted">Saving compiles the predicate for the test user selected below and rejects it if it does not compile.</p>
          <div className="admin-actions">
            <button className="primary" disabled={busy || !draft.model} type="submit">{busy ? 'Saving…' : 'Save policy'}</button>
            <button onClick={() => setDraft(null)} type="button">Cancel</button>
          </div>
        </form>
      )}
      {saved?.validation && (
        <section aria-label="Save validation" className="admin-form">
          <h2>Validation of {saved.name}</h2>
          {saved.validation.warnings.length === 0 && <p className="muted">No warnings.</p>}
          <ul>{saved.validation.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
          {saved.validation.testCompilation && <CompilationView compilation={saved.validation.testCompilation} />}
        </section>
      )}
      <section aria-label="Policy preview" className="admin-form">
        <h2>Test-user preview</h2>
        <p className="muted">Compiles <code>SELECT * FROM &lt;model&gt;</code> as the chosen user with the saved, enabled policies. The preview is labelled and audited.</p>
        <div className="add-row">
          <label>Test user
            <select onChange={(event) => setTestUserId(event.target.value)} value={testUserId}>
              {testUsers.map((user) => <option key={user.id} value={user.id}>{user.username}</option>)}
            </select>
          </label>
          <label>Model
            <select onChange={(event) => setPreviewModel(event.target.value)} value={effectiveModel}>
              {modelNames.map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
          </label>
          <button disabled={busy || !effectiveModel} onClick={() => void runPreview(effectiveModel)} type="button">Preview as user</button>
        </div>
        {preview && 'error' in preview && <p className="admin-state error" role="alert">{preview.error}</p>}
        {preview && 'result' in preview && (
          <div role="status" aria-label="Preview result">
            <p>Compiled for <strong>{preview.user}</strong> on <code>{preview.model}</code>{preview.result.preview?.labeled ? ' (labelled preview)' : ''}.</p>
            <CompilationView compilation={preview.result} />
          </div>
        )}
      </section>
    </section>
  )
}

function CompilationView({ compilation }: { compilation: Compilation }) {
  return (
    <div className="compilation">
      <p className={compilation.ok ? 'pill' : 'pill warn'}>{compilation.ok ? 'Compiles' : 'Does not compile'}</p>
      {compilation.errors.length > 0 && (
        <ul role="alert">{compilation.errors.map((error) => <li key={`${error.code}${error.message}`}>{error.code}: {error.message}</li>)}</ul>
      )}
      {compilation.sql && <pre className="admin-sql">{compilation.sql}</pre>}
      {compilation.appliedPolicies && (
        <p className="muted">Applied policies: {compilation.appliedPolicies.length ? compilation.appliedPolicies.join(', ') : 'none'}</p>
      )}
      {compilation.missingAttributes && compilation.missingAttributes.length > 0 && (
        <p className="muted">Missing user attributes: {compilation.missingAttributes.join(', ')}</p>
      )}
      {compilation.parameters && Object.keys(compilation.parameters).length > 0 && (
        <p className="muted">Parameters: <code>{JSON.stringify(compilation.parameters)}</code></p>
      )}
    </div>
  )
}
