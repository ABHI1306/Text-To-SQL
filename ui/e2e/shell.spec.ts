import { expect, test } from '@playwright/test'

test('shows the chat workspace and the signed-in account', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByText('Amazatic', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: /demo\.dba/ })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'What would you like to know?' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'New thread' })).toBeVisible()
  await expect(page.locator('.data-card')).toHaveCount(0)
})

test('has no design-review toolbar and fills the window like a real application', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 700 })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'What would you like to know?' })).toBeVisible()
  await expect(page.getByText('Design review')).toHaveCount(0)
  await expect(page.getByLabel('Review screen')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Session-expired banner' })).toHaveCount(0)
  const shell = await page.locator('.app-shell').boundingBox()
  expect(shell?.y).toBe(0)
  expect(Math.round(shell?.height ?? 0)).toBe(700)
  // The composer stays on screen; only the conversation scrolls.
  await expect(page.locator('.composer-footer')).toBeInViewport()
  expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBeLessThanOrEqual(700)
})

test.describe('theme', () => {
  test('the account menu offers System, Light and Dark and remembers the choice', async ({ page }) => {
    await page.emulateMedia({ colorScheme: 'light' })
    await page.goto('/')
    const html = page.locator('html')
    await expect(html).toHaveAttribute('data-theme', 'light')

    await page.getByRole('button', { name: /demo\.dba/ }).click()
    const group = page.getByRole('group', { name: 'Theme' })
    await expect(group.getByRole('menuitemradio', { name: 'System' })).toHaveAttribute('aria-checked', 'true')
    await group.getByRole('menuitemradio', { name: 'Dark' }).click()
    await expect(html).toHaveAttribute('data-theme', 'dark')
    await expect(page.locator('.app-shell')).toHaveCSS('background-color', 'rgb(23, 26, 35)')

    await page.reload()
    await expect(html).toHaveAttribute('data-theme', 'dark')
    await page.getByRole('button', { name: /demo\.dba/ }).click()
    await expect(page.getByRole('menuitemradio', { name: 'Dark' })).toHaveAttribute('aria-checked', 'true')
  })

  test('a button in the top bar switches between dark and light on every screen', async ({ page }) => {
    await page.emulateMedia({ colorScheme: 'dark' })
    await page.goto('/')
    const html = page.locator('html')
    await expect(html).toHaveAttribute('data-theme', 'dark')
    await page.getByRole('button', { name: 'Switch to light theme' }).click()
    await expect(html).toHaveAttribute('data-theme', 'light')
    await page.getByRole('button', { name: 'Switch to dark theme' }).click()
    await expect(html).toHaveAttribute('data-theme', 'dark')
    await page.getByRole('button', { name: 'Switch to light theme' }).click()

    // The same button is on the other screens, and the choice survives a reload.
    await page.getByRole('button', { name: 'Audit', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Switch to dark theme' })).toBeVisible()
    await page.reload()
    await expect(html).toHaveAttribute('data-theme', 'light')
  })

  test('System follows the operating system, live', async ({ page }) => {
    await page.emulateMedia({ colorScheme: 'dark' })
    await page.goto('/')
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
    await page.emulateMedia({ colorScheme: 'light' })
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  })

  test('an explicit Light choice wins over a dark system setting', async ({ page }) => {
    await page.emulateMedia({ colorScheme: 'dark' })
    await page.goto('/')
    await page.getByRole('button', { name: /demo\.dba/ }).click()
    await page.getByRole('menuitemradio', { name: 'Light' }).click()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
    await page.reload()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  })

  test('other screens are readable in dark: no light cards under light text', async ({ page }) => {
    await page.emulateMedia({ colorScheme: 'dark' })
    await page.goto('/')
    await page.getByRole('button', { name: 'Audit', exact: true }).click()
    const form = page.getByRole('form', { name: 'Audit filters' })
    await expect(form).toBeVisible()
    const [background, color] = await form.evaluate((node) => {
      const style = getComputedStyle(node)
      return [style.backgroundColor, style.color]
    })
    const brightness = (css: string) => css.match(/\d+/g)!.slice(0, 3).map(Number).reduce((a, b) => a + b, 0) / 3
    expect(brightness(background)).toBeLessThan(80)
    expect(brightness(color)).toBeGreaterThan(150)
  })
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
  await expect(page.getByRole('heading', { name: 'Select tables to create data models' })).toBeVisible()
  await page.getByLabel('Search tables').fill('customers')
  await page.getByRole('button', { name: 'Search', exact: true }).click()
  await page.getByLabel('ecommerce.customers').check()
  await page.getByRole('button', { name: 'Continue with 1 table' }).click()
  await expect(page.getByText('Draft created. Review descriptions')).toBeVisible()
  await page.getByRole('button', { name: 'Validate and save model' }).click()

  // Saving the draft opens the model editor without any further navigation.
  await expect(page.getByRole('heading', { name: 'Connect and model' })).toBeVisible()
  await expect(page.getByRole('region', { name: 'Model relationship canvas' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Save model' })).toBeDisabled()
  await page.getByRole('tab', { name: 'Deploy report' }).click()
  await expect(page.getByRole('region', { name: 'Deploy report' })).toContainText('models described')
  await page.getByRole('button', { name: 'Deploy model' }).click()
  await expect(page.getByRole('status')).toContainText('is deployed and ready for questions')
  await page.getByRole('button', { name: 'Home', exact: true }).click()
  await page.getByLabel('Ask a question about your data').fill('Show customer names')
  await page.getByRole('button', { name: 'Send' }).click()
  await expect(page.getByText('Computed for you')).toBeVisible()
  await expect(page.getByRole('columnheader', { name: 'first_name' })).toBeVisible()
  await expect(page.getByRole('cell', { name: 'Mock customer' })).toBeVisible()
})
