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
  } else if (request.method === 'POST' && url.pathname === '/api/v1/auth/logout') {
    status = 204
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
