import { expect, test } from '@playwright/test'

test('shows the reference chat workspace and demo account', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByText('Design review', { exact: true })).toBeVisible()
  await expect(page.getByText('Amazatic', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: /demo\.dba/ })).toBeVisible()
  await expect(page.getByText('Net revenue by customer · Q3 2026')).toBeVisible()
  await expect(page.getByRole('button', { name: 'New thread' })).toBeVisible()
  await expect(page.getByRole('region', { name: 'Question execution steps' })).toBeVisible()
})

test('design review controls switch the screen and preview settings', async ({ page }) => {
  await page.goto('/')

  const frame = page.locator('.app-frame')
  await page.getByLabel('Review screen').selectOption('Dashboards')
  await expect(page.getByRole('heading', { name: 'Dashboards' })).toBeVisible()

  await page.getByLabel('Review screen').selectOption('Home')
  await page.getByRole('button', { name: 'Dark', exact: true }).click()
  await expect(frame).toHaveAttribute('data-review-theme', 'dark')

  await page.getByRole('button', { name: 'Streaming', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('Preview state: Streaming')

  await page.getByRole('button', { name: 'Mobile 390' }).click()
  await expect(frame).toHaveClass(/mobile-preview/)
  await expect(frame).toHaveCSS('width', '390px')

  await page.getByRole('button', { name: 'Session-expired banner' }).click()
  await expect(page.getByRole('alert')).toContainText('Your session has expired')
})
