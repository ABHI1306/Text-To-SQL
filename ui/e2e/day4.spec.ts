import { expect, test, type Page } from '@playwright/test'

type Doc = Record<string, unknown>

function demo(): Doc {
  const column = (name: string, extra: Doc = {}) => ({ name, type: 'integer', description: name, ...extra })
  return {
    version: 3,
    dialect: 'postgresql',
    models: [
      {
        name: 'customers',
        tableReference: 'ecommerce.customers',
        description: 'Customers',
        primaryKey: 'customer_id',
        columns: [column('customer_id', { isPrimaryKey: true })],
      },
      {
        name: 'products',
        tableReference: 'ecommerce.products',
        description: '',
        primaryKey: 'product_id',
        columns: [column('product_id', { isPrimaryKey: true })],
      },
      {
        name: 'order_items',
        tableReference: 'ecommerce.order_items',
        description: 'Order lines',
        primaryKey: 'order_item_id',
        columns: [
          column('order_item_id', { isPrimaryKey: true }),
          column('product_id'),
          column('quantity'),
          column('unit_price', { type: 'numeric' }),
        ],
      },
    ],
    relationships: [],
    metrics: [],
    instructions: [],
    layout: { customers: { x: 0, y: 0 }, products: { x: 400, y: 0 }, order_items: { x: 200, y: 240 } },
  }
}

/** Serves /mdl from memory and records what was saved. */
async function serveModel(page: Page) {
  const state: { saved: Doc[]; current: Doc } = { saved: [], current: { ...demo(), project_id: 'p', revision: 1, status: 'draft' } }
  await page.route('**/api/v1/projects/*/mdl', async (route) => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON() as Doc
      state.saved.push(body)
      state.current = { ...body, project_id: 'p', revision: state.saved.length + 1, status: 'draft' }
      await route.fulfill({ json: state.current })
    } else {
      await route.fulfill({ json: state.current })
    }
  })
  return state
}

async function openCanvas(page: Page) {
  await page.goto('/')
  await page.getByRole('button', { name: 'Connect and model', exact: true }).click()
  await expect(page.locator('.react-flow__node')).toHaveCount(3)
}

async function drag(page: Page, from: string, to: string) {
  const source = page.locator('.react-flow__node').filter({ hasText: from }).locator('.react-flow__handle.source')
  const target = page.locator('.react-flow__node').filter({ hasText: to }).locator('.react-flow__handle.target')
  const a = (await source.boundingBox())!
  const b = (await target.boundingBox())!
  await page.mouse.move(a.x + a.width / 2, a.y + a.height / 2)
  await page.mouse.down()
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 12 })
  await page.mouse.up()
}

test('dragging between two cards creates a relationship with an inferred condition that is saved', async ({ page }) => {
  const state = await serveModel(page)
  await openCanvas(page)
  await expect(page.getByRole('button', { name: 'Save model' })).toBeDisabled()

  await drag(page, 'order_items', 'products')
  await expect(page.locator('.react-flow__edge')).toHaveCount(1)
  const panel = page.getByRole('region', { name: 'Relationship order_items_products' })
  await expect(panel.getByLabel('Join condition')).toHaveValue('order_items.product_id = products.product_id')
  await expect(panel.getByRole('status')).toContainText('Compiles')

  await page.getByRole('button', { name: 'Save model' }).click()
  await expect(page.getByRole('status').filter({ hasText: 'Model saved.' })).toBeVisible()
  const relationships = state.saved[0].relationships as { name: string; condition: string; joinType: string }[]
  expect(relationships).toHaveLength(1)
  expect(relationships[0]).toMatchObject({
    name: 'order_items_products',
    joinType: 'MANY_TO_ONE',
    condition: 'order_items.product_id = products.product_id',
  })
})

test('a relationship with no inferable condition blocks saving until one is entered', async ({ page }) => {
  await serveModel(page)
  await openCanvas(page)
  await drag(page, 'customers', 'products')
  await expect(page.getByRole('alert').filter({ hasText: 'join condition is required' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Save model' })).toBeDisabled()

  await page.getByLabel('Join condition').fill('customers.customer_id = products.product_id')
  await expect(page.getByRole('button', { name: 'Save model' })).toBeEnabled()
})

test('a calculated field is checked live and a failing expression shows the compiler error inline', async ({ page }) => {
  const state = await serveModel(page)
  await openCanvas(page)
  await page.locator('.react-flow__node').filter({ hasText: 'order_items' }).click()
  const panel = page.getByRole('region', { name: 'Model order_items' })
  await panel.getByLabel('New calculated field name').fill('line_total')
  await panel.getByLabel('New calculated field expression').fill('quantity * unit_price')
  await panel.getByRole('button', { name: 'Add field' }).click()
  await expect(panel.getByRole('status').filter({ hasText: 'Compiles' })).toBeVisible()

  await panel.getByLabel('Expression of line_total').fill('quantity * ghost')
  const errors = panel.getByRole('alert', { name: 'Compiler errors' })
  await expect(errors).toContainText('UNKNOWN_COLUMN')
  await expect(errors).toContainText("Column 'ghost' could not be resolved")
  await expect(errors).toContainText('Did you mean order_id')

  await panel.getByLabel('Expression of line_total').fill('quantity * unit_price')
  await panel.getByRole('textbox', { name: 'Description', exact: true }).fill('Order lines, edited')
  await page.getByRole('button', { name: 'Save model' }).click()
  await expect(page.getByRole('status').filter({ hasText: 'Model saved.' })).toBeVisible()
  const saved = state.saved[0].models as { name: string; description: string; columns: { name: string; expression?: string }[] }[]
  const items = saved.find((model) => model.name === 'order_items')!
  expect(items.description).toBe('Order lines, edited')
  expect(items.columns.find((c) => c.name === 'line_total')?.expression).toBe('quantity * unit_price')
})

test('metrics editor previews compiled SQL and a sample result, then saves the metric', async ({ page }) => {
  const state = await serveModel(page)
  await openCanvas(page)
  await page.getByRole('tab', { name: 'Metrics' }).click()
  await page.getByRole('button', { name: 'New metric' }).click()
  const form = page.getByRole('form', { name: 'Metric editor' })
  await form.getByLabel('Metric name').fill('monthly_revenue')
  await form.getByLabel('Base model').selectOption('order_items')
  await form.getByLabel('Dimensions 1 name').fill('order_month')
  await form.getByLabel('Dimensions 1 expression').fill("date_trunc('month', ordered_at)")
  await form.getByLabel('Measures 1 name').fill('revenue')
  await form.getByLabel('Measures 1 expression').fill('sum(quantity * unit_price)')

  await form.getByRole('button', { name: 'Preview' }).click()
  const preview = form.getByLabel('Compiled preview')
  await expect(preview.getByRole('heading', { name: 'Compiled SQL' })).toBeVisible()
  await expect(preview.locator('pre')).toContainText('SELECT * FROM monthly_revenue')
  await expect(preview.getByRole('cell', { name: '1200' })).toBeVisible()

  await form.getByRole('button', { name: 'Apply metric' }).click()
  await page.getByRole('button', { name: 'Save model' }).click()
  await expect(page.getByRole('status').filter({ hasText: 'Model saved.' })).toBeVisible()
  const metrics = state.saved[0].metrics as { name: string; baseObject: string; measures: { name: string }[] }[]
  expect(metrics).toHaveLength(1)
  expect(metrics[0]).toMatchObject({ name: 'monthly_revenue', baseObject: 'order_items' })
})

test('a failing metric expression shows the compiler error and cannot be applied', async ({ page }) => {
  await serveModel(page)
  await openCanvas(page)
  await page.getByRole('tab', { name: 'Metrics' }).click()
  await page.getByRole('button', { name: 'New metric' }).click()
  const form = page.getByRole('form', { name: 'Metric editor' })
  await form.getByLabel('Metric name').fill('broken_metric')
  await form.getByLabel('Dimensions 1 name').fill('d')
  await form.getByLabel('Dimensions 1 expression').fill('ghost')
  await form.getByLabel('Measures 1 name').fill('m')
  await form.getByLabel('Measures 1 expression').fill('count(*)')
  await form.getByRole('button', { name: 'Preview' }).click()
  await expect(form.getByRole('alert', { name: 'Compiler errors' })).toContainText('UNKNOWN_COLUMN')
  await expect(form.getByRole('button', { name: 'Apply metric' })).toBeDisabled()
})

test('metric form explains invalid input before any request is made', async ({ page }) => {
  await serveModel(page)
  await openCanvas(page)
  await page.getByRole('tab', { name: 'Metrics' }).click()
  await page.getByRole('button', { name: 'New metric' }).click()
  const problems = page.getByRole('list', { name: 'Metric problems' })
  await expect(problems).toContainText('Metric name must be lowercase')
  await expect(page.getByRole('button', { name: 'Preview' })).toBeDisabled()
  await page.getByLabel('Metric name').fill('customers')
  await expect(problems).toContainText('already used')
})

test('deploy report shows coverage and gates deployment on unsaved changes', async ({ page }) => {
  const state = await serveModel(page)
  await page.route('**/api/v1/projects/*/deploy-report?*', async (route) => {
    await route.fulfill({
      json: {
        project_id: 'p',
        revision: state.current.revision,
        models: { described: 2, total: 3, percent: 67 },
        columns: { described: 6, total: 6, percent: 100 },
        needsReview: [{ model: 'products', modelDescribed: false, undescribedColumns: [] }],
        checks: [
          { kind: 'metric', name: 'monthly_revenue', ok: true, errors: [] },
          { kind: 'calculated_field', name: 'order_items.bad', ok: false, errors: [{ code: 'UNKNOWN_COLUMN', message: 'nope' }] },
        ],
        deployable: false,
      },
    })
  })
  await openCanvas(page)
  await page.getByRole('tab', { name: 'Deploy report' }).click()
  const report = page.getByRole('region', { name: 'Deploy report' })
  await expect(report).toContainText('67%')
  await expect(report).toContainText('models described (2/3)')
  await expect(report).toContainText('1/2')
  await expect(report).toContainText('UNKNOWN_COLUMN: nope')
  await expect(report).toContainText('products')
  await expect(report.getByRole('alert')).toContainText('Fix the failing compiler checks first.')
  await expect(report.getByRole('button', { name: 'Deploy model' })).toBeDisabled()
})

test('deploying a clean report succeeds and reports the version', async ({ page }) => {
  const state = await serveModel(page)
  await page.route('**/api/v1/projects/*/deploy-report?*', (route) => route.fulfill({
    json: {
      project_id: 'p',
      revision: state.current.revision,
      models: { described: 3, total: 3, percent: 100 },
      columns: { described: 6, total: 6, percent: 100 },
      needsReview: [],
      checks: [],
      deployable: true,
    },
  }))
  await page.route('**/api/v1/projects/*/deploy?*', (route) => route.fulfill({ json: { version: 1, indexed_chunks: 3 } }))
  await openCanvas(page)
  await page.getByRole('tab', { name: 'Deploy report' }).click()
  await page.getByRole('button', { name: 'Deploy model' }).click()
  await expect(page.getByRole('status').filter({ hasText: 'Model revision 1 is deployed' })).toBeVisible()
})

test('profile menu offers change password and sign out', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: /demo\.dba/ }).click()
  const menu = page.getByRole('menu', { name: 'Account menu' })
  await expect(menu.getByRole('menuitem')).toHaveText(['Change password', 'Sign out'])
  await page.keyboard.press('Escape')
  await expect(menu).toBeHidden()

  await page.getByRole('button', { name: /demo\.dba/ }).click()
  await page.getByRole('menuitem', { name: 'Change password' }).click()
  const dialog = page.getByRole('dialog', { name: 'Change password' })
  await dialog.getByLabel('Current password').fill('old-password-123')
  await dialog.getByLabel('New password').fill('a-new-password-123')
  const request = page.waitForRequest((r) => r.url().endsWith('/auth/change-password'))
  await dialog.getByRole('button', { name: 'Change password' }).click()
  expect((await request).postDataJSON()).toEqual({ currentPassword: 'old-password-123', newPassword: 'a-new-password-123' })
  await expect(dialog.getByRole('status')).toContainText('Password changed')
  await dialog.getByRole('button', { name: 'Close' }).click()

  await page.getByRole('button', { name: /demo\.dba/ }).click()
  await page.getByRole('menuitem', { name: 'Sign out' }).click()
  await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
})
