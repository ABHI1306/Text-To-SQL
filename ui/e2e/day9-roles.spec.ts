/*
 * Day 9 (D9-1): end-to-end flows per role, against the UI's mock API (ui/mock-api.mjs).
 *
 * Personas (the mock picks one from the username typed into the real login form; the access token
 * is 'mock-token:<username>', so every later request is answered for that person):
 *   demo.dba   DBA, every capability, sees all rows
 *   maria      User, region EU
 *   jonas      custom role 'Analyst+' (dashboards, schedules, share; NO view_shared), region US
 *   sam.ldap   User from the directory (auth_source ldap), region North
 *   oidc.omar  User from single sign-on (auth_source oidc, no password), region South
 *
 * D9-1 items, as used in the comments below:
 *   [login]  sign in through the real login form (or the SSO link for OIDC)
 *   [ask]    ask a question and read the answer
 *   [dash]   dashboards: create, pin, rename, refresh, delete, tiles
 *   [sched]  scheduled questions: create, run now, pause/resume, run history
 *   [share]  sharing and "Shared with me" (read-only for the grantee)
 *   [audit]  audit browser: paging, filter, detail, CSV export
 *   [gate]   capability gating: navigation entries and the API's 403s
 *   [rls]    row-level isolation: each person only ever sees their own region's rows
 *   [ops]    Operations: tokens and GPU time per question, scheduler health (ok and degraded)
 *
 * Coverage by test:
 *   DBA     "signs in and sees every area"            [login] [gate] [share]
 *           "bell shows the failed scheduled run"     [sched]
 *           "asks and sees every region"              [ask] [rls]
 *           "manages dashboards"                      [dash] [rls]
 *           "schedules, runs now, pauses and resumes" [sched] [rls]
 *           "shares a dashboard"                      [share]
 *           "browses the audit log"                   [audit]
 *           "Operations shows tokens and GPU time"    [ops] [ask]
 *           "Operations shows a degraded scheduler"   [ops]
 *   User    "wrong password, then sign in"            [login]
 *           "has no work or system areas"             [gate]
 *           "asks and only sees EU rows"              [ask] [rls] [gate]
 *           "opens a shared dashboard read-only"      [share] [dash] [rls] [gate]
 *           "the API refuses..."                      [gate] [audit] [ops] [sched] [dash]
 *   Custom  "has work areas but no Shared with me"    [gate] [share] [audit] [ops]
 *           "asks and only sees US rows"              [ask] [rls]
 *           "pins an answer to a dashboard"           [dash] [rls]
 *           "schedules an answer"                     [sched] [rls]
 *           "shares a conversation"                   [share]
 *   LDAP    "directory sign-in"                       [login] [gate]
 *           "asks and only sees North rows"           [ask] [rls]
 *           "reads shared items computed for them"    [share] [rls]
 *   OIDC    "has no password sign-in, only SSO"       [login]
 *           "SSO lands signed in as a User"           [login] [gate]
 *           "asks and only sees South rows"           [ask] [rls]
 *           "reads a role-shared dashboard"           [share] [dash] [rls]
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const MOCK = `http://127.0.0.1:${process.env.VITE_MOCK_API_PORT ?? 4010}`
const PASSWORD = 'local-demo-password'
const ALL_NAMES = ['Ada', 'Grace', 'Linus', 'Hedy', 'Alan', 'Edsger']

// Tests in this file share the mock's in-memory state, so they run in order in one worker.
test.describe.configure({ mode: 'default' })

async function resetMock(request: APIRequestContext) {
  const response = await request.post(`${MOCK}/__mock/reset`, { data: { seed: true } })
  expect(response.ok()).toBeTruthy()
}

test.beforeEach(async ({ request }) => resetMock(request))
test.afterAll(async ({ request }) => resetMock(request))

const bearer = (username: string) => ({ authorization: `Bearer mock-token:${username}` })
const mainNav = (page: Page) => page.getByRole('navigation', { name: 'Main navigation' })
const navButton = (page: Page, name: string) => mainNav(page).getByRole('button', { name, exact: true })

/** Opens the app with no session, so the real login form is shown; allowSession() lets reloads restore it. */
async function openLogin(page: Page) {
  let session = false
  await page.route('**/api/v1/auth/refresh', (route) => (session ? route.fallback() : route.fulfill({ status: 401 })))
  await page.goto('/')
  return { allowSession: () => { session = true } }
}

async function submitLogin(page: Page, username: string, password = PASSWORD) {
  await page.getByLabel('Username').fill(username)
  await page.getByLabel('Password').fill(password)
  await page.getByRole('button', { name: 'Sign in' }).click()
}

async function signIn(page: Page, username: string, role: string) {
  const gate = await openLogin(page)
  await expect(page.getByLabel('Username')).toBeVisible() // the form only shows once refresh said 401
  gate.allowSession()
  await submitLogin(page, username)
  await expect(page.locator('.profile-button')).toContainText(username)
  await expect(page.locator('.profile-button')).toContainText(role)
}

async function expectNav(page: Page, shown: string[], hidden: string[]) {
  for (const name of shown) await expect(navButton(page, name)).toBeVisible()
  for (const name of hidden) await expect(navButton(page, name)).toHaveCount(0)
}

async function ask(page: Page, question: string) {
  await page.getByLabel('Ask a question about your data').fill(question)
  await page.getByRole('button', { name: 'Send' }).click()
  await expect(page.getByText('Computed for you')).toBeVisible()
}

/** Asserts which customer names a region of the page shows, and that every other name is absent. */
async function expectOnlyRows(scope: ReturnType<Page['locator']>, names: string[]) {
  for (const name of names) await expect(scope.getByRole('cell', { name, exact: true })).toBeVisible()
  for (const name of ALL_NAMES.filter((item) => !names.includes(item))) {
    await expect(scope.getByRole('cell', { name, exact: true })).toHaveCount(0)
  }
}

const sharedItem = (page: Page, title: string) =>
  page.getByRole('list', { name: 'Shared items' }).getByRole('listitem').filter({ hasText: title })

test.describe('DBA (demo.dba)', () => {
  test('signs in and sees every area', async ({ page }) => {
    await signIn(page, 'demo.dba', 'DBA')
    await expectNav(page, [
      'Home', 'Shared with me', 'Dashboards', 'Scheduled questions', 'Setup', 'Connect and model',
      'Drift and profiling', 'Knowledge', 'Row policies', 'Users and roles', 'Audit', 'Operations', 'Evaluation',
    ], [])
    await navButton(page, 'Shared with me').click()
    await expect(page.getByText('Nothing has been shared with you yet.')).toBeVisible()
  })

  test('the bell shows the failed scheduled run and marks it read', async ({ page }) => {
    await signIn(page, 'demo.dba', 'DBA')
    await page.getByRole('button', { name: 'Notifications, 1 unread' }).click()
    const panel = page.getByRole('dialog', { name: 'Notifications' })
    await expect(panel).toContainText(/Customers by region.*failed to run: Warehouse connection timed out/)
    await panel.getByRole('button', { name: 'Mark read' }).click()
    await expect(panel.getByRole('button', { name: 'Mark read' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Notifications', exact: true })).toBeVisible()
  })

  test('asks a question and sees the rows of every region', async ({ page }) => {
    await signIn(page, 'demo.dba', 'DBA')
    await ask(page, 'D9 dba customers by region')
    await expectOnlyRows(page.getByLabel('Question and answer'), ALL_NAMES)
  })

  test('manages dashboards: tiles, rename, refresh, create and delete', async ({ page }) => {
    await signIn(page, 'demo.dba', 'DBA')
    await navButton(page, 'Dashboards').click()
    const tabs = page.getByRole('tablist', { name: 'Your dashboards' })
    await expect(tabs.getByRole('tab', { name: 'Regional sales', exact: true })).toHaveAttribute('aria-selected', 'true')
    const grid = page.locator('.tile-grid')
    await expect(grid.getByText('Customers by region')).toBeVisible()
    await expectOnlyRows(grid, ALL_NAMES)

    page.once('dialog', (dialog) => void dialog.accept('Regional sales v2'))
    await page.getByRole('button', { name: 'Rename', exact: true }).click()
    await expect(tabs.getByRole('tab', { name: 'Regional sales v2', exact: true })).toBeVisible()

    await page.getByRole('button', { name: 'Refresh', exact: true }).click()
    await expect(page.getByText('Refreshed 1 tile.')).toBeVisible()

    await page.getByLabel('New dashboard title').fill('Scratch')
    await page.getByRole('button', { name: 'Create dashboard' }).click()
    await expect(tabs.getByRole('tab', { name: 'Scratch', exact: true })).toHaveAttribute('aria-selected', 'true')
    await expect(page.getByText('No tiles yet.')).toBeVisible()

    page.once('dialog', (dialog) => void dialog.accept())
    await page.getByRole('button', { name: 'Delete', exact: true }).click()
    await expect(tabs.getByRole('tab', { name: 'Scratch', exact: true })).toHaveCount(0)
    await expect(tabs.getByRole('tab', { name: 'Regional sales v2', exact: true })).toBeVisible()
  })

  test('schedules a question, runs it now, pauses and resumes it', async ({ page }) => {
    await signIn(page, 'demo.dba', 'DBA')
    await navButton(page, 'Scheduled questions').click()
    await expect(page.getByText('Nothing is scheduled.')).toBeVisible()
    await page.getByLabel('Question to schedule').selectOption({ label: 'Customers by region' })
    await page.getByRole('button', { name: 'Schedule', exact: true }).click()

    const card = page.getByRole('list', { name: 'Schedules' }).getByRole('listitem').first()
    await expect(card).toContainText('Customers by region')
    await expect(card.getByText('Active', { exact: true })).toBeVisible()
    await expect(card.getByText('daily', { exact: true })).toBeVisible()

    await card.getByRole('button', { name: 'Run now' }).click()
    await expect(card.getByText('Ran now. See the history below.')).toBeVisible()
    await card.getByRole('button', { name: 'Run history' }).click()
    const history = card.getByRole('table', { name: 'Run history' })
    await expect(history.getByRole('cell', { name: 'Run now' })).toBeVisible()
    await expect(history.getByRole('cell', { name: 'Succeeded' })).toBeVisible()
    await expect(history.getByRole('cell', { name: '6', exact: true })).toBeVisible() // the DBA's rows: all six

    await card.getByRole('button', { name: 'Pause' }).click()
    await expect(card.getByText('Paused', { exact: true })).toBeVisible()
    await expect(card.getByText('Paused: user paused')).toBeVisible()
    await card.getByRole('button', { name: 'Resume' }).click()
    await expect(card.getByText('Active', { exact: true })).toBeVisible()
  })

  test('shares a dashboard with a person, then revokes the role grant', async ({ page }) => {
    await signIn(page, 'demo.dba', 'DBA')
    await navButton(page, 'Dashboards').click()
    await page.getByRole('button', { name: 'Share', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: 'Share Regional sales' })
    const current = dialog.getByRole('list', { name: 'Current shares' })
    await expect(current.getByRole('listitem')).toHaveCount(1) // seeded: the User role

    await dialog.getByLabel('Who to share with').selectOption({ label: 'maria (User)' })
    await dialog.getByRole('button', { name: 'Share read-only' }).click()
    await expect(current).toContainText('maria')

    // sharing twice is refused with the server's reason
    await dialog.getByLabel('Who to share with').selectOption({ label: 'maria (User)' })
    await dialog.getByRole('button', { name: 'Share read-only' }).click()
    await expect(dialog.getByRole('alert')).toContainText('Already shared with them')

    await current.getByRole('listitem').filter({ hasText: 'Role' }).getByRole('button', { name: 'Revoke' }).click()
    await expect(current.getByRole('listitem')).toHaveCount(1)
    await expect(current).toContainText('maria')
  })

  test('browses the audit log: paging, filter, detail and CSV export', async ({ page }) => {
    await signIn(page, 'demo.dba', 'DBA')
    await navButton(page, 'Audit').click()
    await expect(page.getByText('50 events loaded · more available')).toBeVisible()
    await page.getByRole('button', { name: 'Load more' }).click()
    await expect(page.getByText('100 events loaded · more available')).toBeVisible()
    await page.getByRole('button', { name: 'Load more' }).click()
    await expect(page.getByText('120 events loaded')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Load more' })).toHaveCount(0)

    await page.getByLabel('Action', { exact: true }).fill('login_failed')
    await page.getByRole('button', { name: 'Apply' }).click()
    await expect(page.getByText('12 events loaded')).toBeVisible()

    await page.getByRole('row').nth(1).click() // row 0 is the header
    const detail = page.getByRole('dialog', { name: 'Event detail' })
    await expect(detail).toContainText('login_failed')
    await detail.getByRole('button', { name: 'Close detail' }).click()
    await expect(detail).toHaveCount(0)

    const download = page.waitForEvent('download')
    await page.getByRole('button', { name: 'Export CSV' }).click()
    expect((await download).suggestedFilename()).toBe('audit-events.csv')
  })

  test('Operations shows tokens and GPU time per question and a healthy scheduler', async ({ page }) => {
    await signIn(page, 'demo.dba', 'DBA')
    await navButton(page, 'Operations').click()
    await expect(page.getByText('No questions answered since the last restart.')).toBeVisible()
    await expect(page.getByText('The scheduler is keeping up.', { exact: true })).toBeVisible()

    await navButton(page, 'Home').click()
    await ask(page, 'D9 usage question')
    await navButton(page, 'Operations').click()
    const totals = page.getByLabel('Model usage totals')
    await expect(totals).toContainText('900 / 300')
    await expect(totals).toContainText('4.3 s')
    const recent = page.getByRole('table', { name: 'Recent questions' })
    const latest = recent.getByRole('row').filter({ hasText: 'Most recent' })
    await expect(latest.getByRole('cell', { name: '1,200' })).toBeVisible() // tokens
    await expect(latest.getByRole('cell', { name: '4.3 s' })).toBeVisible() // GPU time
    await expect(page.getByLabel('Scheduler counts')).toContainText('Overdue0')
  })

  test('Operations shows a banner when the scheduler is degraded', async ({ page, request }) => {
    await signIn(page, 'demo.dba', 'DBA')
    await request.post(`${MOCK}/__mock/scheduler`, { data: { status: 'degraded' } })
    await navButton(page, 'Operations').click()
    await expect(page.getByText('The scheduler is behind: 3 overdue and 1 stalled.')).toBeVisible()
    const counts = page.getByLabel('Scheduler counts')
    await expect(counts).toContainText('Overdue3')
    await expect(counts).toContainText('Stalled1')
    await expect(counts).toContainText('Failed (24 h)2')
  })
})

test.describe('User (maria, EU)', () => {
  test('a wrong password is refused, then sign in works', async ({ page }) => {
    const gate = await openLogin(page)
    await submitLogin(page, 'maria', 'wrong-password')
    await expect(page.getByRole('alert')).toContainText('Invalid username or password')
    gate.allowSession()
    await page.getByLabel('Password').fill(PASSWORD)
    await page.getByRole('button', { name: 'Sign in' }).click()
    await expect(page.locator('.profile-button')).toContainText('maria')
  })

  test('has no work or system areas', async ({ page }) => {
    await signIn(page, 'maria', 'User')
    await expectNav(page, ['Home', 'Shared with me'], [
      'Dashboards', 'Scheduled questions', 'Setup', 'Connect and model', 'Drift and profiling', 'Knowledge',
      'Row policies', 'Users and roles', 'Audit', 'Operations', 'Evaluation',
    ])
    await expect(page.getByRole('button', { name: 'Notifications', exact: true })).toBeVisible() // nothing unread
  })

  test('asks a question and only sees EU rows, with no pin, schedule or share', async ({ page }) => {
    await signIn(page, 'maria', 'User')
    await ask(page, 'D9 maria customers by region')
    await expectOnlyRows(page.getByLabel('Question and answer'), ['Ada', 'Hedy'])
    for (const name of ['Pin to dashboard', 'Schedule this question', 'Share']) {
      await expect(page.getByRole('button', { name, exact: true })).toHaveCount(0)
    }
  })

  test('opens a shared dashboard read-only, computed for her', async ({ page }) => {
    await signIn(page, 'maria', 'User')
    await navButton(page, 'Shared with me').click()
    const item = sharedItem(page, 'Regional sales')
    await expect(item).toContainText('Dashboard')
    await expect(page.getByRole('list', { name: 'Shared items' })).not.toContainText('Orders this week')
    await item.getByRole('button', { name: 'Open' }).click()
    await expect(item.getByText('Customers by region')).toBeVisible()
    await expectOnlyRows(item, ['Ada', 'Hedy'])
    for (const name of ['Rename', 'Delete', 'Refresh', 'Share']) {
      await expect(page.getByRole('button', { name, exact: true })).toHaveCount(0)
    }
  })

  test('the API refuses what the screens hide', async ({ page, request }) => {
    await signIn(page, 'maria', 'User')
    const headers = bearer('maria')
    for (const path of ['/api/v1/ops/usage', '/api/v1/ops/scheduler', '/api/v1/audit-events', '/api/v1/scheduled-questions', '/api/v1/share-targets']) {
      expect((await request.get(path, { headers })).status(), path).toBe(403)
    }
    expect((await request.post('/api/v1/dashboards', { headers, data: { project_id: 'p', title: 'Nope' } })).status()).toBe(403)
    // a shared dashboard cannot be changed by the person it is shared with
    const shared = (await (await request.get('/api/v1/shared-with-me', { headers })).json()) as { resource_id: string }[]
    expect(shared.length).toBeGreaterThan(0)
    const id = shared[0].resource_id
    expect((await request.patch(`/api/v1/dashboards/${id}`, { headers, data: { title: 'Mine now' } })).status()).toBe(403)
    expect((await request.delete(`/api/v1/dashboards/${id}`, { headers })).status()).toBe(403)
  })
})

test.describe("Custom role (jonas, 'Analyst+' without view_shared, US)", () => {
  test('has work areas but no Shared with me, Audit or Operations', async ({ page, request }) => {
    await signIn(page, 'jonas', 'Analyst+')
    await expectNav(page, ['Home', 'Dashboards', 'Scheduled questions'], [
      'Shared with me', 'Setup', 'Knowledge', 'Users and roles', 'Audit', 'Operations', 'Evaluation',
    ])
    const headers = bearer('jonas')
    for (const path of ['/api/v1/shared-with-me', '/api/v1/ops/usage', '/api/v1/audit-events']) {
      expect((await request.get(path, { headers })).status(), path).toBe(403)
    }
  })

  test('asks a question and only sees US rows, with pin, schedule and share offered', async ({ page }) => {
    await signIn(page, 'jonas', 'Analyst+')
    await ask(page, 'D9 jonas customers by region')
    await expectOnlyRows(page.getByLabel('Question and answer'), ['Alan'])
    for (const name of ['Pin to dashboard', 'Schedule this question', 'Share']) {
      await expect(page.getByRole('button', { name, exact: true })).toBeVisible()
    }
  })

  test('pins an answer to a new dashboard; the tile shows only his rows', async ({ page }) => {
    await signIn(page, 'jonas', 'Analyst+')
    await navButton(page, 'Dashboards').click()
    await expect(page.getByText('No dashboards yet.')).toBeVisible() // the DBA's dashboard is not his
    await navButton(page, 'Home').click()
    await ask(page, 'D9 jonas pinned question')
    await page.getByRole('button', { name: 'Pin to dashboard' }).click()
    await expect(page.getByText('Pinned to "My dashboard".')).toBeVisible()

    await navButton(page, 'Dashboards').click()
    await expect(page.getByRole('tablist', { name: 'Your dashboards' }).getByRole('tab', { name: 'My dashboard' })).toBeVisible()
    const grid = page.locator('.tile-grid')
    await expect(grid.getByText('D9 jonas pinned question')).toBeVisible()
    await expectOnlyRows(grid, ['Alan'])
  })

  test('schedules an answer, runs it now and the run only counts his rows', async ({ page }) => {
    await signIn(page, 'jonas', 'Analyst+')
    await ask(page, 'D9 jonas scheduled question')
    await page.getByRole('button', { name: 'Schedule this question' }).click()
    await page.getByRole('button', { name: 'daily', exact: true }).click()
    await expect(page.getByText(/Scheduled daily/)).toBeVisible()

    await navButton(page, 'Scheduled questions').click()
    const card = page.getByRole('list', { name: 'Schedules' }).getByRole('listitem')
    await expect(card).toHaveCount(1) // only his own schedule
    await expect(card).toContainText('D9 jonas scheduled question')
    await card.getByRole('button', { name: 'Run now' }).click()
    await expect(card.getByText('Ran now. See the history below.')).toBeVisible()
    await card.getByRole('button', { name: 'Run history' }).click()
    await expect(card.getByRole('table', { name: 'Run history' }).getByRole('cell', { name: '1', exact: true })).toBeVisible()
  })

  test('shares a conversation read-only with a person and revokes it', async ({ page }) => {
    await signIn(page, 'jonas', 'Analyst+')
    await ask(page, 'D9 jonas shared question')
    await page.getByRole('button', { name: 'Share', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: 'Share D9 jonas shared question' })
    await expect(dialog.getByText('Not shared with anyone yet.')).toBeVisible()
    // he cannot share with himself, but can with others and with roles
    await expect(dialog.getByLabel('Who to share with').locator('option', { hasText: 'jonas' })).toHaveCount(0)
    await dialog.getByLabel('Who to share with').selectOption({ label: 'maria (User)' })
    await dialog.getByRole('button', { name: 'Share read-only' }).click()
    await expect(dialog.getByRole('list', { name: 'Current shares' })).toContainText('maria')
    await dialog.getByRole('button', { name: 'Revoke' }).click()
    await expect(dialog.getByText('Not shared with anyone yet.')).toBeVisible()
  })
})

test.describe('LDAP user (sam.ldap, North)', () => {
  test('directory sign-in: the form says so and the account is a User', async ({ page, request }) => {
    const gate = await openLogin(page)
    await expect(page.getByText('Directory accounts sign in with their directory username and password.')).toBeVisible()
    gate.allowSession()
    await submitLogin(page, 'sam.ldap')
    await expect(page.locator('.profile-button')).toContainText('sam.ldap')
    await expect(page.locator('.profile-button')).toContainText('User')
    await expectNav(page, ['Home', 'Shared with me'], ['Dashboards', 'Scheduled questions', 'Audit', 'Operations', 'Users and roles'])
    const me = (await (await request.get('/api/v1/me', { headers: bearer('sam.ldap') })).json()) as { auth_source: string }
    expect(me.auth_source).toBe('ldap')
  })

  test('asks a question and only sees North rows', async ({ page }) => {
    await signIn(page, 'sam.ldap', 'User')
    await ask(page, 'D9 sam customers by region')
    await expectOnlyRows(page.getByLabel('Question and answer'), ['Linus'])
  })

  test('reads shared items, each computed with his own access', async ({ page }) => {
    await signIn(page, 'sam.ldap', 'User')
    await navButton(page, 'Shared with me').click()
    const dashboard = sharedItem(page, 'Regional sales')
    const question = sharedItem(page, 'Orders this week')
    await expect(question).toContainText('Saved question')
    await dashboard.getByRole('button', { name: 'Open' }).click()
    await expectOnlyRows(dashboard, ['Linus'])
    // jonas owns this question and his own run shows US rows; sam gets North rows
    await question.getByRole('button', { name: 'Open' }).click()
    await expectOnlyRows(question, ['Linus'])
  })
})

test.describe('OIDC user (oidc.omar, South)', () => {
  async function signInWithSso(page: Page) {
    const gate = await openLogin(page)
    const link = page.getByRole('link', { name: 'Sign in with single sign-on' })
    await expect(link).toBeVisible()
    gate.allowSession()
    await link.click()
    await expect(page.locator('.profile-button')).toContainText('oidc.omar')
  }

  test('has no password sign-in, only SSO', async ({ page }) => {
    await openLogin(page)
    await submitLogin(page, 'oidc.omar')
    await expect(page.getByRole('alert')).toContainText('Invalid username or password')
    await expect(page.getByRole('link', { name: 'Sign in with single sign-on' })).toBeVisible()
  })

  test('single sign-on lands signed in as a User with no work or system areas', async ({ page, request }) => {
    await signInWithSso(page)
    await expect(page.locator('.profile-button')).toContainText('User')
    await expectNav(page, ['Home', 'Shared with me'], ['Dashboards', 'Scheduled questions', 'Audit', 'Operations', 'Users and roles'])
    const me = (await (await request.get('/api/v1/me', { headers: bearer('oidc.omar') })).json()) as { auth_source: string }
    expect(me.auth_source).toBe('oidc')
    // the session survives a reload (the refresh cookie carries the persona)
    await page.reload()
    await expect(page.locator('.profile-button')).toContainText('oidc.omar')
  })

  test('asks a question and only sees South rows', async ({ page }) => {
    await signInWithSso(page)
    await ask(page, 'D9 omar customers by region')
    await expectOnlyRows(page.getByLabel('Question and answer'), ['Grace', 'Edsger'])
  })

  test('reads a dashboard shared with the User role, with South rows only', async ({ page }) => {
    await signInWithSso(page)
    await navButton(page, 'Shared with me').click()
    await expect(page.getByRole('list', { name: 'Shared items' })).not.toContainText('Orders this week')
    const dashboard = sharedItem(page, 'Regional sales')
    await dashboard.getByRole('button', { name: 'Open' }).click()
    await expectOnlyRows(dashboard, ['Grace', 'Edsger'])
  })
})
