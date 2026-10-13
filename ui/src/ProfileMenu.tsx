import { useEffect, useRef, useState, type FormEvent } from 'react'

import { PasswordField } from './PasswordField'

export function ProfileMenu({
  username,
  role,
  onChangePassword,
  onSignOut,
}: {
  username: string
  role: string | undefined
  /** Resolves to an error message, or null when the password was changed. */
  onChangePassword: (current: string, next: string) => Promise<string | null>
  onSignOut: () => Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const [dialog, setDialog] = useState(false)
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState(false)
  const [busy, setBusy] = useState(false)
  const root = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const close = (event: MouseEvent | KeyboardEvent) => {
      if (event instanceof KeyboardEvent) {
        if (event.key === 'Escape') setOpen(false)
      } else if (root.current && !root.current.contains(event.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', close)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('keydown', close)
    }
  }, [open])

  function closeDialog() {
    setDialog(false)
    setCurrent('')
    setNext('')
    setError(null)
    setDone(false)
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    const failure = await onChangePassword(current, next)
    setBusy(false)
    if (failure) setError(failure)
    else {
      setCurrent('')
      setNext('')
      setDone(true)
    }
  }

  return (
    <div className="profile-menu" ref={root}>
      {open && (
        <div className="profile-popover" role="menu" aria-label="Account menu">
          <div className="profile-popover-head">
            <strong>{username}</strong>
            <small>{role ?? ''}</small>
          </div>
          <button role="menuitem" type="button" onClick={() => { setOpen(false); setDialog(true) }}>
            Change password
          </button>
          <button role="menuitem" type="button" onClick={() => void onSignOut()}>
            Sign out
          </button>
        </div>
      )}
      <button
        aria-expanded={open}
        aria-haspopup="menu"
        className="profile-button"
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        <span className="avatar">{username.slice(0, 1).toUpperCase()}</span>
        <span className="profile-copy"><strong>{username}</strong><small>{role ?? '…'}</small></span>
        <span className="profile-role">{role ?? '…'}</span>
      </button>
      {dialog && (
        <div className="dialog-backdrop" role="presentation">
          <form className="dialog" role="dialog" aria-modal="true" aria-label="Change password" onSubmit={(event) => void submit(event)}>
            <h2>Change password</h2>
            {done ? (
              <>
                <p role="status">Password changed. Your other sessions were signed out.</p>
                <button type="button" onClick={closeDialog}>Close</button>
              </>
            ) : (
              <>
                <PasswordField label="Current password" autoComplete="current-password" required value={current} onChange={(event) => setCurrent(event.target.value)} />
                <PasswordField label="New password" autoComplete="new-password" minLength={12} required value={next} onChange={(event) => setNext(event.target.value)} />
                <p className="side-panel-hint">At least 12 characters.</p>
                {error && <p className="auth-error" role="alert">{error}</p>}
                <div className="dialog-actions">
                  <button type="button" onClick={closeDialog}>Cancel</button>
                  <button type="submit" disabled={busy}>{busy ? 'Changing…' : 'Change password'}</button>
                </div>
              </>
            )}
          </form>
        </div>
      )}
    </div>
  )
}
