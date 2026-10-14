import { useState } from 'react'

import { callApi, useResource } from './adminApi'
import { ScreenState } from './ScreenState'
import type { ApiRequest } from './modelTypes'
import type { Role } from './UsersRolesScreen'

type Mapping = { id?: string; source: string; match: string; role: string }
type Source = 'ldap' | 'oidc'

export function IdentityPanel({ apiRequest }: { apiRequest: ApiRequest }) {
  const mappings = useResource<{ items: Mapping[] }>(apiRequest, '/api/v1/identity/mappings')
  const roles = useResource<{ items: Role[] }>(apiRequest, '/api/v1/roles')
  const [source, setSource] = useState<Source>('ldap')
  const [edited, setEdited] = useState<Partial<Record<Source, { match: string; role: string }[]>>>({})
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null)

  const stored = (mappings.data?.items ?? [])
    .filter((item) => item.source === source)
    .map(({ match, role }) => ({ match, role }))
  const rows = edited[source] ?? stored
  const roleNames = roles.data?.items.map((role) => role.name) ?? []
  const set = (next: { match: string; role: string }[]) => setEdited({ ...edited, [source]: next })
  const defaultRole = roleNames.includes('User') ? 'User' : roleNames[0] ?? 'User'

  async function save() {
    setMessage(null)
    try {
      await callApi(apiRequest, '/api/v1/identity/mappings', 'PUT', {
        source,
        mappings: rows.map((row) => ({ match: row.match.trim(), role: row.role })),
      })
      setEdited({ ...edited, [source]: undefined })
      mappings.reload()
      setMessage({ text: `Mappings for ${source.toUpperCase()} saved. They apply at each user's next sign-in.`, error: false })
    } catch (reason: unknown) {
      setMessage({ text: reason instanceof Error ? reason.message : 'Could not save mappings', error: true })
    }
  }

  return (
    <>
      <p className="muted">
        The first matching mapping decides the role, and mappings are re-applied at each sign-in, so removing someone
        from a group removes the role at their next sign-in.
      </p>
      <div className="admin-toolbar">
        <label>Source
          <select onChange={(event) => setSource(event.target.value as Source)} value={source}>
            <option value="ldap">LDAP</option>
            <option value="oidc">OIDC</option>
          </select>
        </label>
      </div>
      {message && <p className={`admin-state${message.error ? ' error' : ''}`} role={message.error ? 'alert' : 'status'}>{message.text}</p>}
      <ScreenState
        label="identity mappings"
        resource={{
          error: mappings.error ?? roles.error,
          loading: mappings.loading || roles.loading,
          reload: () => {
            mappings.reload()
            roles.reload()
          },
        }}
      />
      {mappings.data && (
        <>
          {rows.length === 0 && <p className="admin-empty">No {source.toUpperCase()} group mappings. Members get the default role.</p>}
          <ol className="admin-cards" aria-label={`${source.toUpperCase()} group mappings`}>
            {rows.map((row, index) => (
              <li key={index}>
                <div className="add-row">
                  <input
                    aria-label={`Group ${index + 1}`}
                    onChange={(event) => set(rows.map((item, at) => (at === index ? { ...item, match: event.target.value } : item)))}
                    placeholder="Group name or DN"
                    value={row.match}
                  />
                  <select
                    aria-label={`Role for group ${index + 1}`}
                    onChange={(event) => set(rows.map((item, at) => (at === index ? { ...item, role: event.target.value } : item)))}
                    value={row.role}
                  >
                    {[...new Set([row.role, ...roleNames])].map((name) => <option key={name} value={name}>{name}</option>)}
                  </select>
                </div>
                <div className="admin-actions">
                  <button
                    aria-label={`Move group ${index + 1} up`}
                    disabled={index === 0}
                    onClick={() => set(rows.map((_, at) => rows[at === index ? index - 1 : at === index - 1 ? index : at]))}
                    type="button"
                  >Up</button>
                  <button
                    aria-label={`Move group ${index + 1} down`}
                    disabled={index === rows.length - 1}
                    onClick={() => set(rows.map((_, at) => rows[at === index ? index + 1 : at === index + 1 ? index : at]))}
                    type="button"
                  >Down</button>
                  <button aria-label={`Remove group ${index + 1}`} onClick={() => set(rows.filter((_, at) => at !== index))} type="button">Remove</button>
                </div>
              </li>
            ))}
          </ol>
          <div className="admin-actions">
            <button onClick={() => set([...rows, { match: '', role: defaultRole }])} type="button">Add mapping</button>
            <button className="primary" disabled={rows.some((row) => !row.match.trim())} onClick={() => void save()} type="button">Save mappings</button>
          </div>
        </>
      )}
    </>
  )
}
