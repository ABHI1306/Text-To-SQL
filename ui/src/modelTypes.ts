export type ApiRequest = (path: string, init?: RequestInit) => Promise<Response>

export type Column = {
  name: string
  type: string
  description?: string
  physicalName?: string | null
  isPrimaryKey?: boolean
  isCalculated?: boolean
  expression?: string | null
  needsReview?: boolean
}

export type Model = {
  name: string
  tableReference: string
  description: string
  primaryKey?: string | null
  columns: Column[]
  approxRows?: number
  needsReview?: boolean
}

export type Relationship = {
  name: string
  models: [string, string]
  joinType: string
  condition: string
  source?: string
  needsReview?: boolean
}

export type MetricField = { name: string; expression: string; description?: string }

export type Metric = {
  name: string
  baseObject: string
  dimensions: MetricField[]
  measures: MetricField[]
  description?: string
}

export type Mdl = {
  version: number
  dialect: string
  models: Model[]
  relationships: Relationship[]
  metrics: Metric[]
  instructions: string[]
  layout: Record<string, { x: number; y: number }>
  project_id?: string
  revision?: number
  status?: string
}

export type CompileIssue = {
  code: string
  message: string
  identifier?: string | null
  suggestions?: string[]
}

export type CompileResult = {
  ok: boolean
  sql: string
  errors: CompileIssue[]
  sample?: { columns?: string[]; rows?: unknown[][]; truncated?: boolean; error?: string }
}

export const JOIN_TYPES = ['MANY_TO_ONE', 'ONE_TO_MANY', 'ONE_TO_ONE', 'MANY_TO_MANY'] as const

const NAME_PATTERN = /^[a-z][a-z0-9_]*$/

export function isValidName(name: string): boolean {
  return NAME_PATTERN.test(name)
}

function singular(name: string): string {
  if (name.endsWith('ies')) return `${name.slice(0, -3)}y`
  if (name.endsWith('s')) return name.slice(0, -1)
  return name
}

function keyOf(model: Model): string | undefined {
  return model.primaryKey
    ?? model.columns.find((column) => column.isPrimaryKey)?.name
}

/** Guess the join condition for a dragged connection from `source` to `target`. */
export function inferRelationship(mdl: Mdl, source: string, target: string): Relationship {
  const from = mdl.models.find((model) => model.name === source)
  const to = mdl.models.find((model) => model.name === target)
  let condition = ''
  if (from && to) {
    const names = new Set(from.columns.map((column) => column.name))
    const targetKey = keyOf(to)
    const candidates = [`${singular(target)}_id`, `${target}_id`, targetKey, keyOf(from)]
    for (const candidate of candidates) {
      if (candidate && names.has(candidate) && to.columns.some((c) => c.name === candidate)) {
        const targetColumn = candidate === `${singular(target)}_id` && targetKey ? targetKey : candidate
        condition = `${source}.${candidate} = ${target}.${targetColumn}`
        break
      }
    }
  }
  return {
    name: uniqueRelationshipName(mdl, source, target),
    models: [source, target],
    joinType: 'MANY_TO_ONE',
    condition,
    source: 'manual',
  }
}

export function uniqueRelationshipName(mdl: Mdl, a: string, b: string): string {
  const taken = new Set(mdl.relationships.map((relationship) => relationship.name))
  const base = `${a}_${b}`
  let name = base
  for (let index = 2; taken.has(name); index += 1) name = `${base}_${index}`
  return name
}

export function hasRelationship(mdl: Mdl, a: string, b: string): boolean {
  return mdl.relationships.some(
    ({ models }) => (models[0] === a && models[1] === b) || (models[0] === b && models[1] === a),
  )
}

/** The draft as the compiler can read it: relationships still being typed are left out. */
export function draftForCompile(mdl: Mdl): Omit<Mdl, 'project_id' | 'revision' | 'status'> {
  const { project_id: _p, revision: _r, status: _s, ...rest } = mdl
  void _p
  void _r
  void _s
  return { ...rest, relationships: mdl.relationships.filter((r) => (r.condition ?? '').trim() !== '') }
}

export function describeFailure(body: unknown, fallback: string): string {
  const detail = (body as { detail?: unknown } | null)?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail
      .map((item: { msg?: string; loc?: unknown[] }) => {
        const message = (item.msg ?? '').replace(/^Value error, /, '')
        return message || fallback
      })
      .join('; ')
  }
  return fallback
}

export async function compilePreview(
  apiRequest: ApiRequest,
  projectId: string,
  mdl: Mdl,
  logicalSql: string,
  options: { sample?: boolean } = {},
): Promise<CompileResult> {
  const response = await apiRequest(`/api/v1/projects/${projectId}/compile-preview`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ logicalSql, mdl: draftForCompile(mdl), sample: options.sample ?? false }),
  })
  const body = await response.json().catch(() => null) as CompileResult | null
  if (response.ok && body) return body
  return {
    ok: false,
    sql: '',
    errors: [{ code: 'INVALID_MODEL', message: describeFailure(body, `Compile check failed (${response.status})`) }],
  }
}

export function layoutFor(mdl: Mdl): Record<string, { x: number; y: number }> {
  const layout: Record<string, { x: number; y: number }> = {}
  mdl.models.forEach((model, index) => {
    layout[model.name] = mdl.layout?.[model.name] ?? {
      x: (index % 3) * 300,
      y: Math.floor(index / 3) * 200,
    }
  })
  return layout
}

