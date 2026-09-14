import fs from 'node:fs'
import path from 'node:path'

import { startMockServer } from '../../../tests-js/scripts/mock-server'

import {
  buildAppEnv,
  createSandbox,
  launchDesktop,
  waitForAppReady,
  writeEnvFile,
  writeMockProviderConfig
} from './fixtures'
import { collectErrorBanners, type ElectronApplication, expect, type Page, test } from './test'

// Real Electron/backend restart, with synthetic profiles and no external
// provider. A named profile selected on the local registry source uses the
// legacy profile door and must survive without a forced profile launch flag.
// Returning to explicit local Default must also replace that saved selection.
// eslint-disable-next-line no-empty-pattern
test('restores named and Default selections on the same local gateway after restart', async ({}, testInfo) => {
  test.setTimeout(360_000)
  const sandbox = createSandbox('profile-startup')
  const mock = await startMockServer()
  let app: ElectronApplication | undefined
  let page: Page | undefined
  let closed = true

  try {
    for (const home of [sandbox.hermesHome, path.join(sandbox.hermesHome, 'profiles', 'full')]) {
      fs.mkdirSync(home, { recursive: true })
      writeMockProviderConfig(home, mock.url)
      writeEnvFile(home)
    }

    fs.writeFileSync(path.join(sandbox.userDataDir, 'active-profile.json'), JSON.stringify({ profile: 'default' }))
    fs.writeFileSync(path.join(sandbox.userDataDir, 'connections.json'), JSON.stringify({
      version: 2,
      primary: 'local',
      lastUsed: 'local',
      launchMode: 'last-used',
      connections: [{ id: 'local', kind: 'local', label: 'This device' }]
    }))

    const python = process.env.HERMES_E2E_PYTHON
    const env = buildAppEnv(sandbox, python ? { HERMES_DESKTOP_PYTHON: python } : {})
    const launch = () => launchDesktop(env, current => { app = current; closed = false })
    const first = await launch()
    page = first.page
    await waitForAppReady(first, 120_000)

    const full = first.page.locator('[data-slot="profile-rail"]')
      .getByRole('button', { name: 'full', exact: true })

    await expect(full).toBeVisible({ timeout: 60_000 })
    await full.click()
    await expect(full).toHaveAttribute('aria-pressed', 'true', { timeout: 90_000 })
    // The visible selection can precede the asynchronous startup preference.
    await expect.poll(() => JSON.parse(fs.readFileSync(
      path.join(sandbox.userDataDir, 'active-profile.json'), 'utf8'
    )).profile, { timeout: 10_000 }).toBe('full')
    await testInfo.attach('saved-profile-before-restart', {
      body: JSON.stringify({
        legacy: JSON.parse(fs.readFileSync(path.join(sandbox.userDataDir, 'active-profile.json'), 'utf8')).profile,
        perConnection: await first.page.evaluate(() =>
          JSON.parse(localStorage.getItem('hermes.desktop.lastProfileByConnection') ?? '{}')
        )
      }), contentType: 'application/json'
    })
    expect(await collectErrorBanners(first.page)).toEqual([])
    await testInfo.attach('before-restart', { body: await first.page.screenshot(), contentType: 'image/png' })
    await first.app.close()
    closed = true

    // Do not reseed storage or mutate either profile between launches.
    const second = await launch()
    page = second.page
    await waitForAppReady(second, 120_000)
    await expect(second.page.locator('[data-slot="profile-rail"]')
      .getByRole('button', { name: 'full', exact: true }))
      .toHaveAttribute('aria-pressed', 'true', { timeout: 60_000 })
    expect(await collectErrorBanners(second.page)).toEqual([])
    expect(second.app.windows()).toHaveLength(1)
    await testInfo.attach('after-restart', { body: await second.page.screenshot(), contentType: 'image/png' })

    // The inverse choice takes the explicit local registry door rather than
    // the named profile's legacy door. It must not leave Full saved for boot.
    const secondRail = second.page.locator('[data-slot="profile-rail"]')

    await secondRail.getByRole('button', { name: 'Switch to default', exact: true }).click()
    await expect(secondRail.getByRole('button', { name: 'Show all profiles', exact: true }))
      .toHaveAttribute('aria-pressed', 'true', { timeout: 90_000 })
    await expect.poll(() => JSON.parse(fs.readFileSync(
      path.join(sandbox.userDataDir, 'active-profile.json'), 'utf8'
    )).profile, { timeout: 10_000 }).toBe('default')
    await testInfo.attach('saved-default-before-restart', {
      body: JSON.stringify({ profile: JSON.parse(fs.readFileSync(
        path.join(sandbox.userDataDir, 'active-profile.json'), 'utf8'
      )).profile }), contentType: 'application/json'
    })
    expect(await collectErrorBanners(second.page)).toEqual([])
    await second.app.close()
    closed = true

    const third = await launch()
    page = third.page
    await waitForAppReady(third, 120_000)
    const thirdRail = third.page.locator('[data-slot="profile-rail"]')

    await expect(thirdRail.getByRole('button', { name: 'Show all profiles', exact: true }))
      .toHaveAttribute('aria-pressed', 'true', { timeout: 60_000 })
    await expect(thirdRail.getByRole('button', { name: 'full', exact: true }))
      .toHaveAttribute('aria-pressed', 'false')
    expect(await collectErrorBanners(third.page)).toEqual([])
    expect(third.app.windows()).toHaveLength(1)
    await testInfo.attach('after-default-restart', { body: await third.page.screenshot(), contentType: 'image/png' })
  } finally {
    try {
      if (page && !page.isClosed()) {
        await testInfo.attach('final-alerts', {
          body: JSON.stringify(await collectErrorBanners(page)), contentType: 'application/json'
        })
      }

      // Preserve bounded synthetic diagnostics before fixture cleanup. Never
      // read host-user logs or attach token-bearing endpoint query strings.
      for (const owner of ['', 'profiles/full']) {
        const logs = path.join(sandbox.hermesHome, owner, 'logs')

        if (!fs.existsSync(logs)) {continue}

        for (const name of ['desktop.log', 'gui.log', 'errors.log']) {
          const file = path.join(logs, name)

          if (!fs.existsSync(file)) {continue}
          const stat = fs.lstatSync(file)

          if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 8 * 1024 * 1024) {continue}

          const redacted = fs.readFileSync(file, 'utf8')
            .replace(/https?:\/\/[^\s"'<>]+/gi, '[endpoint]')
            .replace(/((?:token|secret|password|api_key)\s*["']?\s*[:=]\s*["']?)[^\s,}"']+/gi, '$1[redacted]')

          await testInfo.attach(`${owner ? 'full' : 'root'}-${name}`, { body: redacted, contentType: 'text/plain' })
        }
      }
    } finally {
      try {
        if (app && !closed) {
          await app.close()
          closed = true
        }
      } finally {
        try {
          await mock.close()
        } finally {
          if (closed) {
            sandbox.cleanup()
          } else {
            await testInfo.attach('retained-sandbox', { body: sandbox.root, contentType: 'text/plain' })
          }
        }
      }
    }
  }
})
