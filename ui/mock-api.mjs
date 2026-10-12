import { createServer } from 'node:http'

let project = {
  id: '6b8b4567-e89b-12d3-a456-426614174000',
  name: 'E-commerce demo',
  setup_step: 'tables',
}
let selectedTables = []
let mdlRevision = 0
let savedMdl

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

const server = createServer(async (request, response) => {
  const chunks = []
  for await (const chunk of request) chunks.push(chunk)
  const body = Buffer.concat(chunks).toString()
  const url = new URL(request.url ?? '/', 'http://127.0.0.1:4010')
  let status = 200
  let payload

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
  } else if (request.method === 'POST' && url.pathname === '/api/v1/auth/change-password') {
    status = 204
  } else if (request.method === 'GET' && url.pathname === '/api/v1/me') {
    payload = me
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

server.listen(4010, '127.0.0.1', () => {
  console.log('Mock API listening at http://127.0.0.1:4010')
})
