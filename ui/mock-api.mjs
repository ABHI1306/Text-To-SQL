import { createServer } from 'node:http'

let project = {
  id: '6b8b4567-e89b-12d3-a456-426614174000',
  name: 'E-commerce demo',
  setup_step: 'tables',
}
let selectedTables = []
let mdlRevision = 0
let savedMdl
const mockThreads = []
let activeRevision = null

const catalog = [
  { schema: 'ecommerce', table: 'customers', approx_rows: 1200, column_count: 5 },
  { schema: 'ecommerce', table: 'orders', approx_rows: 4800, column_count: 6 },
  { schema: 'ecommerce', table: 'products', approx_rows: 320, column_count: 5 },
  { schema: 'hr', table: 'departments', approx_rows: 8, column_count: 3 },
  { schema: 'hr', table: 'employees', approx_rows: 48, column_count: 6 },
]

const me = {
  user_id: '6b8b4567-e89b-12d3-a456-426614174001',
  username: 'demo.dba',
  role: 'DBA',
  capabilities: [
    'ask_question',
    'give_feedback',
    'view_shared',
    'view_all_threads',
    'rerun_sql',
    'manage_connection',
    'edit_mdl',
    'deploy_mdl',
    'manage_knowledge',
    'approve_pairs',
    'manage_policies',
    'manage_profiling',
    'create_dashboard',
    'schedule_question',
    'share',
    'run_eval',
    'manage_users',
    'manage_roles',
    'view_audit',
  ],
  projects: [project],
}

// ---- Day 6: knowledge, users/roles and row-policy mock state -------------------------------
const allCapabilities = [...me.capabilities].sort()
const uid = (n) => `6b8b4567-e89b-12d3-a456-4266141740${String(n).padStart(2, '0')}`
const roles = [
  { id: uid(10), name: 'DBA', description: 'Full access', is_system: true, capabilities: [...allCapabilities] },
  { id: uid(11), name: 'Admin', description: 'Users and governance', is_system: true, capabilities: ['ask_question', 'view_shared', 'manage_users', 'manage_roles', 'view_audit'] },
  { id: uid(12), name: 'User', description: 'Ask questions', is_system: true, capabilities: ['ask_question', 'give_feedback', 'view_shared'] },
  { id: uid(13), name: 'Analyst+', description: 'Analyst with dashboards', is_system: false, capabilities: ['ask_question', 'view_shared', 'create_dashboard', 'schedule_question'] },
]
const users = [
  { id: me.user_id, username: 'demo.dba', role: 'DBA', status: 'active', auth_source: 'local', locked_until: null, must_change_password: false },
  { id: uid(21), username: 'maria', role: 'User', status: 'active', auth_source: 'local', locked_until: null, must_change_password: false },
  { id: uid(22), username: 'jonas', role: 'Analyst+', status: 'active', auth_source: 'local', locked_until: new Date(Date.now() + 3_600_000).toISOString(), must_change_password: false },
  { id: uid(23), username: 'sam.ldap', role: 'User', status: 'active', auth_source: 'ldap', locked_until: null, must_change_password: false },
]
const userAccess = {}
const userAttributes = { [uid(21)]: { region: 'EU' } }
const roleAccess = {}
let identityMappings = [{ id: '1', source: 'ldap', match: 'cn=dbas,ou=groups', role: 'DBA' }]
let instructions = [{ id: 'i1', text: 'Revenue excludes cancelled orders.' }]
let sqlPairs = [{ id: 'p1', question: 'How many customers do we have?', logical_sql: 'SELECT count(*) FROM customers' }]
let proposals = [
  { id: 'pr1', question: 'Count of all customers', logical_sql: 'SELECT count(customer_id) FROM customers', proposed_by: 'maria', nearest_approved: sqlPairs[0] },
  { id: 'pr2', question: 'Average order value by month', logical_sql: "SELECT date_trunc('month', order_date) m, avg(total) FROM orders GROUP BY 1", proposed_by: 'jonas', nearest_approved: null },
]
let policies = []
let nextId = 100

function currentCapabilities() {
  const role = users[0].role
  return roles.find((item) => item.name === role)?.capabilities ?? []
}

// ---- Day 9: personas, row-level data and the Day 7/8 endpoints -----------------------------
// A login with a username signs in that persona; its token is 'mock-token:<username>' and every
// request derives role, capabilities and rows from it. The plain 'mock-token' (what every older spec
// gets) stays the demo.dba DBA and keeps the old behaviour. State below is in memory and is
// cleared with POST /__mock/reset (sent straight to the mock port, not through the Vite proxy).
const personas = {
  'demo.dba': { id: me.user_id, username: 'demo.dba', role: 'DBA', auth_source: 'local', region: null, capabilities: [...allCapabilities] },
  maria: { id: uid(21), username: 'maria', role: 'User', auth_source: 'local', region: 'EU', capabilities: ['ask_question', 'give_feedback', 'view_shared'] },
  // A custom role without view_shared: it builds and shares things but has no "Shared with me".
  jonas: { id: uid(22), username: 'jonas', role: 'Analyst+', auth_source: 'local', region: 'US', capabilities: ['ask_question', 'give_feedback', 'create_dashboard', 'schedule_question', 'share'] },
  'sam.ldap': { id: uid(23), username: 'sam.ldap', role: 'User', auth_source: 'ldap', region: 'North', capabilities: ['ask_question', 'give_feedback', 'view_shared'] },
  'oidc.omar': { id: uid(24), username: 'oidc.omar', role: 'User', auth_source: 'oidc', region: 'South', capabilities: ['ask_question', 'give_feedback', 'view_shared'] },
}
const refreshCookie = /refresh_token=mock-refresh-token(?::([^;\s]+))?/

// Row-level security model: one table, each persona only gets the rows of their own region.
const customerColumns = ['customer_id', 'first_name', 'region']
const customerRows = [
  [1, 'Ada', 'EU'],
  [2, 'Grace', 'South'],
  [3, 'Linus', 'North'],
  [4, 'Hedy', 'EU'],
  [5, 'Alan', 'US'],
  [6, 'Edsger', 'South'],
]
const rowsFor = (persona) => customerRows.filter((row) => !persona.region || row[2] === persona.region).map((row) => [...row])

function personaFromToken(header) {
  const name = String(header ?? '').match(/^Bearer mock-token:(.+)$/)?.[1]
  const persona = personas[name ?? '']
  return persona ? { persona, session: true } : { persona: personas['demo.dba'], session: false }
}

const auditActions = ['login_succeeded', 'ask_question', 'dashboard_created', 'share_created', 'role_changed']
const auditTargets = { login_succeeded: 'account', login_failed: 'account', ask_question: 'thread', dashboard_created: 'dashboard', share_created: 'share', role_changed: 'role' }
// 120 events, newest first; every tenth is a failed login so a filter has something to narrow to.
const auditEvents = Array.from({ length: 120 }, (_, index) => {
  const id = 120 - index
  const action = id % 10 === 0 ? 'login_failed' : auditActions[id % 5]
  return {
    id,
    actorId: action === 'login_failed' ? null : uid(21 + (id % 4)),
    action,
    resourceType: auditTargets[action],
    resourceId: `6b8b4567-e89b-12d3-a456-${String(id).padStart(12, '0')}`,
    details: { ip: '10.0.0.7', note: `mock event ${id}` },
    createdAt: new Date(Date.now() - id * 60_000).toISOString(),
  }
})

const baselineUsage = () => ({ questions: 0, llm_calls: 0, llm_errors: 0, prompt_tokens: 0, completion_tokens: 0, gpu_ms: 0, recent: [] })
function freshDay9() {
  return {
    dashboards: [], // { id, owner, title, tiles: [{ id, saved_question_id, chart_spec, position_json }] }
    savedQuestions: [], // { id, owner, question, logical_sql }
    jobs: [], // { id, owner, saved_question_id, schedule, timezone, status, paused_reason, next_run_at, last_run_at, last_status }
    runs: {}, // job id -> runs, newest first
    shares: [], // { id, resource_type, resource_id, grantee_type, grantee_id, granted_at }
    notifications: [
      { id: 'n1', owner: 'demo.dba', kind: 'scheduled_run_failed', payload: { question: 'Customers by region', message: 'Warehouse connection timed out' }, created_at: new Date().toISOString(), read_at: null },
    ],
    usage: baselineUsage(),
    scheduler: 'ok',
  }
}
let day9 = freshDay9()

/** Reset with seed: a DBA dashboard shared with the User role and a jonas question shared with sam.ldap. */
function seedDay9() {
  const regions = { id: 'sq-seed-regions', owner: 'demo.dba', question: 'Customers by region', logical_sql: 'SELECT customer_id, first_name, region FROM customers' }
  const orders = { id: 'sq-seed-orders', owner: 'jonas', question: 'Orders this week', logical_sql: 'SELECT customer_id, first_name, region FROM customers' }
  day9.savedQuestions.push(regions, orders)
  day9.dashboards.push({ id: 'db-seed-regional', owner: 'demo.dba', title: 'Regional sales', tiles: [{ id: 'tile-seed-1', saved_question_id: regions.id, chart_spec: null, position_json: {} }] })
  day9.shares.push(
    { id: 'sh-seed-1', resource_type: 'dashboard', resource_id: 'db-seed-regional', grantee_type: 'role', grantee_id: 'User', granted_at: new Date().toISOString() },
    { id: 'sh-seed-2', resource_type: 'saved_question', resource_id: orders.id, grantee_type: 'user', grantee_id: personas['sam.ldap'].id, granted_at: new Date().toISOString() },
  )
}

function sessionResponse(persona, named) {
  return {
    status: 200,
    payload: { access_token: named ? `mock-token:${persona.username}` : 'mock-token', token_type: 'bearer', expires_in: 900, must_change_password: false },
    headers: { 'set-cookie': `refresh_token=mock-refresh-token${named ? `:${persona.username}` : ''}; HttpOnly; SameSite=Strict; Path=/api/v1/auth` },
  }
}

function handleDay9(method, path, params, headers, body) {
  const json = (status, payload) => ({ status, payload })
  const m = (pattern) => path.match(pattern)
  const p = '/api/v1'
  let input = {}
  try {
    input = body ? JSON.parse(body) : {}
  } catch {
    input = {}
  }
  let hit

  // test-only switches (called on the mock port directly)
  if (method === 'POST' && path === '/__mock/reset') {
    day9 = freshDay9()
    if (input.seed) seedDay9()
    if (input.scheduler) day9.scheduler = input.scheduler
    return json(200, { ok: true })
  }
  if (method === 'POST' && path === '/__mock/scheduler') {
    day9.scheduler = input.status === 'degraded' ? 'degraded' : 'ok'
    return json(200, { scheduler: day9.scheduler })
  }

  // sign-in methods and persona selection
  if (method === 'GET' && path === `${p}/auth/methods`) return json(200, { local: true, ldap: true, oidc: true, directory: true, sso: true })
  if (method === 'GET' && path === `${p}/auth/oidc/login`) {
    const persona = personas[params.get('as') ?? ''] ??personas['oidc.omar']
    return { status: 302, headers: { location: '/', ...sessionResponse(persona, true).headers } }
  }
  if (method === 'POST' && path === `${p}/auth/login`) {
    const persona = personas[String(input.username ?? '').toLowerCase()]
    // OIDC accounts have no password; "wrong-password" lets a spec exercise the failure path.
    if (!persona || persona.auth_source === 'oidc' || input.password === 'wrong-password') return json(401, { detail: 'Invalid username or password' })
    return sessionResponse(persona, true)
  }
  if (method === 'POST' && path === `${p}/auth/refresh`) {
    const name = String(headers.cookie ?? '').match(refreshCookie)?.[1]
    const persona = personas[name ?? '']
    return persona ? sessionResponse(persona, true) : sessionResponse(personas['demo.dba'], false)
  }

  const who = personaFromToken(headers.authorization)
  const { persona } = who
  const capabilities = who.session ? persona.capabilities : currentCapabilities()
  const can = (capability) => capabilities.includes(capability)
  const forbidden = (capability) => json(403, { detail: `Your role does not include ${capability}` })
  const now = () => new Date().toISOString()
  const viewerShares = (type, id) => day9.shares.filter((share) => share.resource_type === type && share.resource_id === id
    && ((share.grantee_type === 'user' && share.grantee_id === persona.id) || (share.grantee_type === 'role' && share.grantee_id === persona.role)))
  const dashboardFor = (id) => day9.dashboards.find((item) => item.id === id && (item.owner === persona.username || viewerShares('dashboard', id).length > 0))
  const questionFor = (id) => day9.savedQuestions.find((item) => item.id === id && (item.owner === persona.username || viewerShares('saved_question', id).length > 0))
  const resultFor = (question) => ({ columns: customerColumns, rows: rowsFor(persona), truncated: false, cached: false, computed_at: now(), question })
  const ownsResource = (type, id) => {
    if (type === 'dashboard') return day9.dashboards.find((item) => item.id === id)?.owner === persona.username
    if (type === 'saved_question') return day9.savedQuestions.find((item) => item.id === id)?.owner === persona.username
    return mockThreads.some((thread) => thread.id === id && thread.owner === persona.username)
  }

  if (method === 'GET' && path === `${p}/me` && who.session) {
    return json(200, { ...me, user_id: persona.id, username: persona.username, role: persona.role, auth_source: persona.auth_source, capabilities, projects: [project] })
  }

  // ops: usage and scheduler health (view_audit)
  if (method === 'GET' && path === `${p}/ops/usage`) {
    if (!can('view_audit')) return forbidden('view_audit')
    const usage = day9.usage
    return json(200, { ...usage, avg_tokens_per_question: usage.questions ? Math.round((usage.prompt_tokens + usage.completion_tokens) / usage.questions) : 0, avg_gpu_ms_per_question: usage.questions ? Math.round(usage.gpu_ms / usage.questions) : 0 })
  }
  if (method === 'GET' && path === `${p}/ops/scheduler`) {
    if (!can('view_audit')) return forbidden('view_audit')
    const degraded = day9.scheduler === 'degraded'
    return json(200, {
      status: degraded ? 'degraded' : 'ok',
      checked_at: now(),
      active: day9.jobs.filter((job) => job.status === 'active').length,
      paused: day9.jobs.filter((job) => job.status !== 'active').length,
      overdue: degraded ? 3 : 0,
      last_failed: degraded ? 2 : 0,
      running: 0,
      stale: degraded ? 1 : 0,
      failed_24h: degraded ? 2 : 0,
      succeeded_24h: 7,
      next_slot: new Date(Date.now() + 3_600_000).toISOString(),
      last_run_at: new Date(Date.now() - 600_000).toISOString(),
    })
  }

  // audit browser (view_audit)
  if (method === 'GET' && (path === `${p}/audit-events` || path === `${p}/audit-events/export.csv`)) {
    if (!can('view_audit')) return forbidden('view_audit')
    const filtered = auditEvents.filter((event) =>
      (!params.get('actor_id') || event.actorId === params.get('actor_id'))
      && (!params.get('action') || event.action === params.get('action'))
      && (!params.get('resource_type') || event.resourceType === params.get('resource_type'))
      && (!params.get('since') || event.createdAt >= params.get('since'))
      && (!params.get('until') || event.createdAt < params.get('until')))
    if (path.endsWith('.csv')) {
      const lines = filtered.map((event) => [event.id, event.createdAt, event.actorId ?? '', event.action, event.resourceType, event.resourceId].join(','))
      return { status: 200, contentType: 'text/csv', raw: ['id,created_at,actor_id,action,resource_type,resource_id', ...lines].join('\n') }
    }
    const cursor = Number(params.get('cursor') ?? 0)
    const limit = Number(params.get('limit') ?? 50)
    const after = cursor ? filtered.filter((event) => event.id < cursor) : filtered
    const items = after.slice(0, limit)
    return json(200, { items, nextCursor: after.length > limit ? items.at(-1).id : null })
  }

  // notifications
  if (method === 'GET' && path === `${p}/notifications`) {
    const items = day9.notifications.filter((item) => item.owner === persona.username).map(({ owner, ...rest }) => rest)
    return json(200, items.slice(0, Number(params.get('limit') ?? 50)))
  }
  if (method === 'POST' && (hit = m(/^\/api\/v1\/notifications\/([^/]+)\/read$/))) {
    const notice = day9.notifications.find((item) => item.id === hit[1] && item.owner === persona.username)
    if (!notice) return json(404, { detail: 'Notification not found' })
    notice.read_at = now()
    return json(200, { id: notice.id, read_at: notice.read_at })
  }

  // saved questions
  if ((hit = m(/^\/api\/v1\/projects\/[^/]+\/saved-questions$/))) {
    if (method === 'GET') return json(200, day9.savedQuestions.filter((item) => item.owner === persona.username).map(({ id, question }) => ({ id, question })))
    if (method === 'POST') {
      if (!input.question || !input.logical_sql) return json(422, { detail: 'question and logical_sql are required' })
      const saved = { id: crypto.randomUUID(), owner: persona.username, question: input.question, logical_sql: input.logical_sql }
      day9.savedQuestions.push(saved)
      return json(201, { id: saved.id })
    }
  }
  if (method === 'GET' && (hit = m(/^\/api\/v1\/saved-questions\/([^/]+)\/result$/))) {
    const saved = questionFor(hit[1])
    return saved ? json(200, resultFor(saved.question)) : json(404, { detail: 'Saved question not found' })
  }

  // dashboards
  if (path === `${p}/dashboards`) {
    if (method === 'GET') return json(200, day9.dashboards.filter((item) => item.owner === persona.username).map(({ id, title }) => ({ id, title })))
    if (method === 'POST') {
      if (!can('create_dashboard')) return forbidden('create_dashboard')
      if (!String(input.title ?? '').trim()) return json(422, { detail: 'A dashboard needs a title' })
      const dashboard = { id: crypto.randomUUID(), owner: persona.username, title: String(input.title).trim(), tiles: [] }
      day9.dashboards.push(dashboard)
      return json(201, { id: dashboard.id, title: dashboard.title })
    }
  }
  if ((hit = m(/^\/api\/v1\/dashboards\/([^/]+)(\/.*)?$/))) {
    const [, id, rest = ''] = hit
    const dashboard = dashboardFor(id)
    if (!dashboard) return json(404, { detail: 'Dashboard not found' })
    const isOwner = dashboard.owner === persona.username
    const ownerOnly = () => json(403, { detail: 'Only the owner can change this dashboard' })
    if (rest === '' && method === 'GET') return json(200, { id, title: dashboard.title, tiles: dashboard.tiles, is_owner: isOwner })
    if (rest === '' && method === 'PATCH') {
      if (!isOwner) return ownerOnly()
      dashboard.title = String(input.title ?? dashboard.title)
      return json(200, { id, title: dashboard.title })
    }
    if (rest === '' && method === 'DELETE') {
      if (!isOwner) return ownerOnly()
      day9.dashboards = day9.dashboards.filter((item) => item.id !== id)
      day9.shares = day9.shares.filter((share) => !(share.resource_type === 'dashboard' && share.resource_id === id))
      return json(204)
    }
    if (rest === '/tiles' && method === 'PUT') {
      if (!isOwner) return ownerOnly()
      const tiles = input.tiles ?? []
      if (tiles.some((tile) => !day9.savedQuestions.some((item) => item.id === tile.saved_question_id && item.owner === persona.username))) {
        return json(404, { detail: 'Saved question not found' })
      }
      dashboard.tiles = tiles.map((tile) => ({ id: `tile-${nextId++}`, saved_question_id: tile.saved_question_id, chart_spec: tile.chart_spec ?? null, position_json: tile.position ?? {} }))
      return json(200, { id, tiles: dashboard.tiles })
    }
    if (rest === '/refresh' && method === 'POST') {
      if (!isOwner) return ownerOnly()
      return json(200, { refreshed_tiles: dashboard.tiles.length })
    }
    const tileHit = rest.match(/^\/tiles\/([^/]+)\/result$/)
    if (tileHit && method === 'GET') {
      const tile = dashboard.tiles.find((item) => item.id === tileHit[1])
      const saved = day9.savedQuestions.find((item) => item.id === tile?.saved_question_id)
      return tile && saved ? json(200, resultFor(saved.question)) : json(404, { detail: 'Tile not found' })
    }
  }

  // sharing (read-only grants to a person or a role)
  if (method === 'GET' && path === `${p}/share-targets`) {
    if (!can('share')) return forbidden('share')
    return json(200, {
      users: Object.values(personas).filter((item) => item.username !== persona.username).map(({ id, username, role }) => ({ id, username, role })),
      roles: ['DBA', 'Admin', 'User', 'Analyst+'].map((name) => ({ name })),
    })
  }
  if (path === `${p}/shares`) {
    if (!can('share')) return forbidden('share')
    if (method === 'GET') {
      const type = params.get('resource_type')
      const id = params.get('resource_id')
      if (!ownsResource(type, id)) return json(403, { detail: 'Only the owner can see who it is shared with' })
      return json(200, day9.shares.filter((share) => share.resource_type === type && share.resource_id === id).map(({ resource_type, resource_id, ...rest }) => rest))
    }
    if (method === 'POST') {
      if (!ownsResource(input.resource_type, input.resource_id)) return json(404, { detail: 'Nothing to share' })
      const known = input.grantee_type === 'role'
        ? ['DBA', 'Admin', 'User', 'Analyst+'].includes(input.grantee_id)
        : Object.values(personas).some((item) => item.id === input.grantee_id)
      if (!known) return json(404, { detail: 'Person or role not found' })
      if (day9.shares.some((share) => share.resource_type === input.resource_type && share.resource_id === input.resource_id
        && share.grantee_type === input.grantee_type && share.grantee_id === input.grantee_id)) {
        return json(409, { detail: 'Already shared with them' })
      }
      const share = { id: crypto.randomUUID(), resource_type: input.resource_type, resource_id: input.resource_id, grantee_type: input.grantee_type, grantee_id: input.grantee_id, granted_at: now() }
      day9.shares.push(share)
      return json(201, share)
    }
  }
  if (method === 'DELETE' && (hit = m(/^\/api\/v1\/shares\/([^/]+)$/))) {
    const share = day9.shares.find((item) => item.id === hit[1])
    if (!share) return json(404, { detail: 'Share not found' })
    if (!ownsResource(share.resource_type, share.resource_id)) return json(403, { detail: 'Only the owner can revoke a share' })
    day9.shares = day9.shares.filter((item) => item.id !== share.id)
    return json(204)
  }
  if (method === 'GET' && path === `${p}/shared-with-me`) {
    if (!can('view_shared')) return forbidden('view_shared')
    const seen = new Set()
    const items = []
    for (const share of [...day9.shares].reverse()) {
      const mine = (share.grantee_type === 'user' && share.grantee_id === persona.id) || (share.grantee_type === 'role' && share.grantee_id === persona.role)
      const key = `${share.resource_type}:${share.resource_id}`
      if (!mine || seen.has(key)) continue
      const title = share.resource_type === 'dashboard' ? day9.dashboards.find((item) => item.id === share.resource_id)?.title
        : share.resource_type === 'saved_question' ? day9.savedQuestions.find((item) => item.id === share.resource_id)?.question
          : mockThreads.find((thread) => thread.id === share.resource_id)?.title
      if (title === undefined) continue
      seen.add(key)
      items.push({ share_id: share.id, resource_type: share.resource_type, resource_id: share.resource_id, title, granted_at: share.granted_at })
    }
    return json(200, items)
  }

  // scheduled questions (schedule_question)
  if (path === `${p}/scheduled-questions` || path.startsWith(`${p}/scheduled-questions/`)) {
    if (!can('schedule_question')) return forbidden('schedule_question')
    if (path === `${p}/scheduled-questions`) {
      if (method === 'GET') return json(200, day9.jobs.filter((job) => job.owner === persona.username).map(({ owner, ...rest }) => rest))
      if (method === 'POST') {
        if (!day9.savedQuestions.some((item) => item.id === input.saved_question_id && item.owner === persona.username)) return json(404, { detail: 'Saved question not found' })
        const schedule = String(input.schedule ?? '')
        if (!['hourly', 'daily', 'weekly'].includes(schedule) && schedule.trim().split(/\s+/).length !== 5) return json(422, { detail: 'Not a valid cron expression' })
        const job = {
          id: crypto.randomUUID(), owner: persona.username, saved_question_id: input.saved_question_id, schedule,
          timezone: input.timezone || 'UTC', status: 'active', paused_reason: null,
          next_run_at: new Date(Date.now() + 3_600_000).toISOString(), last_run_at: null, last_status: null,
        }
        day9.jobs.push(job)
        day9.runs[job.id] = []
        const { owner, ...rest } = job
        return json(201, rest)
      }
    }
    if ((hit = m(/^\/api\/v1\/scheduled-questions\/([^/]+)(?:\/(pause|resume|run-now|runs))?$/))) {
      const [, id, action] = hit
      const job = day9.jobs.find((item) => item.id === id && item.owner === persona.username)
      if (!job) return json(404, { detail: 'Schedule not found' })
      if (!action && method === 'DELETE') {
        day9.jobs = day9.jobs.filter((item) => item.id !== id)
        return json(204)
      }
      if (action === 'runs' && method === 'GET') return json(200, day9.runs[id] ?? [])
      if (action === 'pause' && method === 'POST') {
        Object.assign(job, { status: 'paused', paused_reason: 'user_paused' })
        return json(200, { id, status: job.status })
      }
      if (action === 'resume' && method === 'POST') {
        Object.assign(job, { status: 'active', paused_reason: null })
        return json(200, { id, status: job.status })
      }
      if (action === 'run-now' && method === 'POST') {
        const question = day9.savedQuestions.find((item) => item.id === job.saved_question_id)?.question ?? ''
        const failed = /fail/i.test(question) // a question with "fail" in it models a broken schedule
        const run = {
          id: crypto.randomUUID(), started_at: now(), status: failed ? 'failed' : 'succeeded',
          row_count: failed ? null : rowsFor(persona).length, error: failed ? 'Warehouse connection timed out' : null, trigger: 'manual',
        }
        day9.runs[id] = [run, ...(day9.runs[id] ?? [])]
        Object.assign(job, { last_run_at: run.started_at, last_status: run.status })
        if (failed) {
          day9.notifications.unshift({ id: `n${nextId++}`, owner: persona.username, kind: 'scheduled_run_failed', payload: { question, message: run.error }, created_at: now(), read_at: null })
        }
        return json(200, run)
      }
    }
  }
  return null
}

function handleAdmin(method, path, body) {
  const m = (pattern) => path.match(pattern)
  const json = (status, payload) => ({ status, payload })
  const input = body ? JSON.parse(body) : {}
  const p = '/api/v1'
  let hit
  if (method === 'GET' && path === `${p}/capabilities`) return json(200, { items: allCapabilities })
  // knowledge
  if ((hit = m(/^\/api\/v1\/projects\/[^/]+\/knowledge\/(instructions|sql-pairs|proposals)(?:\/([^/]+))?(?:\/(approve|reject))?$/))) {
    const [, kind, id, decision] = hit
    if (kind === 'instructions') {
      if (!id && method === 'GET') return json(200, instructions)
      if (!id && method === 'POST') {
        const item = { id: `i${nextId++}`, text: input.text }
        instructions = [...instructions, item]
        return json(201, item)
      }
      const current = instructions.find((item) => item.id === id)
      if (!current) return json(404, { detail: 'Instruction not found' })
      if (method === 'PATCH') {
        current.text = input.text
        return json(200, current)
      }
      if (method === 'DELETE') {
        instructions = instructions.filter((item) => item.id !== id)
        return json(204)
      }
    }
    if (kind === 'sql-pairs') {
      if (!id && method === 'GET') return json(200, sqlPairs)
      if (!id && method === 'POST') {
        const item = { id: `p${nextId++}`, question: input.question, logical_sql: input.logical_sql }
        sqlPairs = [...sqlPairs, item]
        return json(201, item)
      }
      if (method === 'DELETE') {
        sqlPairs = sqlPairs.filter((item) => item.id !== id)
        return json(204)
      }
    }
    if (kind === 'proposals') {
      if (!id && method === 'GET') return json(200, proposals)
      const proposal = proposals.find((item) => item.id === id)
      if (!proposal) return json(404, { detail: 'Proposal not found' })
      if (method === 'POST' && decision) {
        proposals = proposals.filter((item) => item.id !== id)
        if (decision === 'approve') sqlPairs = [...sqlPairs, { id: `p${nextId++}`, question: proposal.question, logical_sql: proposal.logical_sql }]
        return json(200, { id, status: decision === 'approve' ? 'approved' : 'rejected' })
      }
    }
  }
  // users
  if (method === 'GET' && path === `${p}/users`) return json(200, { items: users })
  if (method === 'POST' && path === `${p}/users`) {
    if (users.some((user) => user.username === String(input.username).toLowerCase())) return json(409, { detail: 'Username already exists' })
    const user = { id: uid(30 + users.length), username: String(input.username).toLowerCase(), role: input.role ?? 'User', status: 'active', auth_source: 'local', locked_until: null, must_change_password: true }
    users.push(user)
    return json(201, { id: user.id, username: user.username, role: user.role })
  }
  if ((hit = m(/^\/api\/v1\/users\/([^/]+)\/(role|unlock|reset-password|project-access|attributes)$/))) {
    const [, id, action] = hit
    const user = users.find((item) => item.id === id)
    if (!user) return json(404, { detail: 'User not found' })
    if (action === 'role' && method === 'PUT') {
      if (!roles.some((role) => role.name === input.role)) return json(404, { detail: 'Role does not exist' })
      user.role = input.role
      return json(200, { user_id: id, role: user.role })
    }
    if (action === 'unlock' && method === 'POST') {
      user.locked_until = null
      return json(200, { user_id: id, locked: false })
    }
    if (action === 'reset-password' && method === 'POST') {
      if (user.auth_source !== 'local') return json(422, { detail: 'Only local accounts have a password to reset' })
      user.must_change_password = true
      user.locked_until = null
      return json(200, { user_id: id, must_change_password: true })
    }
    if (action === 'project-access') {
      if (method === 'GET') return json(200, { project_ids: userAccess[id] ?? [] })
      userAccess[id] = input.project_ids
      return json(200, { user_id: id, project_ids: input.project_ids })
    }
    if (action === 'attributes') {
      if (method === 'GET') return json(200, { attributes: userAttributes[id] ?? {} })
      userAttributes[id] = input.attributes
      return json(200, { user_id: id, attributes: input.attributes })
    }
  }
  // roles
  if (path === `${p}/roles` && method === 'GET') return json(200, { items: roles })
  if (path === `${p}/roles` && method === 'POST') {
    if (roles.some((role) => role.name === input.name)) return json(409, { detail: 'Role name already exists' })
    const role = { id: uid(40 + roles.length), name: input.name, description: input.description ?? '', is_system: false, capabilities: input.capabilities ?? [] }
    roles.push(role)
    return json(201, role)
  }
  if ((hit = m(/^\/api\/v1\/roles\/([^/]+)(\/project-access)?$/))) {
    const [, id, access] = hit
    const role = roles.find((item) => item.id === id)
    if (!role) return json(404, { detail: 'Role not found' })
    if (access) {
      if (method === 'GET') return json(200, { project_ids: roleAccess[id] ?? [] })
      roleAccess[id] = input.project_ids
      return json(200, { role_id: id, project_ids: input.project_ids })
    }
    if (method === 'PUT') {
      Object.assign(role, { name: input.name, description: input.description ?? '', capabilities: input.capabilities ?? [] })
      return json(200, role)
    }
    if (method === 'DELETE') {
      if (users.some((user) => user.role === role.name)) return json(409, { detail: 'Role is still assigned to users' })
      roles.splice(roles.indexOf(role), 1)
      return json(204)
    }
  }
  // identity mappings
  if (path === `${p}/identity/mappings` && method === 'GET') return json(200, { items: identityMappings })
  if (path === `${p}/identity/mappings` && method === 'PUT') {
    identityMappings = [
      ...identityMappings.filter((item) => item.source !== input.source),
      ...input.mappings.map((item, index) => ({ id: `${input.source}-${index}`, source: input.source, ...item })),
    ]
    return json(200, { source: input.source, count: input.mappings.length })
  }
  // row policies
  if ((hit = m(/^\/api\/v1\/projects\/[^/]+\/row-policies(?:\/([^/]+))?$/))) {
    const [, id] = hit
    if (!id && method === 'GET') return json(200, { items: policies })
    if (method === 'DELETE') {
      policies = policies.filter((item) => item.id !== id)
      return json(204)
    }
    if (method === 'POST' || method === 'PUT') {
      if (String(input.predicate).includes('ghost')) {
        return json(422, { detail: { code: 'POLICY_INVALID', errors: [{ code: 'UNKNOWN_COLUMN', message: "Column 'ghost' could not be resolved" }] } })
      }
      if (method === 'POST' && policies.some((item) => item.name === input.name)) return json(409, { detail: 'Policy name already exists' })
      const policy = {
        id: id ?? `pol${nextId++}`,
        name: input.name,
        model: input.model,
        predicate: input.predicate,
        appliesTo: { roles: input.appliesTo?.roles ?? [], exemptRoles: input.appliesTo?.exemptRoles ?? [] },
        onMissingAttribute: 'deny',
        enabled: input.enabled !== false,
      }
      policies = id ? policies.map((item) => (item.id === id ? policy : item)) : [...policies, policy]
      const attrs = userAttributes[input.testUserId] ?? {}
      const missing = [...String(input.predicate).matchAll(/\{\{\s*user\.(\w+)\s*\}\}/g)].map((x) => x[1]).filter((name) => !(name in attrs))
      return json(method === 'POST' ? 201 : 200, {
        ...policy,
        validation: {
          warnings: missing.map((name) => `Test user has no attribute ${name}; the policy denies by default`),
          testCompilation: { ok: true, sql: `SELECT * FROM ${policy.model} WHERE ${policy.predicate}`, parameters: {}, appliedPolicies: [policy.name], missingAttributes: missing, errors: [] },
        },
      })
    }
  }
  return null
}

const server = createServer(async (request, response) => {
  const chunks = []
  for await (const chunk of request) chunks.push(chunk)
  const body = Buffer.concat(chunks).toString()
  const url = new URL(request.url ?? '/', 'http://127.0.0.1:4010')
  let status = 200
  let payload

  const handled = handleDay9(request.method ?? 'GET', url.pathname, url.searchParams, request.headers, body)
  if (handled) {
    response.writeHead(handled.status, {
      'content-type': handled.contentType ?? 'application/json',
      'access-control-allow-origin': '*',
      ...handled.headers,
    })
    response.end([204, 302].includes(handled.status) ? undefined : (handled.raw ?? JSON.stringify(handled.payload)))
    return
  }
  const { persona, session } = personaFromToken(request.headers.authorization)

  const admin = handleAdmin(request.method ?? 'GET', url.pathname, body)
  if (admin) {
    response.writeHead(admin.status, {
      'content-type': 'application/json',
      'access-control-allow-origin': '*',
    })
    response.end(admin.status === 204 ? undefined : JSON.stringify(admin.payload))
    return
  }

  const json = (status, payload) => {
    response.writeHead(status, { 'content-type': 'application/json', 'access-control-allow-origin': '*' })
    response.end(JSON.stringify(payload))
  }
  if (request.method === 'POST' && url.pathname.match(/^\/api\/v1\/projects\/[^/]+\/threads$/)) {
    const thread = { id: crypto.randomUUID(), owner: persona.username, title: '', turns: 0, last_activity: new Date().toISOString(), messages: [] }
    mockThreads.push(thread)
    json(201, { id: thread.id, project_id: url.pathname.split('/')[4] })
    return
  }
  if (request.method === 'GET' && url.pathname.match(/^\/api\/v1\/projects\/[^/]+\/threads$/)) {
    json(200, { items: mockThreads.filter((t) => t.turns > 0 && t.owner === persona.username).map(({ messages, owner, ...rest }) => rest).reverse() })
    return
  }
  if (request.method === 'GET' && url.pathname.match(/^\/api\/v1\/threads\/[^/]+$/)) {
    const thread = mockThreads.find((t) => t.id === url.pathname.split('/')[4])
    if (!thread) return json(404, { detail: 'Thread not found' })
    json(200, { id: thread.id, project_id: 'mock', owner_id: 'mock', messages: thread.messages })
    return
  }
  if (request.method === 'POST' && url.pathname.match(/^\/api\/v1\/threads\/[^/]+\/rerun-sql$/)) {
    json(200, { columns: ['customer_id', 'first_name'], rows: [[9, 'Rerun person']], truncated: false, row_count: 1, sql: 'SELECT 1' })
    return
  }
  if (request.method === 'POST' && url.pathname.match(/^\/api\/v1\/messages\/[^/]+\/(feedback|run)$/)) {
    if (url.pathname.endsWith('/run') && session) {
      json(200, { columns: customerColumns, rows: rowsFor(persona), truncated: false, row_count: rowsFor(persona).length, sql: 'SELECT customer_id, first_name, region FROM ecommerce.customers' })
    } else if (url.pathname.endsWith('/run')) {
      json(200, { columns: ['customer_id', 'first_name'], rows: [[1, 'Ada'], [2, 'Grace']], truncated: false, row_count: 2, sql: 'WITH m_customers AS (SELECT customer_id, first_name FROM ecommerce.customers) SELECT customer_id, first_name FROM m_customers AS customers' })
    } else {
      json(201, { id: 'fb-1', rating: JSON.parse(body || '{}').rating, proposal: null })
    }
    return
  }

  if (
    request.method === 'POST'
    && url.pathname.match(/^\/api\/v1\/(projects\/[^/]+\/query\/stream|threads\/[^/]+\/ask)$/)
  ) {
    const question = String(JSON.parse(body || '{}').question ?? '')
    const askedThread = mockThreads.find((t) => url.pathname.includes(t.id))
    if (askedThread) {
      askedThread.title ||= question
      askedThread.turns += 1
      askedThread.messages.push({
        id: String(askedThread.messages.length + 1), question, standalone_question: question,
        logical_sql: 'SELECT customer_id, first_name FROM customers ORDER BY customer_id', answer: null,
        summary: 'Two customers.', chart: null, suggestions: [], latency_ms: 150, failure: null,
        model_version: 3,
        trace: { models: ['customers'], steps: [
          { title: 'Intent', detail: 'data question', ok: true, ms: 300 },
          { title: 'Retrieval', detail: '1 models, 0 SQL pairs, 0 instructions', ok: true, ms: 400 },
          { title: 'Compilation', detail: 'Row policies applied', ok: true, ms: 100 },
        ] },
        created_at: new Date().toISOString(),
      })
    }
    const send = (type, data) => response.write(`event: ${type}\ndata: ${JSON.stringify(data)}\n\n`)
    const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
    response.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' })
    // Day 9 persona sessions always have a deployed model, so they can ask without running Setup.
    if (!activeRevision && !session) {
      send('error', { code: 'NO_MODEL', message: 'Deploy a semantic model before asking questions', retryable: false })
      response.end()
      return
    }
    send('retrieval', { models: ['customers'], pairs: [], instructions: [], ms: 12 })
    await pause(40)
    if (/clarify/i.test(question)) {
      send('clarification', { question: 'Which customers do you mean?', options: ['All customers', 'Active customers'] })
      response.end()
      return
    }
    send('sql_candidate', { logical_sql: 'SELECT customer_id, first_name FROM customers ORDER BY customer_id', attempt: 1, ms: 30 })
    await pause(40)
    if (/fail/i.test(question)) {
      send('compilation', { ok: false, errors: [{ code: 'UNKNOWN_COLUMN', message: "Column 'x' could not be resolved" }], ms: 4 })
      send('error', { code: 'UNKNOWN_COLUMN', message: "Column 'x' could not be resolved", retryable: false })
      response.end()
      return
    }
    send('compilation', { ok: true, errors: [], sql: 'WITH m_customers AS (SELECT customer_id, first_name FROM ecommerce.customers) SELECT customer_id, first_name FROM m_customers AS customers ORDER BY customer_id LIMIT 1000', ms: 4 })
    send('validation', { ok: true, errors: [], ms: 9 })
    await pause(40)
    if (session) {
      const rows = rowsFor(persona)
      send('rows', { columns: customerColumns, rows, truncated: false, row_count: rows.length, ms: 11 })
      // one answered question: 900 prompt + 300 answer tokens, 4.3 s of GPU, two model calls
      Object.assign(day9.usage, {
        questions: day9.usage.questions + 1,
        llm_calls: day9.usage.llm_calls + 2,
        prompt_tokens: day9.usage.prompt_tokens + 900,
        completion_tokens: day9.usage.completion_tokens + 300,
        gpu_ms: day9.usage.gpu_ms + 4300,
        recent: [...day9.usage.recent, { tokens: 1200, gpu_ms: 4300, llm_calls: 2, latency_ms: 5100 }],
      })
    } else {
      send('rows', { columns: ['customer_id', 'first_name'], rows: [[1, 'Mock customer'], [2, 'Preview customer']], truncated: false, row_count: 2, ms: 11 })
    }
    send('done', { message_id: 'm1', latency_ms: 106, attempts: 0 })
    response.end()
    return
  }

  // login and refresh (persona selection) are handled in handleDay9
  let setCookie
  if (request.method === 'POST' && url.pathname === '/api/v1/auth/logout') {
    status = 204
    setCookie = 'refresh_token=; HttpOnly; SameSite=Strict; Path=/api/v1/auth; Max-Age=0'
  } else if (request.method === 'POST' && url.pathname === '/api/v1/auth/change-password') {
    status = 204
  } else if (request.method === 'GET' && url.pathname === '/api/v1/me') {
    payload = { ...me, role: users[0].role, capabilities: currentCapabilities(), projects: [project] }
  } else if (request.method === 'GET' && url.pathname === '/api/v1/projects') {
    payload = { items: [project] }
  } else if (request.method === 'POST' && url.pathname === '/api/v1/connections/test') {
    payload = { server_version: '16.4 (mock)', latency_ms: 8 }
  } else if (request.method === 'POST' && url.pathname === '/api/v1/projects') {
    const input = JSON.parse(body)
    status = 201
    project = {
      id: crypto.randomUUID(),
      name: input.displayName,
      setup_step: 'tables',
    }
    me.projects = [project]
    selectedTables = []
    savedMdl = undefined
    activeRevision = null
    payload = project
  } else if (request.method === 'GET' && url.pathname.match(/^\/api\/v1\/projects\/[^/]+\/setup$/)) {
    payload = {
      project_id: project.id,
      step: project.setup_step,
      selected_tables: selectedTables,
    }
  } else if (request.method === 'GET' && url.pathname.match(/^\/api\/v1\/projects\/[^/]+\/catalog\/tables$/)) {
    const search = (url.searchParams.get('search') ?? '').toLowerCase()
    const filtered = catalog.filter((item) => `${item.schema}.${item.table}`.toLowerCase().includes(search))
    const page = Number(url.searchParams.get('page') ?? 1)
    const pageSize = Number(url.searchParams.get('page_size') ?? 50)
    payload = {
      items: filtered.slice((page - 1) * pageSize, page * pageSize),
      page,
      page_size: pageSize,
      total: filtered.length,
    }
  } else if (request.method === 'PUT' && url.pathname.match(/^\/api\/v1\/projects\/[^/]+\/selected-tables$/)) {
    selectedTables = JSON.parse(body).tables
    project.setup_step = 'relationships'
    payload = { project_id: project.id, step: project.setup_step, selected_count: selectedTables.length }
  } else if (request.method === 'POST' && url.pathname.match(/^\/api\/v1\/projects\/[^/]+\/introspect$/)) {
    const models = selectedTables.map(({ schema, table }) => ({
      name: table,
      tableReference: `${schema}.${table}`,
      description: '',
      primaryKey: null,
      needsReview: true,
      columns: [{ name: 'id', type: 'integer', physicalName: 'id', description: '', isPrimaryKey: true, needsReview: true }],
    }))
    payload = {
      mdl: { version: 3, dialect: 'postgresql', models, relationships: [], metrics: [], instructions: [], layout: {} },
      tables: [],
      summary: { selected_tables: selectedTables.length, total_rows: 0, models_needing_review: models.map(({ name }) => name) },
    }
  } else if (request.method === 'PUT' && url.pathname.match(/^\/api\/v1\/projects\/[^/]+\/mdl$/)) {
    const input = JSON.parse(body)
    mdlRevision += 1
    project.setup_step = 'complete'
    savedMdl = { ...input, project_id: project.id, revision: mdlRevision }
    payload = savedMdl
  } else if (request.method === 'GET' && url.pathname.match(/^\/api\/v1\/projects\/[^/]+\/mdl$/)) {
    if (savedMdl) {
      payload = savedMdl
    } else {
      status = 404
      payload = { detail: 'Project has no saved MDL' }
    }
  } else if (
    request.method === 'POST'
    && url.pathname.match(/^\/api\/v1\/projects\/[^/]+\/deploy$/)
  ) {
    if (!savedMdl) {
      status = 409
      payload = { detail: 'Save a model before deployment' }
    } else {
      activeRevision = Number(url.searchParams.get('revision') ?? savedMdl.revision)
      payload = {
        project_id: project.id,
        version: activeRevision,
        indexed_chunks: savedMdl.models.length,
        previous_version: null,
      }
    }
  } else if (
    request.method === 'POST'
    && url.pathname.match(/^\/api\/v1\/projects\/[^/]+\/compile-preview$/)
  ) {
    const input = JSON.parse(body)
    const bad = (input.mdl?.models ?? [])
      .flatMap((model) => model.columns)
      .find((column) => column.isCalculated && String(column.expression).includes('ghost'))
    const badMetric = (input.mdl?.metrics ?? [])
      .flatMap((metric) => [...metric.dimensions, ...metric.measures])
      .find((field) => String(field.expression).includes('ghost'))
    const failing = bad ?? badMetric
    if (failing) {
      payload = {
        ok: false,
        sql: '',
        parameters: {},
        errors: [{
          code: 'UNKNOWN_COLUMN',
          message: "Column 'ghost' could not be resolved",
          identifier: 'ghost',
          suggestions: ['order_id'],
        }],
        preview: { asUserId: null, labeled: false },
      }
    } else {
      payload = {
        ok: true,
        sql: `WITH m_mock AS (SELECT 1) /* ${input.logicalSql} */ SELECT * FROM m_mock LIMIT 1000`,
        parameters: {},
        errors: [],
        preview: { asUserId: null, labeled: false },
      }
      if (input.previewAsUserId) {
        const target = users.find((user) => user.id === input.previewAsUserId)
        const attrs = userAttributes[input.previewAsUserId] ?? {}
        const applied = policies.filter((policy) => policy.enabled && String(input.logicalSql).includes(policy.model))
        const parameters = {}
        let sql = payload.sql
        for (const policy of applied) {
          const predicate = policy.predicate.replace(/\{\{\s*user\.(\w+)\s*\}\}/g, (_, name) => {
            parameters[`user_${name}`] = attrs[name] ?? null
            return `%(user_${name})s`
          })
          sql += ` /* policy ${policy.name}: ${predicate} */`
        }
        payload = {
          ...payload,
          sql,
          parameters,
          appliedPolicies: applied.map((policy) => policy.name),
          missingAttributes: [],
          preview: { asUserId: target?.id ?? input.previewAsUserId, labeled: true },
        }
      }
      if (input.sample) {
        payload.sample = {
          columns: ['order_month', 'revenue'],
          rows: [['2026-01-01', 1200], ['2026-02-01', 980]],
          truncated: false,
        }
      }
    }
  } else if (
    request.method === 'GET'
    && url.pathname.match(/^\/api\/v1\/projects\/[^/]+\/deploy-report$/)
  ) {
    if (!savedMdl) {
      status = 404
      payload = { detail: 'MDL revision not found' }
    } else {
      const columns = savedMdl.models.flatMap((model) => model.columns)
      const described = columns.filter((column) => (column.description ?? '').trim()).length
      const modelsDescribed = savedMdl.models.filter((model) => (model.description ?? '').trim()).length
      payload = {
        project_id: project.id,
        revision: savedMdl.revision,
        models: { described: modelsDescribed, total: savedMdl.models.length, percent: Math.round(100 * modelsDescribed / savedMdl.models.length) },
        columns: { described, total: columns.length, percent: columns.length ? Math.round(100 * described / columns.length) : 100 },
        needsReview: savedMdl.models
          .filter((model) => !(model.description ?? '').trim())
          .map((model) => ({ model: model.name, modelDescribed: false, undescribedColumns: [] })),
        checks: (savedMdl.metrics ?? []).map((metric) => ({ kind: 'metric', name: metric.name, ok: true, errors: [] })),
        deployable: true,
      }
    }
  } else if (
    request.method === 'POST'
    && url.pathname.match(/^\/api\/v1\/projects\/[^/]+\/query$/)
  ) {
    if (!activeRevision) {
      status = 409
      payload = { detail: 'Deploy a semantic model before asking questions' }
    } else {
      payload = {
        sql: 'SELECT customer_id, first_name FROM ecommerce.customers ORDER BY customer_id LIMIT 5',
        columns: ['customer_id', 'first_name'],
        rows: [[1, 'Mock customer'], [2, 'Preview customer']],
        retrieved_models: ['customers'],
        model_version: activeRevision,
        elapsed_ms: 12,
        truncated: false,
        source: 'mock',
      }
    }
  } else if (request.method === 'POST' && url.pathname === '/api/v1/threads') {
    const input = JSON.parse(body)
    status = 201
    payload = {
      id: crypto.randomUUID(),
      project_id: input.project_id,
      title: 'New question',
    }
  } else if (
    request.method === 'POST'
    && url.pathname.match(/^\/api\/v1\/threads\/[^/]+\/ask$/)
  ) {
    status = 501
    payload = {
      code: 'NOT_IMPLEMENTED',
      message: 'The Text-to-SQL pipeline is not implemented yet',
    }
  } else {
    status = 404
    payload = { code: 'NOT_FOUND', message: 'Mock endpoint not found' }
  }

  response.writeHead(status, {
    'content-type': 'application/json',
    'access-control-allow-origin': '*',
    ...(setCookie ? { 'set-cookie': setCookie } : {}),
  })
  response.end(status === 204 ? undefined : JSON.stringify(payload))
})

const port = Number(process.env.VITE_MOCK_API_PORT ?? '4010')
server.listen(port, '127.0.0.1', () => {
  console.log(`Mock API listening at http://127.0.0.1:${port}`)
})
