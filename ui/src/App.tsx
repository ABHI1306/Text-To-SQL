import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'

import { hasCapability } from './api/access'
import type { components } from './api/schema'
import { AnswerPanel } from './AnswerPanel'
import { AuditScreen } from './AuditScreen'
import { DashboardsScreen } from './DashboardsScreen'
import { DriftScreen } from './DriftScreen'
import { EvaluationScreen } from './EvaluationScreen'
import { callApi } from './adminApi'
import { ThemeToggle } from './ThemeToggle'
import { useTheme } from './theme'
import type { AnswerActions } from './AnswerPanel'
import {
  streamQuestion,
  groupThreads,
  turnsFromThread,
  type AnswerRun,
  type PastTurn,
  type ThreadSummary,
} from './askStream'
import { KnowledgeScreen } from './KnowledgeScreen'
import { ModelCanvas } from './ModelCanvas'
import { ShareDialog } from './ShareDialog'
import { NotificationsPanel } from './NotificationsPanel'
import { PasswordField } from './PasswordField'
import { ProfileMenu } from './ProfileMenu'
import { RowPoliciesScreen } from './RowPoliciesScreen'
import { SetupWizard } from './SetupWizard'
import { ScheduledScreen } from './ScheduledScreen'
import { SharedWithMeScreen } from './SharedWithMeScreen'
import { UsersRolesScreen } from './UsersRolesScreen'
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

const adminPages: Page[] = [
  'Shared with me',
  'Dashboards',
  'Scheduled questions',
  'Drift and profiling',
  'Knowledge',
  'Row policies',
  'Users and roles',
  'Audit',
  'Evaluation',
]

const mobileNavigation: Page[] = ['Home', 'Shared with me', 'Dashboards']

const suggestions = [
  'Break it down by region',
  'Compare with the previous quarter',
  'Show as a monthly trend',
]


const PROJECT_KEY = 'selectedProjectId'

// The refresh token rotates on every use and the server revokes the session when an old one is
// replayed. Two refreshes at once (React dev mode runs effects twice, and several requests can
// get a 401 together) would look like a replay, so concurrent callers share one request.
let refreshInFlight: Promise<AuthResponse | null> | null = null

function refreshSession(): Promise<AuthResponse | null> {
  refreshInFlight ??= fetch('/api/v1/auth/refresh', { method: 'POST', credentials: 'include' })
    .then(async (response) => (response.ok ? ((await response.json()) as AuthResponse) : null))
    .finally(() => {
      refreshInFlight = null
    })
  return refreshInFlight
}

function readStoredProject(): string | null {
  try {
    return window.localStorage.getItem(PROJECT_KEY)
  } catch {
    return null
  }
}

function storeProject(projectId: string) {
  try {
    window.localStorage.setItem(PROJECT_KEY, projectId)
  } catch {
    // Remembering the choice is a convenience only.
  }
}

function projectStatus(project: { setup_step?: string }): string {
  if (!project.setup_step || project.setup_step === 'complete') return 'Model saved'
  return `Setup: ${project.setup_step}`
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
  const [selectedId, setSelectedId] = useState<string | null>(readStoredProject)
  const [addingProject, setAddingProject] = useState(false)
  const [switcherOpen, setSwitcherOpen] = useState(false)
  const switcherRef = useRef<HTMLDivElement>(null)
  const [activeThread, setActiveThread] = useState<string | null>(null)
  const [threads, setThreads] = useState<ThreadSummary[]>([])
  const [threadId, setThreadId] = useState<string | null>(null)
  const [turns, setTurns] = useState<PastTurn[]>([])
  const [query, setQuery] = useState('')
  const [search, setSearch] = useState('')
  const [run, setRun] = useState<AnswerRun | null>(null)
  const [queryError, setQueryError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const [userPreview, setUserPreview] = useState('Myself')
  const [notice, setNotice] = useState<string | null>(null)
  const [sessionExpired, setSessionExpired] = useState(false)
  const [theme, setTheme, resolvedTheme] = useTheme()
  const [methods, setMethods] = useState<{ directory: boolean; sso: boolean }>({ directory: false, sso: false })
  const [notificationCount, setNotificationCount] = useState(0)
  const [noticesOpen, setNoticesOpen] = useState(false)

  useEffect(() => {
    fetch('/api/v1/auth/methods')
      .then((response) => (response.ok ? response.json() : null))
      .then((body: { directory?: boolean; sso?: boolean } | null) => {
        if (body) setMethods({ directory: Boolean(body.directory), sso: Boolean(body.sso) })
      })
      .catch(() => undefined) // the login form works without the extra options
  }, [])

  useEffect(() => {
    let active = true

    async function restoreSession() {
      try {
        const auth = await refreshSession()
        if (!auth) {
          if (active) setAuthReady(true)
          return
        }
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
    const auth = await refreshSession()
    if (!auth) return null
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

  // The project menu closes on any click outside it, so it never sits under the profile menu.
  useEffect(() => {
    if (!switcherOpen) return
    const close = (event: MouseEvent) => {
      if (switcherRef.current && !switcherRef.current.contains(event.target as Node)) setSwitcherOpen(false)
    }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [switcherOpen])

  useEffect(() => {
    if (!profile) return
    let active = true
    apiRequest('/api/v1/notifications?limit=50')
      .then((response) => (response.ok ? response.json() : []))
      .then((items: { read_at: string | null }[]) => {
        if (active) setNotificationCount(items.filter((item) => !item.read_at).length)
      })
      .catch(() => undefined) // the bell simply shows no count
    return () => {
      active = false
    }
  }, [profile, apiRequest])

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
      setRun(null)
      setThreads([])
      setTurns([])
      setThreadId(null)
      setActiveThread(null)
    }
  }

  const visibleNavigation = useMemo(
    () => navigation.filter(({ capability }) => hasCapability(profile?.capabilities ?? [], capability)),
    [profile],
  )
  /** Refetches /me so a role or access change is reflected on the very next request. */
  const refreshProfile = useCallback(async (): Promise<readonly string[] | null> => {
    if (!accessToken) return null
    try {
      const next = await loadProfile(accessToken)
      setProfile(next)
      return next.capabilities
    } catch {
      return null
    }
  }, [accessToken])
  const visibleThreads = threads.filter((thread) =>
    thread.title.toLowerCase().includes(search.toLowerCase()),
  )

  async function openThread(id: string) {
    abortRef.current?.abort()
    setQueryError(null)
    setNotice(null)
    try {
      const body = await callApi<Parameters<typeof turnsFromThread>[0]>(apiRequest, `/api/v1/threads/${id}`)
      const opened = turnsFromThread(body)
      setTurns(opened)
      setThreadId(id)
      setActiveThread(null)
      setRun(null)
      // Show each answer with its table, as it looked when it was asked. A failed recompute
      // leaves that turn with its "Show results" button.
      void Promise.allSettled(
        opened.filter((turn) => turn.run.logicalSql).map((turn) => fillTurnRows(turn)),
      )
    } catch (reason: unknown) {
      setQueryError(reason instanceof Error ? reason.message : 'Could not open this conversation')
    }
  }

  function newThread() {
    abortRef.current?.abort()
    setThreadId(null)
    setTurns([])
    setActiveThread(null)
    setRun(null)
    setQueryError(null)
    setNotice(null)
  }

  /** Recomputes one saved answer's rows (they are never stored) and puts them on its turn. */
  async function fillTurnRows(turn: PastTurn) {
    if (!turn.run.messageId) return
    const result = await callApi<{ columns: string[]; rows: unknown[][]; truncated: boolean; sql?: string }>(
      apiRequest,
      `/api/v1/messages/${turn.run.messageId}/run`,
      'POST',
    )
    setTurns((current) => current.map((item) => item.key === turn.key
      ? {
          ...item,
          run: {
            ...item.run,
            columns: result.columns,
            rows: result.rows,
            truncated: result.truncated,
            compiledSql: result.sql ?? item.run.compiledSql,
          },
        }
      : item))
  }

  /** Runs logical SQL again in this conversation (DBA only) and puts the rows on the answer. */
  async function runSqlFor(target: string | null, sql: string) {
    if (!threadId) throw new Error('Ask a question first')
    const result = await callApi<{ columns: string[]; rows: unknown[][]; truncated: boolean; sql?: string }>(
      apiRequest,
      `/api/v1/threads/${threadId}/rerun-sql`,
      'POST',
      { logical_sql: sql },
    )
    const apply = (current: AnswerRun): AnswerRun => ({
      ...current,
      logicalSql: sql,
      columns: result.columns,
      rows: result.rows,
      truncated: result.truncated,
      compiledSql: result.sql ?? current.compiledSql,
    })
    if (target === null) setRun((current) => (current ? apply(current) : current))
    else setTurns((current) => current.map((item) => item.key === target ? { ...item, run: apply(item.run) } : item))
  }

  /** Pin, schedule and share all start from a saved question made from this answer. */
  async function saveAnswer(answer: AnswerRun): Promise<string> {
    if (!selectedProjectId || !answer.logicalSql) throw new Error('There is no SQL to save')
    const saved = await callApi<{ id: string }>(
      apiRequest,
      `/api/v1/projects/${selectedProjectId}/saved-questions`,
      'POST',
      { question: answer.question, logical_sql: answer.logicalSql },
    )
    return saved.id
  }

  function answerActions(answer: AnswerRun): AnswerActions {
    const capabilities = profile?.capabilities ?? []
    const actions: AnswerActions = {}
    if (hasCapability(capabilities, 'create_dashboard')) {
      actions.pin = async () => {
        const questionId = await saveAnswer(answer)
        const projectId = selectedProjectId as string
        const existing = await callApi<{ id: string; title: string }[]>(
          apiRequest,
          `/api/v1/dashboards?project_id=${projectId}`,
        )
        const dashboard = existing[0]
          ?? await callApi<{ id: string; title: string }>(apiRequest, '/api/v1/dashboards', 'POST', {
            project_id: projectId,
            title: 'My dashboard',
          })
        const current = await callApi<{ tiles: { saved_question_id: string; chart_spec: unknown; position_json: unknown }[] }>(
          apiRequest,
          `/api/v1/dashboards/${dashboard.id}`,
        )
        const tiles = [
          ...current.tiles.map((tile) => ({
            saved_question_id: tile.saved_question_id,
            chart_spec: tile.chart_spec ?? null,
            position: tile.position_json ?? {},
          })),
          { saved_question_id: questionId },
        ]
        await callApi(apiRequest, `/api/v1/dashboards/${dashboard.id}/tiles`, 'PUT', { tiles })
        return `Pinned to "${dashboard.title}".`
      }
    }
    if (hasCapability(capabilities, 'schedule_question')) {
      actions.schedule = async (preset) => {
        const questionId = await saveAnswer(answer)
        await callApi(apiRequest, '/api/v1/scheduled-questions', 'POST', {
          saved_question_id: questionId,
          schedule: preset,
        })
        return `Scheduled ${preset}. It runs as you, with your own data access.`
      }
    }
    if (hasCapability(capabilities, 'share')) {
      actions.shareDialog = (onClose) => threadId
        ? <ShareDialog apiRequest={apiRequest} onClose={onClose} resource={{ type: 'thread', id: threadId, title: answer.question }} />
        : <p className="action-note error" role="alert">Ask a question first.</p>
    }
    return actions
  }


  async function loadTurnRows(key: string) {
    const turn = turns.find((item) => item.key === key)
    if (turn) await fillTurnRows(turn)
  }

  async function sendFeedback(messageId: string | null, rating: 'up' | 'down') {
    if (!messageId) return
    await callApi(apiRequest, `/api/v1/messages/${messageId}/feedback`, 'POST', { rating })
  }

  async function submitQuestion(question = query) {
    const trimmed = question.trim()
    if (!trimmed) return
    const projectId = selectedProjectId
    if (!projectId) {
      setQueryError('Create or select a PostgreSQL project before asking questions.')
      return
    }
    setQuery('')
    setQueryError(null)
    setNotice(null)
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    // The previous live answer becomes a finished turn above the new question.
    if (run && activeThread) {
      const finished: PastTurn = { key: run.messageId ?? `local-${turns.length}`, question: activeThread, run }
      setTurns((current) => [...current, finished])
    }
    setActiveThread(trimmed)
    setRun(null)
    try {
      let id = threadId
      if (!id) {
        const created = await callApi<{ id: string }>(apiRequest, `/api/v1/projects/${projectId}/threads`, 'POST')
        id = created.id
        setThreadId(id)
      }
      await streamQuestion(apiRequest, id, trimmed, setRun, controller.signal)
      void refreshThreads()
    } catch (reason: unknown) {
      if (controller.signal.aborted) {
        setRun((current) => current && current.status === 'running'
          ? { ...current, status: 'error', error: { code: 'STOPPED', message: 'You stopped this question.', retryable: false } }
          : current)
      } else {
        setQueryError(reason instanceof Error ? reason.message : 'Question failed')
        setRun(null)
      }
    }
  }

  function switchProject(projectId: string) {
    setSwitcherOpen(false)
    setAddingProject(false)
    if (projectId === selectedProjectId) return
    abortRef.current?.abort()
    setSelectedId(projectId)
    storeProject(projectId)
    // Nothing from the previous database may linger on screen.
    setRun(null)
    setQueryError(null)
    setQuery('')
    setActiveThread(null)
    setThreads([])
    setTurns([])
    setThreadId(null)
    setNotice(null)
  }

  function startNewProject() {
    setSwitcherOpen(false)
    setAddingProject(true)
    setConnectionDraft({})
    setActivePage('Setup')
  }

  function projectCreated(projectId: string) {
    if (!accessToken) return
    void loadProfile(accessToken)
      .then((next) => {
        setProfile(next)
        if (next.projects.some((project) => project.id === projectId)) {
          setSelectedId(projectId)
          storeProject(projectId)
        }
      })
      .catch((reason: unknown) => {
        setError(reason instanceof Error ? reason.message : 'Could not reload project')
      })
  }

  // Leaving Setup ends "add project" mode; coming back resumes the active project's setup.
  if (addingProject && activePage !== 'Setup') setAddingProject(false)

  const busy = run?.status === 'running'
  // Exactly one project (one database) is active at a time; every screen reads from it.
  const activeProject = profile?.projects.find((project) => project.id === selectedId) ?? profile?.projects[0]
  const selectedProject = activeProject?.name ?? (profile ? 'No project yet' : 'Loading project')
  const username = profile?.username ?? 'Loading account'
  const selectedProjectId = activeProject?.id

  /** Re-reads the saved conversation list for the active project from the database. */
  const refreshThreads = useCallback(async () => {
    if (!selectedProjectId) return
    try {
      const body = await callApi<{ items: ThreadSummary[] }>(
        apiRequest,
        `/api/v1/projects/${selectedProjectId}/threads`,
      )
      setThreads(body.items)
    } catch {
      // The list is a convenience; asking still works without it.
    }
  }, [apiRequest, selectedProjectId])

  // Load the saved conversations whenever the signed-in user or the project changes.
  useEffect(() => {
    if (!profile || !selectedProjectId) return
    void refreshThreads()
  }, [profile, selectedProjectId, refreshThreads])

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
          {!mustChangePassword && methods.directory && (
            <p className="auth-hint">Directory accounts sign in with their directory username and password.</p>
          )}
          <button className="auth-submit" type="submit">
            {mustChangePassword ? 'Change password' : 'Sign in'}
          </button>
          {!mustChangePassword && methods.sso && (
            <a className="auth-sso" href="/api/v1/auth/oidc/login">Sign in with single sign-on</a>
          )}
        </form>
      </main>
    )
  }

  return (
    <div className="app-frame">
      <div className="app-shell">
        <aside className="product-sidebar">
          <a className="brand" href="/" aria-label="Amazatic home">
            <span className="brand-mark">A</span>
            <span>Amazatic</span>
          </a>
          <div
            className="project-switcher-wrap"
            ref={switcherRef}
            onKeyDown={(event) => { if (event.key === 'Escape') setSwitcherOpen(false) }}
          >
            <button
              aria-expanded={switcherOpen}
              aria-haspopup="listbox"
              aria-label="Switch project"
              className="project-switcher"
              onClick={() => setSwitcherOpen((open) => !open)}
              type="button"
            >
              <span className="project-switcher-name">{selectedProject}</span>
              {activeProject && <span className="model-version">{projectStatus(activeProject)}</span>}
              <small>PostgreSQL</small>
            </button>
            {switcherOpen && (
              <ul aria-label="Projects" className="project-menu" role="listbox">
                {(profile?.projects ?? []).map((project) => (
                  <li key={project.id} role="presentation">
                    <button
                      aria-selected={project.id === selectedProjectId}
                      className="project-menu-item"
                      onClick={() => switchProject(project.id)}
                      role="option"
                      type="button"
                    >
                      <span>{project.name}</span>
                      <small>{projectStatus(project)}</small>
                    </button>
                  </li>
                ))}
                {hasCapability(profile?.capabilities ?? [], 'manage_connection') && (
                  <li role="presentation">
                    <button className="project-menu-item project-menu-add" onClick={startNewProject} role="option" type="button">
                      + Add project
                    </button>
                  </li>
                )}
              </ul>
            )}
          </div>
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
            theme={theme}
            onTheme={setTheme}
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
              <ThemeToggle onTheme={setTheme} resolved={resolvedTheme} />
              <button
                className="icon-button notification-button"
                type="button"
                aria-expanded={noticesOpen}
                aria-label={notificationCount ? `Notifications, ${notificationCount} unread` : 'Notifications'}
                onClick={() => setNoticesOpen((value) => !value)}
              >
                ♧{notificationCount > 0 && <b>{notificationCount}</b>}
              </button>
              {noticesOpen && <NotificationsPanel apiRequest={apiRequest} onUnreadChange={setNotificationCount} />}
            </div>
          </header>
          {error && <div className="profile-error" role="alert">{error}</div>}

          <div className="workspace-columns">
            <aside className="thread-sidebar">
              <button className="new-thread-button" onClick={newThread} type="button">
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
              <div className="thread-list">
                {groupThreads(visibleThreads).map((group) => (
                  <div className="thread-group" key={group.label}>
                    <span className="thread-date">{group.label.toUpperCase()}</span>
                    {group.threads.map((thread) => (
                      <button
                        className={`thread-item${thread.id === threadId ? ' active' : ''}`}
                        key={thread.id}
                        onClick={() => void openThread(thread.id)}
                        title={`${thread.title} (${thread.turns} question${thread.turns === 1 ? '' : 's'})`}
                        type="button"
                      >
                        {thread.title}
                      </button>
                    ))}
                  </div>
                ))}
                {visibleThreads.length === 0 && (
                  <span className="thread-empty">
                    {threads.length === 0 ? 'Your conversations are saved here.' : 'No conversation matches.'}
                  </span>
                )}
              </div>
            </aside>

            <section className="conversation" aria-label="Question and answer">
              <div className="conversation-scroll">
                {turns.map((turn) => (
                  <div className="past-turn" key={turn.key}>
                    <div className="question-row">
                      <div className="question-spacer" />
                      <div className="question-bubble">{turn.question}</div>
                    </div>
                    <AnswerPanel
                      run={turn.run}
                      onAsk={(question) => void submitQuestion(question)}
                      actions={answerActions(turn.run)}
                      onFeedback={(rating) => sendFeedback(turn.run.messageId, rating)}
                      onLoadRows={() => loadTurnRows(turn.key)}
                      onRunSql={hasCapability(profile.capabilities, 'rerun_sql') ? (sql) => runSqlFor(turn.key, sql) : undefined}
                      onRerun={() => void loadTurnRows(turn.key)}
                    />
                  </div>
                ))}
                {!activeThread && !busy && turns.length === 0 && (
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
                {queryError && <div className="query-error" role="alert">{queryError}</div>}
                {notice && <div className="preview-notice" role="status">{notice}</div>}
                {run && (
                  <AnswerPanel
                    run={run}
                    onAsk={(question) => void submitQuestion(question)}
                    actions={answerActions(run)}
                    onFeedback={(rating) => sendFeedback(run.messageId, rating)}
                    onRerun={() => void submitQuestion(activeThread ?? '')}
                    onRunSql={hasCapability(profile.capabilities, 'rerun_sql') ? (sql) => runSqlFor(null, sql) : undefined}
                  />
                )}
              </div>

              <div className="composer-area">
                <div className="suggestion-list">
                  {suggestions.map((suggestion) => (
                    <button disabled={!selectedProjectId || busy} key={suggestion} onClick={() => void submitQuestion(suggestion)} type="button">{suggestion}</button>
                  ))}
                </div>
                <form className="question-composer" onSubmit={(event) => { event.preventDefault(); void submitQuestion() }}>
                  <textarea
                    aria-label="Ask a question about your data"
                    onChange={(event) => setQuery(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === 'Escape') abortRef.current?.abort()
                      if (event.key === 'Enter' && !event.shiftKey) {
                        event.preventDefault()
                        void submitQuestion()
                      }
                    }}
                    placeholder="Ask a question about your data"
                    rows={2}
                    value={query}
                  />
                  <button className="send-button" disabled={!query.trim() || busy || !selectedProjectId} type="submit">{busy ? '…' : 'Send'}</button>
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
            <div className="topbar-actions">
              <span className="local-status"><i />Local only · PostgreSQL</span>
              <ThemeToggle onTheme={setTheme} resolved={resolvedTheme} />
            </div>
          </header>
          {activePage === 'Setup' ? <SetupWizard
            apiRequest={apiRequest}
            draft={connectionDraft}
            existingProject={addingProject ? undefined : activeProject}
            key={addingProject ? 'new-project' : (selectedProjectId ?? 'none')}
            onDraftChange={setConnectionDraft}
            onModelSaved={() => {
              setAddingProject(false)
              setActivePage('Connect and model')
            }}
            onProjectCreated={projectCreated}
          /> : <ModelCanvas
            apiRequest={apiRequest}
            key={selectedProjectId ?? 'none'}
            projectId={selectedProjectId}
          />}
          </main>
        ) : adminPages.includes(activePage) ? (
          <main className="placeholder-main">
          <header className="workspace-topbar">
            <div className="breadcrumbs"><strong>{selectedProject}</strong><span>/</span><span>{activePage}</span></div>
            <div className="topbar-actions">
              <span className="local-status"><i />Local only · PostgreSQL</span>
              <ThemeToggle onTheme={setTheme} resolved={resolvedTheme} />
            </div>
          </header>
          {!visibleNavigation.some(({ page }) => page === activePage) ? (
            <section className="placeholder-content">
              <h1>No access</h1>
              <p role="alert">Your role does not include the capability needed for {activePage}.</p>
              <button className="primary-action" onClick={() => setActivePage('Home')} type="button">Return to Home</button>
            </section>
          ) : activePage === 'Shared with me' ? (
            <SharedWithMeScreen apiRequest={apiRequest} />
          ) : activePage === 'Dashboards' ? (
            <DashboardsScreen apiRequest={apiRequest} key={selectedProjectId ?? 'none'} projectId={selectedProjectId ?? undefined} />
          ) : activePage === 'Scheduled questions' ? (
            <ScheduledScreen apiRequest={apiRequest} key={selectedProjectId ?? 'none'} projectId={selectedProjectId ?? undefined} />
          ) : activePage === 'Drift and profiling' ? (
            <DriftScreen
              apiRequest={apiRequest}
              canEditModel={hasCapability(profile.capabilities, 'edit_mdl')}
              key={selectedProjectId ?? 'none'}
              onOpenModel={() => setActivePage('Connect and model')}
              projectId={selectedProjectId ?? undefined}
            />
          ) : activePage === 'Audit' ? (
            <AuditScreen apiRequest={apiRequest} />
          ) : activePage === 'Evaluation' ? (
            <EvaluationScreen apiRequest={apiRequest} key={selectedProjectId ?? 'none'} projectId={selectedProjectId ?? undefined} />
          ) : activePage === 'Knowledge' ? (
            <KnowledgeScreen
              apiRequest={apiRequest}
              canApprove={hasCapability(profile.capabilities, 'approve_pairs')}
              key={selectedProjectId ?? 'none'}
              projectId={selectedProjectId}
            />
          ) : activePage === 'Row policies' ? (
            <RowPoliciesScreen
              apiRequest={apiRequest}
              key={selectedProjectId ?? 'none'}
              projectId={selectedProjectId}
              self={{ id: profile.user_id, username: profile.username }}
            />
          ) : (
            <UsersRolesScreen
              apiRequest={apiRequest}
              capabilities={profile.capabilities}
              refreshProfile={refreshProfile}
            />
          )}
          </main>
        ) : (
          <main className="placeholder-main">
          <header className="workspace-topbar">
            <div className="breadcrumbs"><strong>{selectedProject}</strong><span>/</span><span>{activePage}</span></div>
            <div className="topbar-actions">
              <span className="local-status"><i />Local only · acme-db-01.internal</span>
              <ThemeToggle onTheme={setTheme} resolved={resolvedTheme} />
            </div>
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
