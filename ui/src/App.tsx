import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'

import { hasCapability } from './api/access'
import type { components } from './api/schema'
import { ModelCanvas } from './ModelCanvas'
import { PasswordField } from './PasswordField'
import { ProfileMenu } from './ProfileMenu'
import { SetupWizard } from './SetupWizard'
import './App.css'

type Me = components['schemas']['Me']
type AuthResponse = components['schemas']['AuthResponse']
type Page =
  | 'Home'
  | 'Shared with me'
  | 'Dashboards'
  | 'Scheduled questions'
  | 'Setup'
  | 'Connect and model'
  | 'Drift and profiling'
  | 'Knowledge'
  | 'Row policies'
  | 'Users and roles'
  | 'Audit'
  | 'Evaluation'

type ReviewRole = 'DBA' | 'Admin' | 'User' | 'Custom: Analyst+'
type ReviewTheme = 'Light' | 'Dark'
type ReviewViewport = 'Desktop 1280' | 'Mobile 390'

const navigation: { page: Page; group: 'ASK' | 'WORK' | 'ADMIN' | 'SYSTEM'; capability: string }[] = [
  { page: 'Home', group: 'ASK', capability: 'ask_question' },
  { page: 'Shared with me', group: 'ASK', capability: 'view_shared' },
  { page: 'Dashboards', group: 'WORK', capability: 'create_dashboard' },
  { page: 'Scheduled questions', group: 'WORK', capability: 'schedule_question' },
  { page: 'Setup', group: 'ADMIN', capability: 'manage_connection' },
  { page: 'Connect and model', group: 'ADMIN', capability: 'edit_mdl' },
  { page: 'Drift and profiling', group: 'ADMIN', capability: 'manage_profiling' },
  { page: 'Knowledge', group: 'ADMIN', capability: 'manage_knowledge' },
  { page: 'Row policies', group: 'ADMIN', capability: 'manage_policies' },
  { page: 'Users and roles', group: 'SYSTEM', capability: 'manage_users' },
  { page: 'Audit', group: 'SYSTEM', capability: 'view_audit' },
  { page: 'Evaluation', group: 'SYSTEM', capability: 'run_eval' },
]

// Failed scheduled runs and drift reports arrive with the notification API (Day 7/8).
const notificationCount = 0

const mobileNavigation: Page[] = ['Home', 'Shared with me', 'Dashboards']

const suggestions = [
  'Break it down by region',
  'Compare with the previous quarter',
  'Show as a monthly trend',
]

const reviewRoles: ReviewRole[] = ['DBA', 'Admin', 'User', 'Custom: Analyst+']
const reviewStates = ['Success', 'Streaming', 'Clarifying', 'Out of scope', 'Typed failure', 'Empty result', 'LLM unavailable']
const setupSteps = ['Choose source', '1 Connect', '2 Tables', '3 Relationships']

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return 'NULL'
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return String(value)
  }
  return JSON.stringify(value) ?? String(value)
}

type QueryAnswer = {
  sql: string
  columns: string[]
  rows: unknown[][]
  retrieved_models: string[]
  model_version: number
  elapsed_ms: number
  truncated: boolean
  source: 'postgresql' | 'mock'
}

function App() {
  const [profile, setProfile] = useState<Me | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [authReady, setAuthReady] = useState(false)
  const [accessToken, setAccessToken] = useState<string | null>(null)
  const [mustChangePassword, setMustChangePassword] = useState(false)
  const [loginUsername, setLoginUsername] = useState('')
  const [loginPassword, setLoginPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [connectionDraft, setConnectionDraft] = useState<
    Partial<components['schemas']['ConnectionRequest']>
  >({})
  const [activePage, setActivePage] = useState<Page>('Home')
  const [activeThread, setActiveThread] = useState<string | null>(null)
  const [threadHistory, setThreadHistory] = useState<string[]>([])
  const [query, setQuery] = useState('')
  const [search, setSearch] = useState('')
  const [showSql, setShowSql] = useState(false)
  const [queryAnswer, setQueryAnswer] = useState<QueryAnswer | null>(null)
  const [queryError, setQueryError] = useState<string | null>(null)
  const [queryBusy, setQueryBusy] = useState(false)
  const [copied, setCopied] = useState(false)
  const [userPreview, setUserPreview] = useState('Myself')
  const [feedback, setFeedback] = useState<'up' | 'down' | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [reviewRole, setReviewRole] = useState<ReviewRole>('DBA')
  const [reviewTheme, setReviewTheme] = useState<ReviewTheme>('Light')
  const [reviewViewport, setReviewViewport] = useState<ReviewViewport>('Desktop 1280')
  const [sessionExpired, setSessionExpired] = useState(false)
  const [reviewState, setReviewState] = useState<string | null>(null)
  const [setupStep, setSetupStep] = useState(setupSteps[0])

  useEffect(() => {
    let active = true

    async function restoreSession() {
      try {
        const response = await fetch('/api/v1/auth/refresh', {
          method: 'POST',
          credentials: 'include',
        })
        if (!response.ok) {
          if (active) setAuthReady(true)
          return
        }
        const auth = (await response.json()) as AuthResponse
        if (!active) return
        setAccessToken(auth.access_token)
        setMustChangePassword(auth.must_change_password)
        setAuthReady(true)
        if (!auth.must_change_password) {
          setProfile(await loadProfile(auth.access_token))
        }
      } catch (reason: unknown) {
        if (active) {
          setError(reason instanceof Error ? reason.message : 'Session restore failed')
          setAuthReady(true)
        }
      }
    }
    void restoreSession()

    return () => {
      active = false
    }
  }, [])

  async function loadProfile(token: string): Promise<Me> {
    const response = await fetch('/api/v1/me', {
      headers: { authorization: `Bearer ${token}` },
      credentials: 'include',
    })
    if (!response.ok) throw new Error(`Profile request failed (${response.status})`)
    return (await response.json()) as Me
  }

  const restoreAccessToken = useCallback(async (): Promise<AuthResponse | null> => {
    const response = await fetch('/api/v1/auth/refresh', {
      method: 'POST',
      credentials: 'include',
    })
    if (!response.ok) return null
    const auth = (await response.json()) as AuthResponse
    setAccessToken(auth.access_token)
    setMustChangePassword(auth.must_change_password)
    return auth
  }, [])

  const apiRequest = useCallback(async (path: string, init: RequestInit = {}): Promise<Response> => {
    const perform = (token: string | null) => {
      const headers = new Headers(init.headers)
      if (token) headers.set('authorization', `Bearer ${token}`)
      return fetch(path, { ...init, headers, credentials: 'include' })
    }
    let response = await perform(accessToken)
    if (response.status === 401) {
      const renewed = await restoreAccessToken()
      if (renewed) {
        response = await perform(renewed.access_token)
      } else {
        setAccessToken(null)
        setProfile(null)
        setSessionExpired(true)
      }
    }
    return response
  }, [accessToken, restoreAccessToken])

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError(null)
    try {
      const response = await fetch('/api/v1/auth/login', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ username: loginUsername, password: loginPassword }),
      })
      if (!response.ok) throw new Error('Invalid username or password')
      const auth = (await response.json()) as AuthResponse
      setAccessToken(auth.access_token)
      setMustChangePassword(auth.must_change_password)
      setSessionExpired(false)
      if (auth.must_change_password) return
      setLoginPassword('')
      setProfile(await loadProfile(auth.access_token))
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : 'Sign in failed')
    }
  }

  async function changeTemporaryPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!accessToken) return
    setError(null)
    try {
      const response = await fetch('/api/v1/auth/change-password', {
        method: 'POST',
        headers: {
          authorization: `Bearer ${accessToken}`,
          'content-type': 'application/json',
        },
        credentials: 'include',
        body: JSON.stringify({
          currentPassword: loginPassword,
          newPassword,
        }),
      })
      if (!response.ok) {
        const failure = (await response.json()) as { detail?: string }
        throw new Error(failure.detail ?? 'Could not change the temporary password')
      }
      setMustChangePassword(false)
      setLoginPassword('')
      setNewPassword('')
      setProfile(await loadProfile(accessToken))
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : 'Password change failed')
    }
  }

  async function changePassword(current: string, next: string): Promise<string | null> {
    try {
      const response = await apiRequest('/api/v1/auth/change-password', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ currentPassword: current, newPassword: next }),
      })
      if (response.ok) return null
      const failure = await response.json().catch(() => null) as { detail?: unknown } | null
      return typeof failure?.detail === 'string' ? failure.detail : 'Could not change the password'
    } catch {
      return 'Could not reach the server'
    }
  }

  async function signOut() {
    try {
      await fetch('/api/v1/auth/logout', { method: 'POST', credentials: 'include' })
    } finally {
      setAccessToken(null)
      setProfile(null)
      setSessionExpired(false)
      setLoginPassword('')
      setActivePage('Home')
      setQueryAnswer(null)
      setThreadHistory([])
    }
  }

  const visibleNavigation = useMemo(
    () => navigation.filter(({ capability }) => hasCapability(profile?.capabilities ?? [], capability)),
    [profile],
  )
  const visibleThreads = threadHistory.filter((thread) =>
    thread.toLowerCase().includes(search.toLowerCase()),
  )

  async function submitQuestion(question = query) {
    const trimmed = question.trim()
    if (!trimmed) return
    const projectId = profile?.projects[0]?.id
    if (!projectId) {
      setQueryError('Create or select a PostgreSQL project before asking questions.')
      return
    }
    setQuery('')
    setActiveThread(trimmed)
    setThreadHistory((history) => [
      trimmed,
      ...history.filter((item) => item !== trimmed),
    ].slice(0, 20))
    setQueryAnswer(null)
    setQueryError(null)
    setNotice(null)
    setQueryBusy(true)
    try {
      const response = await apiRequest(`/api/v1/projects/${projectId}/query`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ question: trimmed }),
      })
      if (!response.ok) {
        const failure = (await response.json()) as { detail?: unknown }
        const message = typeof failure.detail === 'string'
          ? failure.detail
          : `Question failed (${response.status})`
        throw new Error(message)
      }
      setQueryAnswer((await response.json()) as QueryAnswer)
    } catch (reason: unknown) {
      setQueryError(reason instanceof Error ? reason.message : 'Question failed')
    } finally {
      setQueryBusy(false)
    }
  }

  async function copySql(sql: string) {
    await navigator.clipboard.writeText(sql)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1600)
  }

  const selectedProject = profile?.projects[0]?.name ?? 'Loading project'
  const username = profile?.username ?? 'Loading account'
  const selectedProjectId = profile?.projects[0]?.id

  if (!authReady) {
    return <main className="auth-page" aria-live="polite">Checking local session…</main>
  }

  if (!profile) {
    return (
      <main className="auth-page">
        <form
          className="auth-card"
          onSubmit={mustChangePassword ? changeTemporaryPassword : signIn}
        >
          <span className="brand-mark">A</span>
          <p className="auth-eyebrow">LOCAL TEXT-TO-SQL WORKSPACE</p>
          <h1>{mustChangePassword ? 'Set your new password' : 'Welcome back'}</h1>
          <p className="auth-description">
            {mustChangePassword
              ? 'Change the initial temporary password before using the workspace.'
              : 'Sign in to your local project workspace.'}
          </p>
          {!mustChangePassword && (
            <>
              <label>Username
                <input
                  autoComplete="username"
                  onChange={(event) => setLoginUsername(event.target.value)}
                  required
                  value={loginUsername}
                />
              </label>
              <PasswordField label="Password"
                  autoComplete="current-password"
                  onChange={(event) => setLoginPassword(event.target.value)}
                  required
                  value={loginPassword}
                />
            </>
          )}
          {mustChangePassword && (
            <>
              <PasswordField label="Temporary password"
                  autoComplete="current-password"
                  onChange={(event) => setLoginPassword(event.target.value)}
                  required
                  value={loginPassword}
                />
              <PasswordField label="New password"
                  autoComplete="new-password"
                  minLength={12}
                  onChange={(event) => setNewPassword(event.target.value)}
                  required
                  value={newPassword}
                />
            </>
          )}
          {error && <p className="auth-error" role="alert">{error}</p>}
          {sessionExpired && <p className="auth-error" role="status">Your session expired. Sign in again.</p>}
          <button className="auth-submit" type="submit">
            {mustChangePassword ? 'Change password' : 'Sign in'}
          </button>
        </form>
      </main>
    )
  }

  return (
    <div
      className={`app-frame${reviewViewport === 'Mobile 390' ? ' mobile-preview' : ' desktop-preview'}`}
      data-review-theme={reviewTheme.toLowerCase()}
    >
      <div className="review-header">
        <div className="review-toolbar">
          <strong className="review-title">Design review</strong>
          <label className="review-control">Screen
            <select
              aria-label="Review screen"
              onChange={(event) => {
                const page = navigation.find(({ page: option }) => option === event.target.value)?.page
                if (page) setActivePage(page)
              }}
              value={activePage}
            >
              {navigation.map(({ page }) => <option key={page} value={page}>{page}</option>)}
            </select>
          </label>
          <div className="review-control-group" aria-label="Role preview">
            <span>Role</span>
            {reviewRoles.map((role) => (
              <button
                aria-pressed={reviewRole === role}
                key={role}
                onClick={() => setReviewRole(role)}
                title="Visual preview only; this does not change authentication or permissions"
                type="button"
              >{role}</button>
            ))}
          </div>
          <div className="review-control-group" aria-label="Theme preview">
            <span>Theme</span>
            {(['Light', 'Dark'] as const).map((theme) => (
              <button aria-pressed={reviewTheme === theme} key={theme} onClick={() => setReviewTheme(theme)} type="button">{theme}</button>
            ))}
          </div>
          <div className="review-control-group" aria-label="Viewport preview">
            {(['Desktop 1280', 'Mobile 390'] as const).map((viewport) => (
              <button
                aria-pressed={reviewViewport === viewport}
                key={viewport}
                onClick={() => setReviewViewport(viewport)}
                type="button"
              >{viewport}</button>
            ))}
          </div>
          <button
            aria-pressed={sessionExpired}
            className="review-toggle"
            onClick={() => setSessionExpired((expired) => !expired)}
            type="button"
          >Session-expired banner</button>
          {activePage === 'Home' && (
            <div className="review-control-group review-state-controls" aria-label="Follow-up turn preview">
              <span>Follow-up turn</span>
              {reviewStates.map((state) => (
                <button
                  aria-pressed={reviewState === state}
                  key={state}
                  onClick={() => setReviewState(state)}
                  type="button"
                >{state}</button>
              ))}
            </div>
          )}
          {activePage === 'Setup' && (
            <div className="review-control-group" aria-label="Setup step preview">
              <span>Step</span>
              {setupSteps.map((step) => (
                <button
                  aria-pressed={setupStep === step}
                  key={step}
                  onClick={() => setSetupStep(step)}
                  type="button"
                >{step}</button>
              ))}
            </div>
          )}
        </div>
        {sessionExpired && <div className="session-expired" role="alert">Your session has expired. Sign in again to continue.</div>}
        {reviewState && activePage === 'Home' && (
          <div className="review-state-note" role="status">
            Preview state: {reviewState}. This is a design-only simulation; it does not run the question pipeline.
            <button aria-label="Clear follow-up preview" onClick={() => setReviewState(null)} type="button">×</button>
          </div>
        )}
        {activePage === 'Setup' && (
          <div className="review-state-note" role="status">Design focus: {setupStep}. The live setup workflow is below.</div>
        )}
      </div>
      <div className="app-shell">
        <aside className="product-sidebar">
          <a className="brand" href="/" aria-label="Amazatic home">
            <span className="brand-mark">A</span>
            <span>Amazatic</span>
          </a>
          <button className="project-switcher" type="button">
            <span className="project-switcher-name">{selectedProject}</span>
            <span className="model-version">v3 active</span>
            <small>acme_prod · PostgreSQL 16</small>
          </button>
          <nav className="product-nav" aria-label="Main navigation">
            {(['ASK', 'WORK', 'ADMIN', 'SYSTEM'] as const).map((group) => {
              const items = visibleNavigation.filter((item) => item.group === group)
              if (!items.length) return null
              return (
                <div className="nav-group" key={group}>
                  <span className="nav-group-label">{group}</span>
                  {items.map(({ page }) => (
                    <button
                      className={`product-nav-item${activePage === page ? ' active' : ''}`}
                      key={page}
                      onClick={() => setActivePage(page)}
                      type="button"
                    >
                      <NavGlyph page={page} />
                      <span>{page}</span>
                    </button>
                  ))}
                </div>
              )
            })}
          </nav>
          <ProfileMenu
            username={username}
            role={profile?.role}
            onChangePassword={changePassword}
            onSignOut={signOut}
          />
        </aside>

        {activePage === 'Home' ? (
          <main className="workspace">
          <header className="workspace-topbar">
            <button className="icon-button menu-button" type="button" aria-label="Toggle navigation">☰</button>
            <div className="breadcrumbs"><strong>{selectedProject}</strong><span>/</span><span>Home</span></div>
            <div className="topbar-actions">
              <span className="local-status"><i />Local only · acme-db-01.internal</span>
              <button
                className="icon-button notification-button"
                type="button"
                aria-label={notificationCount ? `Notifications, ${notificationCount} unread` : 'Notifications'}
              >
                ♧{notificationCount > 0 && <b>{notificationCount}</b>}
              </button>
            </div>
          </header>
          {error && <div className="profile-error" role="alert">{error}</div>}

          <div className="workspace-columns">
            <aside className="thread-sidebar">
              <button className="new-thread-button" onClick={() => {
                setActiveThread(null)
                setQueryAnswer(null)
                setQueryError(null)
                setNotice(null)
              }} type="button">
                <span>＋</span> New thread
              </button>
              <label className="thread-search">
                <span aria-hidden="true">⌕</span>
                <input
                  aria-label="Search threads"
                  onChange={(event) => setSearch(event.target.value)}
                  placeholder="Search threads"
                  value={search}
                />
              </label>
              <span className="thread-date">THIS SESSION</span>
              <div className="thread-list">
                {visibleThreads.map((thread) => (
                  <button
                    className={`thread-item${thread === activeThread ? ' active' : ''}`}
                    key={thread}
                    onClick={() => void submitQuestion(thread)}
                    type="button"
                  >
                    {thread}
                  </button>
                ))}
                {visibleThreads.length === 0 && (
                  <span className="thread-empty">Your questions will appear here.</span>
                )}
              </div>
            </aside>

            <section className="conversation" aria-label="Question and answer">
              <div className="conversation-scroll">
                {!activeThread && !queryBusy && (
                  <div className="conversation-empty">
                    <span className="eyebrow">ASK YOUR DATA</span>
                    <h1>What would you like to know?</h1>
                    <p>Ask a question about the connected PostgreSQL project. Answers show live query results.</p>
                    {!selectedProjectId && <p role="status">Create a project in Setup to get started.</p>}
                  </div>
                )}
                {activeThread && (
                  <div className="question-row">
                    <div className="question-spacer" />
                    <div className="question-bubble">{activeThread}</div>
                  </div>
                )}
                {queryBusy && <div className="query-progress" role="status">Retrieving the deployed model and querying PostgreSQL…</div>}
                {queryError && <div className="query-error" role="alert">{queryError}</div>}
                {notice && <div className="preview-notice" role="status">{notice}</div>}
                {queryAnswer && (
                  <div className="result-content">
                    <div className="result-labels">
                      <span className="computed-label">
                        {queryAnswer.source === 'postgresql' ? 'PostgreSQL results' : 'Mock preview data'}
                      </span>
                      <span className="model-label">
                        {queryAnswer.retrieved_models.join(' · ')} · model v{queryAnswer.model_version}
                      </span>
                    </div>
                    <section className="trace-card" aria-label="Question execution steps">
                      <div className="trace-heading">
                        <strong>Query completed · {queryAnswer.elapsed_ms} ms</strong>
                      </div>
                      <div className="trace-steps">
                        {[
                          ['Retrieval', queryAnswer.retrieved_models.join(', ')],
                          ['SQL safety check', 'Single read-only query; retrieved tables and columns checked'],
                          ['PostgreSQL', `${queryAnswer.rows.length} row${queryAnswer.rows.length === 1 ? '' : 's'} returned${queryAnswer.truncated ? ' (results capped)' : ''}`],
                        ].map(([title, detail]) => (
                          <div className="trace-step done" key={title}>
                            <span className="step-status" aria-hidden="true">✓</span>
                            <div className="step-body">
                              <div className="step-main"><strong>{title}</strong><span>{detail}</span></div>
                            </div>
                          </div>
                        ))}
                      </div>
                    </section>
                    <section className="data-card">
                      <div className="data-card-heading">
                        <strong>Query results</strong>
                        <div>
                          <span>{queryAnswer.rows.length} rows</span>
                          {queryAnswer.truncated && <span className="truncate-label">Showing first {queryAnswer.rows.length} rows</span>}
                        </div>
                      </div>
                      {showSql ? (
                        <pre className="sql-viewer"><code>{queryAnswer.sql}</code></pre>
                      ) : queryAnswer.columns.length === 0 ? (
                        <p className="no-results">The query returned no tabular columns.</p>
                      ) : (
                        <div className="table-scroll">
                          <table>
                            <thead><tr>{queryAnswer.columns.map((column, index) => <th key={`${column}-${index}`}>{column}</th>)}</tr></thead>
                            <tbody>
                              {queryAnswer.rows.map((row, rowIndex) => (
                                <tr key={rowIndex}>
                                  {queryAnswer.columns.map((column, columnIndex) => (
                                    <td key={`${column}-${columnIndex}`}>{formatCell(row[columnIndex])}</td>
                                  ))}
                                </tr>
                              ))}
                              {queryAnswer.rows.length === 0 && (
                                <tr><td colSpan={queryAnswer.columns.length}>No rows matched this question.</td></tr>
                              )}
                            </tbody>
                          </table>
                        </div>
                      )}
                    </section>
                    <div className="answer-toolbar">
                      <div className="sql-actions">
                        <button className={!showSql ? 'selected' : ''} onClick={() => setShowSql(false)} type="button">Results</button>
                        <button className={showSql ? 'selected' : ''} onClick={() => setShowSql(true)} type="button">SQL</button>
                      </div>
                    </div>
                    <div className="result-actions">
                      <button onClick={() => void submitQuestion(activeThread ?? '')} type="button">Re-run</button>
                      <button onClick={() => void copySql(queryAnswer.sql)} type="button">{copied ? 'Copied' : 'Copy SQL'}</button>
                      <span className="action-divider" />
                      <button aria-label="Good answer" className={feedback === 'up' ? 'feedback-selected' : ''} onClick={() => setFeedback('up')} type="button">👍</button>
                      <button aria-label="Poor answer" className={feedback === 'down' ? 'feedback-selected' : ''} onClick={() => setFeedback('down')} type="button">👎</button>
                    </div>
                  </div>
                )}
              </div>

              <div className="composer-area">
                <div className="suggestion-list">
                  {suggestions.map((suggestion) => (
                    <button disabled={!selectedProjectId || queryBusy} key={suggestion} onClick={() => void submitQuestion(suggestion)} type="button">{suggestion}</button>
                  ))}
                </div>
                <form className="question-composer" onSubmit={(event) => { event.preventDefault(); void submitQuestion() }}>
                  <textarea
                    aria-label="Ask a question about your data"
                    onChange={(event) => setQuery(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' && !event.shiftKey) {
                        event.preventDefault()
                        void submitQuestion()
                      }
                    }}
                    placeholder="Ask a question about your data"
                    rows={2}
                    value={query}
                  />
                  <button className="send-button" disabled={!query.trim() || queryBusy || !selectedProjectId} type="submit">{queryBusy ? '…' : 'Send'}</button>
                </form>
                <div className="composer-footer">
                  <span>Enter to send · Shift+Enter for a new line · Esc to stop</span>
                  <label>Preview as user
                    <select aria-label="Preview as user" onChange={(event) => setUserPreview(event.target.value)} value={userPreview}>
                      <option value="Myself">Myself ({username})</option>
                      <option value="EU">maria@acme · EU region</option>
                      <option value="US">jonas@acme · US region</option>
                    </select>
                  </label>
                </div>
              </div>
            </section>
          </div>
          </main>
        ) : activePage === 'Setup' || activePage === 'Connect and model' ? (
          <main className="placeholder-main">
          <header className="workspace-topbar">
            <div className="breadcrumbs"><strong>{selectedProject}</strong><span>/</span><span>{activePage}</span></div>
            <span className="local-status"><i />Local only · PostgreSQL</span>
          </header>
          {activePage === 'Setup' ? <SetupWizard
            apiRequest={apiRequest}
            draft={connectionDraft}
            existingProject={profile.projects[0]}
            onDraftChange={setConnectionDraft}
            onProjectCreated={() => {
              if (accessToken) {
                void loadProfile(accessToken).then(setProfile).catch((reason: unknown) => {
                  setError(reason instanceof Error ? reason.message : 'Could not reload project')
                })
              }
            }}
          /> : <ModelCanvas
            apiRequest={apiRequest}
            projectId={profile.projects[0]?.id}
          />}
          </main>
        ) : (
          <main className="placeholder-main">
          <header className="workspace-topbar">
            <div className="breadcrumbs"><strong>{selectedProject}</strong><span>/</span><span>{activePage}</span></div>
            <span className="local-status"><i />Local only · acme-db-01.internal</span>
          </header>
          <section className="placeholder-content">
            <span className="eyebrow">PROJECT WORKSPACE</span>
            <h1>{activePage}</h1>
            <p>This screen is part of the design reference. Its feature workflow will be implemented in a later task.</p>
            <button className="primary-action" onClick={() => setActivePage('Home')} type="button">Return to Home</button>
          </section>
          </main>
        )}
        <nav className="mobile-bottom-nav" aria-label="Mobile navigation">
          {visibleNavigation
            .filter(({ page }) => mobileNavigation.includes(page))
            .map(({ page }) => (
              <button
                aria-current={activePage === page ? 'page' : undefined}
                className={`mobile-bottom-item${activePage === page ? ' active' : ''}`}
                key={page}
                onClick={() => setActivePage(page)}
                type="button"
              >
                <NavGlyph page={page} />
                <span>{page}</span>
              </button>
            ))}
        </nav>
      </div>
    </div>
  )
}

function NavGlyph({ page }: { page: Page }) {
  const glyphs: Record<Page, string> = {
    Home: '⌂',
    'Shared with me': '⇄',
    Dashboards: '▦',
    'Scheduled questions': '◷',
    Setup: '⚙',
    'Connect and model': '◇',
    'Drift and profiling': '⌁',
    Knowledge: '▤',
    'Row policies': '⬡',
    'Users and roles': '♙',
    Audit: '▣',
    Evaluation: '◉',
  }
  return <span className="nav-glyph" aria-hidden="true">{glyphs[page]}</span>
}

export default App
