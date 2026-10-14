import { useState, type FormEvent } from 'react'

import { callApi, useResource } from './adminApi'
import { ScreenState } from './ScreenState'
import type { ApiRequest } from './modelTypes'
import type { Role, RoleEffect } from './UsersRolesScreen'

type Project = { id: string; name: string }
type Draft = { id: string | null; name: string; description: string; capabilities: string[] }

const blank: Draft = { id: null, name: '', description: '', capabilities: [] }

export function RolesPanel({ apiRequest, notifyChange }: { apiRequest: ApiRequest; notifyChange: RoleEffect }) {
  const roles = useResource<{ items: Role[] }>(apiRequest, '/api/v1/roles')
  const catalogue = useResource<{ items: string[] }>(apiRequest, '/api/v1/capabilities')
  const [draft, setDraft] = useState<Draft | null>(null)
  const [accessRole, setAccessRole] = useState<Role | null>(null)
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null)

  async function save(event: FormEvent) {
    event.preventDefault()
    if (!draft) return
    const body = { name: draft.name.trim(), description: draft.description, capabilities: draft.capabilities }
    setMessage(null)
    try {
      if (draft.id) await callApi(apiRequest, `/api/v1/roles/${draft.id}`, 'PUT', body)
      else await callApi(apiRequest, '/api/v1/roles', 'POST', body)
      setDraft(null)
      roles.reload()
      await notifyChange(`Role ${body.name} saved.`)
    } catch (reason: unknown) {
      setMessage({ text: reason instanceof Error ? reason.message : 'Could not save the role', error: true })
    }
  }

  async function remove(role: Role) {
    setMessage(null)
    try {
      await callApi(apiRequest, `/api/v1/roles/${role.id}`, 'DELETE')
      roles.reload()
      await notifyChange(`Role ${role.name} deleted.`)
    } catch (reason: unknown) {
      setMessage({ text: reason instanceof Error ? reason.message : 'Could not delete the role', error: true })
    }
  }

  const items = roles.data?.items
  const names = catalogue.data?.items ?? []

  return (
    <>
      <div className="admin-toolbar">
        <button className="primary" onClick={() => setDraft({ ...blank })} type="button">New custom role</button>
      </div>
      {message && <p className={`admin-state${message.error ? ' error' : ''}`} role={message.error ? 'alert' : 'status'}>{message.text}</p>}
      <ScreenState label="roles" resource={roles} />
      {items && items.length === 0 && <p className="admin-empty">No roles found.</p>}
      {items && items.length > 0 && (
        <ul className="admin-cards" aria-label="Roles">
          {items.map((role) => (
            <li key={role.id}>
              <div>
                <strong>{role.name}</strong>{' '}
                <span className="pill">{role.is_system ? 'System' : 'Custom'}</span>
                {role.description && <p className="muted">{role.description}</p>}
                <p className="muted">{role.capabilities.length ? [...role.capabilities].sort().join(', ') : 'No capabilities'}</p>
              </div>
              <div className="admin-actions">
                {!role.is_system && (
                  <>
                    <button
                      aria-label={`Edit role ${role.name}`}
                      onClick={() => setDraft({ id: role.id, name: role.name, description: role.description ?? '', capabilities: [...role.capabilities] })}
                      type="button"
                    >Edit</button>
                    <button aria-label={`Delete role ${role.name}`} onClick={() => void remove(role)} type="button">Delete</button>
                  </>
                )}
                <button aria-label={`Project access for role ${role.name}`} onClick={() => setAccessRole(role)} type="button">Project access</button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {draft && (
        <form aria-label="Role editor" className="admin-form" onSubmit={(event) => void save(event)}>
          <h2>{draft.id ? `Edit role ${draft.name}` : 'New custom role'}</h2>
          <label>Role name
            <input
              maxLength={64}
              minLength={2}
              onChange={(event) => setDraft({ ...draft, name: event.target.value })}
              pattern="[A-Za-z][A-Za-z0-9 _\-]*"
              required
              value={draft.name}
            />
          </label>
          <label>Description
            <input maxLength={500} onChange={(event) => setDraft({ ...draft, description: event.target.value })} value={draft.description} />
          </label>
          <ScreenState label="capabilities" resource={catalogue} />
          <fieldset>
            <legend>Capabilities</legend>
            <div className="check-grid">
              {names.map((name) => (
                <label className="check" key={name}>
                  <input
                    checked={draft.capabilities.includes(name)}
                    onChange={(event) => setDraft({
                      ...draft,
                      capabilities: event.target.checked
                        ? [...draft.capabilities, name]
                        : draft.capabilities.filter((item) => item !== name),
                    })}
                    type="checkbox"
                  />
                  {name}
                </label>
              ))}
            </div>
          </fieldset>
          <div className="admin-actions">
            <button className="primary" disabled={!draft.name.trim()} type="submit">Save role</button>
            <button onClick={() => setDraft(null)} type="button">Cancel</button>
          </div>
        </form>
      )}
      {accessRole && (
        <RoleAccess apiRequest={apiRequest} key={accessRole.id} notifyChange={notifyChange} onClose={() => setAccessRole(null)} role={accessRole} />
      )}
    </>
  )
}

function RoleAccess({
  apiRequest,
  role,
  notifyChange,
  onClose,
}: {
  apiRequest: ApiRequest
  role: Role
  notifyChange: RoleEffect
  onClose: () => void
}) {
  const projects = useResource<{ items: Project[] }>(apiRequest, '/api/v1/projects')
  const access = useResource<{ project_ids: string[] }>(apiRequest, `/api/v1/roles/${role.id}/project-access`)
  const [chosen, setChosen] = useState<string[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const selectedIds = chosen ?? access.data?.project_ids ?? []

  async function save() {
    setError(null)
    try {
      await callApi(apiRequest, `/api/v1/roles/${role.id}/project-access`, 'PUT', { project_ids: selectedIds })
      access.reload()
      setChosen(null)
      await notifyChange(`Project access for role ${role.name} saved.`)
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : 'Could not save project access')
    }
  }

  return (
    <section aria-label={`Project access for role ${role.name}`} className="admin-form">
      <h2>Project access for role {role.name}</h2>
      <ScreenState
        label="access"
        resource={{
          error: access.error ?? projects.error,
          loading: access.loading || projects.loading,
          reload: () => {
            access.reload()
            projects.reload()
          },
        }}
      />
      {error && <p className="admin-state error" role="alert">{error}</p>}
      <fieldset>
        <legend>Projects every holder of this role can open</legend>
        {projects.data?.items.length === 0 && <p className="admin-empty">No projects exist yet.</p>}
        {projects.data?.items.map((project) => (
          <label className="check" key={project.id}>
            <input
              checked={selectedIds.includes(project.id)}
              onChange={(event) => setChosen(event.target.checked
                ? [...selectedIds, project.id]
                : selectedIds.filter((id) => id !== project.id))}
              type="checkbox"
            />
            {project.name}
          </label>
        ))}
      </fieldset>
      <div className="admin-actions">
        <button className="primary" disabled={!access.data} onClick={() => void save()} type="button">Save role access</button>
        <button onClick={onClose} type="button">Close</button>
      </div>
    </section>
  )
}
