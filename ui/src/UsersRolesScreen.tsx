import { useEffect, useRef, useState, type FormEvent } from 'react'

import { callApi, useResource } from './adminApi'
import { ScreenState } from './ScreenState'
import { diffCapabilities, isLocked, parseAttributes } from './adminLogic'
import { IdentityPanel } from './IdentityPanel'
import type { ApiRequest } from './modelTypes'
import { RolesPanel } from './RolesPanel'

export type Role = {
  id: string
  name: string
  description?: string
  is_system: boolean
  capabilities: string[]
}
type User = {
  id: string
  username: string
  role: string
  status?: string
  auth_source?: string
  locked_until?: string | null
  must_change_password?: boolean
}
type Project = { id: string; name: string }
type Tab = 'Accounts' | 'Roles' | 'Identity mappings'

export type RoleEffect = (what: string) => Promise<void>

export function UsersRolesScreen({
  apiRequest,
  capabilities,
  refreshProfile,
}: {
  apiRequest: ApiRequest
  capabilities: readonly string[]
  refreshProfile: () => Promise<readonly string[] | null>
}) {
  const [tab, setTab] = useState<Tab>('Accounts')
  const [effect, setEffect] = useState<string | null>(null)
  const canManageRoles = capabilities.includes('manage_roles')
  const tabs: Tab[] = canManageRoles ? ['Accounts', 'Roles', 'Identity mappings'] : ['Accounts']
  const current = useRef(capabilities)
  useEffect(() => {
    current.current = capabilities
  }, [capabilities])

  /** After any role or access change, refetch /me so the next request uses the new capabilities. */
  const notifyChange: RoleEffect = async (what) => {
    const before = current.current
    const after = await refreshProfile()
    if (!after) {
      setEffect(`${what} Could not refresh your own session; sign in again to see your capabilities.`)
      return
    }
    const { gained, lost } = diffCapabilities(before, after)
    const changes = [
      gained.length ? `gained ${gained.join(', ')}` : '',
      lost.length ? `lost ${lost.join(', ')}` : '',
    ].filter(Boolean)
    setEffect(
      changes.length
        ? `${what} Your own capabilities changed on the next request: ${changes.join('; ')}.`
        : `${what} Other accounts pick this up on their next request; your own capabilities are unchanged.`,
    )
  }

  return (
    <section className="admin-screen" aria-label="Users and roles">
      <header className="admin-header">
        <div>
          <span className="eyebrow">SYSTEM</span>
          <h1>Users and roles</h1>
          <p>Manage accounts, custom roles, identity-provider mappings and project access.</p>
        </div>
      </header>
      {effect && (
        <p className="admin-state" role="status" data-testid="role-effect">
          {effect}
          <button aria-label="Dismiss message" onClick={() => setEffect(null)} type="button">×</button>
        </p>
      )}
      <div className="tab-bar" role="tablist" aria-label="Users and roles sections">
        {tabs.map((name) => (
          <button
            aria-selected={tab === name}
            className={tab === name ? 'active' : undefined}
            key={name}
            onClick={() => setTab(name)}
            role="tab"
            type="button"
          >{name}</button>
        ))}
      </div>
      <div className="admin-panel" role="tabpanel" aria-label={tab}>
        {tab === 'Accounts' && <AccountsPanel apiRequest={apiRequest} notifyChange={notifyChange} />}
        {tab === 'Roles' && canManageRoles && <RolesPanel apiRequest={apiRequest} notifyChange={notifyChange} />}
        {tab === 'Identity mappings' && canManageRoles && <IdentityPanel apiRequest={apiRequest} />}
      </div>
    </section>
  )
}

function AccountsPanel({ apiRequest, notifyChange }: { apiRequest: ApiRequest; notifyChange: RoleEffect }) {
  const users = useResource<{ items: User[] }>(apiRequest, '/api/v1/users')
  const roles = useResource<{ items: Role[] }>(apiRequest, '/api/v1/roles')
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null)
  const [resetting, setResetting] = useState<User | null>(null)
  const [selected, setSelected] = useState<User | null>(null)
  const dialog = useRef<HTMLDialogElement>(null)

  async function act(action: () => Promise<unknown>, done: string, role = false) {
    setMessage(null)
    try {
      await action()
      users.reload()
      if (role) await notifyChange(done)
      else setMessage({ text: done, error: false })
    } catch (reason: unknown) {
      setMessage({ text: reason instanceof Error ? reason.message : 'Request failed', error: true })
    }
  }

  const items = users.data?.items
  const roleNames = roles.data?.items.map((role) => role.name) ?? []

  return (
    <>
      <div className="admin-toolbar">
        <button className="primary" onClick={() => dialog.current?.showModal()} type="button">Create user</button>
      </div>
      <CreateUserDialog
        apiRequest={apiRequest}
        dialogRef={dialog}
        roleNames={roleNames}
        onCreated={(name) => {
          users.reload()
          setMessage({ text: `Account ${name} created. They must change the temporary password at first sign-in.`, error: false })
        }}
      />
      {message && <p className={`admin-state${message.error ? ' error' : ''}`} role={message.error ? 'alert' : 'status'}>{message.text}</p>}
      <ScreenState label="accounts" resource={users} />
      {items && items.length === 0 && <p className="admin-empty">No accounts yet.</p>}
      {items && items.length > 0 && (
        <table className="admin-table" aria-label="Accounts">
          <thead>
            <tr>
              <th scope="col">Username</th>
              <th scope="col">Role</th>
              <th scope="col">Source</th>
              <th scope="col">Status</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {items.map((user) => {
              const locked = isLocked(user.locked_until)
              return (
                <tr key={user.id}>
                  <th scope="row">{user.username}</th>
                  <td>
                    <select
                      aria-label={`Role for ${user.username}`}
                      onChange={(event) => void act(
                        () => callApi(apiRequest, `/api/v1/users/${user.id}/role`, 'PUT', { role: event.target.value }),
                        `Role of ${user.username} set to ${event.target.value}.`,
                        true,
                      )}
                      value={user.role}
                    >
                      {[...new Set([user.role, ...roleNames])].map((name) => <option key={name} value={name}>{name}</option>)}
                    </select>
                  </td>
                  <td>{user.auth_source ?? 'local'}</td>
                  <td>
                    {locked ? <span className="pill warn">Locked</span> : user.status && user.status !== 'active'
                      ? <span className="pill warn">{user.status}</span> : <span className="pill">Active</span>}
                    {user.must_change_password && <span className="pill">Password change due</span>}
                  </td>
                  <td>
                    <div className="admin-actions">
                      {locked && (
                        <button
                          aria-label={`Unlock ${user.username}`}
                          onClick={() => void act(
                            () => callApi(apiRequest, `/api/v1/users/${user.id}/unlock`, 'POST'),
                            `Account ${user.username} unlocked.`,
                          )}
                          type="button"
                        >Unlock</button>
                      )}
                      {(user.auth_source ?? 'local') === 'local' && (
                        <button aria-label={`Reset password for ${user.username}`} onClick={() => setResetting(user)} type="button">Reset password</button>
                      )}
                      <button aria-label={`Access and attributes for ${user.username}`} onClick={() => setSelected(user)} type="button">Access</button>
                      <button
                        aria-label={`Delete ${user.username}`}
                        onClick={() => {
                          if (!window.confirm(`Delete account ${user.username}? Their chats, dashboards and saved questions are removed too. This cannot be undone.`)) return
                          void act(
                            () => callApi(apiRequest, `/api/v1/users/${user.id}`, 'DELETE'),
                            `Account ${user.username} deleted.`,
                          )
                        }}
                        type="button"
                      >Delete</button>
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
      {resetting && (
        <ModalShell label={`Reset password for ${resetting.username}`} onDismiss={() => setResetting(null)}>
          <ResetPasswordForm
            apiRequest={apiRequest}
            user={resetting}
            onClose={(done) => {
              setResetting(null)
              if (done) {
                users.reload()
                setMessage({ text: `Password for ${resetting.username} reset. They must change it at next sign-in.`, error: false })
              }
            }}
          />
        </ModalShell>
      )}
      {selected && (
        <ModalShell label={`Access for ${selected.username}`} onDismiss={() => setSelected(null)}>
          <UserAccessPanel apiRequest={apiRequest} key={selected.id} notifyChange={notifyChange} onClose={() => setSelected(null)} user={selected} />
        </ModalShell>
      )}
    </>
  )
}

/** Shows its children in a modal dialog as soon as it mounts; Esc dismisses it. */
function ModalShell({ label, onDismiss, children }: { label: string; onDismiss: () => void; children: React.ReactNode }) {
  const dialog = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const element = dialog.current
    if (element && !element.open) element.showModal()
  }, [])
  return (
    <dialog aria-label={label} className="admin-dialog" onCancel={onDismiss} ref={dialog}>
      {children}
    </dialog>
  )
}

function CreateUserDialog({
  apiRequest,
  dialogRef,
  roleNames,
  onCreated,
}: {
  apiRequest: ApiRequest
  dialogRef: React.RefObject<HTMLDialogElement | null>
  roleNames: string[]
  onCreated: (username: string) => void
}) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState('User')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await callApi(apiRequest, '/api/v1/users', 'POST', { username: username.trim(), password, role })
      onCreated(username.trim())
      setUsername('')
      setPassword('')
      dialogRef.current?.close()
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : 'Could not create the account')
    } finally {
      setBusy(false)
    }
  }

  return (
    <dialog aria-labelledby="create-user-title" className="admin-dialog" ref={dialogRef}>
      <form onSubmit={(event) => void submit(event)}>
        <h2 id="create-user-title">Create user</h2>
        <label>Username
          <input autoComplete="off" minLength={3} onChange={(event) => setUsername(event.target.value)} pattern="[A-Za-z0-9_.@\-]+" required value={username} />
        </label>
        <label>Temporary password
          <input autoComplete="new-password" minLength={12} onChange={(event) => setPassword(event.target.value)} required type="password" value={password} />
        </label>
        <small>At least 12 characters. The user must change it at first sign-in.</small>
        <label>Role
          <select onChange={(event) => setRole(event.target.value)} value={role}>
            {[...new Set(['User', ...roleNames])].map((name) => <option key={name} value={name}>{name}</option>)}
          </select>
        </label>
        {error && <p className="admin-state error" role="alert">{error}</p>}
        <div className="admin-actions">
          <button className="primary" disabled={busy} type="submit">{busy ? 'Creating…' : 'Create account'}</button>
          <button onClick={() => dialogRef.current?.close()} type="button">Cancel</button>
        </div>
      </form>
    </dialog>
  )
}

function ResetPasswordForm({
  apiRequest,
  user,
  onClose,
}: {
  apiRequest: ApiRequest
  user: User
  onClose: (done: boolean) => void
}) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    try {
      await callApi(apiRequest, `/api/v1/users/${user.id}/reset-password`, 'POST', { password })
      onClose(true)
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : 'Could not reset the password')
    }
  }

  return (
    <form aria-label={`Reset password for ${user.username}`} className="admin-form" onSubmit={(event) => void submit(event)}>
      <h2>Reset password for {user.username}</h2>
      <label>New temporary password
        <input autoComplete="new-password" minLength={12} onChange={(event) => setPassword(event.target.value)} required type="password" value={password} />
      </label>
      {error && <p className="admin-state error" role="alert">{error}</p>}
      <div className="admin-actions">
        <button className="primary" type="submit">Set temporary password</button>
        <button onClick={() => onClose(false)} type="button">Cancel</button>
      </div>
    </form>
  )
}

function UserAccessPanel({
  apiRequest,
  user,
  notifyChange,
  onClose,
}: {
  apiRequest: ApiRequest
  user: User
  notifyChange: RoleEffect
  onClose: () => void
}) {
  const projects = useResource<{ items: Project[] }>(apiRequest, '/api/v1/projects')
  const access = useResource<{ project_ids: string[] }>(apiRequest, `/api/v1/users/${user.id}/project-access`)
  const attributes = useResource<{ attributes: Record<string, unknown> }>(apiRequest, `/api/v1/users/${user.id}/attributes`)
  const [chosen, setChosen] = useState<string[] | null>(null)
  const [attributeText, setAttributeText] = useState<string | null>(null)
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null)

  const selectedIds = chosen ?? access.data?.project_ids ?? []
  const attributeValue = attributeText ?? JSON.stringify(attributes.data?.attributes ?? {}, null, 2)

  async function saveAccess() {
    setMessage(null)
    try {
      await callApi(apiRequest, `/api/v1/users/${user.id}/project-access`, 'PUT', { project_ids: selectedIds })
      access.reload()
      setChosen(null)
      await notifyChange(`Project access for ${user.username} saved.`)
    } catch (reason: unknown) {
      setMessage({ text: reason instanceof Error ? reason.message : 'Could not save project access', error: true })
    }
  }

  async function saveAttributes() {
    const parsed = parseAttributes(attributeValue)
    if (!parsed.value) {
      setMessage({ text: parsed.error ?? 'Invalid attributes', error: true })
      return
    }
    setMessage(null)
    try {
      await callApi(apiRequest, `/api/v1/users/${user.id}/attributes`, 'PUT', { attributes: parsed.value })
      attributes.reload()
      setAttributeText(null)
      setMessage({ text: 'Attributes saved. Row policies use them on the next request.', error: false })
    } catch (reason: unknown) {
      setMessage({ text: reason instanceof Error ? reason.message : 'Could not save attributes', error: true })
    }
  }

  return (
    <section aria-label={`Access for ${user.username}`} className="admin-form">
      <h2>Access for {user.username}</h2>
      <ScreenState
        label="access"
        resource={{
          error: access.error ?? projects.error ?? attributes.error,
          loading: access.loading || projects.loading || attributes.loading,
          reload: () => {
            access.reload()
            projects.reload()
            attributes.reload()
          },
        }}
      />
      {message && <p className={`admin-state${message.error ? ' error' : ''}`} role={message.error ? 'alert' : 'status'}>{message.text}</p>}
      <fieldset>
        <legend>Projects this account can open directly</legend>
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
        <button disabled={!access.data} onClick={() => void saveAccess()} type="button">Save project access</button>
      </fieldset>
      <label>Attributes (JSON, used by row policies as {'{{ user.<name> }}'})
        <textarea className="mono" onChange={(event) => setAttributeText(event.target.value)} rows={4} value={attributeValue} />
      </label>
      <div className="admin-actions">
        <button disabled={!attributes.data} onClick={() => void saveAttributes()} type="button">Save attributes</button>
        <button onClick={onClose} type="button">Close</button>
      </div>
    </section>
  )
}
