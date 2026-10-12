import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'

import { hasCapability } from './api/access'
import type { components } from './api/schema'
import { SetupWizard } from './SetupWizard'
import './App.css'

type Me = components['schemas']['Me']
type AuthResponse = components['schemas']['AuthResponse']
type Page =
  | 'Home'
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

const navigation: { page: Page; group: 'ASK' | 'WORK' | 'ADMIN'; capability: string }[] = [
  { page: 'Home', group: 'ASK', capability: 'ask_question' },
  { page: 'Dashboards', group: 'WORK', capability: 'create_dashboard' },
  { page: 'Scheduled questions', group: 'WORK', capability: 'schedule_question' },
  { page: 'Setup', group: 'ADMIN', capability: 'manage_connection' },
  { page: 'Connect and model', group: 'ADMIN', capability: 'edit_mdl' },
  { page: 'Drift and profiling', group: 'ADMIN', capability: 'manage_profiling' },
  { page: 'Knowledge', group: 'ADMIN', capability: 'manage_knowledge' },
  { page: 'Row policies', group: 'ADMIN', capability: 'manage_policies' },
  { page: 'Users and roles', group: 'ADMIN', capability: 'manage_users' },
  { page: 'Audit', group: 'ADMIN', capability: 'view_audit' },
  { page: 'Evaluation', group: 'ADMIN', capability: 'run_eval' },
]

const threads = [
  'Top 5 customers by net revenue last quarter',
  'Refund rate by product category',
  'Orders delayed over 3 days, EU',
  'Average order value by region',
  'New customers per month, 2026',
  'Inventory below reorder point',
]

const tableRows = [
  ['1', 'Northwind Traders', '1,284,500.00'],
  ['2', 'Helios Retail', '1,102,340.00'],
  ['3', 'Brightside GmbH', '968,720.00'],
  ['4', 'Corvid & Sons', '874,115.00'],
  ['5', 'Alpine Outfitters', '812,060.00'],
]

const sqlText = `SELECT c.customer_name,
       SUM(o.net_revenue) AS net_revenue
FROM orders o
JOIN customers c ON c.id = o.customer_id
WHERE o.order_date >= DATE '2026-07-01'
  AND o.order_date <  DATE '2026-10-01'
GROUP BY c.customer_name
ORDER BY net_revenue DESC
LIMIT 5`

const steps = [
  { title: 'Intent', detail: 'Aggregate: revenue ranking, period “last quarter”', duration: '0.3 s', status: 'done' },
  { title: 'Retrieval', detail: '2 models, 1 SQL pair, 1 instruction', duration: '0.4 s', status: 'done', tags: ['orders', 'customers', 'pair: “revenue by customer”', 'rule: net = gross − refunds'] },
  { title: 'SQL candidate', detail: 'Attempt 1 of 3 · Column order_dt is not in the model', duration: '0.6 s', status: 'error' },
  { title: 'Correction', detail: 'Attempt 2 of 3 · Replaced order_dt with order_date', duration: '0.5 s', status: 'done' },
  { title: 'Compilation', detail: 'Logical SQL compiled to PostgreSQL', duration: '0.1 s', status: 'done' },
  { title: 'Validation', detail: 'Read-only, 1 policy applied, cost within limit', duration: '0.5 s', status: 'done' },
]

const suggestions = [
  'Break it down by region',
  'Compare with the previous quarter',
  'Show as a monthly trend',
]

const reviewRoles: ReviewRole[] = ['DBA', 'Admin', 'User', 'Custom: Analyst+']
const reviewStates = ['Success', 'Streaming', 'Clarifying', 'Out of scope', 'Typed failure', 'Empty result', 'LLM unavailable']
const setupSteps = ['Choose source', '1 Connect', '2 Tables', '3 Relationships']

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
  const [activeThread, setActiveThread] = useState(threads[0])
  const [query, setQuery] = useState('')
  const [search, setSearch] = useState('')
  const [showSql, setShowSql] = useState(false)
  const [copied, setCopied] = useState(false)
  const [traceVisible, setTraceVisible] = useState(true)
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

  const visibleNavigation = useMemo(
    () => navigation.filter(({ capability }) => hasCapability(profile?.capabilities ?? [], capability)),
    [profile],
  )
  const visibleThreads = threads.filter((thread) => thread.toLowerCase().includes(search.toLowerCase()))

  async function submitQuestion(question = query) {
    const trimmed = question.trim()
    if (!trimmed) return
    setQuery('')
    setActiveThread(trimmed)
    setNotice('This is a design preview. The live question pipeline is not connected yet.')
    try {
      await apiRequest('/api/v1/threads/6b8b4567-e89b-12d3-a456-426614174002/ask', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ question: trimmed }),
      })
    } catch {
      setNotice('Mock API is unavailable. The live question pipeline is not connected yet.')
    }
  }

  async function copySql() {
    await navigator.clipboard.writeText(sqlText)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1600)
  }

  const selectedProject = profile?.projects[0]?.name ?? 'Loading project'
  const username = profile?.username ?? 'Loading account'

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
              <label>Password
                <input
                  autoComplete="current-password"
                  onChange={(event) => setLoginPassword(event.target.value)}
                  required
                  type="password"
                  value={loginPassword}
                />
              </label>
            </>
          )}
          {mustChangePassword && (
            <>
              <label>Temporary password
                <input
                  autoComplete="current-password"
                  onChange={(event) => setLoginPassword(event.target.value)}
                  required
                  type="password"
                  value={loginPassword}
                />
              </label>
              <label>New password
                <input
                  autoComplete="new-password"
                  minLength={12}
                  onChange={(event) => setNewPassword(event.target.value)}
                  required
                  type="password"
                  value={newPassword}
                />
              </label>
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
            {(['ASK', 'WORK', 'ADMIN'] as const).map((group) => {
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
          <button className="profile-button" type="button">
            <span className="avatar">{username.slice(0, 1).toUpperCase()}</span>
            <span className="profile-copy"><strong>{username}</strong><small>{profile?.role ?? '…'}</small></span>
            <span className="profile-role">{profile?.role === 'DBA' ? 'DBA' : profile?.role ?? '…'}</span>
          </button>
        </aside>

        {activePage === 'Home' ? (
          <main className={`workspace${traceVisible ? '' : ' trace-collapsed'}`}>
          <header className="workspace-topbar">
            <button className="icon-button menu-button" type="button" aria-label="Toggle navigation">☰</button>
            <div className="breadcrumbs"><strong>{selectedProject}</strong><span>/</span><span>Home</span></div>
            <div className="topbar-actions">
              <span className="local-status"><i />Local only · acme-db-01.internal</span>
              <button className="icon-button notification-button" type="button" aria-label="Notifications">♧<b>2</b></button>
            </div>
          </header>
          {error && <div className="profile-error" role="alert">{error}</div>}

          <div className="workspace-columns">
            <aside className="thread-sidebar">
              <button className="new-thread-button" onClick={() => { setActiveThread('New question'); setNotice(null) }} type="button">
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
              <span className="thread-date">TODAY</span>
              <div className="thread-list">
                {visibleThreads.map((thread) => (
                  <button
                    className={`thread-item${thread === activeThread ? ' active' : ''}`}
                    key={thread}
                    onClick={() => { setActiveThread(thread); setNotice(null) }}
                    type="button"
                  >
                    {thread}
                  </button>
                ))}
              </div>
            </aside>

            <section className="conversation" aria-label="Question and answer">
              <div className="conversation-scroll">
                <div className="question-row">
                  <div className="question-spacer" />
                  <div className="question-bubble">{activeThread}</div>
                </div>

                <div className="result-content">
                  <div className="result-labels">
                    <span className="computed-label">Illustrative sample</span>
                    <span className="model-label">Orders · Customers · model v3</span>
                  </div>

                  {notice && <div className="preview-notice" role="status">{notice}</div>}

                  <section className="trace-card" aria-label="Question execution steps">
                    <div className="trace-heading">
                      <strong>Steps · 6 steps · 2.4 s</strong>
                      <button className="text-button" onClick={() => setTraceVisible(false)} type="button">Hide</button>
                    </div>
                    <div className="trace-steps">
                      {steps.map((step) => (
                        <div className={`trace-step ${step.status}`} key={step.title}>
                          <span className="step-status" aria-hidden="true">{step.status === 'error' ? '×' : '✓'}</span>
                          <div className="step-body">
                            <div className="step-main"><strong>{step.title}</strong><span>{step.detail}</span></div>
                            {step.tags && <div className="trace-tags">{step.tags.map((tag) => <span key={tag}>{tag}</span>)}</div>}
                          </div>
                          <time>{step.duration}</time>
                        </div>
                      ))}
                    </div>
                  </section>
                  {!traceVisible && <button className="show-trace-button" onClick={() => setTraceVisible(true)} type="button">Show execution steps</button>}

                  <section className="data-card">
                    <div className="data-card-heading">
                      <strong>Net revenue by customer · Q3 2026</strong>
                      <div><span>5 rows</span><span className="truncate-label">Truncated: showing 1000 rows</span></div>
                    </div>
                    {showSql ? (
                      <pre className="sql-viewer"><code>{sqlText}</code></pre>
                    ) : (
                      <div className="table-scroll">
                        <table>
                          <thead><tr><th>#</th><th>Customer name</th><th>Net revenue (EUR)</th></tr></thead>
                          <tbody>{tableRows.map(([rank, customer, revenue]) => (
                            <tr key={rank}><td>{rank}</td><td>{customer}</td><td>{revenue}</td></tr>
                          ))}</tbody>
                        </table>
                      </div>
                    )}
                  </section>

                  <p className="answer-copy">
                    Northwind Traders led Q3 2026 with <strong>€1,284,500</strong> in net revenue,
                    followed by Helios Retail (€1,102,340). The top five customers account for €5,041,735.
                  </p>

                  <div className="answer-toolbar">
                    <div className="chart-switch"><span>Chart</span><button type="button">Bar</button><button type="button">Line</button></div>
                    <div className="sql-actions">
                      <button className={showSql ? 'selected' : ''} onClick={() => setShowSql(false)} type="button">Results</button>
                      <button className={showSql ? 'selected' : ''} onClick={() => setShowSql(true)} type="button">Logical SQL</button>
                      <button disabled type="button" title="SQL execution is not implemented yet">Compiled SQL</button>
                    </div>
                  </div>
                  <div className="result-actions">
                    <button onClick={() => setNotice('SQL editing will be available when the compiler and execution pipeline are connected.')} type="button">Edit</button>
                    <button onClick={() => setNotice('Re-run is not available in the design preview.')} type="button">Re-run</button>
                    <button onClick={() => void copySql()} type="button">{copied ? 'Copied' : 'Copy SQL'}</button>
                    <span className="action-divider" />
                    <button aria-label="Good answer" className={feedback === 'up' ? 'feedback-selected' : ''} onClick={() => setFeedback('up')} type="button">👍</button>
                    <button aria-label="Poor answer" className={feedback === 'down' ? 'feedback-selected' : ''} onClick={() => setFeedback('down')} type="button">👎</button>
                    <span className="action-divider" />
                    <button onClick={() => setNotice('Pinning will be available when dashboards are connected.')} type="button">Pin to dashboard</button>
                    <button onClick={() => setNotice('Scheduling will be available when the worker is connected.')} type="button">Schedule</button>
                    <button onClick={() => setNotice('Sharing will be available when sharing permissions are connected.')} type="button">Share</button>
                  </div>

                  <div className="followup-card">
                    <div className="followup-question">what about last year?</div>
                    <span className="followup-context">Computed for you · Used previous question as context</span>
                    <div className="followup-answer">
                      <span>Northwind Traders</span><strong>€1,061,880</strong>
                      <span>Helios Retail</span><strong>€1,009,415</strong>
                    </div>
                    <p>In 2025, Northwind Traders also ranked first. Alpine Outfitters moved up from fifth to fourth place this year.</p>
                    <button className="text-button" onClick={() => setShowSql(true)} type="button">Show SQL</button>
                  </div>
                </div>
              </div>

              <div className="composer-area">
                <div className="suggestion-list">
                  {suggestions.map((suggestion) => (
                    <button key={suggestion} onClick={() => void submitQuestion(suggestion)} type="button">{suggestion}</button>
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
                  <button className="send-button" disabled={!query.trim()} type="submit">Send</button>
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
          <SetupWizard
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
          />
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
      </div>
    </div>
  )
}

function NavGlyph({ page }: { page: Page }) {
  const glyphs: Record<Page, string> = {
    Home: '⌂',
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
