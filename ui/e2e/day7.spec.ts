import { expect, test } from '@playwright/test'

test('a conversation is saved, listed after a reload, and reopens with a way to show its results', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Ask a question about your data').fill('List all customers')
  await page.getByRole('button', { name: 'Send' }).click()
  await expect(page.getByLabel('Question and answer')).toContainText('List all customers')

  // The list comes from the server, so it survives a reload.
  await page.reload()
  const saved = page.getByRole('button', { name: 'List all customers' })
  await expect(saved).toBeVisible()
  await saved.click()

  // Rows are not stored; reopening recomputes them, so the chat looks as it did when asked.
  await expect(page.getByRole('cell', { name: 'Ada' })).toBeVisible()
  await expect(page.getByText('Two customers.')).toBeVisible()
  await expect(page.getByText('Computed for you')).toBeVisible()
  await expect(page.getByText('TODAY')).toBeVisible()
})

test('New thread clears the conversation but keeps the saved list', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Ask a question about your data').fill('Another question')
  await page.getByRole('button', { name: 'Send' }).click()
  await expect(page.getByRole('button', { name: 'Another question' })).toBeVisible()
  await page.getByRole('button', { name: /New thread/ }).click()
  await expect(page.getByRole('heading', { name: 'What would you like to know?' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Another question' })).toBeVisible()
})

test('a reopened chat shows the saved Steps card and the model chips from the database', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Ask a question about your data').fill('Steps please')
  await page.getByRole('button', { name: 'Send' }).click()
  await page.reload()
  await page.getByRole('button', { name: 'Steps please' }).first().click()
  const steps = page.getByLabel('Question execution steps')
  await expect(steps).toContainText('3 steps · 0.8 s')
  await expect(steps).toContainText('Retrieval')
  await expect(page.getByText('customers · model v3')).toBeVisible()
  // The Steps card folds away and back.
  await steps.getByRole('button', { name: /Steps/ }).click()
  await expect(steps.getByRole('listitem')).toHaveCount(0)
  await steps.getByRole('button', { name: /Steps/ }).click()
  await expect(steps.getByRole('listitem').first()).toBeVisible()
})

test('an account that may see SQL can edit and re-run it from the SQL card', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Ask a question about your data').fill('Edit my sql')
  await page.getByRole('button', { name: 'Send' }).click()
  await page.reload()
  await page.getByRole('button', { name: 'Edit my sql' }).first().click()
  await expect(page.getByRole('cell', { name: 'Ada' })).toBeVisible()
  await page.getByRole('button', { name: 'Edit', exact: true }).click()
  await page.getByLabel('Edit SQL').fill('SELECT customer_id, first_name FROM customers LIMIT 1')
  await expect(page.getByText('validated and policy-checked again')).toBeVisible()
  await page.getByRole('button', { name: 'Re-run' }).click()
  await expect(page.getByRole('cell', { name: 'Rerun person' })).toBeVisible()
})

test('pin and schedule turn an answer into a saved question first', async ({ page }) => {
  const calls: string[] = []
  await page.route('**/api/v1/projects/*/saved-questions', (route) => {
    calls.push('saved-question')
    return route.fulfill({ status: 201, json: { id: 'q-1' } })
  })
  await page.route('**/api/v1/dashboards**', async (route) => {
    const request = route.request()
    calls.push(`${request.method()} ${new URL(request.url()).pathname}`)
    if (request.method() === 'GET' && request.url().includes('project_id')) {
      return route.fulfill({ json: [{ id: 'd-1', title: 'My dashboard' }] })
    }
    if (request.method() === 'GET') return route.fulfill({ json: { id: 'd-1', tiles: [] } })
    return route.fulfill({ json: [] })
  })
  await page.route('**/api/v1/scheduled-questions', (route) => {
    calls.push(`schedule ${(route.request().postDataJSON() as { schedule: string }).schedule}`)
    return route.fulfill({ status: 201, json: { id: 'job-1' } })
  })
  await page.goto('/')
  await page.getByLabel('Ask a question about your data').fill('Pin me')
  await page.getByRole('button', { name: 'Send' }).click()
  await page.reload()
  await page.getByRole('button', { name: 'Pin me' }).first().click()
  await page.getByRole('button', { name: 'Pin to dashboard' }).click()
  await expect(page.getByText('Pinned to "My dashboard".')).toBeVisible()
  await page.getByRole('button', { name: 'Schedule this question' }).click()
  await page.getByRole('button', { name: 'daily' }).click()
  await expect(page.getByText(/Scheduled daily/)).toBeVisible()
  expect(calls).toEqual(expect.arrayContaining(['saved-question', 'PUT /api/v1/dashboards/d-1/tiles', 'schedule daily']))
})

test('Logical SQL and Compiled SQL are tabs with Edit, Re-run and Copy beside them', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Ask a question about your data').fill('Tab my sql')
  await page.getByRole('button', { name: 'Send' }).click()
  await page.reload()
  await page.getByRole('button', { name: 'Tab my sql' }).first().click()
  const card = page.getByRole('region', { name: 'SQL' })
  await expect(card.getByRole('tab', { name: 'Logical SQL' })).toHaveAttribute('aria-selected', 'true')
  await expect(card).toContainText('SELECT customer_id')
  for (const name of ['Edit', 'Re-run', 'Copy logical SQL']) {
    await expect(card.getByRole('button', { name })).toBeVisible()
  }
  // The compiled form arrives with the recomputed results.
  await card.getByRole('tab', { name: 'Compiled SQL' }).click()
  await expect(card).toContainText('WITH m_customers AS')
  // The compiled tab is read-only: only Copy is offered there.
  await expect(card.getByRole('button', { name: 'Copy compiled SQL' })).toBeVisible()
  await expect(card.getByRole('button', { name: 'Edit', exact: true })).toHaveCount(0)
  await expect(card.getByRole('button', { name: 'Re-run' })).toHaveCount(0)
  // Back on the logical tab, Edit opens the editor.
  await card.getByRole('tab', { name: 'Logical SQL' }).click()
  await card.getByRole('button', { name: 'Edit', exact: true }).click()
  await expect(card.getByLabel('Edit SQL')).toHaveValue(/SELECT customer_id/)
})
