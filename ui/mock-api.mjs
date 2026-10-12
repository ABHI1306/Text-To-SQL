import { createServer } from 'node:http'

const project = {
  id: '6b8b4567-e89b-12d3-a456-426614174000',
  name: 'E-commerce demo',
}

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

  if (request.method === 'POST' && url.pathname === '/api/v1/auth/login') {
    payload = { access_token: 'mock-token', token_type: 'bearer' }
  } else if (request.method === 'GET' && url.pathname === '/api/v1/me') {
    payload = me
  } else if (request.method === 'GET' && url.pathname === '/api/v1/projects') {
    payload = { items: [project] }
  } else if (request.method === 'POST' && url.pathname === '/api/v1/projects') {
    const input = JSON.parse(body)
    status = 201
    payload = { id: crypto.randomUUID(), name: input.name }
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
  } else if (url.pathname.match(/^\/api\/v1\/projects\/[^/]+\/mdl$/)) {
    payload = {
      project_id: url.pathname.split('/')[4],
      version: 1,
      models: [],
      relationships: [],
    }
  } else {
    status = 404
    payload = { code: 'NOT_FOUND', message: 'Mock endpoint not found' }
  }

  response.writeHead(status, {
    'content-type': 'application/json',
    'access-control-allow-origin': '*',
  })
  response.end(JSON.stringify(payload))
})

server.listen(4010, '127.0.0.1', () => {
  console.log('Mock API listening at http://127.0.0.1:4010')
})
