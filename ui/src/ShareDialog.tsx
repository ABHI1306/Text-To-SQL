import { useState } from 'react'

import { callApi, failureMessage, useResource } from './adminApi'
import type { ApiRequest } from './modelTypes'
import { ScreenState } from './ScreenState'

export type ShareResource = {
  type: 'dashboard' | 'saved_question' | 'thread'
  id: string
  title: string
}
type Share = { id: string; grantee_type: 'user' | 'role'; grantee_id: string; granted_at: string }
type Targets = { users: { id: string; username: string; role: string }[]; roles: { name: string }[] }

/** Lists who can open a resource read-only, and lets its owner add or revoke people and roles. */
export function ShareDialog({
  apiRequest,
  resource,
  onClose,
}: {
  apiRequest: ApiRequest
  resource: ShareResource
  onClose: () => void
}) {
  const shares = useResource<Share[]>(
    apiRequest,
    `/api/v1/shares?resource_type=${resource.type}&resource_id=${resource.id}`,
  )
  const targets = useResource<Targets>(apiRequest, '/api/v1/share-targets')
  const [granteeType, setGranteeType] = useState<'user' | 'role'>('user')
  const [grantee, setGrantee] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const usernames = new Map(targets.data?.users.map((user) => [user.id, user.username]))
  const options =
    granteeType === 'user'
      ? (targets.data?.users ?? []).map((user) => ({ value: user.id, label: `${user.username} (${user.role})` }))
      : (targets.data?.roles ?? []).map((role) => ({ value: role.name, label: role.name }))

  async function run(action: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await action()
      shares.reload()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : failureMessage(null, 'Request failed'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div aria-label={`Share ${resource.title}`} className="share-dialog" role="dialog">
      <div className="admin-toolbar">
        <strong>Share “{resource.title}”</strong>
        <button onClick={onClose} type="button">Close</button>
      </div>
      <p className="muted">
        People you share with can only read. Every number is computed for them, with their own data access.
      </p>
      <form
        className="admin-toolbar"
        onSubmit={(event) => {
          event.preventDefault()
          void run(async () => {
            await callApi(apiRequest, '/api/v1/shares', 'POST', {
              resource_type: resource.type,
              resource_id: resource.id,
              grantee_type: granteeType,
              grantee_id: grantee,
            })
            setGrantee('')
          })
        }}
      >
        <select
          aria-label="Share with a"
          onChange={(event) => {
            setGranteeType(event.target.value as 'user' | 'role')
            setGrantee('')
          }}
          value={granteeType}
        >
          <option value="user">Person</option>
          <option value="role">Role</option>
        </select>
        <select aria-label="Who to share with" onChange={(event) => setGrantee(event.target.value)} value={grantee}>
          <option value="">Choose…</option>
          {options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
        <button disabled={!grantee || busy} type="submit">Share read-only</button>
      </form>
      {error && <p className="admin-state error" role="alert">{error}</p>}
      <ScreenState label="people" resource={targets} />
      <ScreenState label="shares" resource={shares} />
      {shares.data && shares.data.length === 0 && <p className="admin-empty">Not shared with anyone yet.</p>}
      {shares.data && shares.data.length > 0 && (
        <ul aria-label="Current shares" className="admin-cards">
          {shares.data.map((share) => (
            <li key={share.id}>
              <div className="admin-toolbar">
                <span className="status-pill">{share.grantee_type === 'role' ? 'Role' : 'Person'}</span>
                <strong>{share.grantee_type === 'user' ? usernames.get(share.grantee_id) ?? share.grantee_id : share.grantee_id}</strong>
                <button
                  disabled={busy}
                  onClick={() => void run(() => callApi(apiRequest, `/api/v1/shares/${share.id}`, 'DELETE'))}
                  type="button"
                >
                  Revoke
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
