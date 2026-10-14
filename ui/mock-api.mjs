import { createServer } from 'node:http'

let project = {
  id: '6b8b4567-e89b-12d3-a456-426614174000',
  name: 'E-commerce demo',
  setup_step: 'tables',
}
let selectedTables = []
let mdlRevision = 0
let savedMdl
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

  const admin = handleAdmin(request.method ?? 'GET', url.pathname, body)
  if (admin) {
    response.writeHead(admin.status, {
      'content-type': 'application/json',
      'access-control-allow-origin': '*',
    })
    response.end(admin.status === 204 ? undefined : JSON.stringify(admin.payload))
    return
  }

  if (
    request.method === 'POST'
    && url.pathname.match(/^\/api\/v1\/projects\/[^/]+\/query\/stream$/)
  ) {
    const question = String(JSON.parse(body || '{}').question ?? '')
    const send = (type, data) => response.write(`event: ${type}\ndata: ${JSON.stringify(data)}\n\n`)
    const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
    response.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' })
    if (!activeRevision) {
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
    send('rows', { columns: ['customer_id', 'first_name'], rows: [[1, 'Mock customer'], [2, 'Preview customer']], truncated: false, row_count: 2, ms: 11 })
    send('done', { message_id: 'm1', latency_ms: 106, attempts: 0 })
    response.end()
    return
  }

  if (
    request.method === 'POST'
    && ['/api/v1/auth/login', '/api/v1/auth/refresh'].includes(url.pathname)
  ) {
    payload = {
      access_token: 'mock-token',
      token_type: 'bearer',
      expires_in: 900,
      must_change_password: false,
    }
  } else if (request.method === 'POST' && url.pathname === '/api/v1/auth/logout') {
    status = 204
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
    'set-cookie': 'refresh_token=mock-refresh-token; HttpOnly; SameSite=Strict; Path=/api/v1/auth',
  })
  response.end(status === 204 ? undefined : JSON.stringify(payload))
})

const port = Number(process.env.VITE_MOCK_API_PORT ?? '4010')
server.listen(port, '127.0.0.1', () => {
  console.log(`Mock API listening at http://127.0.0.1:${port}`)
})
