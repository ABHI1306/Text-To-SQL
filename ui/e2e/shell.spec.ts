import { expect, test } from '@playwright/test'

test('shows the reference chat workspace and demo account', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByText('Design review', { exact: true })).toBeVisible()
  await expect(page.getByText('Amazatic', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: /demo\.dba/ })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'What would you like to know?' })).toBeVisible()
  await expect(page.getByText('Illustrative sample')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'New thread' })).toBeVisible()
  await expect(page.locator('.data-card')).toHaveCount(0)
})

test('design review controls switch the screen and preview settings', async ({ page }) => {
  await page.goto('/')

  const frame = page.locator('.app-frame')
  await page.getByLabel('Review screen').selectOption('Dashboards')
  await expect(page.getByRole('heading', { name: 'Dashboards' })).toBeVisible()

  await page.getByLabel('Review screen').selectOption('Home')
  await page.getByRole('button', { name: 'Dark', exact: true }).click()
  await expect(frame).toHaveAttribute('data-review-theme', 'dark')
  await expect(page.locator('.review-header')).toHaveCSS('border-radius', '10px')
  await expect(page.locator('.app-shell')).toHaveCSS('border-radius', '12px')
  await expect(page.locator('.composer-area')).toHaveCSS('background-color', 'rgb(32, 36, 49)')
  expect(await page.evaluate(() => window.scrollY)).toBe(0)

  await page.getByRole('button', { name: 'Streaming', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('Preview state: Streaming')

  await page.getByRole('button', { name: 'Mobile 390' }).click()
  await expect(frame).toHaveClass(/mobile-preview/)
  await expect(frame).toHaveCSS('width', '390px')

  await page.getByRole('button', { name: 'Session-expired banner' }).click()
  await expect(page.getByRole('alert')).toContainText('Your session has expired')
})

test('separates the review toolbar and lets the page scroll to the app bottom', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 600 })
  await page.goto('/')

  const review = page.locator('.review-header')
  const shell = page.locator('.app-shell')
  const reviewBox = await review.boundingBox()
  const shellBox = await shell.boundingBox()
  expect(reviewBox).not.toBeNull()
  expect(shellBox).not.toBeNull()
  expect(shellBox!.y).toBeGreaterThan(reviewBox!.y + reviewBox!.height)

  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight))
  await expect(page.locator('.composer-footer')).toBeInViewport()
  expect(await page.evaluate(() => window.scrollY)).toBeGreaterThan(0)
})

test('login form appears when there is no refresh session', async ({ page }) => {
  await page.route('**/api/v1/auth/refresh', (route) => route.fulfill({ status: 401 }))
  await page.goto('/')
  await page.getByLabel('Username').fill('demo.dba')
  await page.getByLabel('Password').fill('local-demo-password')
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByText('Amazatic', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: /demo\.dba/ })).toBeVisible()
})

test('project setup profiles selected tables and saves the semantic model', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: 'Setup', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'E-commerce demo' })).toBeVisible()
  await page.getByLabel('Search tables').fill('customers')
  await page.getByRole('button', { name: 'Search', exact: true }).click()
  await page.getByLabel('ecommerce.customers').check()
  await page.getByRole('button', { name: 'Continue with 1 table' }).click()
  await expect(page.getByText('Draft created. Review descriptions')).toBeVisible()
  await page.getByRole('button', { name: 'Validate and save model' }).click()
  await expect(page.getByText('Semantic model revision 1 is ready for review.')).toBeVisible()

  await page.getByLabel('Review screen').selectOption('Connect and model')
  await expect(page.getByRole('heading', { name: 'Connect and model' })).toBeVisible()
  await expect(page.getByRole('region', { name: 'Model relationship canvas' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Save layout' })).toBeVisible()
  await page.getByRole('button', { name: 'Save layout' }).click()
  await expect(page.getByRole('status')).toContainText('Model layout saved.')
  await page.getByRole('button', { name: 'Deploy model' }).click()
  await expect(page.getByRole('status')).toContainText('is deployed and ready for questions')
  await page.getByRole('button', { name: 'Home', exact: true }).click()
  await page.getByLabel('Ask a question about your data').fill('Show customer names')
  await page.getByRole('button', { name: 'Send' }).click()
  await expect(page.getByText('Mock preview data')).toBeVisible()
  await expect(page.getByRole('columnheader', { name: 'first_name' })).toBeVisible()
  await expect(page.getByRole('cell', { name: 'Mock customer' })).toBeVisible()
})
