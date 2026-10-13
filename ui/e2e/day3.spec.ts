import { expect, test } from '@playwright/test'

const demoMdl = {
  version: 3,
  dialect: 'postgresql',
  models: ['customers', 'orders', 'order_items'].map((name) => ({
    name,
    tableReference: `ecommerce.${name}`,
    description: `The ${name} model`,
    primaryKey: `${name.slice(0, -1)}_id`,
    columns: [{ name: `${name.slice(0, -1)}_id`, type: 'integer', isPrimaryKey: true }],
  })),
  relationships: [
    { name: 'orders_customers', models: ['orders', 'customers'], joinType: 'MANY_TO_ONE', condition: 'orders.customer_id = customers.customer_id' },
    { name: 'items_orders', models: ['order_items', 'orders'], joinType: 'MANY_TO_ONE', condition: 'order_items.order_id = orders.order_id' },
  ],
  metrics: [],
  instructions: [],
  layout: {},
}

test('password fields can be revealed and hidden without breaking the label', async ({ page }) => {
  await page.route('**/api/v1/auth/refresh', (route) => route.fulfill({ status: 401 }))
  await page.goto('/')
  const password = page.getByLabel('Password', { exact: true })
  await password.fill('local-test-password-1')
  await expect(password).toHaveAttribute('type', 'password')

  await page.getByRole('button', { name: 'Show' }).click()
  await expect(password).toHaveAttribute('type', 'text')
  await expect(page.getByRole('button', { name: 'Hide' })).toHaveAttribute('aria-pressed', 'true')

  await page.getByRole('button', { name: 'Hide' }).click()
  await expect(password).toHaveAttribute('type', 'password')
})

test('sidebar has the Ask, Work, Admin and System groups', async ({ page }) => {
  await page.goto('/')
  const nav = page.getByRole('navigation', { name: 'Main navigation' })
  const groups = nav.locator('.nav-group')
  await expect(groups.locator('.nav-group-label')).toHaveText(['ASK', 'WORK', 'ADMIN', 'SYSTEM'])

  const system = groups.filter({ hasText: 'SYSTEM' })
  for (const page_ of ['Users and roles', 'Audit', 'Evaluation']) {
    await expect(system.getByRole('button', { name: page_ })).toBeVisible()
  }
  const admin = groups.filter({ has: page.getByText('ADMIN', { exact: true }) })
  await expect(admin.getByRole('button', { name: 'Users and roles' })).toHaveCount(0)
  await expect(groups.filter({ hasText: 'ASK' }).getByRole('button', { name: 'Shared with me' })).toBeVisible()
})

test('notification bell shows no made-up unread count', async ({ page }) => {
  await page.goto('/')
  const bell = page.getByRole('button', { name: 'Notifications', exact: true })
  await expect(bell).toBeVisible()
  await expect(bell.locator('b')).toHaveCount(0)
})

test('mobile layout offers a bottom bar with Home, Shared with me and Dashboards', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: 'Mobile 390' }).click()
  const bar = page.getByRole('navigation', { name: 'Mobile navigation' })
  await expect(bar).toBeVisible()
  await expect(bar.getByRole('button')).toHaveText([/Home/, /Shared with me/, /Dashboards/])
  await expect(bar.getByRole('button', { name: /Home/ })).toHaveAttribute('aria-current', 'page')

  await bar.getByRole('button', { name: /Dashboards/ }).click()
  await expect(page.getByRole('heading', { name: 'Dashboards' })).toBeVisible()
  await expect(bar.getByRole('button', { name: /Dashboards/ })).toHaveAttribute('aria-current', 'page')

  await page.getByRole('button', { name: 'Desktop 1280' }).click()
  await expect(bar).toBeHidden()
})

test('canvas renders the demo model and saves moved node positions', async ({ page }) => {
  let saved: { layout: Record<string, { x: number; y: number }> } | null = null
  await page.route('**/api/v1/projects/*/mdl', async (route) => {
    if (route.request().method() === 'PUT') {
      saved = route.request().postDataJSON()
      await route.fulfill({
        json: { ...saved, project_id: 'p', revision: 2, status: 'draft' },
      })
    } else {
      await route.fulfill({ json: { ...demoMdl, project_id: 'p', revision: 1, status: 'draft' } })
    }
  })
  await page.goto('/')
  await page.getByRole('button', { name: 'Connect and model', exact: true }).click()

  await expect(page.locator('.react-flow__node')).toHaveCount(3)
  await expect(page.locator('.react-flow__edge')).toHaveCount(2)
  const models = page.getByRole('list', { name: 'Models in this project' })
  await expect(models.getByRole('listitem')).toHaveCount(3)

  const node = page.locator('.react-flow__node').filter({ hasText: 'orders' }).first()
  const box = await node.boundingBox()
  expect(box).not.toBeNull()
  await page.mouse.move(box!.x + box!.width / 2, box!.y + 12)
  await page.mouse.down()
  await page.mouse.move(box!.x + box!.width / 2 + 140, box!.y + 12 + 90, { steps: 8 })
  await page.mouse.up()

  await page.getByRole('button', { name: 'Save model' }).click()
  await expect(page.getByRole('status').filter({ hasText: 'Model saved.' })).toBeVisible()
  expect(saved).not.toBeNull()
  const layout = saved!.layout
  expect(Object.keys(layout).sort()).toEqual(['customers', 'order_items', 'orders'])
  const initial = { customers: [0, 0], orders: [300, 0], order_items: [600, 0] }
  expect([layout.orders.x, layout.orders.y]).not.toEqual(initial.orders)
})
