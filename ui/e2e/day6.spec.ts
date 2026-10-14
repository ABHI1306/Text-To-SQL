import { expect, test, type Page } from '@playwright/test'

async function open(page: Page, screen: string) {
  await page.goto('/')
  await page.getByRole('button', { name: screen, exact: true }).click()
}

const DEMO_MODEL = {
  version: 3,
  dialect: 'postgresql',
  models: [{ name: 'customers', tableReference: 'ecommerce.customers', description: 'c', columns: [] }],
  relationships: [],
  metrics: [],
  instructions: [],
  layout: {},
  project_id: 'p',
  revision: 1,
}

test.describe('knowledge screen', () => {
  test('lists pairs, shows the nearest approved pair beside a proposal and approves or rejects', async ({ page }) => {
    await open(page, 'Knowledge')
    await expect(page.getByRole('heading', { name: 'Knowledge' })).toBeVisible()
    await expect(page.getByRole('list', { name: 'Approved SQL pairs' })).toContainText('How many customers do we have?')

    await page.getByRole('tab', { name: /Proposed pairs/ }).click()
    const first = page.getByRole('listitem').filter({ hasText: 'Count of all customers' })
    await expect(first.getByRole('region', { name: /Nearest approved pair/ })).toContainText('How many customers do we have?')
    const second = page.getByRole('listitem').filter({ hasText: 'Average order value by month' })
    await expect(second).toContainText('No similar approved pair exists.')

    await page.getByRole('button', { name: 'Approve: Count of all customers' }).click()
    await expect(page.getByRole('status').filter({ hasText: 'Proposal approved' })).toBeVisible()
    await expect(page.getByText('Count of all customers')).toHaveCount(0)
    await page.getByRole('button', { name: 'Reject: Average order value by month' }).click()
    await expect(page.getByText('No proposed pairs are waiting for review.')).toBeVisible()

    await page.getByRole('tab', { name: 'SQL pairs' }).click()
    await expect(page.getByRole('list', { name: 'Approved SQL pairs' })).toContainText('Count of all customers')
  })

  test('adds, edits and deletes instructions and adds a pair', async ({ page }) => {
    await open(page, 'Knowledge')
    await page.getByRole('tab', { name: 'Instructions' }).click()
    await page.getByLabel('Instruction', { exact: true }).fill('Fiscal year starts in April.')
    await page.getByRole('button', { name: 'Add instruction' }).click()
    await expect(page.getByRole('list', { name: 'Instructions' })).toContainText('Fiscal year starts in April.')
    await page.getByRole('button', { name: 'Edit instruction: Fiscal year starts in April.' }).click()
    await page.getByRole('textbox', { name: 'Edit instruction' }).fill('Fiscal year starts in July.')
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    await expect(page.getByRole('list', { name: 'Instructions' })).toContainText('Fiscal year starts in July.')
    await page.getByRole('button', { name: 'Delete instruction: Fiscal year starts in July.' }).click()
    await expect(page.getByText('Fiscal year starts in July.', { exact: true })).toHaveCount(0)

    await page.getByRole('tab', { name: 'SQL pairs' }).click()
    await page.getByLabel('Question').fill('List all products')
    await page.getByLabel('SQL', { exact: true }).fill('SELECT * FROM products')
    await page.getByRole('button', { name: 'Add pair' }).click()
    await expect(page.getByRole('list', { name: 'Approved SQL pairs' })).toContainText('List all products')
  })

  test('shows an error state with retry when the API fails', async ({ page }) => {
    await page.route('**/knowledge/sql-pairs', (route) => route.fulfill({ status: 500, json: { detail: 'Knowledge store unavailable' } }))
    await open(page, 'Knowledge')
    await expect(page.getByRole('alert')).toContainText('Knowledge store unavailable')
    await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible()
  })
})

test.describe('users and roles screen', () => {
  test('creates a user, unlocks and resets accounts', async ({ page }) => {
    await open(page, 'Users and roles')
    const accounts = page.getByRole('table', { name: 'Accounts' })
    await expect(accounts).toContainText('maria')
    await page.getByRole('button', { name: 'Create user' }).click()
    const dialog = page.getByRole('dialog', { name: 'Create user' })
    await dialog.getByLabel('Username').fill('new.analyst')
    await dialog.getByLabel('Temporary password').fill('short')
    await dialog.getByRole('button', { name: 'Create account' }).click()
    await expect(dialog).toBeVisible() // native validation blocks a short password
    await dialog.getByLabel('Temporary password').fill('a-long-temporary-password')
    await dialog.getByRole('button', { name: 'Create account' }).click()
    await expect(accounts).toContainText('new.analyst')

    await page.getByRole('button', { name: 'Unlock jonas' }).click()
    await expect(page.getByRole('status').filter({ hasText: 'jonas unlocked' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Unlock jonas' })).toHaveCount(0)

    await page.getByRole('button', { name: 'Reset password for maria' }).click()
    await page.getByRole('form', { name: 'Reset password for maria' }).getByLabel('New temporary password').fill('another-temporary-pass')
    await page.getByRole('button', { name: 'Set temporary password' }).click()
    await expect(page.getByRole('status').filter({ hasText: 'Password for maria reset' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Reset password for sam.ldap' })).toHaveCount(0)
  })

  test('edits access and attributes for a user', async ({ page }) => {
    await open(page, 'Users and roles')
    await page.getByRole('button', { name: 'Access and attributes for maria' }).click()
    const panel = page.getByRole('region', { name: 'Access for maria' })
    await expect(panel.getByLabel(/Attributes/)).toHaveValue(/EU/)
    await panel.getByLabel(/Attributes/).fill('{"region": "US"}')
    await panel.getByRole('button', { name: 'Save attributes' }).click()
    await expect(panel).toContainText('Attributes saved')
    await panel.getByLabel(/Attributes/).fill('[1]')
    await panel.getByRole('button', { name: 'Save attributes' }).click()
    await expect(panel.getByRole('alert')).toContainText('JSON object')
  })

  test('custom role editor lists capabilities and shows the effect of a change on the next /me', async ({ page }) => {
    let changed = false
    await page.route('**/api/v1/roles/*', async (route) => {
      if (route.request().method() === 'PUT') changed = true
      await route.continue()
    })
    await page.route('**/api/v1/me', async (route) => {
      const response = await route.fetch()
      const body = await response.json()
      if (changed) body.capabilities = body.capabilities.filter((name: string) => name !== 'run_eval')
      await route.fulfill({ response, json: body })
    })
    await open(page, 'Users and roles')
    await page.getByRole('tab', { name: 'Roles' }).click()
    await page.getByRole('button', { name: 'New custom role' }).click()
    const editor = page.getByRole('form', { name: 'Role editor' })
    await editor.getByLabel('Role name').fill('Reviewer')
    await editor.getByLabel('manage_knowledge').check()
    await editor.getByLabel('view_audit').check()
    await editor.getByRole('button', { name: 'Save role' }).click()
    await expect(page.getByRole('list', { name: 'Roles' })).toContainText('Reviewer')
    await expect(page.getByTestId('role-effect')).toContainText('unchanged')

    await page.getByRole('button', { name: 'Edit role Reviewer' }).click()
    await page.getByRole('form', { name: 'Role editor' }).getByLabel('view_audit').uncheck()
    await page.getByRole('form', { name: 'Role editor' }).getByRole('button', { name: 'Save role' }).click()
    await expect(page.getByTestId('role-effect')).toContainText('lost run_eval')
    await expect(page.getByRole('button', { name: 'Evaluation', exact: true })).toHaveCount(0)
  })

  test('maps identity groups to roles and keeps the order', async ({ page }) => {
    await open(page, 'Users and roles')
    await page.getByRole('tab', { name: 'Identity mappings' }).click()
    await expect(page.getByLabel('Group 1', { exact: true })).toHaveValue('cn=dbas,ou=groups')
    await page.getByRole('button', { name: 'Add mapping' }).click()
    await page.getByLabel('Group 2', { exact: true }).fill('cn=analysts')
    await page.getByLabel('Role for group 2').selectOption('Analyst+')
    await page.getByRole('button', { name: 'Move group 2 up' }).click()
    await expect(page.getByLabel('Group 1', { exact: true })).toHaveValue('cn=analysts')
    await page.getByRole('button', { name: 'Save mappings' }).click()
    await expect(page.getByRole('status').filter({ hasText: 'Mappings for LDAP saved' })).toBeVisible()
  })
})

test.describe('row policies screen', () => {
  test('creates a policy and previews it for a test user', async ({ page }) => {
    await page.route('**/api/v1/projects/*/mdl', (route) => route.fulfill({ json: DEMO_MODEL }))
    await open(page, 'Row policies')
    await expect(page.getByText('No row policies yet.')).toBeVisible()
    await page.getByRole('button', { name: 'New policy' }).click()
    const editor = page.getByRole('form', { name: 'Policy editor' })
    await editor.getByLabel('Policy name').fill('eu-only')
    await editor.getByLabel(/Predicate/).fill('region = {{ user.region }}')
    await editor.getByRole('button', { name: 'Save policy' }).click()
    await expect(page.getByRole('list', { name: 'Row policies list' })).toContainText('eu-only')
    await expect(page.getByRole('region', { name: 'Save validation' })).toContainText('Compiles')

    await page.getByLabel(/^Test user/).selectOption({ label: 'maria' })
    await page.getByRole('button', { name: 'Preview as user' }).click()
    const result = page.getByRole('status', { name: 'Preview result' })
    await expect(result).toContainText('Compiled for maria')
    await expect(result).toContainText('Applied policies: eu-only')
    await expect(result).toContainText('labelled preview')

    await page.getByRole('button', { name: 'Edit policy eu-only' }).click()
    await page.getByRole('form', { name: 'Policy editor' }).getByLabel(/Predicate/).fill('ghost = 1')
    await page.getByRole('form', { name: 'Policy editor' }).getByRole('button', { name: 'Save policy' }).click()
    await expect(page.getByRole('alert')).toContainText("Column 'ghost' could not be resolved")

    await page.getByRole('button', { name: 'Cancel' }).click()
    await page.getByRole('button', { name: 'Delete policy eu-only' }).click()
    await expect(page.getByText('No row policies yet.')).toBeVisible()
  })
})

test.describe('capability gating', () => {
  test('an account without the capabilities does not see the three screens', async ({ page }) => {
    await page.route('**/api/v1/me', async (route) => {
      const response = await route.fetch()
      const body = await response.json()
      body.role = 'User'
      body.capabilities = ['ask_question', 'view_shared']
      await route.fulfill({ response, json: body })
    })
    await page.goto('/')
    await expect(page.getByRole('button', { name: 'Shared with me', exact: true })).toBeVisible()
    for (const screen of ['Knowledge', 'Row policies', 'Users and roles']) {
      await expect(page.getByRole('button', { name: screen, exact: true })).toHaveCount(0)
      await expect(page.getByLabel('Review screen').locator(`option[value="${screen}"]`)).toHaveCount(0)
    }
  })

  test('a user with only manage_knowledge sees Knowledge but not the others, and cannot decide proposals without approve_pairs', async ({ page }) => {
    await page.route('**/api/v1/me', async (route) => {
      const response = await route.fetch()
      const body = await response.json()
      body.capabilities = ['ask_question', 'manage_knowledge']
      await route.fulfill({ response, json: body })
    })
    await page.route('**/knowledge/proposals', (route) => route.fulfill({
      json: [{ id: 'x1', question: 'Gated question', logical_sql: 'SELECT 1', nearest_approved: null }],
    }))
    await page.goto('/')
    await expect(page.getByRole('button', { name: 'Row policies', exact: true })).toHaveCount(0)
    await page.getByRole('button', { name: 'Knowledge', exact: true }).click()
    await page.getByRole('tab', { name: /Proposed pairs/ }).click()
    await expect(page.getByText('need the approve_pairs capability')).toBeVisible()
    await expect(page.getByRole('button', { name: /^Approve:/ }).first()).toBeDisabled()
  })
})
