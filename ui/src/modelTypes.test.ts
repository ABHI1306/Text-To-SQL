import { describe, expect, it } from 'vitest'

import {
  describeFailure,
  draftForCompile,
  hasRelationship,
  inferRelationship,
  isValidName,
  layoutFor,
  uniqueRelationshipName,
  type Mdl,
} from './modelTypes'

const mdl: Mdl = {
  version: 3,
  dialect: 'postgresql',
  models: [
    {
      name: 'orders',
      tableReference: 'ecommerce.orders',
      description: 'Orders',
      primaryKey: 'order_id',
      columns: [
        { name: 'order_id', type: 'integer', isPrimaryKey: true },
        { name: 'customer_id', type: 'integer' },
      ],
    },
    {
      name: 'customers',
      tableReference: 'ecommerce.customers',
      description: 'Customers',
      primaryKey: 'customer_id',
      columns: [{ name: 'customer_id', type: 'integer', isPrimaryKey: true }],
    },
    {
      name: 'categories',
      tableReference: 'ecommerce.categories',
      description: 'Categories',
      primaryKey: 'category_id',
      columns: [{ name: 'category_id', type: 'integer', isPrimaryKey: true }],
    },
  ],
  relationships: [],
  metrics: [],
  instructions: [],
  layout: {},
  project_id: 'p',
  revision: 3,
  status: 'draft',
}

describe('inferRelationship', () => {
  it('uses the foreign-key-like column and the target key', () => {
    expect(inferRelationship(mdl, 'orders', 'customers')).toMatchObject({
      name: 'orders_customers',
      models: ['orders', 'customers'],
      joinType: 'MANY_TO_ONE',
      condition: 'orders.customer_id = customers.customer_id',
    })
  })

  it('leaves the condition empty when nothing matches', () => {
    expect(inferRelationship(mdl, 'orders', 'categories').condition).toBe('')
  })
})

describe('relationship helpers', () => {
  it('detects an existing relationship in either direction', () => {
    const related = {
      ...mdl,
      relationships: [{ name: 'a', models: ['orders', 'customers'] as [string, string], joinType: 'MANY_TO_ONE', condition: 'x' }],
    }
    expect(hasRelationship(related, 'customers', 'orders')).toBe(true)
    expect(hasRelationship(related, 'orders', 'categories')).toBe(false)
    expect(uniqueRelationshipName(related, 'orders', 'customers')).toBe('orders_customers')
    expect(uniqueRelationshipName({ ...related, relationships: [{ ...related.relationships[0], name: 'orders_customers' }] }, 'orders', 'customers')).toBe('orders_customers_2')
  })

  it('keeps relationships without a condition out of compiler requests', () => {
    const draft = draftForCompile({
      ...mdl,
      relationships: [
        { name: 'done', models: ['orders', 'customers'], joinType: 'MANY_TO_ONE', condition: 'orders.customer_id = customers.customer_id' },
        { name: 'typing', models: ['orders', 'categories'], joinType: 'MANY_TO_ONE', condition: '  ' },
      ],
    })
    expect(draft.relationships.map((item) => item.name)).toEqual(['done'])
    expect(draft).not.toHaveProperty('revision')
    expect(draft).not.toHaveProperty('project_id')
  })
})

describe('names, layout and errors', () => {
  it('validates MDL names', () => {
    expect(isValidName('monthly_revenue')).toBe(true)
    expect(isValidName('Monthly')).toBe(false)
    expect(isValidName('1st')).toBe(false)
    expect(isValidName('')).toBe(false)
  })

  it('places unpositioned models on a grid and keeps saved positions', () => {
    const layout = layoutFor({ ...mdl, layout: { customers: { x: 5, y: 6 } } })
    expect(layout.customers).toEqual({ x: 5, y: 6 })
    expect(layout.orders).toEqual({ x: 0, y: 0 })
  })

  it('turns validation failures into one readable message', () => {
    expect(describeFailure({ detail: 'Project not found' }, 'x')).toBe('Project not found')
    expect(describeFailure({ detail: [{ msg: 'Value error, models.orders: bad', loc: [] }] }, 'x')).toBe('models.orders: bad')
    expect(describeFailure(null, 'fallback')).toBe('fallback')
  })
})
